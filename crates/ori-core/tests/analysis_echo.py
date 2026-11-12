"""Owned one-shot fixture checks core source selection and process transport."""
import json
import os
from pathlib import Path
import sys
import time

raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
if len(raw) > 16 * 1024 * 1024:
    raise SystemExit(2)
request = json.loads(raw)
mode = sys.argv[1]
if len(sys.argv) > 2:
    marker = Path(sys.argv[2])
    pending = marker.with_name(f".{marker.name}.{os.getpid()}.pending")
    pending.write_text(str(os.getpid()), encoding="ascii")
    pending.replace(marker)
if mode == "slow":
    time.sleep(30)
response = {"version": 1, "request_id": request["request_id"],
            "operation": "analysis", "ok": True}
if mode == "error":
    response.update(ok=False, error={"code": "validation_error", "type": "ValueError",
        "message": "Synthetic exact error 😀", "api_code": "invalid_request",
        "api_message": "Synthetic exact error 😀", "http_status": 400})
elif mode == "bad-operation":
    response.update(operation="different", result={})
else:
    response["result"] = {key: request[key] for key in
                          ("method", "session", "sweeps", "candidates", "payload")}
sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False,
                           separators=(",", ":")) + "\n")
raise SystemExit(1 if mode == "error" else 0)
