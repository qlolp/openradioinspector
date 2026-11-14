"""Generate additional actual-Python ISO/coercion/rounding boundary vectors."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from radioinspector.task_scheduler import TaskScheduler
from radioinspector.tasks import ControlTask,TaskBook

ROOT=Path(__file__).resolve().parents[2]
BASE=datetime(2026,11,11,12,0,0,123456,tzinfo=timezone.utc)
TIMES=["2026-11-11T12:00:00Z","20261111T120000Z","2026-W46-3T12:00Z",
    "2026W463T120000+0200","2026-11-11_12:00:00Z","2026-11-11😀12:00:00Z",
    "2026-11-11T12+0200","2026-11-11T12.5+02","2026-11-11T12:00:00+01:02:03.123456",
    "2026-11-11T12:00:00+00:00:00.123456","2026-11-11T12:00:00+01:99",
    "2026-11-11T12:00:00+01:02:99","2026-11-11T12:00:00,123456999-03:30",
    "2026-11-11T12:00:00","2026-11-11Z12:00Z","2026-11-11T24:00:00Z",
    "2026-11-11T12:00:60Z","2026-11-11T12:00:00+25:00"]

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",type=Path);args=parser.parse_args()
    times=[]
    with tempfile.TemporaryDirectory(prefix="ori-task-edges-") as temporary:
        root=Path(temporary);input_file=root/"input";input_file.write_text("{}",encoding="utf-8")
        scheduler=TaskScheduler(TaskBook(root/"tasks.json"),interval_seconds=60)
        try:
            for index,start in enumerate(TIMES):
                vector={"input":start}
                try:
                    result=scheduler.create_task(dict(name=f"time-{index}",task_file=str(input_file),start_at=start,duration_s=1))
                    vector["result"]=result
                except (ValueError,TypeError,OverflowError) as error:
                    vector["error"]=str(error)
                times.append(vector)
        finally:scheduler.close()
    coerced=[]
    for changes in [{"duration_s":"1_000.25"},{"duration_s":"١٢.٥"},{"duration_s":True},
        {"completed_cycles":"+1_000"},{"completed_cycles":"١٢"},{"completed_cycles":-2.9},
        {"completed_cycles":True},{"completed_cycles":18446744073709551617},
        {"device_names":"е\u0301","attachments":{"first":1,"second":2},"created_at":"broken"}]:
        value={"name":"не нормализовать е\u0301","start_at":BASE.isoformat(),**changes}
        coerced.append({"input":value,"result":ControlTask.from_dict(value).as_dict()})
    durations=[]
    for value in [0.0000005,0.0000015,0.0000025,1.0000005,100000000.0000006,1e13,1e14,1e20]:
        vector={"duration_s":value}
        try:vector["ends_at"]=ControlTask("duration",start_at=BASE,duration_s=value).ends_at().isoformat()
        except (ValueError,OverflowError) as error:vector["error"]=str(error)
        durations.append(vector)
    sources=["tasks.py","task_scheduler.py","contracts.py"]
    result={"format":"ori-tasks-python-edge-oracle","format_version":1,"python":sys.version,
        "generator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256":{name:hashlib.sha256((ROOT/"radioinspector"/name).read_bytes()).hexdigest() for name in sources},
        "times":times,"coercions":coerced,"durations":durations}
    raw=json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2)+"\n"
    if args.output:args.output.write_text(raw,encoding="utf-8")
    else:print(raw,end="")
if __name__=="__main__":main()
