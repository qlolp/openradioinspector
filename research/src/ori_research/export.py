"""Bounded Arrow batches, no pandas, no audio BLOB reads, no canonical writes."""
from __future__ import annotations

from contextlib import ExitStack
import json
import math

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .protocol import WorkerError
from .resources import QuotaFile
from .derived import DerivedRecords
from .source import (frequency, frequency_selected, interval, json_capabilities, json_object, observation_level,
                     observation_rows, scope_sql, text, time_selected)

TEXT = pa.string()
I64 = pa.int64()
SCOPE = [('session_id',TEXT),('stream_id',TEXT),('started_at_us',I64),('ended_at_us',I64)]
SCHEMAS = {
    'streams':pa.schema([('id',TEXT),('session_id',TEXT),('device',TEXT),('role',TEXT),
        ('level_unit',TEXT),('label',TEXT),('settings_json',TEXT),('capabilities_json',TEXT)]),
    'sweeps':pa.schema([('id',TEXT),*SCOPE,('level_unit',TEXT),('settings_json',TEXT),
        ('source_point_count',I64),('exported_point_count',I64)]),
    'sweep_points':pa.schema([('sweep_id',TEXT),*SCOPE,('point_index',I64),
        ('frequency_hz',I64),('level',pa.float64()),('level_unit',TEXT)]),
    'observations':pa.schema([('id',TEXT),*SCOPE,('frequency_hz',I64),('mode',TEXT),
        ('level',pa.float64()),('level_unit',TEXT),('squelch_open',pa.bool_()),
        ('decoded_mode',TEXT),('audio_uri',TEXT),('metadata_json',TEXT)]),
}


class BatchWriter:
    def __init__(self, name, path, budget, stack):
        self.schema,self.budget = SCHEMAS[name],budget
        self.target = stack.enter_context(QuotaFile(path,budget))
        self.writer = stack.enter_context(pq.ParquetWriter(self.target,self.schema,
            compression='zstd',use_dictionary=False,write_statistics=True))
        self.rows,self.bytes,self.count = [],0,0

    def append(self, row):
        size = 64 + sum(len(v.encode('utf-8'))+8 if isinstance(v,str) else 16 for v in row.values())
        if size > self.budget.limits['batch_bytes']:
            raise WorkerError('source_record_too_large', 'A source record exceeds the bounded batch budget')
        if self.rows and (len(self.rows) >= self.budget.limits['batch_rows']
                          or self.bytes+size > self.budget.limits['batch_bytes']):
            self.flush()
        self.rows.append(row)
        self.bytes += size
        self.count += 1

    def flush(self):
        if self.rows:
            self.budget.checkpoint()
            table = pa.Table.from_pylist(self.rows,schema=self.schema)
            self.budget.metrics['max_batch_rows'] = max(self.budget.metrics['max_batch_rows'],len(self.rows))
            self.budget.metrics['max_batch_bytes'] = max(self.budget.metrics['max_batch_bytes'],table.nbytes)
            self.writer.write_table(table,row_group_size=len(self.rows))
            self.rows.clear()
            self.bytes = 0


def _scope(row):
    start,end = interval(row)
    return dict(session_id=text(row['session_id']),stream_id=text(row['stream_id']),
                started_at_us=start,ended_at_us=end)


def _stream(writers,row,maximum):
    result = {key:text(row[key]) for key in ('id','session_id','device','role','level_unit','label')}
    result['settings_json'] = json_object(row['settings_json'],maximum)
    result['capabilities_json'] = json_capabilities(row['capabilities_json'],maximum)
    writers['streams'].append(result)


def _sweep(writers,row,filters,budget):
    budget.checkpoint()
    common = _scope(row)
    if not time_selected(common['started_at_us'],filters):
        return
    identifier,unit = text(row['id']),text(row['level_unit'],128)
    try:
        hz,levels = json.loads(row['frequencies_json']),json.loads(row['levels_json'])
        if not isinstance(hz,list) or not isinstance(levels,list) or not hz or len(hz)!=len(levels):
            raise ValueError()
        if len(hz)>budget.limits['max_points_per_sweep']:
            raise WorkerError('source_record_too_large','Sweep exceeds the bounded point budget')
        if any(type(v) not in (float,int) for v in levels):
            raise ValueError()
        values = np.asarray(levels,dtype=np.float64)
        if not np.isfinite(values).all():
            raise ValueError()
    except (ValueError,TypeError,OverflowError,RecursionError):
        raise WorkerError('source_record_invalid','Invalid sweep sample arrays') from None
    selected = 0
    for index,hz_value in enumerate(hz):
        frequency(hz_value)
        if frequency_selected(hz_value,filters):
            writers['sweep_points'].append(dict(sweep_id=identifier,**common,point_index=index,
                frequency_hz=hz_value,level=float(values[index]),level_unit=unit))
            selected += 1
    if selected:
        writers['sweeps'].append(dict(id=identifier,**common,level_unit=unit,
            settings_json=json_object(row['settings_json'],budget.limits['max_record_bytes']),
            source_point_count=len(hz),exported_point_count=selected))


