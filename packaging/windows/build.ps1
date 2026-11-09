$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '../..')
python -m pip install -e '.[serial,audio]' 'pyinstaller>=6.16,<7'
if ($LASTEXITCODE) { throw 'Dependency installation failed' }
python -m PyInstaller --clean --noconfirm --name OpenRadioInspector --windowed --collect-all radioinspector --collect-all sounddevice --collect-all _sounddevice_data --hidden-import radioinspector.windows.scheduler_app --hidden-import serial.tools.list_ports --distpath build/windows/dist --workpath build/windows/work --specpath build/windows packaging/desktop_entry.py
if ($LASTEXITCODE) { throw 'PyInstaller build failed' }
python tools/release/package_evidence.py build/windows/dist/OpenRadioInspector --notices-only --inventory-dir build/windows/dist/OpenRadioInspector
if ($LASTEXITCODE) { throw 'Runtime notices could not be prepared' }
$env:ORI_PACKAGE_VERSION = python -c 'from importlib.metadata import version; print(version("openradioinspector"))'
if ($LASTEXITCODE) { throw 'Cannot determine package version' }
$iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidate = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $candidate)) {
        choco install innosetup --yes --no-progress
        if ($LASTEXITCODE) { throw 'Inno Setup installation failed' }
    }
    $isccPath = $candidate
}
if ($iscc) { $isccPath = $iscc.Source }
& $isccPath packaging/windows/installer.iss
if ($LASTEXITCODE) { throw 'Installer build failed' }
