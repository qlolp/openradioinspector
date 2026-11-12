"""Owned cancellation fixture; nonblocked requests use the actual released RPC."""
import json
import os
from pathlib import Path
import sys
import threading

raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
request = json.loads(raw)
if request.get("sweep", {}).get("id") == "first-blocked":
    marker = Path(sys.argv[1])
    temporary = marker.with_suffix(".pending")
    temporary.write_text(json.dumps({"pid": os.getpid(), "sweep_id": "first-blocked"}), encoding="utf-8")
    temporary.replace(marker)
    threading.Event().wait()
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from radioinspector.research_rpc import process_request

    response = process_request(raw)
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    raise SystemExit(0 if response["ok"] else 1)
