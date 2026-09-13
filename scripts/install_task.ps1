<#
.SYNOPSIS
    Register the maintenance pipeline with Windows Task Scheduler.

.DESCRIPTION
    Prints the registration it would create and does nothing else unless
    -Register is passed.  Registering changes the host, so it is opt-in: a
    dry run is the default and cannot modify the machine.

    The task runs as the interactive user with no stored password.  Nothing in
    this script accepts or persists a credential; if the account needs one, use
    Task Scheduler's own UI so the secret never lands in a repository file.

    Auto-starting the external model gateway on Windows is unverified, which is
    why Task Scheduler remains the operational trigger.

.EXAMPLE
    .\install_task.ps1 -RepositoryRoot C:\path\to\repo -InputPath C:\path\to\inbox -OutputRoot C:\path\to\state
    .\install_task.ps1 -RepositoryRoot C:\path\to\repo -InputPath C:\path\to\inbox -OutputRoot C:\path\to\state -Register
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepositoryRoot,
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputRoot,
    [string]$LogPath,
    [string]$TaskName = 'MaintenancePipeline',
    [string]$At = '09:00',
    [switch]$Register
)

$ErrorActionPreference = 'Stop'

$wrapper = Join-Path $RepositoryRoot 'scripts\run_scheduled.bat'
if (-not (Test-Path -LiteralPath $wrapper)) {
    Write-Error "run_scheduled.bat not found under $RepositoryRoot"
    exit 2
}
if (-not (Test-Path -LiteralPath (Join-Path $RepositoryRoot 'run_pipeline.py'))) {
    Write-Error "run_pipeline.py not found under $RepositoryRoot"
    exit 2
}
if (-not $LogPath) {
    $LogPath = Join-Path $OutputRoot 'scheduled.log'
}

$argumentList = '"{0}" "{1}" "{2}"' -f $InputPath, $OutputRoot, $LogPath
$action    = New-ScheduledTaskAction -Execute $wrapper -Argument $argumentList -WorkingDirectory $RepositoryRoot
$trigger   = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

Write-Output "TaskName    : $TaskName"
Write-Output "Execute     : $wrapper"
Write-Output "Arguments   : $argumentList"
Write-Output "WorkingDir  : $RepositoryRoot"
Write-Output "Trigger     : Daily at $At"
Write-Output "Principal   : $($env:USERNAME) (interactive, limited, no stored password)"

if (-not $Register) {
    Write-Output ''
    Write-Output 'Dry run: nothing was registered. Re-run with -Register to apply.'
    exit 0
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output "Registered scheduled task '$TaskName'."
