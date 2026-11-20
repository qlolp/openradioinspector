# Run in a fresh interactive Windows test account, using the installed RC.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Installer, [string]$ExpectedRevision, [string]$ReportPath,
    [string]$PriorInstaller, [string]$PriorInstallerSha256, [string]$PriorRevision,
    [switch]$RequireOffline
)
$ErrorActionPreference = 'Stop'
$report = [ordered]@{schema_version=1; result='running'; phase='fresh_account'; reached=@(); helper_errors=@(); cleanup_errors=@(); installer_exit_code=$null; package_rollback='pending_missing_prior_package'
    installer_sha256=$null; revision=$null; prior_installer_sha256=$null; prior_revision=$null; network_check='not_requested'}
function Save-SmokeReport {
    if ($ReportPath) {
        $target = [IO.Path]::GetFullPath($ReportPath)
        $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
        [IO.File]::WriteAllText($target, ($report | ConvertTo-Json -Depth 4))
    }
}
function Enter-SmokePhase([string]$Name) {
    # Diagnostics use fixed operation/reason names, numeric codes and exception types.
    $report.phase = $Name
    $report.reached += $Name
    if ($RequireOffline) {
        $report.network_check = 'checking_disconnected_host'
        if ([Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable()) {
            $report.network_check = 'network_available'
            throw 'Offline acceptance requires a disconnected Windows host'
        }
    }
    Save-SmokeReport
}
function Invoke-TestInstaller([string]$Package, [string[]]$Arguments) {
    $process = Start-Process -FilePath (Resolve-Path -LiteralPath $Package) -ArgumentList $Arguments -Wait -PassThru
    $report.installer_exit_code = $process.ExitCode
    if ($process.ExitCode -ne 0) { throw "NSIS operation failed ($($process.ExitCode))" }
}
function Assert-Revision([string]$Revision) {
    foreach ($name in @('ori-supervisor','ori-core')) {
        $info = & (Join-Path $runtime ($name + '.exe')) --build-info | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0 -or $info.source_revision -ne $Revision -or $info.target_arch -ne 'x86_64') {
            throw 'Installed package is from an unexpected revision'
        }
    }
}
function Uninstall-TestPackage {
    Invoke-TestInstaller (Join-Path $app 'uninstall.exe') @('/S')
    $end = [DateTime]::UtcNow.AddSeconds(60)
    while ((Test-Path -LiteralPath $desktopExe) -or (Test-Path -LiteralPath $core) -or (Test-Path -LiteralPath $supervisor)) {
        if ([DateTime]::UtcNow -ge $end) { throw 'Uninstaller left installed executables' }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Path $database) -or -not (Test-Path (Join-Path $data 'tasks.json')) -or
        (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue)) {
        throw 'Uninstall did not preserve the archive/task book or remove its sign-in task'
    }
}
try {
Enter-SmokePhase 'fresh_account'
$data = Join-Path $env:LOCALAPPDATA 'OpenRadioInspector-Windows-RC1'
$app = Join-Path $env:LOCALAPPDATA 'OpenRadioInspector RC'
if ((Test-Path -LiteralPath $data) -or (Test-Path -LiteralPath $app)) { throw 'This acceptance test needs a fresh Windows test account.' }
Enter-SmokePhase 'install_nsis'
$report.installer_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
if ($PriorInstaller -or $PriorInstallerSha256 -or $PriorRevision) {
    if ($PriorInstaller) {
        $report.prior_installer_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $PriorInstaller).Hash.ToLowerInvariant()
    }
    if (-not $PriorInstaller -or $PriorRevision -notmatch '^[a-fA-F0-9]{40}$' -or
        $PriorInstallerSha256 -notmatch '^[a-fA-F0-9]{64}$' -or
        $report.prior_installer_sha256 -ne $PriorInstallerSha256 -or $report.installer_sha256 -eq $PriorInstallerSha256) {
        throw 'The prior installer needs a distinct package with a pinned SHA-256 and source revision'
    }
    $report.package_rollback = 'not_run'
}
Invoke-TestInstaller $Installer @('/S')
Enter-SmokePhase 'verify_installed_components'
$runtime = Join-Path $app 'runtime'
$supervisor = Join-Path $runtime 'ori-supervisor.exe'
$core = Join-Path $runtime 'ori-core.exe'
$manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $runtime 'rc-build-info.json') | ConvertFrom-Json
if ($PriorInstaller -and $PriorRevision -eq $manifest.source_revision) { throw 'Prior package has the current revision' }
if ($ExpectedRevision -and $manifest.source_revision -ne $ExpectedRevision) { throw 'Installed release revision mismatch' }
$desktopExe = Join-Path $app 'openradioinspector-windows.exe'
$desktopInfo = & $desktopExe --build-info | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $desktopInfo.source_revision -ne $manifest.source_revision -or
    $desktopInfo.target_arch -ne 'x86_64' -or $desktopInfo.version -ne $manifest.release_version) {
    throw 'Installed Windows UI revision or target mismatch'
}
foreach ($name in @('ori-supervisor','ori-core')) {
    $helper = Join-Path $runtime ($name + '.exe')
    $info = & $helper --build-info | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $info.source_revision -ne $manifest.source_revision -or
        (Get-FileHash $helper).Hash -ne $manifest.components.$name.sha256 -or
        ($name -eq 'ori-core' -and ($info.sqlite_version -ne '3.54.1' -or $manifest.components.$name.sqlite_version -ne $info.sqlite_version))) {
        throw "Installed helper mismatch: $name"
    }
}
$report.revision = $manifest.source_revision
if (-not (Test-Path (Join-Path $runtime 'notices/licenses.json'))) { throw 'Installed notices missing' }

