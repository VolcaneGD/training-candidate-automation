[CmdletBinding()]
param(
    [string]$Title = 'Training Monitor',
    [string]$WatchPath,
    [string]$LogPath,
    [int]$ProcessId,
    [string]$ModalApp,
    [string]$StatePath,
    [int]$RefreshSeconds = 1,
    [string]$RecoveryTask,
    [string]$InstanceKey = 'training-candidate-monitor',
    [switch]$ReplaceExisting,
    [switch]$NoNotify,
    [switch]$WhatIf
)

$toolRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$monitor = Join-Path $toolRoot 'training_monitor.py'
$python = (Get-Command python -ErrorAction Stop).Source
$pythonWindow = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonWindow) { $python = $pythonWindow }
$arguments = @($monitor, '--title', $Title, '--refresh-seconds', $RefreshSeconds, '--instance-key', $InstanceKey)
if ($WatchPath) { $arguments += @('--watch-path', $WatchPath) }
if ($LogPath) { $arguments += @('--log-path', $LogPath) }
if ($PSBoundParameters.ContainsKey('ProcessId')) { $arguments += @('--process-id', $ProcessId) }
if ($ModalApp) { $arguments += @('--modal-app', $ModalApp) }
if ($StatePath) { $arguments += @('--state-path', $StatePath) }
if ($RecoveryTask) { $arguments += @('--recovery-task', $RecoveryTask) }
if (-not $NoNotify) { $arguments += '--notify' }
$quotedArguments = $arguments | ForEach-Object {
    '"' + ([string]$_).Replace('"', '\"') + '"'
}
if ($WhatIf) {
    Write-Output ("Start-Process -FilePath `"{0}`" -ArgumentList `"{1}`" -WorkingDirectory `"{2}`" -WindowStyle Hidden" -f $python, ($quotedArguments -join ' '), $toolRoot)
    exit 0
}
if ($ReplaceExisting) {
    $instancePattern = [regex]::Escape($InstanceKey)
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'pythonw.exe' -and
        $_.CommandLine -match 'training_monitor\.py' -and
        $_.CommandLine -match $instancePattern
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
    }
}
Start-Process -FilePath $python -ArgumentList ($quotedArguments -join ' ') -WorkingDirectory $toolRoot -WindowStyle Hidden | Out-Null
