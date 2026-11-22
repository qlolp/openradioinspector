#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/../../.." && pwd)"
output="${1:?Provide a new or empty build output directory}"
mkdir -p "$output"
output="$(cd "$output" && pwd)"
cd "$project_root"
xcodebuild -version
xcrun swift -version
xcrun simctl list -j devices available > "$output/devices.json"
pair="$(python3 - "$output/devices.json" <<'PY'
import json, sys
from pathlib import Path
devices = json.loads(Path(sys.argv[1]).read_text())['devices']
for runtime in sorted(devices, reverse=True):
    if '.iOS-' in runtime:
        phones = [d for d in devices[runtime] if d['isAvailable'] and 'iPhone' in d['name']]
        if phones:
            print(runtime, phones[0]['deviceTypeIdentifier'])
            break
else:
    raise SystemExit('Install an iOS simulator runtime and iPhone device type with Xcode.')
PY
)"
read -r runtime kind <<< "$pair"
udid="$(xcrun simctl create "ORI Field ${GITHUB_RUN_ID:-local} $$" "$kind" "$runtime")"
trap 'xcrun simctl shutdown "$udid" >/dev/null 2>&1 || true; xcrun simctl delete "$udid"' EXIT
xcrun simctl boot "$udid"
xcrun simctl bootstatus "$udid" -b
xcodebuild -project clients/ios/Field.xcodeproj -scheme Field \
    -destination "platform=iOS Simulator,id=$udid" -derivedDataPath "$output/DerivedData" \
    -resultBundlePath "$output/Field.xcresult" -parallel-testing-enabled NO \
    CODE_SIGN_IDENTITY=- test
xcodebuild -project clients/ios/Field.xcodeproj -scheme Field -configuration Release \
    -destination 'generic/platform=iOS' -derivedDataPath "$output/DerivedData" \
    CODE_SIGNING_ALLOWED=NO build
