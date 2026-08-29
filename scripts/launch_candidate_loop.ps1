[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$RunDir,
    [string]$Title = 'Training candidate automation',
    [string]$RecoveryTask,
    [switch]$NoNotify,
    [switch]$WhatIf
)

$toolRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$runner = Join-Path $toolRoot 'candidate_loop.py'
$monitorLauncher = Join-Path $toolRoot 'scripts\launch_training_monitor.ps1'
$pythonConsole = (Get-Command python -ErrorAction Stop).Source
$python = $pythonConsole
$pythonResolved = (& $pythonConsole -c 'import sys; print(sys.executable)').Trim()
if (-not (Test-Path -LiteralPath $pythonResolved)) { $pythonResolved = $pythonConsole }
$pythonWindow = Join-Path (Split-Path -Parent $pythonResolved) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonWindow) { $python = $pythonWindow }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$automationLog = Join-Path $RunDir 'automation.log'
$automationError = Join-Path $RunDir 'automation.stderr.log'
$arguments = @('-u', $runner, '--config', $ConfigPath, '--run-dir', $RunDir)
$quotedArguments = $arguments | ForEach-Object {
    '"' + ([string]$_).Replace('"', '\"') + '"'
}
if ($WhatIf) {
    Write-Output ("{0} {1}" -f $python, ($quotedArguments -join ' '))
    Write-Output ("{0} -Title `"{1}`" -WatchPath `"{2}`" -LogPath `"{3}`" -StatePath `"{4}`"" -f $monitorLauncher, $Title, $RunDir, $automationLog, (Join-Path $RunDir 'monitor_state.json'))
    exit 0
}
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$process = Start-Process -FilePath $python -ArgumentList ($quotedArguments -join ' ') -WorkingDirectory $toolRoot -RedirectStandardOutput $automationLog -RedirectStandardError $automationError -WindowStyle Hidden -PassThru
$monitorArgs = @{
    Title = $Title
    WatchPath = $RunDir
    LogPath = $automationLog
    StatePath = (Join-Path $RunDir 'monitor_state.json')
    ProcessId = $process.Id
}
$monitorArgs.NoNotify = $true
$monitorArgs.ReplaceExisting = $true
if ($RecoveryTask) { $monitorArgs.RecoveryTask = $RecoveryTask }
& $monitorLauncher @monitorArgs
Write-Output ("Started candidate automation PID {0}; monitor opened." -f $process.Id)
