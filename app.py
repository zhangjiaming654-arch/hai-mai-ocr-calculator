# -*- coding: utf-8 -*-
"""海迈取数计算器

用法：把窗口拖到海迈上，把取景框对准"控制单价"那一列的数字，点【识别】，
      按下浮率算出该填的主材单价，你自己填回海迈。

反推公式（本工程取费口径：取费基数 = 人工 + 材料 + 机械）：
    综合单价 = 主材单价 × (1+管理费) × (1+利润) × (1+税金)
    主材单价 = 控制单价 × (1 − 下浮率) ÷ K
    K = 1.0679 × 1.06 × 1.09 = 1.23385166
实测回归：控制单价 245.89、下浮 9.68% → 主材 180.00，反算综合单价 222.09（下浮 −9.68%）
"""
import ctypes
import itertools
import random
import queue
import json
import os
import re
import subprocess
import threading
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from PIL import Image, ImageChops, ImageFilter, ImageGrab

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, 'config.json')
OCR_PS1 = os.path.join(BASE, 'ocr.ps1')
OCR_SERVE_PS1 = os.path.join(BASE, 'ocr_serve.ps1')
TMP_DIR = os.environ.get('TEMP') or BASE
TMP_IMG = os.path.join(TMP_DIR, 'hm_grab.png')
_TMP_SEQ = itertools.count(1)


def _tmp_png_path():
    """每次换一个临时文件名。

    坑：OCR 服务（WinRT StorageFile）读过某个文件之后，Windows 会让这个路径
    "不能再被创建"——同一个名字第二次保存必报 `OSError: [Errno 22] Invalid
    argument`。所以每次都换新名字，用完删掉。
    """
    return os.path.join(TMP_DIR, 'hm_grab_%d_%d.png' % (os.getpid(), next(_TMP_SEQ)))
TMP_JSON = os.path.join(TMP_DIR, 'hm_grab.json')

DEFAULT_CFG = {
    "down_rate": -9.68,
    "mgmt_rate": 6.79,
    "profit_rate": 6.0,
    "tax_rate": 9.0,
    "geometry": "380x295+1280+110",
    "upscale": 6,
    "mode": "主材单价",
    "rand_mode": True,
    "decimals": True,
}

# 随机模式：输出结果对应的下浮率在"设定下浮率 ± 这个值"内浮动（百分点）
RAND_BAND = 0.25

CREATE_NO_WINDOW = 0x08000000
# 取景框区域用"抠色透明"：画布底色设成这个关不掉的颜色，再把整窗这个颜色挖成透明，
# 于是框里能直接看见海迈的数字（也就知道有没有对准），同时该区域鼠标事件穿透。
KEY_COLOR = '#ff00fe'
FRAME_COLOR = '#2f6fd0'
FRAME_MIN_W, FRAME_MIN_H = 40, 18
MODES = ('主材单价', '调价')
# 每个模式对应的默认取景框大小（切模式时自动换）
FRAME_SIZES = {'主材单价': (80, 48), '调价': (140, 53)}
STIPPLE = 'gray25'


# ---------------------------------------------------------------- 计算
def k_factor(cfg):
    """取费连乘系数 K。"""
    return ((1 + cfg['mgmt_rate'] / 100.0)
            * (1 + cfg['profit_rate'] / 100.0)
            * (1 + cfg['tax_rate'] / 100.0))


def main_material(control_price, down_rate, cfg):
    """由控制单价和下浮率反推主材单价。

    down_rate 用"海迈的那套符号"：跟"单价下浮"列一致，负数=下浮，正数=上浮。
    例：down_rate = −9.68 表示单价下浮 9.68%；填 9.68 则是上浮 9.68%。
    """
    return control_price * (1 + down_rate / 100.0) / k_factor(cfg)


def price_factor(control_price, cur_down, target_down):
    """调价模式：由"控制价 + 当前单价下浮"算"目标下浮率"要填的调价系数。

    海迈"调整人材机"里的定额系数是对人材机等比放大，所以
        目标单价 = 控制价 × (1 + 目标下浮率/100)
        当前单价 = 控制价 × (1 + 当前下浮率/100)
        系数     = 目标单价 ÷ 当前单价 = (1 + 目标) ÷ (1 + 当前)
    """
    cur = 1.0 + cur_down / 100.0
    if cur <= 0.01:
        return None
    return (1.0 + target_down / 100.0) / cur


def selftest(cfg):
    """启动自检：用实测数据验证公式，偏了就返回 False。"""
    value = main_material(245.89, -9.68, cfg)          # 下浮 9.68% → 180.00
    ok_value = abs(value - 180.0) <= 0.02
    up = main_material(245.89, 9.68, cfg)              # 上浮 9.68% → 应大于控制价
    ok_sign = up > value          # 上浮算出来的主材要比下浮的大
    back = 180.0 * k_factor(cfg)
    ok_back = abs(back - 222.09) <= 0.01
    return ok_value and ok_back and ok_sign


