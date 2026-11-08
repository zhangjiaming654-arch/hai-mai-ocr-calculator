# -*- coding: utf-8 -*-
"""海迈取数计算器

用法：把窗口拖到计价软件上，把取景框对准要读的数字，点【识别】，按下浮率算出结果，
      你自己填回软件里。

两种模式：
  · 主材单价：框住「控制单价」，输出该填的主材单价（定额里只有主材时适用）
  · 调价    ：框住「控制单价 + 单价下浮」，输出「调整人材机」里该填的定额系数

反推公式（本工程取费口径：取费基数 = 人工 + 材料 + 机械）：
    综合单价 = 主材单价 × (1+管理费) × (1+利润) × (1+税金)
    目标单价 = 控制单价 × (1 + 下浮率/100)        # 负号=下浮，正号=上浮，与海迈「单价下浮」列一致
    主材单价 = 目标单价 ÷ K
    K = 1.0679 × 1.06 × 1.09 = 1.23385166
调价模式：系数 = (1+目标下浮率/100) ÷ (1+当前下浮率/100)   # 控制价约掉，与取费口径无关

实测回归：控制单价 245.89、下浮 −9.68% → 主材 180.00，反算综合单价 222.09。
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from PIL import Image, ImageChops, ImageFilter, ImageGrab

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, 'config.json')
OCR_PS1 = os.path.join(BASE, 'ocr.ps1')
TMP_DIR = os.environ.get('TEMP') or BASE
TMP_IMG = os.path.join(TMP_DIR, 'hm_grab.png')
TMP_JSON = os.path.join(TMP_DIR, 'hm_grab.json')

DEFAULT_CFG = {
    "down_rate": -9.68,
    "mgmt_rate": 6.79,
    "profit_rate": 6.0,
    "tax_rate": 9.0,
    "geometry": "380x295+1280+110",
    "upscale": 6,
    "mode": "主材单价",
}

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
    big.save(TMP_IMG, 'PNG')
    if os.path.exists(TMP_JSON):
        try:
            os.remove(TMP_JSON)
        except OSError:
            pass
    cmd = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
           '-File', OCR_PS1, '-ImagePath', TMP_IMG, '-OutPath', TMP_JSON]
    proc = subprocess.run(cmd, capture_output=True, timeout=120,
                          creationflags=CREATE_NO_WINDOW)
    if not os.path.exists(TMP_JSON):
        err = (proc.stderr or b'').decode('mbcs', 'ignore')[:300]
        raise RuntimeError('OCR 没有输出：' + err)
    with open(TMP_JSON, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [data]
    for item in data:
        for key in ('x', 'y', 'w', 'h'):
            item[key] = float(item.get(key, 0)) / scale
    return data


def _clean_line(text):
    """按"行文本"清洗：去空白、负号/小数点归一、全角转半角；返回(清洗串, 原串索引表)。

    索引表用来把清洗串里的位置换算回原串位置，从而估算数字在行内的横向位置。
    """
    out = []
    idx = []
    for i, ch in enumerate(str(text)):
        if ch in ' \t\u3000\xa0':
            continue
        if ch in _MINUS:
            ch = '-'
        elif ch in _DOTS:
            ch = '.'
        elif '\uff10' <= ch <= '\uff19':
            ch = chr(ord(ch) - 0xff10 + ord('0'))
        out.append(ch)
        idx.append(i)
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
            if value is None or not (0.01 <= abs(value) <= 99999.99):
                continue
            start = idx[m.start()]
            end = idx[min(m.end(), len(idx)) - 1] + 1
            frac = start / float(max(1, len(text)))
            items.append((y0 + h / 2.0, x0 + frac * w, value, m.group()))
        items.sort(key=lambda z: (round(z[0] / 8.0), z[1]))
    return [[value, raw] for _, _, value, raw in items]


def tokens_to_lines(tokens):
    """OCR 行 -> 按"表格行"分组的数值列表：[[982.33, -39.64], ...]。

    同一行的数字 y 相近，按 y 归并；组内按 x 从左到右。
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
            if value is None or not (0.01 <= abs(value) <= 99999.99):
                continue
            start = idx[m.start()]
            frac = start / float(max(1, len(text)))
            items.append((y0 + h / 2.0, x0 + frac * w, value))
    if not items:
        return []
    items.sort(key=lambda z: (z[0], z[1]))
    lines = []
    for y, x, value in items:
        if lines and abs(y - lines[-1][0]) <= 10:
            lines[-1][1].append((x, value))
        else:
            lines.append((y, [(x, value)]))
    out = []
    for _y, cells in lines:
        cells.sort(key=lambda z: z[0])
        out.append([v for _x, v in cells])
    return out


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
        self.entry.bind('<Return>', lambda e: self.recompute())
        self.entry.bind('<FocusOut>', lambda e: self.recompute())

        self.txt = tk.Text(root, height=5, bd=1, relief='solid',
                           font=('Consolas', 10), bg='#fbfbfb')
        self.txt.grid(row=2, column=0, sticky='nsew', padx=6, pady=(0, 6))
        self.txt.bind('<Double-Button-1>', self.edit_row)

        root.bind('<F8>', lambda e: self.recognize())
        root.protocol('WM_DELETE_WINDOW', self.on_close)

        self._redraw_frame()

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
        self.btn.config(state='disabled', text='识别中')
        self.root.update_idletasks()
        try:
            image = self._grab()
            lines = self._read(image)
            if self._mode() == MODES[1]:
                # 调价：每行要"控制价 + 单价下浮"两个数（下浮可能是负数，必须留着）
                rows = [ln for ln in lines if len(ln) >= 2 and ln[0] > 1.0]
            else:
                rows = [[v] for ln in lines for v in ln if v > 1.0]
            if not rows:
                messagebox.showinfo('没认出数字',
                                    '框里没识别到数字。\n请把框对准数字那一列，窗口拉大一点再试。')
                return
            self.rows = rows
            self.recompute()
            self._redraw_frame()
        except Exception as exc:                      # noqa: BLE001
            messagebox.showerror('识别失败', str(exc))
        finally:
            self.btn.config(state='normal', text='识别')

    def _grab(self):
        canvas = self.canvas
        canvas.update_idletasks()
        x = canvas.winfo_rootx() + self.frame[0]
        y = canvas.winfo_rooty() + self.frame[1]
        w, h = self.frame[2], self.frame[3]
        if w <= 4 or h <= 4:
            raise RuntimeError('取景框太小')
        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)
        try:
            return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        finally:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.root.update()

    def _ocr(self, image, preprocess=True):
        return ocr_image(image, self.cfg.get('upscale', 3), preprocess=preprocess)

    def _read(self, image):
        """预处理版和原图版各识别一遍，取"认出数字最多"的那份。

        二值化能压掉 ClearType 彩边（1 被读成 L 那种），但偶尔会把尾部数字吃掉；
        所以两种都跑，按识别到的数字总个数挑。
        """
        best = None
        for prep in (True, False):
            try:
                toks = self._ocr(image, preprocess=prep)
            except Exception:                         # noqa: BLE001
                continue
            lines = self._to_lines(toks)
            score = sum(len(re.findall(r'\d', str(t.get('t', '')))) for t in toks)
            if best is None or score > best[0]:
                best = (score, lines)
        return best[1] if best else []

    def _to_lines(self, tokens):
        return tokens_to_lines(tokens)

    # -------------------------------------------------- 计算与显示
    def _mode(self):
        return self.var_mode.get() if getattr(self, 'var_mode', None) else MODES[0]

    def _on_mode(self):
        """切换模式：清掉上一次的识别结果，取景框换成该模式的默认大小。"""
        self.cfg['mode'] = self._mode()
        self.rows = []
        self.txt.delete('1.0', 'end')
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

    def _down_rate(self):
        """下浮率：符号跟海迈"单价下浮"一致——负数是下浮，正数是上浮。"""
        try:
            return float(self.var_down.get().replace('%', '').replace('，', '.')
                         .replace('一', '-').replace('−', '-').strip())
        except ValueError:
            return None

    def recompute(self):
        """只显示算出来的主材单价（每行一个数）。"""
        down = self._down_rate()
        if down is None or not self.rows:
            return
        self.cfg['down_rate'] = down
        k = k_factor(self.cfg)
        lines = []
        if self._mode() == MODES[1]:               # 调价：输出该填的系数
            for row in self.rows:
                coef = price_factor(row[0], row[1], down)
                if coef is None:
                    lines.append('—')
                else:
                    lines.append(_fmt(coef, 2))
        else:                                      # 主材单价
            for row in self.rows:
                for control in row:
                    if control > 1.0:
                        lines.append(_fmt(control * (1 + down / 100.0) / k, 2))
        self.txt.delete('1.0', 'end')
        self.txt.insert('1.0', '\n'.join(lines))

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
