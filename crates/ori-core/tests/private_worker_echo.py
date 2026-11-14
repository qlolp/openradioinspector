"""Owned synthetic child for cancellation/JOIN tests; not a scientific worker."""
import json
import os
from pathlib import Path
import sys
import time

request = json.load(sys.stdin)
marker = Path(sys.argv[1])
temporary = marker.with_name(marker.name + ".writing")
temporary.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
os.replace(temporary, marker)
if sys.argv[2] == "block":
    while True:
        time.sleep(1)
json.dump({"version": 1, "request_id": request["request_id"],
           "operation": request["operation"], "ok": True, "result": {}}, sys.stdout)
