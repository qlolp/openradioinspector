#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 -m pip install -e '.[serial,audio]' 'pyinstaller>=6.16,<7'
python3 -m PyInstaller --clean --noconfirm --name openradioinspector-core --collect-all radioinspector --collect-all sounddevice --collect-all _sounddevice_data --hidden-import serial.tools.list_ports --distpath build/macos/core --workpath build/macos/work --specpath build/macos packaging/core_entry.py
xcodebuild -project macos/DvoynoyObzor.xcodeproj -scheme DvoynoyObzor -configuration Release -derivedDataPath build/macos/DerivedData ONLY_ACTIVE_ARCH=YES CODE_SIGNING_ALLOWED=NO build
app=build/macos/OpenRadioInspector.app
mkdir -p build/macos
rm -rf "$app"
cp -R build/macos/DerivedData/Build/Products/Release/DvoynoyObzor.app "$app"
mkdir -p "$app/Contents/Helpers"
mv "$app/Contents/MacOS/DvoynoyObzor" "$app/Contents/Helpers/DvoynoyObzorUI"
cp -R build/macos/core/openradioinspector-core "$app/Contents/Helpers/"
cp packaging/macos/launch-app.sh "$app/Contents/MacOS/OpenRadioInspector"
chmod 755 "$app/Contents/MacOS/OpenRadioInspector"
/usr/libexec/PlistBuddy -c 'Set :CFBundleExecutable OpenRadioInspector' "$app/Contents/Info.plist"
mkdir -p "$app/Contents/Resources/Notices"
version=$(python3 -c 'from importlib.metadata import version; print(version("openradioinspector"))')
/usr/libexec/PlistBuddy -c 'Set :CFBundleShortVersionString 0.4.0' "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :ORIReleaseVersion string $version" "$app/Contents/Info.plist"
# PyInstaller already ad-hoc signs its Mach-O runtime. Sign only our moved
# executable; --deep misidentifies Python metadata directories as code bundles.
# The enclosing application remains explicitly unsigned (no identity/notarization).
codesign --force --sign - "$app/Contents/Helpers/DvoynoyObzorUI"
python3 tools/release/package_evidence.py "$app/Contents/Resources/Notices" --notices-only --inventory-dir "$app"
mkdir -p build/macos/packages
hdiutil create -volname OpenRadioInspector -srcfolder "$app" -ov -format UDZO "build/macos/packages/OpenRadioInspector-$version-macos-$(uname -m)-unsigned.dmg"
