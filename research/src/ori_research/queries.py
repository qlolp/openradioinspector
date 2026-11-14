"""Fixed typed aggregation SQL over local Arrow scanners, with keyset pages."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import re
import threading
import uuid

import duckdb
import pyarrow.dataset as ds

from .cache import remove_owned, tree_bytes
from .protocol import WorkerError, make_cursor, read_cursor
from .resources import resident_bytes

SAMPLES = '''WITH samples AS (
    SELECT 'sweep' AS kind,sweep_id AS source_id,session_id,stream_id,level_unit,
        started_at_us,ended_at_us,frequency_hz,level FROM sweep_points
    UNION ALL
    SELECT 'observation' AS kind,id AS source_id,session_id,stream_id,level_unit,
        started_at_us,ended_at_us,frequency_hz,level FROM observations
)'''
SUMMARY = ''' SELECT kind,session_id,stream_id,level_unit,COUNT(DISTINCT source_id) AS measurement_count,
    COUNT(*) AS sample_count,COUNT(level) AS valid_level_count,
    MIN(started_at_us) AS first_started_at_us,MAX(ended_at_us) AS last_ended_at_us,
    MIN(frequency_hz) AS min_frequency_hz,MAX(frequency_hz) AS max_frequency_hz,
    MIN(level) AS min_level,MAX(level) AS max_level
    FROM samples GROUP BY kind,session_id,stream_id,level_unit'''
OCCUPANCY = ''' SELECT kind,session_id,stream_id,level_unit,frequency_hz,
    COUNT(level) AS sample_count,COUNT(*) FILTER (WHERE level>=?) AS occupied_count,
    CAST(COUNT(*) FILTER (WHERE level>=?) AS DOUBLE)/COUNT(level) AS occupancy_ratio,
    MIN(started_at_us) AS first_started_at_us,MAX(ended_at_us) AS last_ended_at_us
    FROM samples WHERE level_unit=? AND level IS NOT NULL
    GROUP BY kind,session_id,stream_id,level_unit,frequency_hz'''


@contextmanager
def memory_watch(connection, budget):
    stopped = threading.Event()
    failures = []
    def watch():
        while not stopped.wait(0.02):
            try:
                size = resident_bytes()
                budget.metrics['peak_rss_bytes'] = max(budget.metrics['peak_rss_bytes'],size)
                if size > budget.limits['worker_rss_bytes']:
                    raise WorkerError('rss_limit_exceeded', 'Worker memory budget exceeded', retryable=True)
            except WorkerError as error:
                failures.append(error)
                connection.interrupt()
                return
    thread = threading.Thread(target=watch,name='analytics-memory-watch',daemon=True)
    thread.start()
    try:
        yield
    except duckdb.Error:
        if failures:
            raise failures[0] from None
        raise
    finally:
        stopped.set()
        thread.join()
    if failures:
        raise failures[0]


def query(request, directory, cache_key, root, budget):
    last = read_cursor(request,cache_key)
    temporary_root = root/'.temporary'
    temporary_root.mkdir(mode=0o700,exist_ok=True)
    available = budget.limits['temp_bytes']-tree_bytes(temporary_root,skip_temporary=False)
    if available <= 0:
        raise WorkerError('temp_quota_exceeded', 'Existing temporary files exceed the byte quota', retryable=True)
    temporary = temporary_root/('.temp-'+uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    connection = None
    try:
        limits = budget.limits
        connection = duckdb.connect(config={'threads':limits['duckdb_threads'],
            'memory_limit':str(limits['duckdb_memory_bytes'])+'B',
            'temp_directory':str(temporary),
            'max_temp_directory_size':str(available)+'B',
            'autoload_known_extensions':False,'autoinstall_known_extensions':False,
            'preserve_insertion_order':False})
        # DuckDB processes connect options in its own order. Disable external
        # access only after configuring its owned spill path; no SQL is run
        # and extension autoload/install are disabled before this point.
        connection.execute('SET enable_external_access=false')
        configured = connection.execute("SELECT current_setting('threads'),current_setting('memory_limit'),"
            "current_setting('max_temp_directory_size'),current_setting('enable_external_access')").fetchall()[0]
        def as_bytes(value):
            match = re.fullmatch(r'([0-9.]+)\s*(bytes|B|KiB|MiB|GiB|TiB)',value)
            scale = {'bytes':1,'B':1,'KiB':1024,'MiB':1024**2,'GiB':1024**3,'TiB':1024**4}
            return int(Decimal(match[1])*scale[match[2]])
        budget.metrics['duckdb'] = dict(threads=configured[0],memory_limit_bytes=as_bytes(configured[1]),
                                      max_temp_bytes=as_bytes(configured[2]),external_access=configured[3])
        for name in ('sweep_points','observations'):
            scanner = ds.dataset(directory/(name+'.parquet'),format='parquet').scanner(
                batch_size=limits['batch_rows'],batch_readahead=1,fragment_readahead=1,use_threads=False)
            connection.register(name,scanner.to_reader())
        keys = ['kind','session_id','stream_id','level_unit']
        parameters = []
        if request['operation'] == 'occupancy':
            keys.append('frequency_hz')
            sql = OCCUPANCY
            threshold = request['threshold']
            parameters.extend((threshold['value'],threshold['value'],threshold['unit']))
        else:
            sql = SUMMARY
        key_sql = ','.join(keys)
        sql = SAMPLES+', grouped AS ('+sql+') SELECT * FROM grouped'
        if last is not None:
            sql += ' WHERE ('+key_sql+')>('+','.join('?' for _ in keys)+')'
            parameters.extend(last)
        sql += ' ORDER BY '+key_sql+' LIMIT ?'
        parameters.append(request['page_size']+1)
        with memory_watch(connection,budget):
            cursor = connection.execute(sql,parameters)
            names = [field[0] for field in cursor.description]
            rows = [dict(zip(names,row)) for row in cursor.fetchmany(request['page_size']+1)]
        budget.checkpoint()
        temp_size = tree_bytes(temporary_root,skip_temporary=False)
        budget.metrics['temp_bytes'] = temp_size
        if temp_size > limits['temp_bytes']:
            raise WorkerError('temp_quota_exceeded', 'DuckDB temporary byte quota exceeded', retryable=True)
        next_cursor = None
        if len(rows) > request['page_size']:
            rows.pop()
            next_cursor = make_cursor(request,cache_key,[rows[-1][key] for key in keys])
        return {'rows':rows,'next_cursor':next_cursor,'cache_key':cache_key,
                'event_watermark':request['_source_watermark'],
                **({'occupancy_basis':'samples','threshold':request['threshold']}
                   if request['operation'] == 'occupancy' else {})}
    except duckdb.OutOfMemoryException as error:
        if 'max_temp_directory_size' in str(error):
            raise WorkerError('temp_quota_exceeded', 'DuckDB temporary byte quota exceeded', retryable=True) from None
        raise WorkerError('duckdb_memory_limit', 'DuckDB memory budget exceeded', retryable=True) from None
    except duckdb.Error:
        raise WorkerError('query_failed', 'Bounded analytics query failed', retryable=True) from None
    finally:
        if connection is not None:
            connection.close()
        remove_owned(temporary,temporary_root)
