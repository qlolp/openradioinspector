from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import wave

from .client import LINE_LIMIT,JSON_LIMIT,segment
from .config import BenchmarkError
from .source import audio_segment,measurement_hash,observation,pcm_chunks,start_us,sweep


def archive(client,config,ledger):
    path=client.session_path(config)+'/archive/export.ndjson'
    digest=hashlib.sha256();size=0;counts={'sweep':0,'observation':0};header=True
    with client.open(path) as response:
        if response.status!=200:raise BenchmarkError('archive_invalid','Archive download failed')
        while raw:=response.readline(LINE_LIMIT+1):
            if len(raw)>LINE_LIMIT or not raw.endswith(b'\n'):
                raise BenchmarkError('archive_invalid','Archive contains an oversized or incomplete line')
            digest.update(raw);size+=len(raw)
            try:value=json.loads(raw)
            except (ValueError,RecursionError):raise BenchmarkError('archive_invalid','Invalid NDJSON record') from None
            if header:
                if value!={'format':'openradioinspector-archive-ndjson','format_version':1,'session_id':config.session_id}:
                    raise BenchmarkError('archive_invalid','Unexpected archive snapshot header')
                header=False;continue
            try:
                kind,item=value['kind'],value['measurement']
                row=ledger.get(item['id'])
                if kind not in counts or row is None or row['kind']!=kind \
                        or row['status'] not in ('accepted','transport_unknown','invalid_ack'):
                    raise ValueError()
                expected=(sweep if kind=='sweep' else observation)(config,row['idx'])
                if measurement_hash(kind,item)!=measurement_hash(kind,expected) or not ledger.verified(item['id']):
                    raise ValueError()
                counts[kind]+=1
            except (ValueError,KeyError,TypeError,OverflowError,RecursionError):
                raise BenchmarkError('archive_invalid','Missing, duplicate, changed, or foreign source measurement') from None
    if header:raise BenchmarkError('archive_invalid','Archive is missing its header')
    return {'status':'passed','ndjson_sha256':digest.hexdigest(),'ndjson_bytes':size,'source_counts':counts}


def audio(client,config,ledger,output):
    root=client.session_path(config)+'/streams/'+segment(config.receiver_id)+'/instrument/artifacts'
    offset=0;listed=0
    while True:
        result=client.json(root+f'?limit=100&offset={offset}')
        items=result.get('items')
        if not isinstance(items,list) or len(items)>100:
            raise BenchmarkError('audio_invalid','Artifact listing is not bounded')
        for item in items:
            row=ledger.get(item.get('id'))
            if row is None or row['kind']!='audio' or row['status'] not in ('accepted','transport_unknown','invalid_ack') \
                    or not ledger.listed(row['id']):
                raise BenchmarkError('audio_invalid','Unexpected or duplicate audio artifact')
            expected=audio_segment(config,row['idx'])
            _,frames=config.audio_bounds(row['idx'])
            metadata=item.get('metadata',{})
            if item.get('sha256')!=expected['sha256'] or item.get('size_bytes')!=44+frames*2 \
                    or any(metadata.get(key)!=value for key,value in {
                        'sample_rate':48000,'frame_count':frames,'frequency_hz':expected['frequency_hz'],
                        'mode':expected['mode'],'interval':expected['interval']}.items()):
                raise BenchmarkError('audio_invalid','Original audio provenance or metadata changed')
            listed+=1
        offset+=len(items)
        if len(items)<100:break
    expected_pcm,actual_pcm=hashlib.sha256(),hashlib.sha256()
    temporary=output/'audio-verify.tmp'
    for row in ledger.rows('audio'):
        start,frames=config.audio_bounds(row['idx'])
        for block in pcm_chunks(start,frames):expected_pcm.update(block)
        if not row['listed']:continue
        expected=audio_segment(config,row['idx'])
        digest=hashlib.sha256();size=0
        try:
            with client.open(root+'/'+segment(row['id'])+'/content') as response,temporary.open('xb') as target:
                while block:=response.read(65_536):
                    size+=len(block)
                    if size>JSON_LIMIT:raise BenchmarkError('audio_invalid','Artifact exceeds bounded audio size')
                    digest.update(block);target.write(block)
            if digest.hexdigest()!=expected['sha256']:
                raise BenchmarkError('audio_invalid','Original WAV bytes or checksum changed')
            with wave.open(str(temporary),'rb') as reader:
                if (reader.getnchannels(),reader.getsampwidth(),reader.getframerate(),reader.getnframes())!=(1,2,48000,frames):
                    raise BenchmarkError('audio_invalid','WAV sample format or frame count changed')
                while block:=reader.readframes(32_768):actual_pcm.update(block)
            ledger.verified(row['id'])
        finally:
            temporary.unlink(missing_ok=True)
    counts=ledger.audio_counts()
    return counts|{'listed_segments':listed,'expected_pcm_sha256':expected_pcm.hexdigest(),
        'retained_pcm_sha256':actual_pcm.hexdigest(),'all_offered_pcm_preserved':
            counts['offered_frames']==counts['verified_frames'] and expected_pcm.digest()==actual_pcm.digest()}


