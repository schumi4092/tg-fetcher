$ErrorActionPreference = "Stop"

$utf8 = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = $utf8
try {
  [Console]::OutputEncoding = $utf8
  [Console]::InputEncoding = $utf8
} catch {
}
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

Set-Location $root
$logPath = Join-Path $logDir "server-task.log"
$streamPath = Join-Path $logDir "server-stream.log"
$stopFile = Join-Path $root "tg_fetcher.stop"
$serverScript = Join-Path $root "server.py"
$serverPattern = [regex]::Escape($serverScript)

while (-not (Test-Path $stopFile)) {
  $existing = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like "python*" -and $_.CommandLine -match $serverPattern } |
    Select-Object -First 1

  if ($existing) {
    Start-Sleep -Seconds 60
    continue
  }

  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $logPath -Value "`n===== start $stamp ====="

  $cmdLine = "`"python`" `"$serverScript`" 2>&1"
  & cmd.exe /d /s /c $cmdLine | ForEach-Object {
    $line = [string]$_
    Write-Host $line
    Add-Content -Path $streamPath -Value $line -Encoding UTF8
  }
  $code = $LASTEXITCODE
  $end = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $logPath -Value "===== exit $end code=$code ====="

  Start-Sleep -Seconds 60
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logPath -Value "===== watchdog stopped $stamp ====="
