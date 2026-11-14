# Explicit per-user, offline logon registration. Run manually after acceptance.
# No elevation, downloads, credentials, or machine-wide service registration.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SupervisorExe,
    [Parameter(Mandatory = $true)][string]$Configuration,
    [string]$TaskName = 'OpenRadioInspector supervisor'
)
$ErrorActionPreference = 'Stop'
$supervisorPath = (Resolve-Path -LiteralPath $SupervisorExe).Path
$configPath = (Resolve-Path -LiteralPath $Configuration).Path
if ($supervisorPath.Contains('"') -or $configPath.Contains('"')) {
    throw 'Executable/configuration paths cannot contain quote characters.'
}
# The task engine starts this executable directly; no cmd.exe or PowerShell child wrapper.
$action = New-ScheduledTaskAction -Execute $supervisorPath -Argument ('run "' + $configPath + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
# Do not use Stop-ScheduledTask as a graceful cutover mechanism. See the documented Windows limits.
