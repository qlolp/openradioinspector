"""Shared client forms for semantic operations; no shell strings in inputs."""

def field(label,kind='integer',default=None,**extra):
    return dict(label=label,type=kind,default=default,**extra)

def choice(label,values,default=None):
    return field(label,'choice',values[0] if default is None else default,choices=values)

OPERATIONS={
    'inspect':dict(label='Проверить профиль и команды',fields={}),
    'acquire.raw':dict(label='Исходный scanraw',fields={
        'start_hz':field('Начало, Гц',default=100000000,min=100000,max=6000000000),
        'stop_hz':field('Конец, Гц',default=101000000,min=100000,max=6000000000),
        'points':field('Точек',default=450,min=1,max=65536)}),
    'acquire.zero':dict(label='Zero-span (UTC-интервал)',fields={
        'start_hz':field('Частота, Гц',default=100000000,min=100000,max=6000000000),
        'points':field('Точек',default=450,min=1,max=65536)}),
    'trace':dict(label='Трассы прибора',fields={
        'trace':field('Трасса',default=1,min=1,max=4),
        'action':choice('Действие',['read','copy','freeze','subtract','view','calculation']),
        'source_trace':field('Исходная/вычитаемая трасса',default=2,min=1,max=4),
        'enabled':field('Включено','boolean',False),
        'calculation':choice('Обработка',['off','minh','maxh','maxd','aver4','aver16','aver','quasi','log','lin'])}),
    'marker':dict(label='Маркеры прибора',fields={
        'marker':field('Маркер',default=1,min=1,max=8),
        'action':choice('Действие',['read','on','off','peak','frequency','delta','noise','tracking','trace','trace_aver']),
        'frequency_hz':field('Частота, Гц',default=100000000,min=100000,max=6000000000),
        'reference':field('Опорный маркер / трасса',default=1,min=1,max=4),
        'enabled':field('Включено','boolean',True)}),
    'measurement':dict(label='Измерение ядром (по исходному scanraw)',fields={
        'method':choice('Метод',['peak','occupied_bandwidth','bandwidth_3db','centroid','channel_power']),
        'start_hz':field('Начало, Гц',default=100000000,min=100000,max=6000000000),
        'stop_hz':field('Конец, Гц',default=101000000,min=100000,max=6000000000),
        'points':field('Точек',default=450,min=2,max=65536)}),
    'sd.list':dict(label='Список файлов SD',fields={}),
    'sd.read':dict(label='Получить файл SD в архив',fields={'filename':field('Имя файла','text','TRACE.CSV')}),
    'sd.delete':dict(label='Удалить файл с SD',lab=True,fields={'filename':field('Имя файла','text','TRACE.CSV'),'confirm_delete':field('Подтверждаю удаление','boolean',False)}),
    'preset.save':dict(label='Сохранить пресет прибора',lab=True,fields={'slot':field('Слот',default=0,min=0,max=4),'confirm':field('Подтверждаю перезапись','boolean',False)}),
    'preset.load':dict(label='Загрузить пресет прибора',lab=True,fields={'slot':field('Слот',default=0,min=0,max=4),'confirm':field('Пресет может изменить RF','boolean',False)}),
    'preset.export':dict(label='Экспорт настроек ядра в JSON',fields={}),
    'preset.import':dict(label='Импорт настроек ядра из JSON',lab=True,fields={'document':field('JSON пресета','multiline',''),'confirm':field('Применить приёмные настройки','boolean',False)}),
    'service.guide':dict(label='Сервис / функции экрана',fields={'name':choice('Процедура',['multiband','vbw','measurements','listen','calibration','selftest','firmware','expert','generator_extended','generator_sweep','corrections','sd_csv'])}),
    'selftest.start':dict(label='Запустить self-test прибора',lab=True,fields={'test':field('Номер 1–3',default=1,min=1,max=3),'confirm':field('Стенд подключён, возможен RF','boolean',False)}),
    'generator.sweep':dict(label='Sweep генератора',lab=True,fields={
        'start_hz':field('Начало, Гц',default=30000000,min=100000,max=800000000),
        'stop_hz':field('Конец, Гц',default=31000000,min=100000,max=800000000),
        'points':field('Точек',default=100,min=2,max=450),
        'seconds':field('Время, с','number',1,min=0.003,max=60),
        'level_dbm':field('Уровень, dBm','number',-30,min=-115,max=-19),
        'delta_db':field('Изменение уровня, дБ','number',0,min=-90,max=90),
        'confirm':field('Включить RF sweep','boolean',False)}),
    'output.off':dict(label='Отключить RF и CAL',fields={}),
    'audio.inputs':dict(label='Аналоговые аудиовходы ядра',fields={}),
    'audio.capture':dict(label='Записать аналоговый LISTEN в WAV',lab=True,fields={
        'device':field('Индекс входа из списка',default=0,min=0,max=1024),
        'seconds':field('Длительность, с','number',5,min=0.1,max=30),
        'sample_rate_hz':choice('Частота дискретизации, Гц',[8000,12000,24000,48000],48000),
        'confirm_connection':field('LISTEN → аудиовход ядра подключён','boolean',False)}),
}

def validate_parameters(operation,parameters):
    """Validate complete multi-field actions before the first mutation."""
    import math
    spec=OPERATIONS[operation]
    if set(parameters)-set(spec['fields']):raise ValueError('Неизвестный параметр операции')
    result={}
    for key,item in spec['fields'].items():
        value=parameters.get(key,item['default']);kind=item['type']
        if kind=='boolean':valid=type(value) is bool
        elif kind=='choice':valid=type(value) in (int,str) and value in item['choices']
        elif kind in {'text','multiline'}:valid=isinstance(value,str) and len(value)<=32768 and '\x00' not in value
        else:
            valid=(type(value) is int if kind=='integer' else type(value) in (int,float))
            valid=valid and math.isfinite(value) and item.get('min',-1e100)<=value<=item.get('max',1e100)
        if not valid:raise ValueError('Некорректный параметр: '+item['label'])
        result[key]=value
    return result
