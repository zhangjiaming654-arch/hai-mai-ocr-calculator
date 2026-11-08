# 生成带图标的桌面快捷方式（Windows）
# 用法：右键「使用 PowerShell 运行」，或
#   powershell -ExecutionPolicy Bypass -File make_shortcut.ps1
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$app  = Join-Path $here 'app.py'
$icon = Join-Path $here '图标.ico'

if (-not (Test-Path $app)) { throw "找不到 app.py：$app" }

# 找 pythonw.exe（无控制台窗口的解释器）
$pyw = $null
$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) { $pyw = $cmd.Source }
if (-not $pyw) {
    foreach ($v in '312', '311', '310', '39') {
        $p = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\pythonw.exe"
        if (Test-Path $p) { $pyw = $p; break }
    }
}
if (-not $pyw) { throw '找不到 pythonw.exe，请先安装 Python 3.8+ 并加入 PATH。' }

$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop '海迈取数计算器.lnk'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath       = $pyw
$sc.Arguments        = '"' + $app + '"'
$sc.WorkingDirectory = $here
$sc.Description      = '海迈取数计算器 - 框住控制单价，算出该填的价'
if (Test-Path $icon) { $sc.IconLocation = $icon + ',0' }
$sc.Save()

Write-Host "已创建快捷方式：$lnk"
Write-Host "目标：$pyw `"$app`""
