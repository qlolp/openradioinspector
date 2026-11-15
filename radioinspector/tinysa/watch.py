"""Bounded core-owned background detection. Unknown means new relative to baseline."""
import copy
import math
from uuid import uuid4
from ..contracts import format_time
from ..monitoring.detector import Detector, build_baseline, fingerprint


def number(value, low, high, label, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Некорректное значение: '+label)
    return value


def validate_plan(value, *, ultra=False):
    defaults=dict(margin_db=6, impulse_db=12, confirmations=3, window_sweeps=5,
                  baseline_sweeps=20, baseline_seconds=60, allowed=[])
    if not isinstance(value,dict) or set(value)-set(defaults)-{'bands'}:
        raise ValueError('Неизвестное поле плана наблюдения')
    result={**defaults,**copy.deepcopy(value)}
    bands=result.get('bands')
    if not isinstance(bands,list) or not 1<=len(bands)<=8:raise ValueError('Нужны 1–8 полос')
    for band in bands:
        if not isinstance(band,dict) or set(band)!={'name','start_hz','stop_hz','rbw_hz','points'}:
            raise ValueError('Полоса: name/start_hz/stop_hz/rbw_hz/points')
        if not isinstance(band['name'],str) or not 1<=len(band['name'].strip())<=80:raise ValueError('Имя полосы: 1–80 символов')
        for key in ('start_hz','stop_hz'):number(band[key],100000,6000000000 if ultra else 800000000,key,True)
        if band['start_hz']>=band['stop_hz']:raise ValueError('Начало полосы должно быть ниже конца')
        number(band['points'],2,450,'points',True)
        if type(band['rbw_hz']) is not int or band['rbw_hz'] not in (200,1000,3000,10000,30000,100000,300000,600000,850000):
            raise ValueError('Для воспроизводимого фона нужен фиксированный RBW')
    for key,low,high,integer in [('margin_db',1,60,False),('impulse_db',1,100,False),
        ('confirmations',1,20,True),('window_sweeps',1,20,True),('baseline_sweeps',20,256,True),('baseline_seconds',60,3600,False)]:
        number(result[key],low,high,key,integer)
    if result['confirmations']>result['window_sweeps'] or result['impulse_db']<result['margin_db']:
        raise ValueError('Подтверждения превышают окно или импульсный порог ниже обычного')
    if not isinstance(result['allowed'],list) or len(result['allowed'])>128:raise ValueError('До 128 известных полос')
    for rule in result['allowed']:
        if not isinstance(rule,dict) or set(rule)!={'start_hz','end_hz'}:raise ValueError('Известная полоса: start_hz/end_hz')
        number(rule['start_hz'],100000,6000000000,'known start',True)
        number(rule['end_hz'],rule['start_hz'],6000000000,'known end',True)
    return result


class SpectrumWatch:
    def __init__(self, plan):
        self.plan=copy.deepcopy(plan);self.id=uuid4().hex
        self.bands=[dict(samples=[],detector=None,active={},count=0,last_at=None,last_id=None) for _ in plan['bands']]
        self.events_opened=0;self.completed=0;self.recent=[]

    def status(self):
        return dict(run_id=self.id,events_opened=self.events_opened,completed_sweeps=self.completed,
            interpretation='new_relative_to_baseline_not_identification',
            bands=[dict(name=p['name'],state='watching' if b['detector'] else 'learning',
                sweeps=b['count'],last_at=b['last_at'],active_events=len(b['active']),
                baseline_samples=len(b['samples']),minimum_sweeps=self.plan['baseline_sweeps'],
                minimum_seconds=self.plan['baseline_seconds']) for p,b in zip(self.plan['bands'],self.bands)],
            recent_events=copy.deepcopy(self.recent[-32:]))

    def observe(self, sweep, index):
        b=self.bands[index]
        if b['last_id']==sweep.id:return []
        fp=fingerprint(sweep)
        if b['samples'] and fp!=fingerprint(b['samples'][0]):
            raise ValueError('Настройки изменились: остановите наблюдение и соберите новый фон')
        b['last_id']=sweep.id;b['last_at']=format_time(sweep.interval.ended_at);b['count']+=1;self.completed+=1
        if b['detector'] is None:
            b['samples'].append(sweep)
            # Keep the start and latest bounded sample set, including at fast cadence.
            if len(b['samples'])>256:del b['samples'][1]
            elapsed=(sweep.interval.ended_at-b['samples'][0].interval.started_at).total_seconds()
            if len(b['samples'])>=self.plan['baseline_sweeps'] and elapsed>=self.plan['baseline_seconds']:
                baseline=build_baseline(b['samples'])
                b['detector']=Detector(baseline,{**self.plan,'hysteresis_db':3})
                b['detector'].state['last_started_at']=format_time(sweep.interval.started_at)
            return []
        rules=[dict(rule,id=str(i),watch_changes=False) for i,rule in enumerate(self.plan['allowed'])]
        regions,excess,thresholds=b['detector'].step(sweep,rules)
        events=[]
        def emit(transition,event):
            events.append(dict(event,transition=transition,run_id=self.id,band_index=index,
                band_name=self.plan['bands'][index]['name'],occurred_at=b['last_at'],
                source=dict(session_id=sweep.session_id,stream_id=sweep.stream_id,sweep_id=sweep.id),
                detector_version=b['detector'].baseline['detector_version']))
        if b['detector'].state.get('quality_issue'):
            emit('data_gap',dict(event_id=uuid4().hex,**b['detector'].state['quality_issue']))
            for event in b['active'].values():emit('interrupted',event)
            b['active'].clear()
        matched=set()
        for region in regions:
            key=next((key for key,event in b['active'].items() if key not in matched and
                region['first_bin']<=event['last_bin'] and region['last_bin']>=event['first_bin']),None)
            if key is None:
                key=uuid4().hex
                event=dict(region,event_id=key,first_seen=b['last_at'],last_seen=b['last_at'],quiet_sweeps=0)
                b['active'][key]=event;self.events_opened+=1;emit('opened',event)
            else:b['active'][key].update(region,last_seen=b['last_at'],quiet_sweeps=0)
            matched.add(key)
        for key,event in list(b['active'].items()):
            if key in matched:continue
            held=any(excess[i]>=thresholds[i]-3 for i in range(event['first_bin'],event['last_bin']+1))
            event['quiet_sweeps']=0 if held else event['quiet_sweeps']+1
            if event['quiet_sweeps']>=3:emit('closed',event);del b['active'][key]
        self.recent=(self.recent+copy.deepcopy(events))[-32:]
        return events
