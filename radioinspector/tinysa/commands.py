"""Semantic allow-list. A menu name is never treated as a shell command."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json
import math

CONTROL_PROFILE = 'tinysa-ultra-zs405-control-v1'
SOURCE_REVISION = 'ea71dfd9e53d6dec95256ac70914659cf260d48d'


@dataclass(frozen=True)
class Command:
    wire: str
    requires_lab: bool = False


# The schema is consumed by all three clients. Units are API units, not shell
# units. Screen-only controls remain available via the exclusive lab screen.
SETTINGS = {
    'start_hz': dict(label='Начало, Гц', command='sweep start', type='integer', min=100000, max=6000000000),
    'stop_hz': dict(label='Конец, Гц', command='sweep stop', type='integer', min=100000, max=6000000000),
    'center_hz': dict(label='Центр, Гц', command='sweep center', type='integer', min=100000, max=6000000000),
    'span_hz': dict(label='Полоса обзора, Гц', command='sweep span', type='integer', min=0, max=5999900000),
    'points': dict(label='Точек', command='sweep', type='integer', min=2, max=450),
    'rbw_hz': dict(label='RBW, Гц', command='rbw', type='choice', choices=['auto',200,1000,3000,10000,30000,100000,300000,600000,850000]),
    'attenuation_db': dict(label='Аттенюатор, дБ', command='attenuate', type='integer_or_auto', min=0, max=31),
    'lna': dict(label='LNA', command='lna', type='boolean'),
    'ultra': dict(label='Ultra (расширенный диапазон)', command='ultra', type='boolean'),
    'spur': dict(label='Подавление spur', command='spur', type='choice', choices=['off','on','auto']),
    'repeat': dict(label='Повторов на частоту', command='repeat', type='integer', min=1, max=1000),
    'sweep_seconds': dict(label='Время sweep, с (0=auto)', command='sweeptime', type='number', min=0, max=600),
    'external_gain_db': dict(label='Внешний gain, дБ', command='ext_gain', type='number', min=-100, max=100),
    'calculation': dict(label='Обработка трассы', command='calc', type='choice', choices=['off','minh','maxh','maxd','aver4','aver16','aver','quasi','log','lin']),
    'trigger_mode': dict(label='Trigger', command='trigger', type='choice', choices=['auto','normal','single']),
    'trigger_dbm': dict(label='Порог trigger, dBm', command='trigger', type='number', min=-174, max=30),
    'trace_unit': dict(label='Единицы экрана', command='trace', type='choice', choices=['dBm','dBmV','dBuV','dBV','V','W']),
    'ref_level': dict(label='Reference level (единицы экрана)', command='trace reflevel', type='number_or_auto', min=-174, max=150),
    'scale_div': dict(label='Масштаб на деление', command='trace scale', type='number_or_auto', min=0.01, max=100),
    'output_hz': dict(label='Генератор, Гц', command='freq', type='integer', min=100000, max=800000000, lab=True),
    'output_dbm': dict(label='Генератор, dBm', command='level', type='number', min=-115, max=-19, lab=True),
    'output_enabled': dict(label='RF выход', command='output', type='boolean', lab=True),
    'modulation': dict(label='Модуляция', command='modulation', type='choice', choices=['off','am','fm'], lab=True),
    'modulation_hz': dict(label='Частота модуляции, Гц', command='modulation freq', type='integer', min=50, max=5000, lab=True),
    'am_depth_percent': dict(label='Глубина AM, %', command='modulation depth', type='integer', min=10, max=100, lab=True),
    'fm_deviation_hz': dict(label='Девиация FM, Гц', command='modulation deviation', type='integer', min=1000, max=300000, lab=True),
    'output_delta_db': dict(label='Изменение уровня sweep, дБ', command='levelchange', type='number', min=-90, max=90, lab=True),
    'caloutput_mhz': dict(label='CAL, МГц (off/1/2/4/10/15/30)', command='caloutput', type='choice', choices=['off',1,2,4,10,15,30], lab=True),
}


def setting_command(name: str, value) -> Command:
    if name not in SETTINGS:
        raise ValueError('Неизвестная настройка прибора')
    spec = SETTINGS[name]
    kind = spec['type']
    if kind == 'boolean':
        if type(value) is not bool:
            raise ValueError('Требуется boolean')
        token = 'on' if value else 'off'
    elif kind == 'choice':
        if type(value) not in (int,str) or value not in spec['choices']:
            raise ValueError('Недопустимое значение настройки')
        token = str(value)
    elif kind.endswith('_or_auto') and value == 'auto':
        token = 'auto'
    else:
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise ValueError('Требуется конечное число')
        if kind.startswith('integer') and type(value) is not int:
            raise ValueError('Требуется целое число')
        if not spec['min'] <= value <= spec['max']:
            raise ValueError('Значение вне диапазона профиля')
        token = str(value) if type(value) is int else format(value,'.12g')
    if name == 'rbw_hz' and value != 'auto':
        token = format(value/1000,'.12g')
    return Command(spec['command']+' '+token, bool(spec.get('lab')))


def catalog() -> dict:
    data = json.loads(files(__package__).joinpath('menu.json').read_text(encoding='utf-8'))
    return dict(profile=CONTROL_PROFILE, source_revision=SOURCE_REVISION,
                model='tinySA Ultra ZS405', screen=dict(width=480,height=320,format='rgb565be'),
                hardware_validation='not_run', settings=json.loads(json.dumps(SETTINGS)), menu=data['items'],
                limitations=['No simultaneous analyzer and generator','No documented USB PCM or IQ',
                             'Screen is read-only in monitoring; lab actions require exclusive lease',
                             'Firmware source evidence is not physical validation'])
