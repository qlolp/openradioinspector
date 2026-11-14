#!/usr/bin/env bash
# Pin the hosted CI compiler; deployment remains macOS 14 in the project.
set -euo pipefail
: "${GITHUB_ENV:?GitHub Actions environment file required}"
: "${GITHUB_PATH:?GitHub Actions path file required}"
: "${RUNNER_TEMP:?GitHub Actions temporary directory required}"

export DEVELOPER_DIR=/Applications/Xcode_27.2.app/Contents/Developer
if [[ ! -d "$DEVELOPER_DIR" ]]; then
    printf '%s\n' 'Xcode 27.2 is required; select the documented xcode-27 runner image.' >&2
    exit 1
fi
sudo xcode-select --switch "$DEVELOPER_DIR"
xcodebuild -version | tee "$RUNNER_TEMP/native-xcode-version.txt"
grep -Fxq 'Xcode 27.2' "$RUNNER_TEMP/native-xcode-version.txt"
xcrun swift --version | tee "$RUNNER_TEMP/native-swift-version.txt"
grep -Eq '^(Apple )?Swift version 6[.]4[.]2([[:space:]]|$)' "$RUNNER_TEMP/native-swift-version.txt"
native_swift_binary="$(xcrun --find swift)"
printf 'DEVELOPER_DIR=%s\n' "$DEVELOPER_DIR" >> "$GITHUB_ENV"
printf '%s\n' "${native_swift_binary%/*}" >> "$GITHUB_PATH"
python3 "$(dirname -- "${BASH_SOURCE[0]}")/prepare_c_sdk.py"
