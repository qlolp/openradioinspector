from __future__ import annotations

from array import array
import base64
from datetime import datetime,timedelta,timezone
import hashlib
import heapq
import io
import json
import re
import struct
import sys
import wave

BASE=datetime(2026,11,11,0,0,0,123456,tzinfo=timezone.utc)
EDGE_LEVELS=(-100.125,-0.0,-1.0000000000000002,-32.75)


def identifier(config,kind,index):return f'{config.run_id}-{kind}-{index:09d}'


def stamp(us):return (BASE+timedelta(microseconds=us)).isoformat(timespec='microseconds').replace('+00:00','Z')


def interval(start_us,end_us):return {'started_at':stamp(start_us),'ended_at':stamp(end_us)}


def start_us(config,kind,index):
    if config.scenario=='lifecycle':return index*int(config.period_us(kind))
    return index*1_000_000//(config.sweep_rate if kind=='sweep' else config.observation_rate)


def session(config):
    return {'id':config.session_id,'name':'Synthetic modernization benchmark '+config.run_id,
        'metadata':{'synthetic':True,'benchmark_version':1,'run_id':config.run_id},
        'streams':[{'id':config.spectrum_id,'device':'synthetic-benchmark','role':'spectrum_analyzer',
            'level_unit':'dBm','settings':{'adapter':'synthetic-benchmark','generator_output':False}},
            {'id':config.receiver_id,'device':'synthetic-benchmark','role':'receiver',
             'level_unit':'s_meter_count','settings':{'adapter':'synthetic-benchmark'}}]}


def sweep(config,index):
    start=start_us(config,'sweep',index)
    return {'id':identifier(config,'sweep',index),'session_id':config.session_id,'stream_id':config.spectrum_id,
        'interval':interval(start,start+70_000),
        'frequencies_hz':[100_000_001+1000*j for j in range(config.points)],
        'levels':[EDGE_LEVELS[j] if j<4 else -100.0+((index+j)%128)/8 for j in range(config.points)],
        'level_unit':'dBm','settings':{'adapter':'synthetic-benchmark','synthetic':True,
            'generator_output':False,'rbw_hz':10_000,'benchmark_index':index}}


def observation(config,index):
    start=start_us(config,'observation',index)
    return {'id':identifier(config,'observation',index),'session_id':config.session_id,'stream_id':config.receiver_id,
        'interval':interval(start,start+70_000),'frequency_hz':100_000_001+1000*(index%config.points),
        'mode':'FM','level':float(137+index%32),'level_unit':'s_meter_count','squelch_open':True,
        'decoded_mode':None,'audio_uri':None,'metadata':{'synthetic':True,'benchmark_index':index}}


def pcm_chunks(start_frame,frames,*,chunk_frames=32_768):
    for offset in range(0,frames,chunk_frames):
        block=array('h',(((start_frame+offset+j)*7919+17)%65536-32768
                         for j in range(min(chunk_frames,frames-offset))))
        if sys.byteorder!='little':block.byteswap()
        yield block.tobytes()


def audio_segment(config,index):
    start,frames=config.audio_bounds(index)
    stream=io.BytesIO()
    with wave.open(stream,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(48_000)
        for block in pcm_chunks(start,frames):audio.writeframesraw(block)
    raw=stream.getvalue()
    return {'id':identifier(config,'audio',index),'stream_id':config.receiver_id,
        'frequency_hz':100_000_001,'mode':'FM',
        'interval':interval(start*1_000_000//48_000,(start+frames)*1_000_000//48_000),
        'data_base64':base64.b64encode(raw).decode('ascii'),'sha256':hashlib.sha256(raw).hexdigest()}


def encode(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode()


def measurement_hash(kind,value):
    fields=('id','session_id','stream_id','interval','level_unit')
    result={key:value[key] for key in fields}
    def utc_time(text):
        moment=datetime.fromisoformat(text.replace('Z','+00:00'))
        if moment.tzinfo is None or moment.utcoffset() is None \
                or any(any(digit!='0' for digit in fraction[6:]) for fraction in re.findall(r'[.,]([0-9]+)',text)):
            raise ValueError('Time zone or source precision changed')
        return moment.astimezone(timezone.utc).isoformat(timespec='microseconds')
    result['interval']={key:utc_time(value['interval'][key]) for key in ('started_at','ended_at')}
    if kind=='sweep':
        hz=value['frequencies_hz']
        if any(type(item) is not int for item in hz):raise ValueError('Noninteger frequency')
        result.update(frequencies_hz=hz,levels=[struct.pack('>d',item).hex() for item in value['levels']],
                      settings=value['settings'])
    else:
        if type(value['frequency_hz']) is not int:raise ValueError('Noninteger frequency')
        for key in ('frequency_hz','mode','squelch_open','decoded_mode','audio_uri','metadata'):
            result[key]=value[key]
        result['level']=None if value['level'] is None else struct.pack('>d',value['level']).hex()
    return hashlib.sha256(encode(result)).hexdigest()


def scheduled(config):
    counts=config.planned_counts()
    heap=[]
    def due(kind,index):
        if kind!='audio':return start_us(config,kind,index)
        start,frames=config.audio_bounds(index)
        return (start+frames)*1_000_000//48_000
    for kind,count in counts.items():
        if count:heapq.heappush(heap,(due(kind,0),kind,0))
    while heap:
        offset,kind,index=heapq.heappop(heap)
        yield offset,kind,index
        if index+1<counts[kind]:heapq.heappush(heap,(due(kind,index+1),kind,index+1))
