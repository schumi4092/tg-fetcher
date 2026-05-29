$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $root "run_tg_fetcher_task.ps1"
$taskName = "TG Fetcher"

$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""

$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
  -MultipleInstances IgnoreNew `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries

try {
  Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Run tg-fetcher at logon and restart it if it exits." `
    -Force | Out-Null

  Write-Host "Installed scheduled task: $taskName"
  Write-Host "Runner: $runner"
} catch {
  $startup = [Environment]::GetFolderPath("Startup")
  $shortcutPath = Join-Path $startup "TG Fetcher.lnk"
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($shortcutPath)
  $shortcut.TargetPath = "powershell.exe"
  $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
  $shortcut.WorkingDirectory = $root
  $shortcut.WindowStyle = 7
  $shortcut.Description = "Run tg-fetcher at user logon."
  $shortcut.Save()
  Write-Host "Scheduled task install failed; installed Startup shortcut instead."
  Write-Host "Shortcut: $shortcutPath"
  Write-Host "Runner: $runner"
}
