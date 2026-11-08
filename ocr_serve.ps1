# Windows built-in OCR (WinRT) as a long-lived server.
# Reads image paths from stdin, one per line; writes one compact JSON line per image.
# ASCII only on purpose: PS 5.1 mis-decodes non-BOM UTF-8 source files.
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($task, $type) {
    $m = $asTask.MakeGenericMethod($type)
    $t = $m.Invoke($null, @($task))
    $t.Wait(-1) | Out-Null
    $t.Result
}

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null

$engine = $null
try {
    $lang = New-Object Windows.Globalization.Language 'zh-Hans-CN'
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
} catch { }
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) { throw 'no OCR engine available' }

$inv = [System.Globalization.CultureInfo]::InvariantCulture
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
[Console]::InputEncoding = $utf8

function Ocr-Json([string]$path) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync(0)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('[')
    $first = $true
    foreach ($line in @($result.Lines)) {
        $ws = @($line.Words)
        if ($ws.Count -eq 0) { continue }
        $x0 = 1E+30
        $y0 = 1E+30
        $x1 = 0.0
        $y1 = 0.0
        foreach ($w in $ws) {
            $r = $w.BoundingRect
            if ($r.X -lt $x0) { $x0 = [double]$r.X }
            if ($r.Y -lt $y0) { $y0 = [double]$r.Y }
            if (($r.X + $r.Width) -gt $x1) { $x1 = [double]($r.X + $r.Width) }
            if (($r.Y + $r.Height) -gt $y1) { $y1 = [double]($r.Y + $r.Height) }
        }
        $text = [string]$line.Text
        $text = $text.Replace('\', '\\').Replace('"', '\"')
        $text = $text.Replace("`r", ' ').Replace("`n", ' ')
        if (-not $first) { [void]$sb.Append(',') }
        $first = $false
        [void]$sb.Append('{"t":"' + $text + '","x":')
        [void]$sb.Append(([double]$x0).ToString($inv))
        [void]$sb.Append(',"y":')
        [void]$sb.Append(([double]$y0).ToString($inv))
        [void]$sb.Append(',"w":')
        [void]$sb.Append(([double]($x1 - $x0)).ToString($inv))
        [void]$sb.Append(',"h":')
        [void]$sb.Append(([double]($y1 - $y0)).ToString($inv))
        [void]$sb.Append('}')
    }
    [void]$sb.Append(']')
    $stream.Dispose()
    $sb.ToString()
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '') { continue }
    if ($line -eq '__exit__') { break }
    try {
        $json = Ocr-Json $line
    } catch {
        $json = '[]'
    }
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}
