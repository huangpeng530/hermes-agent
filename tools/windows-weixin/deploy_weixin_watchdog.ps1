<#
.SYNOPSIS
  deploy_weixin_watchdog.ps1 - one-time deploy of the weixin watchdog on Windows.
.DESCRIPTION
  1. Copies the two scripts + launcher into %HERMES_HOME%\weixin\
     (that is where the scheduled task and the runtime state live).
  2. Creates scheduled task \Hermes_WeixinWatchdog (every 10 minutes).
  3. Seeds a clean watchdog.state.json baseline (log scan starts at EOF).
.PARAMETER HermesHome
  Default: $env:HERMES_HOME or E:\BACK-AI\Hermes-win
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy_weixin_watchdog.ps1
#>
param([string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "E:\BACK-AI\Hermes-win" }))

$ErrorActionPreference = "Stop"
$src      = Split-Path -Parent $MyInvocation.MyCommand.Path
$destDir  = Join-Path $HermesHome "weixin"
$stateFile = Join-Path $destDir "watchdog.state.json"

# 1. copy scripts
New-Item -ItemType Directory -Force -Path $destDir | Out-Null
Copy-Item (Join-Path $src "venv_integrity.py")        $destDir -Force
Copy-Item (Join-Path $src "weixin_watchdog.py")       $destDir -Force
Copy-Item (Join-Path $src "weixin_watchdog_launcher.bat") $destDir -Force
Write-Host "[1/3] scripts copied to $destDir"

# 2. scheduled task: every 10 min, interactive, user = current user
$taskName  = "Hermes_WeixinWatchdog"
$batPath   = Join-Path $destDir "weixin_watchdog_launcher.bat"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batPath`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration ([timespan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User $env:USERNAME | Out-Null
Write-Host "[2/3] scheduled task \\\$taskName created (every 10 min)"

# 3. seed clean state (log scan starts at the current end of agent.log)
$py  = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
$log = Join-Path $HermesHome "logs\agent.log"
$pos = if (Test-Path $log) { (Get-Item $log).Length } else { 0 }
$state = @{ log_pos = $pos; seeded = $true; last_restart_ts = 0; consec_fail = 0 }
$state | ConvertTo-Json | Set-Content $stateFile -Encoding ASCII
Write-Host "[3/3] state seeded (agent.log pos=$pos)"

Write-Host "Deploy complete. Test with:"
Write-Host "  schtasks /run /tn \$taskName"
Write-Host "  type `$env:HERMES_HOME\weixin\watchdog.stdout.log"