# ---------------------------------------------------------------- 数字清洗
_FULLWIDTH = str.maketrans('０１２３４５６７８９', '0123456789')
_MINUS = '一—−–－‐'
_DOTS = ('，', '。', '．', '·', '•', '٫')
_CONFUSE = {'O': '0', 'o': '0', 'D': '0', 'l': '1', 'I': '1', 'i': '1', '|': '1',
            'Z': '2', 'S': '5', 'B': '8', 'g': '9', 'q': '9'}


def parse_number(text):
    """把 OCR 出来的一小段文字解析成数字；解析不了返回 None。"""
    if not text:
        return None
    t = re.sub(r'\s+', '', str(text).replace('\u3000', ''))
    t = t.translate(_FULLWIDTH)
    if t[:1] and t[0] in _MINUS:
        t = '-' + t[1:]
    for ch in _DOTS:
        t = t.replace(ch, '.')
    if ',' in t:
        if re.fullmatch(r'-?\d{1,3}(,\d{3})+(\.\d+)?', t):
            t = t.replace(',', '')
        else:
            t = t.replace(',', '.')
    if re.fullmatch(r'-?\d+(\.\d+)?', t):
        return float(t)
    if re.fullmatch(r'-?\.\d+', t):
        return float('0' + t)
    if re.fullmatch(r'[-0-9.OoDlIi|ZSBgq]+', t):
        t2 = ''.join(_CONFUSE.get(c, c) for c in t)
        if re.fullmatch(r'-?\d+(\.\d+)?', t2):
            return float(t2)
    return None


def _fmt(value, nd=None):
    if nd is None:
        return ('%.4f' % value).rstrip('0').rstrip('.')
    return '%.*f' % (nd, value)


