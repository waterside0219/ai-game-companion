# Launcher (11-07): pull the newest desk-overlay.ps1 from the server, verify it, then start it.
# Any failure keeps the current copy and starts it anyway. ASCII only so PowerShell 5.1 reads it without a BOM.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $here 'desk-overlay.ps1'
$appDir = Join-Path $env:APPDATA 'desk-companion'
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
$logPath = Join-Path $appDir 'update.log'
function Log($text) { try { Add-Content -Path $logPath -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $text) -Encoding UTF8 } catch {} }
function Sha256Hex([byte[]]$bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (-join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })) } finally { $sha.Dispose() }
}
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $config = Get-Content -Raw -Encoding UTF8 (Join-Path $appDir 'config.json') | ConvertFrom-Json
    $base = ([string]$config.base_url).TrimEnd('/')
    if (-not $base) { $base = 'https://play.example.com' }
    $client = New-Object System.Net.WebClient
    $client.Encoding = [System.Text.Encoding]::UTF8
    $client.Headers['Authorization'] = 'Bearer ' + [string]$config.device_token
    $manifest = $client.DownloadString($base + '/v1/client/manifest.json') | ConvertFrom-Json
    $want = [string]$manifest.files.'desk-overlay.ps1'
    $have = ''
    if (Test-Path $target) { $have = Sha256Hex ([System.IO.File]::ReadAllBytes($target)) }
    if ($want -and $want -ne $have) {
        $client.Headers['Authorization'] = 'Bearer ' + [string]$config.device_token
        $bytes = $client.DownloadData($base + '/v1/client/desk-overlay.ps1')
        if ((Sha256Hex $bytes) -ne $want) { throw 'downloaded file hash mismatch' }
        $staged = Join-Path $here 'desk-overlay.new.ps1'
        [System.IO.File]::WriteAllBytes($staged, $bytes)
        $test = Start-Process powershell.exe -ArgumentList @('-NoProfile','-STA','-ExecutionPolicy','Bypass','-File',$staged,'-SelfTest') -WindowStyle Hidden -PassThru -Wait
        if ($test.ExitCode -ne 0) { Remove-Item $staged -Force; throw ('new version failed self-test, exit ' + $test.ExitCode) }
        if (Test-Path $target) { Copy-Item $target (Join-Path $here 'desk-overlay.prev.ps1') -Force }
        Move-Item $staged $target -Force
        Log ('updated to ' + $want.Substring(0,12))
    } else {
        Log 'already newest'
    }
} catch {
    Log ('update skipped: ' + $_.Exception.Message)
}
Start-Process powershell.exe -ArgumentList @('-NoProfile','-STA','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',$target) -WindowStyle Hidden
