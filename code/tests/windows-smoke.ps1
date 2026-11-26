# Run on Windows PowerShell 5.1. Exercises synthetic bitmaps only; never captures the screen.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\win\desk-overlay.ps1" -SelfTest
if (-not [DeskClient]::IsGamePath('C:\Steam\steamapps\common\Test\game.exe', 'game.exe', @())) { throw 'Steam detection' }
if ([DeskClient]::IsGamePath('C:\Windows\notepad.exe', 'notepad.exe', @())) { throw 'Desktop must stay closed' }
if (-not [DeskClient]::IsGamePath('', 'custom.exe', @('custom.exe'))) { throw 'Game allowlist' }
if ([DeskClient]::HashDistance('0000000000000000', '000000000000003f') -ne 6) { throw 'Hamming boundary' }
if ([DeskClient]::HashDistance('0000000000000000', '000000000000007f') -ne 7) { throw 'Hamming above boundary' }
$bitmap = New-Object System.Drawing.Bitmap(768,432)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
    $graphics.Clear([System.Drawing.Color]::Black)
    if (-not [DeskClient]::IsBlack($bitmap)) { throw 'Black detection' }
    $graphics.Clear([System.Drawing.Color]::White)
    if ([DeskClient]::IsBlack($bitmap)) { throw 'White detection' }
    $data = [DeskClient]::EncodeJpeg($bitmap)
    if ($data.Length -gt 122880 -or $data[0] -ne 255 -or $data[1] -ne 216) { throw 'JPEG contract' }
    if ([DeskClient]::DHash($bitmap).Length -ne 16) { throw 'dHash contract' }
} finally { $graphics.Dispose(); $bitmap.Dispose() }
Write-Output 'PASS: detection, dHash, black capture, JPEG; no network or screen capture'