def history(client,config,capabilities,histograms,ledger):
    summaries=capabilities.get('features',{}).get('sweep_summaries') is True
    metric='history_summary_ms' if summaries else 'history_full_payload_ms'
    path=client.session_path(config)+('/sweep-summaries' if summaries else '/sweeps')+'?limit=100'
    import time
    first=None;first_ids=[]
    for attempt in range(config.history_repeats+1):
        started=time.monotonic_ns();result=client.json(path)
        elapsed=(time.monotonic_ns()-started)/1_000_000
        rows=result.get('items') if summaries else result
        if not isinstance(rows,list) or len(rows)>100:
            raise BenchmarkError('history_invalid','First history page is not bounded to 100 rows')
        expected=min(100,config.planned_counts()['sweep'])
        if len(rows)!=expected:
            # Rejected offered inputs can reduce source counts in fault runs.
            if config.scenario not in ('fault','overload'):
                raise BenchmarkError('history_invalid','First history page source count is wrong')
        if summaries and any('levels' in row or 'frequencies_hz' in row for row in rows):
            raise BenchmarkError('history_invalid','Summary endpoint returned raw arrays')
        ordering=[]
        try:
            for item in rows:
                source=ledger.get(item['id'])
                if source is None or source['kind']!='sweep':raise ValueError()
                value=sweep(config,source['idx'])
                ordering.append((start_us(config,'sweep',source['idx']),item['id'].encode('utf-8')))
                if summaries:
                    peak=max(value['levels']);peak_index=value['levels'].index(peak)
                    expected={key:value[key] for key in ('id','session_id','stream_id','interval','level_unit')}
                    expected.update(point_count=config.points,min_frequency_hz=value['frequencies_hz'][0],
                        max_frequency_hz=value['frequencies_hz'][-1],peak_frequency_hz=value['frequencies_hz'][peak_index])
                    if any(item.get(key)!=field for key,field in expected.items()) \
                            or any(type(item.get(key)) is not int for key in ('point_count','min_frequency_hz','max_frequency_hz','peak_frequency_hz')) \
                            or struct.pack('>d',item['peak_level'])!=struct.pack('>d',peak):raise ValueError()
                elif measurement_hash('sweep',item)!=measurement_hash('sweep',value):raise ValueError()
            if ordering!=sorted(set(ordering),reverse=True):raise ValueError()
        except (ValueError,KeyError,TypeError,OverflowError):
            raise BenchmarkError('history_invalid','History source metadata, peak, or newest-first ordering changed') from None
        if attempt:histograms[metric].add(elapsed)
        else:first=elapsed;first_ids=[item['id'] for item in rows]
    return {'endpoint_kind':'summaries' if summaries else 'full_measurements',
            'first_request_ms':first,'repeat_count':config.history_repeats,'warm_cache_requested':True,
            'first_page_source_ids':first_ids}
