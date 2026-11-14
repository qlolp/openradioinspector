"""Owned deterministic .pan/legacy HTTP oracle, using real existing algorithms."""
from __future__ import annotations
import os
for _name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','VECLIB_MAXIMUM_THREADS','ARROW_NUM_THREADS'):os.environ[_name]='1'
from pathlib import Path
import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from tests.test_legacy_rpc import create_pan,direct_get,direct_threshold,source_rows
from radioinspector.web.server import ApplicationState
from radioinspector.jsonio import to_jsonable


def freeze(value):
    if isinstance(value,dict):return {key:('__SOURCE_PAN__' if key=='database' and item is not None else freeze(item)) for key,item in value.items()}
    if isinstance(value,list):return [freeze(item) for item in value]
    return value


def create(directory,sidecar=False):
    directory.mkdir(parents=True,exist_ok=True)
    source=directory/'source-é-é-😀.pan';create_pan(source)
    if sidecar:
        (directory/'signals.json').write_text(json.dumps({'format':'openradioinspector-signal-list','version':1,'cycles':7,'signals':[
            {'frequency_hz':100000001.125,'bandwidth_hz':.5,'first_seen':'2026-11-11T12:00:00.123456+05:30','fixed':True,'from_assignment':True,
             'assignment_id':'ид-é-é-😀/"\\','level_db':-0.,'power_over_threshold_w':1.0000000000000002,'cycles':7,'events':[]} ]},ensure_ascii=False,indent=2))
    raw_before=source_rows(source)
    state=ApplicationState();result={'fixture_version':1,'synthetic':True,'no_database':freeze(direct_get(state,'/api/state')[1]),'queries':[],'thresholds':[]}
    try:
        state.load_database(str(source));result['loaded']=freeze(state.snapshot())
        for action,parameters,path in (
            ('coverage',{'rows':3,'cols':4},'/api/coverage?rows=3&cols=4'),
            ('occupancy',{},'/api/occupancy'),('occupancy',{'threshold':-30.},'/api/occupancy?threshold=-30'),
            ('efficiency',{},'/api/efficiency'),('measurements',{'start':100000000.625,'stop':100000002.125,'norm':1.},'/api/measure?start=100000000.625&stop=100000002.125&norm=1'),
            ('keywords',{},'/api/keywords')):
            status,body=direct_get(state,path);result['queries'].append({'action':action,'parameters':parameters,'status':status,'body':freeze(body)})
        for payload in ({'offset_db':3.5},{'offset_db':4.,'frequencies':[100000000.125,100000002.625],'levels':[-0.,-45.]},{},
            {'frequencies':[1,1],'levels':[-80,-70]},{'offset_db':'bad'}):
            status,body=direct_threshold(state,payload)
            result['thresholds'].append({'payload':payload,'status':status,'body':freeze(body),'state':freeze(state.snapshot())})
        assert source_rows(source)==raw_before
        with closing(sqlite3.connect(source)) as db:
            result['raw_rows']=[{'id':row[0],'recorded_at':row[1],'levels_hex':row[2],'start_hz':row[3],'stop_hz':row[4],'step_hz':row[5],'unit':row[6]} for row in db.execute('SELECT id,recorded_at,hex(levels),start_hz,stop_hz,step_hz,unit FROM panorama ORDER BY id')]
            result['schema_sql']=list(db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"))
    finally:state.close()
    # Runtime tests must begin with the original unmodified source threshold.
    with closing(sqlite3.connect(source)) as db,db:db.execute('DELETE FROM threshold_line WHERE line_index=0')
    return source,result


if __name__=='__main__':
    if len(sys.argv)>=2:
        source,_=create(Path(sys.argv[1]),sidecar='--sidecar' in sys.argv[2:]);print(source)
    else:
        with tempfile.TemporaryDirectory() as temporary:
            _,fixture=create(Path(temporary)/'basic')
            _,sidecar=create(Path(temporary)/'sidecar',sidecar=True)
            fixture['sidecar']={'loaded':sidecar['loaded'],'changed':sidecar['thresholds'][0]}
        (ROOT/'contracts/fixtures/legacy-workspace-v1.json').write_text(json.dumps(fixture,ensure_ascii=False,allow_nan=False,indent=2)+'\n')