function Invoke-Node([string]$Action) {
    # A previous release can leave a capture pipe open in its long-lived node.
    # Read the CLI's one-line reply and wait on this command's process handle.
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $supervisor
    $info.Arguments = "windows-node $Action"
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    $process = [Diagnostics.Process]::Start($info)
    $outReader = $process.StandardOutput
    $errReader = $process.StandardError
    try {
        $stdout = $outReader.ReadLineAsync()
        $stderr = $errReader.ReadLineAsync()
        # Covers the 70s launch loop, a 5s pipe connect and a 150s exchange.
        if (-not $process.WaitForExit(230000)) {
            $process.Kill()
            $null = $process.WaitForExit(5000)
            throw "Windows node $Action command exceeded its deadline"
        }
        $code = $process.ExitCode
        $reply = if ($code -eq 0) { $stdout } else { $stderr }
        if (-not $reply.Wait(2000)) { throw "Windows node $Action supplied no complete response line" }
        $line = $reply.GetAwaiter().GetResult()
    } finally {
        $outReader.Dispose()
        $errReader.Dispose()
        $process.Dispose()
    }
    if ($code -ne 0) {
        $reason = 'helper_exit'
        $native = $null
        $schedulerStage = $null
        $schedulerHResult = $null
        $diagnostic = [string]$line
        if ($diagnostic -match '\(os error ([0-9]{1,6})\)') {
            $reason = 'windows_io'
            $native = [int]$Matches[1]
        } elseif ($diagnostic -match 'Windows Task Scheduler (?:Register|Unregister) failed at (identity|supervisor_path|configuration_path|scheduler_module|query_task|verify_existing|remove_task|enable_task|prepare_task|register_task) \(HRESULT (-?[0-9]{1,10})\)') {
            $reason = 'scheduler_script'
            $schedulerStage = $Matches[1]
            $schedulerHResult = [int]$Matches[2]
        } elseif ($diagnostic -match 'Windows Task Scheduler (?:Register|Unregister) failed before a stage receipt') { $reason = 'scheduler_launch' }
        elseif ($diagnostic -match 'private Windows status frame was refused') { $reason = 'status_frame_refused' }
        elseif ($diagnostic -match 'owned Windows backend status unavailable') { $reason = 'backend_status_unavailable' }
        elseif ($diagnostic -match 'owned Windows node is not ready') { $reason = 'backend_not_ready' }
        if ($report.helper_errors.Count -lt 8) {
            $report.helper_errors += [ordered]@{phase=$report.phase; action=$Action; exit=$code; reason=$reason; win32=$native; scheduler_stage=$schedulerStage; hresult=$schedulerHResult}
        }
        throw "Windows node $Action failed ($reason)"
    }
    $output = $line | ConvertFrom-Json
    if (-not $output.ok) { throw "Windows node $Action returned a refused response" }
    $output
}

