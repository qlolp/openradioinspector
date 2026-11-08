param([Parameter(Mandatory=$true)][string]$Installer)
$ErrorActionPreference = 'Stop'
$installerPath = (Resolve-Path $Installer).Path
$root = Join-Path $env:RUNNER_TEMP ('ori-install-' + [guid]::NewGuid().ToString('N'))
$appRoot = Join-Path $root 'app'
$dataRoot = Join-Path $root 'data'
New-Item -ItemType Directory -Path $root,$dataRoot | Out-Null
Set-Content (Join-Path $dataRoot 'preserve.txt') 'synthetic measurement archive sentinel'
function Install-OwnedApp {
    $child = Start-Process $installerPath -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/DIR=`"$appRoot`"" -PassThru -Wait
    if ($child.ExitCode -ne 0) { throw "Installer failed: $($child.ExitCode)" }
}
try {
    Install-OwnedApp
    $app = Join-Path $appRoot 'OpenRadioInspector.exe'
    if (-not (Test-Path $app)) { throw 'Installed executable missing' }
    $child = Start-Process $app -ArgumentList '--self-check','--data-dir',"`"$dataRoot`"" -PassThru -Wait
    if ($child.ExitCode -ne 0) { throw 'Installed core self-check failed' }
    if (-not (Test-Path (Join-Path $dataRoot 'sessions.sqlite3'))) { throw 'Installed archive missing' }
    Install-OwnedApp
    if ((Get-Content (Join-Path $dataRoot 'preserve.txt')).Trim() -ne 'synthetic measurement archive sentinel') { throw 'Update changed user data' }
    $uninstaller = Join-Path $appRoot 'unins000.exe'
    $child = Start-Process $uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -PassThru -Wait
    if ($child.ExitCode -ne 0) { throw 'Uninstaller failed' }
    $deadline = (Get-Date).AddSeconds(20)
    while ((Test-Path $app) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
    if (Test-Path $app) { throw 'Application remains after uninstall' }
    foreach ($file in 'preserve.txt','sessions.sqlite3') {
        if (-not (Test-Path (Join-Path $dataRoot $file))) { throw "Uninstall removed user data: $file" }
    }
    Write-Host 'Per-user install, owned-core startup, update and data-preserving uninstall passed'
} finally {
    # Only this job's explicitly owned synthetic directory is removed.
    if (Test-Path (Join-Path $appRoot 'unins000.exe')) {
        Start-Process (Join-Path $appRoot 'unins000.exe') -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait | Out-Null
    }
    Remove-Item -Recurse -Force $root
}
