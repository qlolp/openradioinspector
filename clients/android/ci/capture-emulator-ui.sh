#!/usr/bin/env bash
# Run from clients/android against the live Android 12 CI emulator.
set -euo pipefail
output=app/build/outputs/emulator-ui
mkdir -p "$output"
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell input keyevent KEYCODE_WAKEUP
adb shell wm dismiss-keyguard
adb shell am start -W -n org.openradioinspector.field/.MainActivity > "$output/activity.txt"
deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
    if adb shell uiautomator dump /data/local/tmp/field-screen.xml &&
        adb exec-out cat /data/local/tmp/field-screen.xml > "$output/setup.xml" &&
        python3 - "$output/setup.xml" <<'PY'
import sys
from xml.etree import ElementTree as ET

nodes = ET.parse(sys.argv[1]).iter("node")
labels = {node.get("text") for node in nodes if node.get("package") == "org.openradioinspector.field"}
expected = {"Полевой приём", "Прибор", "Сигнал", "Архив", "USB-прибор"}
missing = expected - labels
if missing:
    sys.exit(f"Waiting for Field setup controls: {sorted(missing)}")
PY
    then
        adb exec-out screencap -p > "$output/setup.png"
        python3 - "$output/setup.png" <<'PY'
from pathlib import Path
import struct
import sys

header = Path(sys.argv[1]).read_bytes()[:24]
if header[:8] != b"\x89PNG\r\n\x1a\n" or min(struct.unpack(">II", header[16:24])) < 320:
    sys.exit("Invalid Android screencap")
PY
        {
            echo "Android emulator, synthetic CI verification"
            echo "commit=$(git rev-parse HEAD)"
            echo "fingerprint=$(adb shell getprop ro.build.fingerprint | tr -d '\r')"
            echo "supported_abis=$(adb shell getprop ro.product.cpu.abilist | tr -d '\r')"
        } > "$output/source.txt"
        exit 0
    fi
    sleep 1
done
adb exec-out screencap -p > "$output/failure.png"
adb logcat -d -s AndroidRuntime ActivityManager ActivityTaskManager > "$output/failure-logcat.txt"
tail -40 "$output/failure-logcat.txt" >&2
if [[ -s "$output/setup.xml" ]]; then
    python3 - "$output/setup.xml" <<'PYXML'
from xml.etree import ElementTree as ET
import sys

for node in ET.parse(sys.argv[1]).iter("node"):
    if node.get("text") or node.get("content-desc"):
        print(node.get("package"), node.get("text"), node.get("content-desc"))
PYXML
fi
echo "Field setup controls did not appear in time" >&2
exit 1
