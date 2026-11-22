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
# Prefer a standard iOS runtime and full-size iPhone over device-specific runtimes.
import re
standard = sorted((r for r in devices if re.fullmatch(r'.*\.iOS-\d+(?:-\d+)*', r)), reverse=True)
for runtime in standard + sorted(set(devices) - set(standard), reverse=True):
    if '.iOS-' in runtime:
        phones = [d for d in devices[runtime] if d['isAvailable'] and 'iPhone' in d['name']]
        if phones:
            selected = next((d for d in phones if 'Pro Max' in d['name']), phones[0])
            print(runtime, selected['deviceTypeIdentifier'])
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
destination="platform=iOS Simulator,id=$udid"
host_arch="$(uname -m)"
if [[ -n "${ORI_FIELD_CORE:-}" ]]; then
    xcodebuild -project clients/ios/Field.xcodeproj -scheme Field \
        -destination "$destination" -derivedDataPath "$output/DerivedData" \
        ONLY_ACTIVE_ARCH=YES ARCHS="$host_arch" CODE_SIGN_IDENTITY=- build-for-testing
    plans=("$output"/DerivedData/Build/Products/*.xctestrun)
    [[ ${#plans[@]} -eq 1 && -f "${plans[0]}" ]]
    python3 clients/ios/ci/reader_fixture.py "$ORI_FIELD_CORE" "${plans[0]}" "$destination" "$output/Field.xcresult"
else
    xcodebuild -project clients/ios/Field.xcodeproj -scheme Field \
        -destination "$destination" -derivedDataPath "$output/DerivedData" \
        -resultBundlePath "$output/Field.xcresult" -parallel-testing-enabled NO \
        ONLY_ACTIVE_ARCH=YES ARCHS="$host_arch" CODE_SIGN_IDENTITY=- test
fi
xcodebuild -project clients/ios/Field.xcodeproj -scheme Field -configuration Release \
    -destination 'generic/platform=iOS' -derivedDataPath "$output/DerivedData" \
    CODE_SIGNING_ALLOWED=NO build
