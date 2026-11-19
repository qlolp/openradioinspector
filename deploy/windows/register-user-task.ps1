# Current-user, interactive Task Scheduler registration. Running this script
# explicitly enables or removes startup at the next Windows sign-in.
[CmdletBinding()]
param(
    [ValidateSet('Register', 'Unregister')][string]$Action = 'Register',
    [string]$SupervisorExe = (Join-Path $PSScriptRoot 'ori-supervisor.exe'),
    [string]$Configuration,
    [string]$TaskName
)
$ErrorActionPreference = 'Stop'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$supervisorPath = (Resolve-Path -LiteralPath $SupervisorExe).Path
if ($Configuration) {
    # Existing installations may still register an explicit generic config.
    $configPath = (Resolve-Path -LiteralPath $Configuration).Path
    $arguments = 'run "' + $configPath + '"'
    if (-not $TaskName) { $TaskName = 'OpenRadioInspector supervisor' }
} else {
    $arguments = 'windows-node run'
    if (-not $TaskName) { $TaskName = 'OpenRadioInspector RC1 ' + $identity.User.Value }
}

$existing = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing) {
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
        Unregister-ScheduledTask -InputObject $existing -Confirm:$false
    } else {
        Enable-ScheduledTask -InputObject $existing | Out-Null
    }
    return
}
if ($Action -eq 'Unregister') { return }
$launch = New-ScheduledTaskAction -Execute $supervisorPath -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskPath '\' -TaskName $TaskName -Action $launch -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
