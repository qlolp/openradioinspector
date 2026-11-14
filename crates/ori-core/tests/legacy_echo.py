"""Owned synthetic IPC process: transport/ownership fixture, not RF science."""
import json
import os
from pathlib import Path
import sys
import time

request = json.loads(sys.stdin.buffer.read())
marker = Path(sys.argv[2])
pending = marker.with_name(f".{marker.name}.{os.getpid()}.pending")
pending.write_text(str(os.getpid()), encoding="ascii")
pending.replace(marker)
if sys.argv[1] == "wait":
    while True:
        time.sleep(0.05)
result = {"reply": {"keywords": [], "count": 0}}
json.dump({"version": 1, "request_id": request["request_id"],
           "operation": "analysis", "ok": True, "result": result}, sys.stdout)
