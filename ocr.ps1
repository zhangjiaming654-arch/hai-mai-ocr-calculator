param(
    [Parameter(Mandatory = $true)][string]$ImagePath,
    [Parameter(Mandatory = $true)][string]$OutPath
)
# Windows built-in OCR (WinRT). ASCII only on purpose: PS 5.1 mis-decodes non-BOM UTF-8.
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

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync(0)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = $null
try {
    $lang = New-Object Windows.Globalization.Language 'zh-Hans-CN'
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
} catch { }
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) { throw 'no OCR engine available' }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$items = New-Object System.Collections.ArrayList
foreach ($line in @($result.Lines)) {
    $ws = @($line.Words)
    if ($ws.Count -eq 0) { continue }
    $x0 = [double]::Parse('Infinity'.Replace('Infinity', '1E+30'))
    $y0 = [double]::Parse('1E+30')
    $x1 = 0.0
    $y1 = 0.0
    foreach ($w in $ws) {
        $r = $w.BoundingRect
        if ($r.X -lt $x0) { $x0 = [double]$r.X }
        if ($r.Y -lt $y0) { $y0 = [double]$r.Y }
        if (($r.X + $r.Width) -gt $x1) { $x1 = [double]($r.X + $r.Width) }
        if (($r.Y + $r.Height) -gt $y1) { $y1 = [double]($r.Y + $r.Height) }
    }
    [void]$items.Add([pscustomobject]@{
            t = [string]$line.Text
            x = $x0
            y = $y0
            w = [double]($x1 - $x0)
            h = [double]($y1 - $y0)
        })
}

if ($items.Count -eq 0) {
    $json = '[]'
}
else {
    $json = ConvertTo-Json -InputObject $items.ToArray() -Depth 3
}
[System.IO.File]::WriteAllText($OutPath, $json, (New-Object System.Text.UTF8Encoding($false)))