def _observation(writers,row,filters,budget):
    budget.checkpoint()
    common = _scope(row)
    hz_value = frequency(row['frequency_hz'])
    if not time_selected(common['started_at_us'],filters) or not frequency_selected(hz_value,filters):
        return
    level = observation_level(row['level'],row['level_bits_le'])
    if (level is not None and (type(level) not in (float,int) or not math.isfinite(level))) \
            or row['squelch_open'] not in (None,0,1):
        raise WorkerError('source_record_invalid','Invalid receiver observation')
    writers['observations'].append(dict(id=text(row['id']),**common,frequency_hz=hz_value,
        mode=text(row['mode']),level=level,level_unit=text(row['level_unit'],128),
        squelch_open=None if row['squelch_open'] is None else bool(row['squelch_open']),
        decoded_mode=text(row['decoded_mode'],nullable=True),
        audio_uri=text(row['audio_uri'],budget.limits['max_record_bytes'],nullable=True),
        metadata_json=json_object(row['metadata_json'],budget.limits['max_record_bytes'])))


def _derived_row(kind,value,maximum):
    def document(key,expected):
        item = value[key]
        if not isinstance(item,expected):
            raise ValueError()
        raw = json.dumps(item,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        text(raw,maximum)
        return raw
    try:
        row = dict(value)
        if kind=='stream':
            row['settings_json'] = document('settings',dict)
            row['capabilities_json'] = document('capabilities',list)
            row.setdefault('label','')
        else:
            row.update(started_at=value['interval']['started_at'],ended_at=value['interval']['ended_at'])
            if kind=='sweep':
                row['frequencies_json'] = document('frequencies_hz',list)
                row['levels_json'] = document('levels',list)
                row['settings_json'] = document('settings',dict)
            else:
                row['metadata_json'] = document('metadata',dict)
                if type(row['squelch_open']) not in (bool,type(None)):
                    raise ValueError()
                row['level_bits_le'] = None # Raw canonical JSON already carries the original float bits.
        return row
    except (ValueError,KeyError,TypeError,UnicodeError,OverflowError,RecursionError):
        raise WorkerError('source_record_invalid','Invalid canonical derived source fields') from None


def build_tables(db, directory, filters, budget):
    maximum = budget.limits['max_record_bytes']
    scope,parameters = scope_sql(filters)
    with ExitStack() as stack:
        writers = {name:BatchWriter(name,directory/(name+'.parquet'),budget,stack) for name in SCHEMAS}
        if isinstance(db,DerivedRecords):
            for kind,value in db.records():
                row = _derived_row(kind,value,maximum)
                try:
                    if kind=='stream':
                        _stream(writers,row,maximum)
                    elif kind=='sweep':
                        _sweep(writers,row,filters,budget)
                    else:
                        _observation(writers,row,filters,budget)
                except (KeyError,TypeError):
                    raise WorkerError('source_record_invalid','Missing canonical derived source field') from None
        else:
            stream_scope = scope.replace('stream_id=?','id=?')
            for row in db.execute('SELECT id,session_id,device,role,level_unit,label,settings_json,'
                                  'capabilities_json FROM streams'+stream_scope+' ORDER BY id',parameters):
                _stream(writers,row,maximum)
            for row in db.execute('SELECT id,session_id,stream_id,started_at,ended_at,frequencies_json,'
                    'levels_json,level_unit,settings_json FROM sweeps'+scope+' ORDER BY id',parameters):
                _sweep(writers,row,filters,budget)
            for row in observation_rows(db,scope,parameters):
                _observation(writers,row,filters,budget)
        for writer in writers.values():
            writer.flush()
        return {name:writer.count for name,writer in writers.items()}
