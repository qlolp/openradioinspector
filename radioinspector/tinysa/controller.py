"""Single-worker control and expiring, exclusive manual/RF ownership."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import uuid

from ..contracts import format_time
from ..tinysa_profile import matches_identity
from .commands import CONTROL_PROFILE, catalog, setting_command
from .protocol import capture_png, read_binary, decode_scanraw, MAX_RAW_POINTS
from .forms import OPERATIONS,validate_parameters


class Controller:
    def __init__(self, adapter, *, monotonic=time.monotonic):
        self.adapter=adapter
        self.monotonic=monotonic
        self.mode='monitoring'
        self.revision=0
        self.connection_id=uuid.uuid4().hex
        self.settings={'start_hz':adapter.start_hz,'stop_hz':adapter.stop_hz,
                       'points':adapter.points,'rbw_hz':adapter.rbw_hz,
                       'attenuation_db':'auto','lna':False,'ultra':False,'calculation':'off'}
        self.commands=set()
        self._verified=False
        self._lease=None
        self._expires=0.0
        self._source='unknown'
        self._trace_processed=False
        self._generator=False
        self._output_confirmed=set()
        self._stop_requested=threading.Event()
        self._io_lock=threading.Lock()
        self._io_active=False
        self._public={}
        self._publish()

    def _publish(self):
        self._public=dict(mode=self.mode,profile=CONTROL_PROFILE,revision=self.revision,
                          connection_id=self.connection_id,settings_complete=False,
                          profile_verified=self._verified,settings=dict(self.settings),
                          settings_source=self._source,settings_readback=False,
                          lease_active=self._lease is not None,lease_seconds=max(0,self._expires-self.monotonic()),
                          generator_output_readback=False,hardware_validation='not_run')

    def status(self):
        # No I/O lock: HTTP status/stop must not wait for a long serial read.
        result=json.loads(json.dumps(self._public))
        result['lease_active']=bool(result['lease_active'] and self.monotonic()<self._expires)
        result['lease_seconds']=max(0,self._expires-self.monotonic()) if result['lease_active'] else 0
        return result

    def provenance(self):
        return dict(profile=CONTROL_PROFILE,revision=self.revision,settings=dict(self.settings),
                    connection_id=self.connection_id,segment_id=f'{self.connection_id}:{self.revision}',
                    settings_source=self._source,settings_readback=False,settings_complete=False,
                    hardware_validation='not_run')

    def request_stop(self):
        with self._io_lock:
            self._stop_requested.set()
            return self._io_active or self.mode=='monitoring'

    def _io(self, function, *, mutates=False):
        self.adapter._ensure_usable()
        with self._io_lock:
            if self._stop_requested.is_set():
                raise ValueError('Прибор останавливается')
            self._io_active=True
        if mutates:self._source='unknown'
        self._publish()
        self.adapter.transport._ownership_check=self._check_ownership
        try:
            result=function()
        except Exception as exc:
            self._source='unknown';self._publish()
            raise self.adapter._taint_connection(exc) from exc
        finally:
            self.adapter.transport._ownership_check=None
            with self._io_lock:self._io_active=False
        if mutates:self._source='command_acknowledged'
        self._publish()
        return result

    def _check_ownership(self):
        if self._lease is not None and self.monotonic()>=self._expires:
            raise ValueError('Lab-аренда истекла во время I/O; состояние прибора неизвестно')

    def _ack(self, command):
        if command.split()[0] not in self.commands:
            raise ValueError('Команда отсутствует в help проверенного профиля')
        return self._io(lambda:self.adapter._acknowledge(command),mutates=True)

    def _text(self, command, timeout=30):
        if command.split()[0] not in self.commands:
            raise ValueError('Команда отсутствует в help проверенного профиля')
        return self._io(lambda:self.adapter._transact(command,timeout))

    def _profile(self):
        self.adapter._ensure_usable()
        if self._verified:return
        self._io(self.adapter._prepare,mutates=True)
        identity=self.adapter._identity
        if not matches_identity(identity.get('info',''),identity.get('firmware','')):
            raise ValueError('Модель/прошивка не подтверждены профилем управления')
        help_text=self._io(lambda:self.adapter._transact('help',3.0))
        self.commands=set(re.findall(r'[a-z][a-z0-9_]*',help_text))
        self._verified=True;self._publish()

    def _require_lease(self,payload):
        if self._lease is None or payload.get('lease_id')!=self._lease or self.monotonic()>=self._expires:
            raise ValueError('Требуется действующая эксклюзивная lab-аренда')

    def renew_only(self,payload):
        # Pure ownership metadata; callable while the one serial owner is
        # receiving a long file. Never sends a byte or acquires its I/O lock.
        with self._io_lock:
            self._require_lease(payload)
            self._expires=self.monotonic()+30
            self._publish()
        return self.status()

    def _safe_off(self):
        for command in ['output off','caloutput off','release']:
            if command.split()[0] in self.commands:self._ack(command)
        self.settings={**self.settings,'output_enabled':False,'caloutput_mhz':'off'}
        self._generator=False
        self._output_confirmed.clear()

    def before_poll(self):
        if self.mode=='monitoring':return True
        if self._lease is not None and self.monotonic()>=self._expires:
            self._lease=None
            try:self._safe_off()
            finally:self._publish()
        return False

    def shutdown(self):
        """Called only by the owning worker, before transport.close()."""
        if self.mode=='lab' and not self.adapter._restart_required:
            # An idle lab stop has not cancelled the transport; attempt an
            # orderly off. Interrupted/tainted I/O cannot confirm off.
            try:
                for command in ['output off','caloutput off','release']:
                    if command.split()[0] in self.commands:self.adapter._acknowledge(command)
            except Exception as exc:
                self.adapter._taint_connection(exc)
        self._lease=None;self._source='unknown';self._publish()

    def _artifact(self, kind, raw, mime, name, **metadata):
        occurred_at=format_time(self.adapter._clock())
        interval=dict(started_at=metadata.pop('started_at',occurred_at),ended_at=metadata.pop('ended_at',occurred_at),
                      timing_source=metadata.pop('timing_source','artifact_creation_only'))
        return {'artifact':dict(kind=kind,mime_type=mime,filename=name,
                 data_base64=base64.b64encode(raw).decode('ascii'),sha256=hashlib.sha256(raw).hexdigest(),
                 metadata=dict(**self.provenance(),
                               occurred_at=occurred_at,**interval,**metadata))}

    def execute(self,payload):
        if not isinstance(payload,dict) or set(payload)-{'operation','parameters','lease_id','expected_revision'}:
            raise ValueError('Некорректная операция прибора')
        op=payload.get('operation');params=payload.get('parameters',{})
        if not isinstance(params,dict):raise ValueError('parameters должен быть объектом')
        allowed={'inspect','capture','lease.acquire','lease.renew','lease.release','monitor.resume',
                 'setting','touch','release','keypad','menu.home','sd.list','sd.read','sd.delete',
                 'preset.save','preset.load','acquire.raw','acquire.zero','service.guide'}
        allowed.update(OPERATIONS)
        if op not in allowed:raise ValueError('Неизвестная типизированная операция')
        if op in OPERATIONS and op not in {'acquire.raw','acquire.zero'}:
            params=validate_parameters(op,params)
        expected=payload.get('expected_revision',self.revision)
        if type(expected) is not int or expected!=self.revision:
            raise ValueError('Настройки изменились; обновите состояние прибора')
        self._profile()
        self.before_poll()
        if op in OPERATIONS and OPERATIONS[op].get('lab'):self._require_lease(payload)
        if op=='output.off':
            self._safe_off();self.revision+=1;self._publish();return self.status()
        if op=='service.guide':
            from .workflows import service_guide
            return service_guide(params['name'])
        if op=='inspect':
            return dict(status=self.status(),capabilities=catalog(),commands=sorted(self.commands))
        if op=='lease.acquire':
            if self._lease is not None:raise ValueError('Прибор уже занят другим lab-клиентом')
            if params.get('acknowledge_rf') is not True:raise ValueError('Нужно явное разрешение возможной RF-генерации')
            self._ack('pause');self._safe_off()
            self.mode='lab';self._lease=uuid.uuid4().hex;self._expires=self.monotonic()+30
            self.revision+=1;self._publish()
            return dict(lease_id=self._lease,expires_in=30,status=self.status())
        if op in {'lease.renew','lease.release','monitor.resume','touch','release','keypad','menu.home','sd.delete','preset.save','preset.load'}:
            self._require_lease(payload)
        if op=='lease.renew':
            return self.renew_only(payload)
        if op in {'lease.release','monitor.resume'}:
            self._safe_off()
            if op=='monitor.resume':
                self._ack('mode input')
                # Manual changes are never silently restored as the old data
                # segment. Reapply an explicit conservative acquisition setup.
                for command in ['lna off','attenuate auto','calc off','trigger auto','trace dBm','refresh off']:
                    if command.split()[0] in self.commands:self._ack(command)
                self.settings={**self.settings,'lna':False,'attenuation_db':'auto','calculation':'off','trigger_mode':'auto','trace_unit':'dBm'}
                self.mode='monitoring'
            self._lease=None;self.revision+=1;self._publish();return self.status()
        if op=='capture':
            if 'capture' not in self.commands:raise ValueError('capture отсутствует в help')
            started=format_time(self.adapter._clock())
            raw=self._io(lambda:read_binary(self.adapter.transport,'capture',size=480*320*2))
            ended=format_time(self.adapter._clock())
            return self._artifact('screen',capture_png(raw),'image/png','tinysa-screen.png',started_at=started,ended_at=ended,timing_source='host_usb_interval')
        if op in {'touch','release','keypad','menu.home'}:
            if op=='touch':
                x,y=params.get('x'),params.get('y')
                if type(x) is not int or type(y) is not int or not 0<=x<480 or not 0<=y<320:raise ValueError('Координаты вне экрана Ultra')
                wire=f'touch {x} {y}'
            elif op=='keypad':
                text=params.get('text')
                if not isinstance(text,str) or not re.fullmatch(r'-?[0-9]{1,12}(?:\.[0-9]{1,6})?[kMGmu]?',text):
                    raise ValueError('Keypad поддерживает числовой ввод; имена вводятся экранной клавиатурой')
                wire='text '+text
            else:wire='release' if op=='release' else 'menu'
            self._ack(wire);self._trace_processed=True;self._output_confirmed.clear();self._source='unknown';self.revision+=1;self._publish();return self.status()
        if op=='setting':
            name,value=params.get('name'),params.get('value')
            command=setting_command(name,value)
            if command.requires_lab or self.mode=='lab':self._require_lease(payload)
            if name=='lna' and value and self.settings.get('attenuation_db')!='auto' and self.settings.get('attenuation_db')!=0:
                raise ValueError('LNA несовместим с аттенюатором; сначала выберите 0 дБ')
            if name=='attenuation_db' and self.settings.get('lna') and value!=0:
                raise ValueError('Сначала выключите LNA')
            if name in {'start_hz','stop_hz','center_hz'} and value>800000000 and not self.settings.get('ultra'):
                raise ValueError('Расширенная частота требует явного Ultra-профиля')
            updated={**self.settings,name:value}
            if name=='ultra' and value is False and updated['stop_hz']>800000000:
                raise ValueError('Сначала верните диапазон не выше 800 МГц')
            if name in {'center_hz','span_hz'}:
                span=value if name=='span_hz' else self.settings['stop_hz']-self.settings['start_hz']
                center=value if name=='center_hz' else (self.settings['stop_hz']+self.settings['start_hz'])//2
                updated.update(start_hz=center-span//2,stop_hz=center+(span-span//2))
                if not 100000<=updated['start_hz']<=updated['stop_hz']<= (6000000000 if updated.get('ultra') else 800000000):
                    raise ValueError('Диапазон выходит за пределы выбранного профиля')
            if name=='start_hz' and value>=updated['stop_hz'] or name=='stop_hz' and value<=updated['start_hz']:
                raise ValueError('Начало должно быть ниже конца')
            if name=='modulation_hz' and self.settings.get('modulation')=='fm' and value>1000:
                raise ValueError('FM modulation frequency не выше 1000 Гц')
            if name=='modulation' and value=='fm' and self.settings.get('modulation_hz',1000)>1000:
                raise ValueError('Сначала уменьшите частоту модуляции до 1000 Гц')
            if name=='output_enabled' and value and not {'output_hz','output_dbm'}<=self._output_confirmed:
                raise ValueError('Сначала явно задайте частоту и уровень генератора')
            if (name.startswith('output_') and not (name=='output_enabled' and value is False)) or name in {'modulation','modulation_hz','am_depth_percent','fm_deviation_hz'}:
                if not self._generator:
                    self._ack('output off');self._ack('mode output');self._ack('output off');self._generator=True
            if name=='points':
                self._ack(f"sweep {updated['start_hz']} {updated['stop_hz']} {value}")
                self.adapter.points=value
            else:self._ack(command.wire)
            if name in {'output_hz','output_dbm'}:self._output_confirmed.add(name)
            if name in {'start_hz','stop_hz'}:setattr(self.adapter,name,value)
            if name=='rbw_hz' and value!='auto':self.adapter.rbw_hz=value
            if name=='rbw_hz' and value=='auto':self.adapter.rbw_hz='auto'
            if name in {'center_hz','span_hz'}:
                self.adapter.start_hz=updated['start_hz'];self.adapter.stop_hz=updated['stop_hz']
            self.settings=updated;self.revision+=1;self._publish();return self.status()
        if op in {'sd.list','sd.read','sd.delete'}:
            if op=='sd.list':
                data=self._text('sd_list');return dict(directory_text=data)
            name=params.get('filename')
            if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',name) or '..' in name:
                raise ValueError('Требуется имя файла SD без путей и управляющих символов')
            if op=='sd.delete':
                if params.get('confirm_delete') is not True:raise ValueError('Подтвердите удаление файла SD')
                text=self._text('sd_delete '+name)
                if text.strip()!=f'delete: {name} OK':raise ValueError('Удаление SD не подтверждено: '+text)
                return dict(deleted=name,confirmation='device_response')
            started=format_time(self.adapter._clock())
            data=self._io(lambda:read_binary(self.adapter.transport,'sd_read '+name))
            return self._artifact('sd_file',data,'application/octet-stream',name,started_at=started,ended_at=format_time(self.adapter._clock()),timing_source='host_usb_interval')
        if op in {'preset.save','preset.load'}:
            slot=params.get('slot')
            if type(slot) is not int or not 0<=slot<=4 or params.get('confirm') is not True:
                raise ValueError('Слот 0–4 и явное подтверждение обязательны')
            self._ack(('save' if op=='preset.save' else 'recall')+f' {slot}')
            if op=='preset.load':self._trace_processed=True
            self._output_confirmed.clear();self._source='unknown';self.revision+=1;self._publish();return self.status()
        if op in {'acquire.raw','acquire.zero'}:
            if self.mode=='lab':
                self._require_lease(payload)
                if self._generator or self._source=='unknown':
                    raise ValueError('Верните прибор в monitoring перед приёмным измерением')
            return self._raw(params,zero_span=op=='acquire.zero')
        if op in {'trace','marker'}:
            return self._trace_marker(op,params,payload)
        if op=='preset.export':
            from .commands import SETTINGS
            data=dict(schema_version=1,profile=CONTROL_PROFILE,settings={k:v for k,v in self.settings.items() if k in SETTINGS and not SETTINGS[k].get('lab') and k not in {'center_hz','span_hz'}})
            return self._artifact('diagnostic',json.dumps(data,allow_nan=False).encode(),'application/json','tinysa-preset.json')
        if op=='preset.import':return self._import_preset(params,payload)
        if op=='measurement':
            if self.mode!='monitoring':raise ValueError('Измерение ядра требует monitoring; lab сначала завершить')
            from .measurements import analyze
            artifact=self._raw({k:v for k,v in params.items() if k!='method'})['artifact']
            data=json.loads(base64.b64decode(artifact['data_base64']))
            rbw=self._read_rbw()
            result=analyze(data,params['method'],rbw_hz=rbw)
            result['source']=data
            return {**self._artifact('measurement',json.dumps(result,allow_nan=False).encode(),'application/json','tinysa-measurement.json',rbw_applied_hz=rbw,started_at=data['started_at'],ended_at=data['ended_at'],timing_source='host_usb_interval'),
                    'measurement':{k:v for k,v in result.items() if k!='source'}}
        if op=='selftest.start':
            if params['confirm'] is not True:raise ValueError('Подтвердите подключение стенда')
            self._safe_off();self._ack('selftest '+str(params['test']))
            self._source='unknown';self.revision+=1;self._publish()
            return dict(completion='inspect_live_screen',hardware_validation='not_run')
        if op=='generator.sweep':return self._generator_sweep(params)
        if op in {'audio.inputs','audio.capture'}:
            from .audio import inputs,capture
            if op=='audio.inputs':return inputs()
            if params['confirm_connection'] is not True:raise ValueError('Подтвердите аналоговое подключение LISTEN')
            started=format_time(self.adapter._clock())
            raw=capture(params,check=lambda:(self._check_ownership(),self._check_stopped()))
            return self._artifact('audio',raw,'audio/wav','tinysa-listen.wav',started_at=started,ended_at=format_time(self.adapter._clock()),timing_source='host_audio_interval',audio_source='analog_input',device=params['device'],sample_rate_hz=params['sample_rate_hz'])
        raise ValueError('Неизвестная операция')

    def _check_stopped(self):
        if self._stop_requested.is_set():raise ValueError('Запись отменена')

    def _read_rbw(self):
        text=self._text('rbw')
        match=re.search(r'\n([0-9]+(?:\.[0-9]+)?)Hz$',text.strip())
        if not match:raise ValueError('Нет readback фактического RBW; результат не вычислен')
        return float(match[1])

    def _trace_marker(self,op,p,payload):
        if self.mode=='lab':self._require_lease(payload)
        action=p['action']
        if op=='trace':
            index=p['trace'];prefix=f'trace {index}'
            if action=='read':
                started=format_time(self.adapter._clock())
                text=self._text(prefix+' value')
                return self._artifact('processed_trace',json.dumps(dict(device_response=text,trace=index,level_unit='internal_dBm',readback='firmware_trace_value')).encode(),'application/json','tinysa-device-trace.json',started_at=started,ended_at=format_time(self.adapter._clock()),timing_source='host_usb_interval')
            if action in {'copy','subtract'}:wire=f"{prefix} {action} {p['source_trace']}"
            elif action in {'freeze','view'}:wire=f"{prefix} {action} "+('on' if p['enabled'] else 'off')
            else:wire=f"calc {index} {p['calculation']}"
            self._ack(wire);self._trace_processed=True
        else:
            index=p['marker'];wire=f'marker {index}'
            if action not in {'read','frequency'}:wire+=' '+action
            if action=='frequency':wire+=' '+str(p['frequency_hz'])
            elif action in {'delta','trace'}:wire+=' '+str(p['reference'])
            elif action in {'noise','tracking','trace_aver'}:wire+=' '+('on' if p['enabled'] else 'off')
            text=self._text(wire)
            if action in {'read','peak'}:return dict(device_response=text,level_unit=self.settings.get('trace_unit','unknown'))
        self.revision+=1;self._publish();return self.status()

    def _import_preset(self,p,payload):
        from .commands import SETTINGS
        if p['confirm'] is not True:raise ValueError('Подтвердите импорт')
        def unique(pairs):
            result={}
            for k,v in pairs:
                if k in result:raise ValueError('Повторяющийся ключ пресета')
                result[k]=v
            return result
        data=json.loads(p['document'],object_pairs_hook=unique,parse_constant=lambda x:(_ for _ in ()).throw(ValueError('Non-finite preset')))
        if not isinstance(data,dict) or set(data)!={'schema_version','profile','settings'} or type(data['schema_version']) is not int or data['schema_version']!=1 or data['profile']!=CONTROL_PROFILE or not isinstance(data['settings'],dict):raise ValueError('Неподдерживаемый preset')
        settings=data['settings']
        # Validate the complete document before sending any byte. No generator,
        # calibration tables or arbitrary console strings in portable presets.
        for name,value in settings.items():
            if name not in SETTINGS or SETTINGS[name].get('lab') or name in {'center_hz','span_hz'}:raise ValueError('Пресет содержит недопустимую настройку')
            setting_command(name,value)
        merged={**self.settings,**settings}
        if not 100000<=merged['start_hz']<=merged['stop_hz']<=(6000000000 if merged.get('ultra') else 800000000):raise ValueError('Некорректный диапазон пресета')
        if merged.get('lna') and merged.get('attenuation_db') not in (0,'auto'):raise ValueError('LNA/attenuator конфликт')
        self._safe_off();self._ack('mode input');self._ack('lna off')
        # Range changes are applied together, avoiding transient invalid ranges.
        self._ack('ultra '+('on' if merged.get('ultra') else 'off'))
        self._ack(f"sweep {merged['start_hz']} {merged['stop_hz']} {merged['points']}")
        for name,value in settings.items():
            if name not in {'start_hz','stop_hz','points','ultra','lna'}:self._ack(setting_command(name,value).wire)
        if merged.get('lna'):self._ack('lna on')
        self.settings=merged;self.adapter.start_hz=merged['start_hz'];self.adapter.stop_hz=merged['stop_hz'];self.adapter.points=merged['points'];self.adapter.rbw_hz=merged['rbw_hz']
        self.revision+=1;self._source='command_acknowledged';self._publish()
        return dict(status=self.status(),application='sequential_acknowledged',rollback='not_available_on_device')

    def _generator_sweep(self,p):
        if p['start_hz']>=p['stop_hz'] or p['confirm'] is not True:raise ValueError('Нужен возрастающий диапазон и подтверждение RF')
        if not -115<=p['level_dbm']+p['delta_db']<=-19:raise ValueError('Конечный уровень sweep вне диапазона')
        self._safe_off();self._ack('mode output');self._ack('output off')
        for command in [f"sweep {p['start_hz']} {p['stop_hz']} {p['points']}",f"level {p['level_dbm']:g}",f"levelchange {p['delta_db']:g}",f"sweeptime {p['seconds']:g}",'output on','sweep go','resume']:
            self._ack(command)
        self._generator=True;self.settings.update(output_enabled=True,generator_sweep=p)
        self.revision+=1;self._publish();return self.status()

    def _raw(self,params,*,zero_span=False):
        start=params.get('start_hz',self.adapter.start_hz)
        stop=start if zero_span else params.get('stop_hz',self.adapter.stop_hz)
        points=params.get('points',self.adapter.points)
        if type(start) is not int or type(stop) is not int or not 100000<=start<=stop<=6000000000 or type(points) is not int or not 1<=points<=MAX_RAW_POINTS:
            raise ValueError('Недопустимые параметры scanraw')
        if stop>800000000 and not self.settings.get('ultra'):raise ValueError('Сначала включите Ultra')
        zero_span=start==stop
        zero=self._text('zero')
        m=re.fullmatch(r'zero \{level\}\n([0-9]{1,4})dBm',zero.strip())
        if not m:raise ValueError('Нет однозначного readback zero; scanraw не отправлен')
        command=f'scanraw {start} {stop} {points} 0'
        started=format_time(self.adapter._clock())
        raw=self._io(lambda:read_binary(self.adapter.transport,command,size=3*points+2,timeout=90))
        try:hz,levels=decode_scanraw(raw,start,stop,points,zero_db=int(m[1]))
        except Exception as exc:raise self.adapter._taint_connection(exc) from exc
        data=dict(frequencies_hz=hz,levels=levels,level_unit='dBm',wire_base64=base64.b64encode(raw).decode(),
                  zero_db=int(m[1]),started_at=started,ended_at=format_time(self.adapter._clock()),
                  sample_offsets_seconds=None,timing_source='interval_only',grid_source='firmware_ea71dfd_scanraw',
                  requested_stop_hz=stop,stop_is_inclusive=None if zero_span else False)
        return self._artifact('zero_span' if zero_span else 'raw_scan',json.dumps(data,allow_nan=False).encode(),
                              'application/json','tinysa-zero-span.json' if zero_span else 'tinysa-raw-scan.json',started_at=started,ended_at=data['ended_at'],timing_source='host_usb_interval')
