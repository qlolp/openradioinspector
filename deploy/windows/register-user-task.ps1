# Current-user, interactive Task Scheduler registration. Running this script
# explicitly enables or removes startup at the next Windows sign-in.
[CmdletBinding()]
param(
    [ValidateSet('Register', 'Unregister')][string]$Action = 'Register',
    [string]$SupervisorExe,
    [string]$Configuration,
    [string]$TaskName
)
$ErrorActionPreference = 'Stop'
$stage = 'identity'
trap {
    # ASCII stage and numeric HRESULT are safe to retain from a detached helper.
    [Console]::Error.WriteLine("ORI_TASK_ERROR $stage $($_.Exception.HResult)")
    break
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$stage = 'supervisor_path'
if (-not $SupervisorExe) { $SupervisorExe = [IO.Path]::Combine($PSScriptRoot, 'ori-supervisor.exe') }
$supervisorPath = (Resolve-Path -LiteralPath $SupervisorExe).Path
if ($Configuration) {
    # Existing installations may still register an explicit generic config.
    $stage = 'configuration_path'
    $configPath = (Resolve-Path -LiteralPath $Configuration).Path
    $arguments = 'run "' + $configPath + '"'
    if (-not $TaskName) { $TaskName = 'OpenRadioInspector supervisor' }
} else {
    $arguments = 'windows-node run'
    if (-not $TaskName) { $TaskName = 'OpenRadioInspector RC1 ' + $identity.User.Value }
}

$stage = 'scheduler_module'
$systemModules = [IO.Path]::Combine($env:SystemRoot, 'System32\WindowsPowerShell\v1.0\Modules')
Import-Module -Name ([IO.Path]::Combine($systemModules, 'ScheduledTasks', 'ScheduledTasks.psd1')) -ErrorAction Stop
$stage = 'query_task'
try {
    $existing = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction Stop
} catch {
    if ($_.CategoryInfo.Category -ne 'ObjectNotFound' -or
        $_.FullyQualifiedErrorId -notmatch '^CmdletizationQuery_NotFound(?:_[A-Za-z]+)?,Get-ScheduledTask$') { throw }
    $existing = $null
}
if ($existing) {
    $stage = 'verify_existing'
    $principalId = $existing.Principal.UserId
    if ($principalId -match '^S-1-') {
        $existingSid = $principalId
    } else {
        $account = New-Object System.Security.Principal.NTAccount $principalId
        $existingSid = $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    }
    if ($existingSid -ne $identity.User.Value -or
        @($existing.Actions).Count -ne 1 -or $existing.Actions[0].Execute -ne $supervisorPath -or
        $existing.Actions[0].Arguments -ne $arguments) {
        throw "Task '$TaskName' belongs to another installation or user."
    }
    if ($Action -eq 'Unregister') {
        $stage = 'remove_task'
        Unregister-ScheduledTask -InputObject $existing -Confirm:$false
    } else {
        $stage = 'enable_task'
        Enable-ScheduledTask -InputObject $existing | Out-Null
    }
    return
}
if ($Action -eq 'Unregister') { return }
$stage = 'prepare_task'
$launch = New-ScheduledTaskAction -Execute $supervisorPath -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$stage = 'register_task'
Register-ScheduledTask -TaskPath '\' -TaskName $TaskName -Action $launch -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