function Assert-RawSweep([string]$Route) {
    $saved = (Invoke-RestMethod -TimeoutSec 30 "$Route/sweeps/windows-rc-sweep").data
    if ($saved.id -ne 'windows-rc-sweep' -or ($saved.frequencies_hz -join ',') -ne '100000001,100000011' -or
        $saved.level_unit -ne 'dBm' -or $saved.levels[0] -ne -95.375 -or $saved.levels[1] -ne -87.25) {
        throw 'Recovered measurement differs from the original sweep'
    }
}

function Assert-TaskBook {
    $tasks = (Invoke-RestMethod -TimeoutSec 30 "$url/api/v1/task-scheduler/tasks").data
    $saved = @($tasks.items | Where-Object { $_.name -eq 'windows-rc-operator-plan' })
    if ($saved.Count -ne 1 -or $saved[0].duration_s -ne 15) { throw 'Operator task did not survive' }
}

function Find-Button($Client, [string[]]$Labels) {
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    $type = [System.Windows.Automation.ControlType]::Button
    $condition = New-Object System.Windows.Automation.PropertyCondition ([System.Windows.Automation.AutomationElement]::ControlTypeProperty, $type)
    while ([DateTime]::UtcNow -lt $deadline) {
        $Client.Refresh()
        if ($Client.HasExited) { throw "Installed UI exited ($($Client.ExitCode))" }
        if ($Client.MainWindowHandle -ne 0) {
            $window = [System.Windows.Automation.AutomationElement]::FromHandle($Client.MainWindowHandle)
            foreach ($button in $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
                if ($Labels -contains $button.Current.Name) { return $button }
            }
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Installed UI button unavailable: $Labels"
}

Enter-SmokePhase 'verify_no_implicit_start'
$owner = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$task = 'OpenRadioInspector RC1 ' + $owner
if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { throw 'Unexpected automatic sign-in registration' }
if ((Invoke-Node 'status').service_state -ne 'stopped') { throw 'Installer started monitoring implicitly' }
Enter-SmokePhase 'launch_desktop'
$client = Start-Process -FilePath $desktopExe -PassThru
$primaryError = $null
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Enter-SmokePhase 'invoke_local_choice'
    $button = Find-Button $client @('Connect to the local node','Подключить локальный узел')
    $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    Enter-SmokePhase 'wait_for_owned_core'
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Local node did not reach readiness from the installed UI' }
        Start-Sleep -Milliseconds 200
        $node = Invoke-Node 'status'
    } until ($node.status.ready)
    if (-not $node.status.writer_owned -or -not $node.status.preflight_complete) { throw 'Writer/preflight unavailable' }
    $id = $node.status.instance_id
    Enter-SmokePhase 'reject_duplicate_owner'
    if ((Invoke-Node 'start').status.instance_id -ne $id) { throw 'Second start replaced the owner' }
    $duplicate = Start-Process -FilePath $supervisor -ArgumentList 'windows-node run' -PassThru -Wait -NoNewWindow
    if ($duplicate.ExitCode -eq 0 -or (Invoke-Node 'status').status.instance_id -ne $id) { throw 'Duplicate service admitted' }
    Enter-SmokePhase 'close_desktop'
    if (-not $client.CloseMainWindow() -or -not $client.WaitForExit(15000)) { throw 'Could not close the installed UI normally' }
    if ((Invoke-Node 'status').status.instance_id -ne $id) { throw 'Closing UI interrupted monitoring' }

    Enter-SmokePhase 'verify_archive_acl'
    $acl = Get-Acl -LiteralPath $data
    $rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
    if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $owner) { throw 'RC data DACL is not private' }
    Enter-SmokePhase 'verify_logon_task'
    Invoke-Node 'register' | Out-Null
    $registered = Get-ScheduledTask -TaskName $task
    if ($registered.Actions.Execute -ne $supervisor -or $registered.Actions.Arguments -ne 'windows-node run' -or
        $registered.Principal.RunLevel -ne 'Limited' -or $registered.Triggers.CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger') {
        throw 'Incorrect per-user logon task'
    }
    Invoke-Node 'unregister' | Out-Null
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) { throw 'Sign-in task still registered' }

    Enter-SmokePhase 'verify_offline_assets'
    $url = 'http://127.0.0.1:8770'
    $webClient = New-Object System.Net.WebClient
    $webClient.Proxy = $null
    try {
        foreach ($file in $manifest.web.PSObject.Properties) {
            $bytes = $webClient.DownloadData($url + '/modern/' + $file.Name)
            $hasher = [Security.Cryptography.SHA256]::Create()
            try { $hash = [BitConverter]::ToString($hasher.ComputeHash($bytes)).Replace('-','') }
            finally { $hasher.Dispose() }
            if ($hash -ne $file.Value) { throw "Installed offline asset changed: $($file.Name)" }
        }
    } finally { $webClient.Dispose() }
    Enter-SmokePhase 'check_live_capabilities'
    $caps = (Invoke-RestMethod -TimeoutSec 30 "$url/api/v1/capabilities").data
    if ($caps.candidate.mode -ne 'live' -or -not $caps.persistent_archive -or -not $caps.features.explicit_serial_adapters -or $caps.features.analog_audio_inputs) {
        throw 'Incorrect installed live capabilities'
    }
    Enter-SmokePhase 'commit_measurement'
    $sample = @{id='windows-rc-smoke';name='Installed Windows node';streams=@(@{id='spectrum';device='synthetic';role='spectrum_analyzer';level_unit='dBm'})}
    $null = Invoke-RestMethod -TimeoutSec 30 "$url/api/v1/sessions" -Method Post -ContentType 'application/json' -Body ($sample | ConvertTo-Json -Depth 5)
    $route = "$url/api/v1/sessions/windows-rc-smoke"
    $null = Invoke-RestMethod -TimeoutSec 30 "$route/start" -Method Post -ContentType 'application/json' -Body '{}'
    $sampleSweep = @{id='windows-rc-sweep';session_id=$sample.id;stream_id='spectrum';
        interval=@{started_at='2026-11-01T12:00:00Z';ended_at='2026-11-01T12:00:01Z'};
        frequencies_hz=@(100000001,100000011);levels=@(-95.375,-87.25);level_unit='dBm';
        settings=@{adapter='synthetic-contract';generator_output=$false}}
    $null = Invoke-RestMethod -TimeoutSec 30 "$route/sweeps" -Method Post -ContentType 'application/json' -Body ($sampleSweep | ConvertTo-Json -Depth 5)
    $taskInput = Join-Path $data 'smoke-input.task'
    [IO.File]::WriteAllText($taskInput, 'unused receive-only test input')
    $plan = @{name='windows-rc-operator-plan'; start_at=[DateTime]::UtcNow.AddDays(7).ToString('yyyy-MM-ddTHH:mm:ss.ffffffZ');
        duration_s=15; task_file=$taskInput}
    $null = Invoke-RestMethod -TimeoutSec 30 "$url/api/v1/task-scheduler/tasks" -Method Post -ContentType 'application/json' -Body ($plan | ConvertTo-Json)
    Assert-TaskBook
    Invoke-Node 'shutdown' | Out-Null
    Invoke-Node 'check-stopped' | Out-Null
    if ((Invoke-Node 'status').service_state -ne 'stopped') { throw 'Explicit shutdown was not completed' }
    Enter-SmokePhase 'reinstall_update'
    # Move one package resource from this fresh test account to prove /UPDATE
    # actually writes files. The application archive stays outside the install.
    $readme = Join-Path $runtime 'README.txt'
    $readmeHash = (Get-FileHash $readme).Hash
    Move-Item -LiteralPath $readme -Destination (Join-Path $data 'smoke-preinstall-readme.txt')
    Invoke-TestInstaller $Installer @('/S','/UPDATE')
    if ((Get-FileHash $readme).Hash -ne $readmeHash) { throw 'NSIS did not restore its installed resource' }
    Assert-Revision $manifest.source_revision
    Enter-SmokePhase 'recover_after_explicit_shutdown'
    $null = Invoke-Node 'start'
    Enter-SmokePhase 'verify_recovered_measurements'
    $restored = (Invoke-RestMethod -TimeoutSec 30 "$url/api/v1/sessions/windows-rc-smoke").data
    if ($restored.id -ne 'windows-rc-smoke') { throw 'Committed session not recovered after restart' }
    Assert-RawSweep $route
    Assert-TaskBook
    Invoke-Node 'shutdown' | Out-Null
    Enter-SmokePhase 'materialize_v6'
    $previous = Join-Path $data 'smoke-format-v6.sqlite3'
    & $core archive-materialize (Join-Path $data 'sessions.sqlite3') $previous
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $previous)) { throw 'Installed compatibility export failed' }
    # Only this test's freshly created files are used. Preserve the v7 source.
    $database = Join-Path $data 'sessions.sqlite3'
    foreach ($suffix in @('-wal','-shm','-journal')) {
        if (Test-Path -LiteralPath ($database + $suffix)) { throw 'Source archive has live sidecars' }
    }
    Enter-SmokePhase 'open_existing_v6'
    Move-Item -LiteralPath $database -Destination (Join-Path $data 'smoke-source-v7.sqlite3')
    Move-Item -LiteralPath $previous -Destination $database
    Enter-SmokePhase 'uninstall_preserving_data'
    Invoke-Node 'register' | Out-Null
    Uninstall-TestPackage
    if ($PriorInstaller) {
        Enter-SmokePhase 'install_prior_package'
        $report.package_rollback = 'failed'
        Invoke-TestInstaller $PriorInstaller @('/S')
        Assert-Revision $PriorRevision
        $report.prior_revision = $PriorRevision.ToLowerInvariant()
        $null = Invoke-Node 'start'
        Assert-RawSweep $route
        Assert-TaskBook
        Invoke-Node 'shutdown' | Out-Null
        $report.package_rollback = 'passed'
        Enter-SmokePhase 'upgrade_prior_package'
        Invoke-TestInstaller $Installer @('/S','/UPDATE')
    } else {
        Enter-SmokePhase 'install_current_over_saved_data'
        Invoke-TestInstaller $Installer @('/S')
    }
    Assert-Revision $manifest.source_revision
    $null = Invoke-Node 'start'
    Assert-RawSweep $route
    Assert-TaskBook
    Invoke-Node 'shutdown' | Out-Null
    Enter-SmokePhase 'final_uninstall'
    Invoke-Node 'register' | Out-Null
    Uninstall-TestPackage
} catch {
    $primaryError = $_
    throw
} finally {
    # Teardown must preserve the original operation and continue independent cleanup.
    foreach ($part in @('close_ui','shutdown','unregister','dispose_ui')) {
        try {
            switch ($part) {
                'close_ui' { if (-not $client.HasExited) { $null = $client.CloseMainWindow() } }
                'shutdown' {
                    if (Test-Path -LiteralPath $supervisor) {
                        $status = Invoke-Node 'status'
                        if ($status.status) { Invoke-Node 'shutdown' | Out-Null }
                    }
                }
                'unregister' { if (Test-Path -LiteralPath $supervisor) { Invoke-Node 'unregister' | Out-Null } }
                'dispose_ui' { $client.Dispose() }
            }
        } catch {
            $report.cleanup_errors += [ordered]@{operation=$part; type=$_.Exception.GetType().Name; line=$_.InvocationInfo.ScriptLineNumber}
        }
    }
    if (-not $primaryError -and $report.cleanup_errors.Count -gt 0) { throw 'Installed smoke teardown failed' }
}
if ($RequireOffline) {
    if ([Net.NetworkInformation.NetworkInterface]::GetIsNetworkAvailable()) {
        $report.network_check = 'network_available'
        throw 'Windows network became available during offline acceptance'
    }
    $report.network_check = 'disconnected_at_all_phase_checks'
}
$report.result = 'passed'
} catch {
    $report.result = 'failed'
    $report.error_type = $_.Exception.GetType().Name
    $report.error_line = $_.InvocationInfo.ScriptLineNumber
    throw
} finally {
    Save-SmokeReport
}
