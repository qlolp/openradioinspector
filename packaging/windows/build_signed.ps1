[CmdletBinding()]
param([Parameter(Mandatory)] [string] $Output)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$windows = Join-Path $root 'clients/windows'
$sign = Join-Path $PSScriptRoot 'authenticode.ps1'
$outputPath = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $outputPath) { throw 'Choose a new release output directory' }
$scratch = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
$null = [IO.Directory]::CreateDirectory($scratch)
try {
    # Tauri signs the UI and the NSIS (including its uninstaller) using the
    # same command as the separately staged core and supervisor.
    $env:ORI_WINDOWS_DISTRIBUTION = 'authenticode'
    $overlay = Join-Path $scratch 'signing.json'
    $config = @{ version = '0.4.0'; bundle = @{ windows = @{ signCommand = @{
        cmd = 'pwsh'; args = @('-NoProfile', '-NonInteractive', '-File', $sign, '%1')
    }}}}
    [IO.File]::WriteAllText($overlay, ($config | ConvertTo-Json -Depth 6))
    Push-Location $windows
    try {
        & npm.cmd run build -- --ci --config $overlay -- --locked
        if ($LASTEXITCODE -ne 0) { throw 'Signed Tauri build failed' }
    } finally { Pop-Location }
    $runtime = Join-Path $windows 'runtime'
    $manifestPath = Join-Path $runtime 'rc-build-info.json'
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    foreach ($name in @('ori-core', 'ori-supervisor')) {
        $file = Join-Path $runtime ($name + '.exe')
        & $sign -Path $file -VerifyOnly
        if ((Get-FileHash $file).Hash.ToLowerInvariant() -ne $manifest.components.$name.sha256) {
            throw 'Bundled helper changed after build manifest creation'
        }
    }
    $release = Join-Path $windows 'target/release'
    $ui = Join-Path $release 'openradioinspector-windows.exe'
    $nsis = @(Get-ChildItem (Join-Path $release 'bundle/nsis/*.exe') -File)
    if ($nsis.Count -ne 1) { throw 'Expected a single signed NSIS installer' }
    & $sign -Path $ui -VerifyOnly
    & $sign -Path $nsis[0].FullName -VerifyOnly
    $fingerprint = (Get-AuthenticodeSignature -LiteralPath $ui).SignerCertificate.Thumbprint
    foreach ($signedFile in @($nsis[0].FullName, (Join-Path $runtime 'ori-core.exe'), (Join-Path $runtime 'ori-supervisor.exe'))) {
        if ((Get-AuthenticodeSignature -LiteralPath $signedFile).SignerCertificate.Thumbprint -ne $fingerprint) {
            throw 'Different signing certificates across the packaged native binaries'
        }
    }
    $proof = @{source_revision=$manifest.source_revision; ui_sha256=(Get-FileHash $ui).Hash.ToLowerInvariant()
        signing=@{kind='authenticode'; certificate_sha1=$fingerprint.ToLowerInvariant()
        timestamp='RFC3161-SHA256'; verified=@('ori-core','ori-supervisor','ui','nsis')}}
    $receipt = Join-Path $scratch 'signing.json'
    [IO.File]::WriteAllText($receipt, ($proof | ConvertTo-Json -Depth 4))
    Push-Location $root
    try {
        python -m tools.release.release_artifacts windows --artifact $nsis[0].FullName --metadata $manifestPath --signing $receipt --out $outputPath
        if ($LASTEXITCODE -ne 0) { throw 'Failed to freeze the verified Windows package' }
    } finally { Pop-Location }
} finally { Remove-Item -LiteralPath $scratch -Recurse -Force }
