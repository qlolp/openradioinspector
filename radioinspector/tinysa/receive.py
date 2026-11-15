"""Receive-only operations serialized by Controller; no client owns serial I/O."""
import json
from .commands import setting_command
from .watch import SpectrumWatch, validate_plan

RECEIVE_KEYS={'start_hz','stop_hz','points','rbw_hz','attenuation_db','lna','ultra','spur',
              'repeat','sweep_accuracy','sweep_seconds','external_gain_db','trigger_mode','trigger_dbm','vbw_ratio'}
VBW_REPEATS={'auto':1,'1':1,'1/3':3,'1/10':10,'1/30':30,'1/100':100}


class ReceiveOperations:
    def receive_configure(self, values):
        if self.mode!='monitoring' or self._device_mode!='input':raise ValueError('Нужен режим приёма')
        if not isinstance(values,dict) or not values or set(values)-RECEIVE_KEYS:raise ValueError('Неизвестная приёмная настройка')
        updated={**self.settings,**values}
        for name,value in values.items():
            command=setting_command(name,value)
            if command.wire.split()[0] not in self.commands:raise ValueError('Команда отсутствует в help: '+name)
        low,high=updated['start_hz'],updated['stop_hz']
        limit=6000000000 if updated.get('ultra') else 800000000
        if not 100000<=low<=high<=limit:raise ValueError('Диапазон вне выбранного профиля')
        if updated.get('lna') and updated.get('attenuation_db') not in (0,'auto'):
            raise ValueError('LNA несовместим с ручным аттенюатором; выберите 0 дБ')
        if 'repeat' in values and 'vbw_ratio' in values:raise ValueError('Задайте repeat или отношение VBW, не оба')
        if 'vbw_ratio' in values:updated['repeat']=VBW_REPEATS[values['vbw_ratio']]
        if 'repeat' in values:updated.pop('vbw_ratio',None)
        # Validate everything above before the first byte. Each acknowledged step
        # is published separately: a later failure never pretends atomic rollback.
        order=['lna','ultra','attenuation_db','rbw_hz','repeat','vbw_ratio','sweep_accuracy',
               'sweep_seconds','external_gain_db','spur','trigger_dbm','trigger_mode']
        if values.get('lna') is True:order.remove('lna');order.append('lna')
        # Lower the range before disabling Ultra; enable Ultra before raising it.
        if values.get('ultra') is False:order.remove('ultra');order.append('ultra')
        def edges():
            if {'start_hz','stop_hz','points'} & values.keys():
                self._ack(f"sweep {low} {high} {updated['points']}")
                self.settings.update(start_hz=low,stop_hz=high,points=updated['points'])
                self.adapter.start_hz=low;self.adapter.stop_hz=high;self.adapter.points=updated['points']
                self.revision+=1;self._publish()
        if values.get('ultra') is not True:edges()
        for name in order:
            if name not in values:continue
            self._ack(setting_command(name,values[name]).wire)
            self.settings[name]=values[name]
            if name=='rbw_hz':self.adapter.rbw_hz=values[name]
            if name=='vbw_ratio':self.settings['repeat']=updated['repeat']
            if name=='repeat':self.settings.pop('vbw_ratio',None)
            self.revision+=1;self._publish()
        if values.get('ultra') is True:edges()
        return self.status()

    def watch_operation(self, operation, parameters):
        if operation=='watch.stop':
            if parameters:raise ValueError('Остановка принимает пустой объект')
            previous=self.watch.status() if self.watch else None
            self.watch=None;self.watch_band=None;self.revision+=1;self._publish()
            journal=self._artifact('diagnostic',json.dumps(dict(transition='stopped',watch=previous)).encode(),
                'application/json','spectrum-watch-stop.json',event_type='spectrum_watch_lifecycle')
            return {**self.status(),**journal}
        if self.watch is not None:raise ValueError('Сначала остановите наблюдение')
        if self.mode!='monitoring':raise ValueError('Нужен режим monitoring')
        if set(parameters)!={'document'} or not isinstance(parameters['document'],str):raise ValueError('Нужен JSON плана')
        plan=validate_plan(json.loads(parameters['document']),ultra=self.settings.get('ultra',False))
        # Reproducible ordinary input; no trigger wait, trace transform or RF.
        needed={'output','caloutput','mode','trigger','calc','trace','refresh','sweep','rbw'}
        if not needed<=self.commands:raise ValueError('Прошивка не подтверждает команды наблюдения')
        self._ack('output off');self._ack('caloutput off');self._ack('mode input')
        for command in ('trigger auto','calc off','trace dBm','refresh off'):self._ack(command)
        self.settings.update(trigger_mode='auto',calculation='off',trace_unit='dBm')
        self._trace_processed=False
        self.watch=SpectrumWatch(plan);self.watch_band=None
        self.revision+=1;self._publish()
        journal=self._artifact('diagnostic',json.dumps(dict(transition='started',run_id=self.watch.id,plan=plan)).encode(),
            'application/json','spectrum-watch-start.json',event_type='spectrum_watch_lifecycle')
        return {**self.status(),**journal}

    def prepare_watch_sweep(self):
        if self.watch is None:return
        index=self.watch.completed % len(self.watch.plan['bands'])
        if index!=self.watch_band:
            band=self.watch.plan['bands'][index]
            self.receive_configure({k:v for k,v in band.items() if k!='name'})
            self.watch_band=index
            self._publish()

    def after_committed_sweep(self, sweep):
        if self.watch is None:return []
        was_learning=self.watch.bands[self.watch_band]['detector'] is None
        events=self.watch.observe(sweep,self.watch_band)
        self._publish()
        artifacts=[]
        detector=self.watch.bands[self.watch_band]['detector']
        if was_learning and detector is not None:
            baseline=dict(schema_version=1,run_id=self.watch.id,band_index=self.watch_band,
                          plan=self.watch.plan,baseline=detector.baseline)
            artifacts.append(self._artifact('diagnostic',json.dumps(baseline,allow_nan=False).encode(),
                'application/json','spectrum-baseline.json',event_type='spectrum_watch_baseline',run_id=self.watch.id)['artifact'])
        if not events:return artifacts
        # One bounded journal artifact per sweep, referencing already committed originals.
        raw=json.dumps(dict(schema_version=1,events=events),allow_nan=False).encode()
        return artifacts+[self._artifact('diagnostic',raw,'application/json','spectrum-events.json',
            started_at=format_time(sweep.interval.started_at),ended_at=format_time(sweep.interval.ended_at),
            timing_source='source_sweep_interval',event_type='spectrum_watch',run_id=self.watch.id)['artifact']]


from ..contracts import format_time
