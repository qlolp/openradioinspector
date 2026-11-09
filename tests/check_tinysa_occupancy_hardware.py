"""Explicit receive-only hardware check; excluded from automatic test discovery."""
import argparse
import sys
import tempfile
import threading
import time
import json
import math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radioinspector.web.server import create_server
from radioinspector.windows.client import PlatformClient
from radioinspector.windows.sweep_occupancy import validate_occupancy, save_occupancy_file
from radioinspector.sweep_occupancy import summarize_occupancy
from radioinspector.occupancy_export import export_csv


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main(argv=None):
    parser=argparse.ArgumentParser(description="tinySA Ultra: receive-only sweep → temporary SQLite → HTTP → occupancy → JSON")
    parser.add_argument("--port", required=True, help="physical port; close other owners before running")
    args=parser.parse_args(argv)
    root=Path(tempfile.mkdtemp(prefix='ori-live-occupancy-'))
    print('LOCAL_ARTIFACTS',root,flush=True)
    server=create_server(host='127.0.0.1',port=0,session_database=str(root/'archive.sqlite'))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=PlatformClient(f'http://127.0.0.1:{server.server_address[1]}')
    try:
        session=client.create_session({'name':'tinySA receive-only acceptance','streams':[{'device':'tinySA Ultra','role':'spectrum_analyzer','level_unit':'dBm','port':args.port,'capabilities':['sweep'],'settings':{'adapter':'tinysa-serial','start_hz':88000000,'stop_hz':108000000,'points':101,'rbw_hz':30000,'period_s':2,'max_consecutive_errors':1}}]})
        sid=session['id']; stream=session['streams'][0]['id']
        client.start_session(sid)
        print('START',client.start_stream(sid,stream),flush=True)
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            rows=client.list_sweeps(sid,stream)
            if len(rows)>=5: break
            time.sleep(.5)
        print('STOP',client.stop_stream(sid,stream),flush=True)
        rows=client.list_sweeps(sid,stream)
        require(len(rows)>=5, f'Only {len(rows)} sweeps acquired')
        for row in rows:
            require(len(row['levels'])==101 and len(row['frequencies_hz'])==101 and row['frequencies_hz'][0]==88000000 and row['frequencies_hz'][-1]==108000000, 'Wrong grid')
            require(row['level_unit']=='dBm' and all(math.isfinite(v) for v in row['levels']), 'Invalid raw levels')
            require(row['settings']['simulated'] is False and row['settings']['generator_output']=='off' and row['settings']['generator_output_source']=='command_acknowledged', 'Unconfirmed receive setup')
        rows.sort(key=lambda r:(r['interval']['started_at'],r['id']))
        anchor=rows[2]
        analysis=client.sweep_occupancy(sid,anchor['id'],2,2,-80)
        validate_occupancy(analysis,(sid,stream),anchor,2,2,-80)
        require(summarize_occupancy(analysis,-80)==analysis['occupancy'], 'Inconsistent occupancy')
        save_occupancy_file(root/'occupancy.json',analysis,analysis.data_json)
        require(analysis.data_json in (root/'occupancy.json').read_text(encoding='utf-8'), 'Wire JSON was changed during export')
        (root/'occupancy.csv').write_text(export_csv((root/'occupancy.json').read_bytes()), encoding='utf-8', newline='')
        time.sleep(2)
        require(len(client.list_sweeps(sid,stream))==len(rows), 'Acquisition continued after stop')
    except Exception as exc:
        print('FAIL', str(exc), file=sys.stderr, flush=True)
        return 1
    finally:
        for name in ('automatic_completion','automatic_detection','automatic_handoff','control_scheduler','worker_manager'):
            getattr(server,name).close()
        server.shutdown();thread.join(timeout=3);server.server_close();server.session_service.close()
        print('CLOSED',flush=True)

    print('PASS receive-only archive/occupancy/JSON/CSV; not RF calibration',json.dumps({'sweeps':len(rows),'points':101,'sample_count':analysis['occupancy']['sample_count'],'complete':analysis['complete'],'gap_count':analysis['occupancy']['acquisition_gap_count'],'raw_json_preserved':True}),flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