def win_clipboard_text(text):
    """用 Win32 API 直接写系统剪贴板（CF_UNICODETEXT），毫秒级、窗口关掉也还在。

    返回 True 表示写入成功；失败时调用方可以退回 Tk 的剪贴板。
    注意：64 位下句柄必须显式声明 c_void_p，否则会被截断成 32 位导致写入失败。
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]

    opened = False
    for _ in range(6):                                     # 剪贴板偶尔被别人占着，重试几次
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.02)
    if not opened:
        return False

    handle = None
    try:
        user32.EmptyClipboard()
        data = str(text).encode('utf-16-le') + b'\x00\x00'
        GMEM_MOVEABLE = 0x0002
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if user32.SetClipboardData(13, handle):             # 13 = CF_UNICODETEXT
            handle = None                                   # 交给系统了，别自己释放
            return True
        return False
    except Exception:                                       # noqa: BLE001
        return False
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


# ---------------------------------------------------------------- OCR 通道
def prep_for_ocr(image, threshold=120):
    """OCR 前置：取 RGB 最暗通道去掉 ClearType 彩边，再二值化。

    屏幕上的数字是彩色抗锯齿渲染的，边缘带蓝/橙彩边，Windows OCR 会把
    '1' 认成 'L'、把小数点丢掉（实测 '6711.17' → '671L 17'）。
    取最暗通道 = 去掉彩边；阈值取 120（只留黑芯，不糊成一坨）。
    彩色行底色（浅黄/浅蓝/浅粉）最暗通道都 >190，会变白，不影响文字。
    实测：两张真实截图各 3 个数字，raw 得 3/3+2/3，本方案 6 倍放大得 3/3+3/3。
    """
    rgb = image.convert('RGB')
    r, g, b = rgb.split()
    gray = ImageChops.darker(ImageChops.darker(r, g), b)
    return gray.point(lambda p: 0 if p < threshold else 255)


def _fit_scale(width, scale):
    """放大倍数按宽度限一下，别把图搞成几千像素。"""
    scale = max(1, int(scale or 1))
    while scale > 1 and width * scale > 4200:
        scale -= 1
    return scale


def ocr_image(image, scale=6, preprocess=True):
    """截图 -> 预处理 -> 放大 -> Windows 自带 OCR -> 词条列表（坐标已换算回原图尺度）。"""
    src = prep_for_ocr(image) if preprocess else image
    scale = _fit_scale(src.width, scale)
    big = src.resize((src.width * scale, src.height * scale), Image.LANCZOS) \
        if scale > 1 else src
    path = _tmp_png_path()
    big.save(path, 'PNG')

    data = None
    try:
        try:                              # 先问常驻服务（毫秒级）
            data = _OCR_SERVER.query(path)
        except Exception:                 # noqa: BLE001
            data = None
        if data is None:                  # 服务挂了就退回一次性启动
            if os.path.exists(TMP_JSON):
                try:
                    os.remove(TMP_JSON)
                except OSError:
                    pass
            cmd = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                   '-File', OCR_PS1, '-ImagePath', path, '-OutPath', TMP_JSON]
            proc = subprocess.run(cmd, capture_output=True, timeout=120,
                                  creationflags=CREATE_NO_WINDOW)
            if not os.path.exists(TMP_JSON):
                err = (proc.stderr or b'').decode('mbcs', 'ignore')[:300]
                raise RuntimeError('OCR 没有输出：' + err)
            with open(TMP_JSON, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if isinstance(data, dict):
        data = [data]
    for item in data:
        for key in ('x', 'y', 'w', 'h'):
            item[key] = float(item.get(key, 0)) / scale
    return data


class OcrServer:
    """常驻的 OCR 进程：PowerShell 只启动一次，之后每次识别只递一个图片路径给它。

    一次性启动 ocr.ps1 的话，光 PowerShell 起来就要 ~0.7 秒，一次识别 ~1.1 秒；
    常驻之后同样的图每次只要几毫秒。
    """

    def __init__(self, script):
        self.script = script
        self.proc = None

    def _start(self):
        self.proc = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', self.script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace',
            creationflags=CREATE_NO_WINDOW)

    def query(self, path):
        """返回 OCR 结果（list）；失败抛异常，由调用方回退。"""
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        self.proc.stdin.write(path + '\n')
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError('OCR 服务已退出')
        return json.loads(line)

    def close(self):
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.stdin.write('__exit__\n')
            proc.stdin.flush()
            proc.wait(timeout=2)
        except Exception:                 # noqa: BLE001
            try:
                proc.kill()
            except Exception:             # noqa: BLE001
                pass


_OCR_SERVER = OcrServer(OCR_SERVE_PS1)


_WS = ' \t\u3000\xa0'
# 这些字符夹在数字中间时，空格属于"数字内部的空格"（如 "1218 . 59"），可以删掉；
# 其他位置的空格是两个数字之间的分隔（"59 40" 是两格里的两个数），必须留标记。
_GLUE = set('.,_') | set(_DOTS) | set(_MINUS) | set('-')


def _is_glue(ch):
    return bool(ch) and (ch in _GLUE)


def _clean_line(text):
    """按"行文本"清洗：负号/小数点归一、全角转半角；返回(清洗串, 原串索引表)。

    空格的处理是这里的关键：以前一律删掉，结果相邻两格的数字会被拼成一个
    （"1218 ， 59 40" → 1218.594）。现在只有紧挨小数点/逗号的空格才删，
    其余空格换成分隔符 '|'，避免把两个数粘成一个。
    """
    raw = str(text)
    n = len(raw)
    out = []
    idx = []
    i = 0
    while i < n:
        ch = raw[i]
        if ch in _WS:
            j = i
            while j < n and raw[j] in _WS:
                j += 1
            prev = out[-1] if out else ''
            nxt = raw[j] if j < n else ''
            if _is_glue(prev) or _is_glue(nxt):
                pass                      # 数字内部/小数点旁的空格，丢掉
            else:
                out.append('|')           # 两格之间的空隙，留分隔符
                idx.append(i)
            i = j
            continue
        if ch in _MINUS:
            ch = '-'
        elif ch in _DOTS:
            ch = '.'
        elif '\uff10' <= ch <= '\uff19':
            ch = chr(ord(ch) - 0xff10 + ord('0'))
        out.append(ch)
        idx.append(i)
        i += 1
    return ''.join(out), idx


_NUM_PATTERN = re.compile(r'-?\d[\d,]*(?:\.\d+)?')


def tokens_to_rows(tokens):
    """OCR 行 -> [[数值, 原文], ...]，按"先行后列"排序，丢掉不像单价的（编码等）。

    数字按行文本抽，避免 Windows OCR 把"245.89"拆成"245 / 。 / 89"三个词。
    """
    items = []
    for tok in tokens:
        text = str(tok.get('t', ''))
        cleaned, idx = _clean_line(text)
        if not cleaned:
            continue
        try:
            x0 = float(tok.get('x', 0))
            y0 = float(tok.get('y', 0))
            w = float(tok.get('w', 0))
            h = float(tok.get('h', 0))
        except (TypeError, ValueError):
            x0 = y0 = w = h = 0.0
        for m in _NUM_PATTERN.finditer(cleaned):
            value = parse_number(m.group())
            if value is None or not (0.01 <= abs(value) <= 1e9):
                continue
            start = idx[m.start()]
            end = idx[min(m.end(), len(idx)) - 1] + 1
            frac = start / float(max(1, len(text)))
            items.append((y0 + h / 2.0, x0 + frac * w, value, m.group()))
        items.sort(key=lambda z: (round(z[0] / 8.0), z[1]))
    return [[value, raw] for _, _, value, raw in items]


def tokens_to_cells(tokens):
    """OCR 行 -> [(y 中心, x 左端, 数值), ...]，按"先行后列"排序。"""
    items = []
    for tok in tokens:
        text = str(tok.get('t', ''))
        cleaned, idx = _clean_line(text)
        if not cleaned:
            continue
        try:
            x0 = float(tok.get('x', 0))
            y0 = float(tok.get('y', 0))
            w = float(tok.get('w', 0))
            h = float(tok.get('h', 0))
        except (TypeError, ValueError):
            x0 = y0 = w = h = 0.0
        for m in _NUM_PATTERN.finditer(cleaned):
            value = parse_number(m.group())
            if value is None or not (0.01 <= abs(value) <= 1e9):
                continue
            start = idx[m.start()]
            frac = start / float(max(1, len(text)))
            items.append((y0 + h / 2.0, x0 + frac * w, value))
    items.sort(key=lambda z: (z[0], z[1]))
    return items


def tokens_to_lines(tokens):
    """OCR 行 -> 按"表格行"分组的数值列表：[[982.33, -39.64], ...]。"""
    lines = []
    for y, x, value in tokens_to_cells(tokens):
        if lines and abs(y - lines[-1][0]) <= 10:
            lines[-1][1].append((x, value))
        else:
            lines.append((y, [(x, value)]))
    out = []
    for _y, cells in lines:
        cells.sort(key=lambda z: z[0])
        out.append([v for _x, v in cells])
    return out


# 拆数是 OCR 自身的毛病（1218.59 会被读成 "121 8.59"/"121 & 59"），换一组预处理
# 参数往往就能整读出来。所以按顺序试几组，谁认出来的个数正好等于"这个模式应有的
# 个数"就用谁；主材单价模式只会框 1 个数、调价模式只框 2 个数。
PREP_VARIANTS = ((True, 120), (True, 140), (True, 160), (True, 100), (False, 0))


def pick_numbers(cells, want):
    """个数不对时的兜底：主材模式只留"最像一个单价"的那个；调价模式留最左两个。"""
    if want == 1:
        vals = [c[2] for c in cells]
        if not vals:
            return []
        with_dot = [v for v in vals if abs(v - round(v)) > 1e-9]
        pool = with_dot or vals
        return [max(pool, key=lambda v: (len(_fmt(v, 6).rstrip('0')), abs(v)))]
    rows = []
    for cell in cells:
        if rows and abs(cell[0] - rows[-1][0][0]) <= 10:
            rows[-1].append(cell)
        else:
            rows.append([cell])
    for row in rows:
        if len(row) >= 2:
            row = sorted(row, key=lambda z: z[1])
            return [row[0][2], row[1][2]]
    return [c[2] for c in cells[:2]]


# ---------------------------------------------------------------- 配置
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG_PATH, 'r', encoding='utf-8-sig') as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        with open(CFG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 界面
class App:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.rows = []          # [[控制单价, OCR原文], ...]
        self._busy = False
        self.jitter = 0.0

        root.title('取数计算器')
        try:
            root.attributes('-topmost', True)
        except Exception:
            pass
        geo = cfg.get('geometry') or DEFAULT_CFG['geometry']
        m = re.match(r'(\d+)x(\d+)', str(geo))
        if not m or int(m.group(1)) < 320 or int(m.group(2)) < 240:
            geo = DEFAULT_CFG['geometry']      # 存的尺寸太离谱才回默认
        self._place(geo)
        root.minsize(320, 240)

        self.frame = list(cfg.get('frame') or [])[:4]   # 取景框（画布内坐标 x,y,w,h）
        self.frame_user = bool(self.frame)              # 用户亲手调过就不再自动铺满
        self._drag = None

        # 取景框用"抠色透明"，框里直接看见海迈的数字；不支持时退回灰底
        self.transparent = False
        try:
            root.attributes('-transparentcolor', KEY_COLOR)
            self.transparent = True
        except Exception:
            self.transparent = False
        bg = KEY_COLOR if self.transparent else '#e8e8e8'

        self.canvas = tk.Canvas(root, bg=bg, highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky='nsew', padx=6, pady=(6, 0))
        self.canvas.bind('<Configure>', self._redraw_frame)
        self.canvas.bind('<ButtonPress-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.canvas.bind('<Motion>', self._on_hover)
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)

        bar = tk.Frame(root)
        bar.grid(row=1, column=0, sticky='ew', padx=6, pady=4)
        self.btn = tk.Button(bar, text='识别', width=6, command=self.recognize)
        self.btn.pack(side='left')
        tk.Label(bar, text='下浮率').pack(side='left', padx=(10, 2))
        self.var_down = tk.StringVar(value=_fmt(cfg['down_rate']))
        self.entry = tk.Entry(bar, textvariable=self.var_down, width=6, justify='right')
        self.entry.pack(side='left')
        tk.Label(bar, text='%').pack(side='left')
        self.var_mode = tk.StringVar(value=cfg.get('mode') or MODES[0])
        self.cbo_mode = ttk.Combobox(bar, textvariable=self.var_mode, values=list(MODES),
                                     width=7, state='readonly')
        self.cbo_mode.pack(side='right', padx=(8, 0))
        self.cbo_mode.bind('<<ComboboxSelected>>', lambda e: self._on_mode())
        self.randbar = tk.Frame(root)
        self.randbar.grid(row=2, column=0, sticky='ew', padx=8, pady=(0, 4))
        # 随机数固定开启，「随机数」勾选框已隐藏（保留变量以兼容旧的 config.json）
        self.var_rand = tk.BooleanVar(value=True)
        self.chk_rand = tk.Checkbutton(
            self.randbar, variable=self.var_rand, command=self._on_rand,
            text='随机数')
        self.var_dec = tk.BooleanVar(value=bool(cfg.get('decimals', True)))
        self.chk_dec = tk.Checkbutton(
            self.randbar, variable=self.var_dec, command=self._on_dec,
            text='小数')
        self.chk_dec.pack(side='left')
        self.entry.bind('<Return>', lambda e: self.recompute())
        self.entry.bind('<FocusOut>', lambda e: self.recompute())

        self.txt = tk.Text(root, height=5, bd=1, relief='solid',
                           font=('Consolas', 10), bg='#fbfbfb')
        self.txt.grid(row=3, column=0, sticky='nsew', padx=6, pady=(0, 6))
        self.txt.bind('<Double-Button-1>', self.edit_row)

        root.bind('<F8>', lambda e: self.recognize())
        root.protocol('WM_DELETE_WINDOW', self.on_close)

        self._redraw_frame()
        self._sync_dec_widget()

    # -------------------------------------------------- 取景框
    def _place(self, geometry):
        """把上次保存的位置夹回屏幕内，免得窗口跑到屏幕外找不着。"""
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            m = re.match(r'(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', str(geometry or ''))
            if m:
                w, h, x, y = (int(v) for v in m.groups())
                w = max(170, min(w, sw))
                h = max(130, min(h, sh))
                x = max(0, min(x, sw - w))
                y = max(0, min(y, sh - h))
                geometry = '%dx%d+%d+%d' % (w, h, x, y)
            self.root.geometry(geometry)
        except Exception:
            self.root.geometry(DEFAULT_CFG['geometry'])

    def _redraw_frame(self, event=None):
        """画取景框：框里真透明（看得见海迈数字），框外罩点状灰表示"这里不取"。"""
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        if cw <= 20 or ch <= 20:          # 窗口还没真正布局好，别动 frame
            return
        if not self.frame_user or len(self.frame) != 4 or not self.frame[2]:
            dw, dh = FRAME_SIZES.get(self._mode(), (80, 48))
            self.frame = [14, 14, max(FRAME_MIN_W, dw), max(FRAME_MIN_H, dh)]
        self._clamp_frame()
        x, y, w, h = self.frame

        c = self.canvas
        c.delete('all')
        # 框外四块罩点状灰（既看得见底下，又一眼看出哪块不取）
        for bx, by, bw, bh in ((0, 0, cw, y), (0, y + h, cw, ch - y - h),
                               (0, y, x, h), (x + w, y, cw - x - w, h)):
            if bw > 0 and bh > 0:
                c.create_rectangle(bx, by, bx + bw, by + bh,
                                   fill='#ffffff', outline='', stipple=STIPPLE)
        # 取景框
        c.create_rectangle(x, y, x + w, y + h, outline=FRAME_COLOR, width=2)
        for hx, hy in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
            c.create_rectangle(hx - 5, hy - 5, hx + 5, hy + 5,
                               fill=FRAME_COLOR, outline='#ffffff')
        # 尺寸标签放在框外（不打搅框里的数字）
        if y > 18:
            c.create_text(x + w / 2, y - 7, text='%d×%d' % (w, h),
                          fill='#20303f', font=('Consolas', 8))
        else:
            c.create_text(x + w / 2, y + h + 7, text='%d×%d' % (w, h),
                          fill='#20303f', font=('Consolas', 8))

    def _clamp_frame(self):
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        if cw <= 20 or ch <= 20:          # 同上：布局未就绪时跳过，否则会把框夹成最小
            return
        x, y, w, h = self.frame
        w = max(FRAME_MIN_W, min(int(w), cw - 8))
        h = max(FRAME_MIN_H, min(int(h), ch - 8))
        x = max(2, min(int(x), cw - 4 - w))
        y = max(2, min(int(y), ch - 4 - h))
        self.frame = [x, y, w, h]

    def _hit(self, px, py):
        """判断按在哪儿：四角=改大小，其它=挪动。"""
        x, y, w, h = self.frame
        for name, (hx, hy) in (('nw', (x, y)), ('ne', (x + w, y)),
                               ('sw', (x, y + h)), ('se', (x + w, y + h))):
            if abs(px - hx) <= 9 and abs(py - hy) <= 9:
                return name
        return 'move'

    def _on_press(self, event):
        self._drag = [event.x, event.y, list(self.frame), self._hit(event.x, event.y)]

    def _on_drag(self, event):
        if not self._drag:
            return
        self.frame_user = True
        sx, sy, base, mode = self._drag
        dx, dy = event.x - sx, event.y - sy
        x, y, w, h = base
        if mode == 'move':
            self.frame = [x + dx, y + dy, w, h]
        elif mode == 'nw':
            self.frame = [x + dx, y + dy, w - dx, h - dy]
        elif mode == 'ne':
            self.frame = [x, y + dy, w + dx, h - dy]
        elif mode == 'sw':
            self.frame = [x + dx, y, w - dx, h + dy]
        elif mode == 'se':
            self.frame = [x, y, w + dx, h + dy]
        self._redraw_frame()

    def _on_release(self, event):
        self._drag = None
        self.cfg['frame'] = list(self.frame)

    def _on_hover(self, event):
        if self._drag:
            return
        mode = self._hit(event.x, event.y)
        cursor = {'nw': 'size_nw_se', 'se': 'size_nw_se',
                  'ne': 'size_ne_sw', 'sw': 'size_ne_sw'}.get(mode, 'fleur')
        try:
            self.canvas.configure(cursor=cursor)
        except Exception:
            pass

    # -------------------------------------------------- 识别
    def recognize(self):
        """点【识别】：主线程抓屏（几十毫秒）→ 后台线程跑 OCR → 主线程轮询取结果。

        以前 OCR 卡在界面线程里：第一次要一秒多、偶发更久，看起来就像"窗口卡住了"。
        """
        if getattr(self, '_busy', False):
            return
        self._busy = True
        self._queue = queue.Queue()
        self.btn.config(state='disabled', text='识别中')
        self.root.update_idletasks()
        try:
            image = self._grab()
        except Exception as exc:                      # noqa: BLE001
            self._finish_busy()
            self._show_hint('抓屏失败：%s' % exc)
            return
        want = self._want_count()                  # Tk 变量只能在主线程读
        threading.Thread(target=self._recognize_worker, args=(image, want), daemon=True).start()
        self.root.after(30, self._poll_result)

    def _recognize_worker(self, image, want):
        """后台线程：只做 OCR，把结果塞进队列（不碰任何 Tk 对象）。"""
        nums, err = [], None
        try:
            nums = self._read_numbers(image, want)
        except Exception as exc:                      # noqa: BLE001
            err = exc
        self._queue.put((nums, err))

    def _poll_result(self):
        """主线程：每 30ms 看一眼后台算完没有。"""
        try:
            nums, err = self._queue.get_nowait()
        except queue.Empty:
            if getattr(self, '_busy', False):
                self.root.after(30, self._poll_result)
            return
        self._recognize_done(nums, err)

    def _recognize_done(self, nums, err):
        """回到界面线程：整理成行、摇随机数、显示。"""
        self._finish_busy()
        if err is not None:
            self._show_hint('识别失败：%s' % err)
            return
        if self._mode() == MODES[1]:
            rows = [nums] if len(nums) >= 2 and nums[0] > 1.0 else []
        else:
            rows = [[v] for v in nums if v > 1.0]
        if not rows:
            self._show_hint('没认出数字\n把框对准数字那一列，或把窗口拉大一点再试')
            return
        self.rows = rows
        self._roll_jitter()
        self.recompute()
        self._redraw_frame()

    def _finish_busy(self):
        self._busy = False
        try:
            self.btn.config(state='normal', text='识别')
        except Exception:                             # noqa: BLE001
            pass

    def _show_hint(self, text):
        """在输出框里提示（不弹模态框，免得看起来像卡死）。"""
        try:
            self.txt.delete('1.0', 'end')
            self.txt.insert('1.0', text)
        except Exception:                             # noqa: BLE001
            pass

    def _grab(self):
        """抓取景框那一块屏幕。

        不能直接抓：本窗口就压在框上面。以前是 hide→截→show，会"黑一下"，还要白等 0.15 秒。
        现在改成把整个窗口透明度临时调到 0（窗口不隐藏、不重排），系统立刻不再画它，
        抓到的就是底下的海迈；抓完马上恢复。实测比原来快 0.2 秒左右，且不闪。
        """
        canvas = self.canvas
        canvas.update_idletasks()
        x = canvas.winfo_rootx() + self.frame[0]
        y = canvas.winfo_rooty() + self.frame[1]
        w, h = self.frame[2], self.frame[3]
        if w <= 4 or h <= 4:
            raise RuntimeError('取景框太小')
        try:
            self.root.attributes('-alpha', 0.0)
            self.root.update()
            time.sleep(0.02)              # 等 DWM 把这一帧撤掉
            return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        finally:
            self.root.attributes('-alpha', 1.0)
            self.root.update()

    def _ocr(self, image, preprocess=True):
        return ocr_image(image, self.cfg.get('upscale', 3), preprocess=preprocess)

    def _want_count(self):
        """这个模式下框里应该有几个数：主材单价 1 个，调价 2 个（用户明确说的）。"""
        return 2 if self._mode() == MODES[1] else 1

    def _read_numbers(self, image, want=None):
        """识别框里的数字，用"这个模式应该有几个数"当校验挑最靠谱的一次识别。

        依次换几组预处理（二值化阈值 120/140/160/100 + 原图），哪一组认出来的
        个数正好等于期望值就直接用；都不对就取最接近的，再按位置/形态兜底修整。
        """
        if want is None:                           # 主线程传进来，避免后台线程读 Tk 变量
            want = self._want_count()
        best = []
        best_gap = None
        for prep, th in PREP_VARIANTS:
            try:
                img = prep_for_ocr(image, th) if prep else image
                cells = tokens_to_cells(self._ocr(img, preprocess=False))
            except Exception:                         # noqa: BLE001
                continue
            vals = [c[2] for c in cells]
            gap = abs(len(vals) - want)
            if best_gap is None or gap < best_gap:
                best, best_gap = cells, gap
            if gap == 0:
                return vals
        return pick_numbers(best, want)

    # -------------------------------------------------- 计算与显示
    def _mode(self):
        return self.var_mode.get() if getattr(self, 'var_mode', None) else MODES[0]

    def _on_mode(self):
        """切换模式：清掉上一次的识别结果，取景框换成该模式的默认大小。"""
        self.cfg['mode'] = self._mode()
        self.rows = []
        self.txt.delete('1.0', 'end')
        self._sync_dec_widget()
        dw, dh = FRAME_SIZES.get(self._mode(), (80, 48))
        if len(self.frame) == 4:
            self.frame = [self.frame[0], self.frame[1],
                          max(FRAME_MIN_W, dw), max(FRAME_MIN_H, dh)]
            self.frame_user = True
            try:
                self._redraw_frame()
            except Exception:                      # noqa: BLE001
                pass
        save_cfg(self.cfg)

    def _rand_on(self):
        """随机数固定开启（勾选框已隐藏），每次识别都在设定下浮率附近浮动。"""
        return True

    def _on_rand(self):
        """点开关：记住设置，重新摇一个偏移，再算一遍。"""
        self.cfg['rand_mode'] = self._rand_on()
        self.cfg['decimals'] = self._dec_on()
        save_cfg(self.cfg)
        self._roll_jitter()
        self.recompute()

    def _dec_on(self):
        """「小数」勾选框：勾上按小数输出，取消则输出整数。"""
        return bool(getattr(self, 'var_dec', None) and self.var_dec.get())

    def _on_dec(self):
        """切换小数显示：记住设置再算一遍。"""
        self.cfg['decimals'] = self._dec_on()
        save_cfg(self.cfg)
        self.recompute()

    def _dec_places(self):
        """输出保留几位小数。

        调价模式固定带小数，不受「小数」勾选框影响：随机开启 3 位，否则 2 位；
        主材模式才看勾选框：勾上 2 位，取消则整数。
        """
        if self._mode() == MODES[1]:
            return 3 if self._rand_on() else 2
        return 2 if self._dec_on() else 0

    def _sync_dec_widget(self):
        """主材模式显示「小数」勾选框；调价模式固定带小数、把它藏起来。

        整个这一行没有可见控件时（调价模式）整行收起，不留空白。
        """
        if not getattr(self, 'chk_dec', None):
            return
        if self._mode() == MODES[1]:
            self.chk_dec.pack_forget()
        elif not self.chk_dec.winfo_manager():
            self.chk_dec.pack(side='left')
        bar = getattr(self, 'randbar', None)
        if bar is not None:
            if self.chk_dec.winfo_manager():
                bar.grid()
            else:
                bar.grid_remove()

    def _roll_jitter(self):
        """每识别一次摇一个偏移（百分点），随机模式关掉时是 0。"""
        self.jitter = random.uniform(-RAND_BAND, RAND_BAND) if self._rand_on() else 0.0

    def _down_rate(self):
        """下浮率：符号跟海迈"单价下浮"一致——负数是下浮，正数是上浮。"""
        try:
            return float(self.var_down.get().replace('%', '').replace('，', '.')
                         .replace('一', '-').replace('−', '-').strip())
        except ValueError:
            return None

    def _render(self, eff):
        """按"生效下浮率 eff"算出每行要显示的数 + 它来自哪一行。"""
        pairs = []
        if self._mode() == MODES[1]:               # 调价：输出该填的系数
            # 2 位小数时，系数动 0.01 对应下浮率跳 (1+当前下浮) 个百分点（常是 0.6~0.9），
            # 根本落不进 ±0.25 的带宽，所以随机模式下给 3 位小数。
            dec = self._dec_places()
            for row in self.rows:
                coef = price_factor(row[0], row[1], eff)
                pairs.append(('—' if coef is None else _fmt(coef, dec), row))
        else:                                      # 主材单价
            k = k_factor(self.cfg)
            for row in self.rows:
                for control in row:
                    if control > 1.0:
                        dec = self._dec_places()
                        pairs.append((_fmt(control * (1 + eff / 100.0) / k, dec), row))
        return pairs

    def _implied_down(self, text, row):
        """由显示出来的数反算它实际对应的下浮率（%），用来校验随机带宽。"""
        try:
            value = float(text)
        except (TypeError, ValueError):
            return None
        if self._mode() == MODES[1]:
            cur = row[1] / 100.0
            if 1 + cur <= 0:
                return None
            return (value * (1 + cur) - 1) * 100.0
        control = row[0]
        if control <= 0:
            return None
        return (value * k_factor(self.cfg) / control - 1) * 100.0

    def _max_dev(self, pairs, down):
        """这一组显示值里，反算下浮率离目标最远偏差多少（百分点）。"""
        worst = 0.0
        for text, row in pairs:
            implied = self._implied_down(text, row)
            if implied is None:
                return float('inf')
            worst = max(worst, abs(implied - down))
        return worst

    def recompute(self):
        """显示算出来的数（每行一个数）；随机模式下把结果的下浮率圈在 ±0.25 内。"""
        down = self._down_rate()
        if down is None or not self.rows:
            return
        self.cfg['down_rate'] = down
        if self._rand_on() and not getattr(self, 'jitter', 0.0):
            self._roll_jitter()
        if not self._rand_on():
            self.jitter = 0.0
        pairs = self._render(down + getattr(self, 'jitter', 0.0))
        if self._rand_on():
            # 小数位截断可能把结果推出带宽，重摇几次挑偏差最小的
            best_dev = self._max_dev(pairs, down)
            for _ in range(40):
                if best_dev <= RAND_BAND + 1e-9:
                    break
                self.jitter = random.uniform(-RAND_BAND, RAND_BAND)
                cand = self._render(down + self.jitter)
                dev = self._max_dev(cand, down)
                if dev < best_dev:
                    pairs, best_dev = cand, dev
        lines = [text for text, _row in pairs]
        self.txt.delete('1.0', 'end')
        self.txt.insert('1.0', '\n'.join(lines))
        self._copy('\n'.join(lines))

    def _copy(self, text):
        """把结果放进剪贴板，Ctrl+V 直接能粘。

        直接调 Win32 的剪贴板 API（毫秒级）：既能立刻粘，窗口关掉后也还在。
        以前是起一个 clip.exe 进程，那一下要几十到上百毫秒，就是"粘得慢"的来源。
        """
        if not text:
            return
        if win_clipboard_text(text):
            return
        try:                                       # 兜底：走 Tk 自带剪贴板
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception:                          # noqa: BLE001
            pass

    def edit_row(self, event=None):
        if not self.rows:
            return
        index = int(self.txt.index('@%d,%d' % (event.x, event.y)).split('.')[0]) - 1
        if not (0 <= index < len(self.rows)):
            return
        new_value = simpledialog.askfloat(
            '改一下', '控制单价：', initialvalue=self.rows[index][0],
            parent=self.root, minvalue=0.0)
        if new_value is None:
            return
        self.rows[index][0] = float(new_value)
        self.recompute()

    def on_close(self):
        try:
            self.cfg['geometry'] = self.root.geometry()
            self.cfg['mode'] = self._mode()
            self.cfg['rand_mode'] = self._rand_on()
            self.cfg['frame'] = list(self.frame)
        except Exception:
            pass
        save_cfg(self.cfg)
        self.root.destroy()


# ---------------------------------------------------------------- 启动
def _enable_dpi_awareness():
    """让 Tk 坐标 = 物理像素，否则 125% 缩放下截图会错位。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _log_exc(exc_type, exc, tb):
    """pythonw 没有控制台，把异常落到 %TEMP%\\hm_app.log，方便排查。"""
    import traceback
    try:
        with open(os.path.join(TMP_DIR, 'hm_app.log'), 'a', encoding='utf-8') as f:
            f.write(time.strftime('\n=== %Y-%m-%d %H:%M:%S\n'))
            traceback.print_exception(exc_type, exc, tb, file=f)
    except Exception:
        pass


def main():
    _enable_dpi_awareness()
    sys.excepthook = _log_exc
    cfg = load_cfg()
    root = tk.Tk()
    root.report_callback_exception = _log_exc
    app = App(root, cfg)
    if not selftest(cfg):
        messagebox.showwarning(
            '公式自检没过',
            '用 245.89 / 9.68%% 算出 %s，应是 180.00。\n请检查 config.json 里的费率。'
            % _fmt(main_material(245.89, 9.68, cfg), 2))
    app.recompute()
    root.mainloop()


if __name__ == '__main__':
    main()
