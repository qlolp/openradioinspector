"""Local frozen reference selection; only the core computes mean linear power."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic
import re
from .spectral_mask import same_json
from .sweep_threshold import ThresholdPanel

MAX_HZ = 9_007_199_254_740_991


def require(ok):
    if not ok: raise ValueError('Некорректный усреднённый опорный sweep')


def number(value):
    try: return type(value) in (int, float) and isfinite(value)
    except OverflowError: return False


def identity(value):
    return (isinstance(value, str) and 1 <= len(value) <= 128 and bool(value.strip())
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def validate_ids(ids, target):
    require(isinstance(ids, list) and 2 <= len(ids) <= 16 and all(identity(v) for v in ids))
    require(len(set(ids)) == len(ids) and identity(target) and target not in ids)


def utc_interval(interval):
    require(isinstance(interval, dict) and set(interval) == {'started_at', 'ended_at'})
    times = []
    for key in ('started_at', 'ended_at'):
        value = interval[key]
        require(isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)', value) is not None)
        try: stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, TypeError): raise ValueError('Некорректное время UTC') from None
        require(stamp.utcoffset() == timedelta(0)); times.append(stamp)
    require(times[0] <= times[1])
    return times


def json_value(value, nullable=False):
    if value is None: return nullable
    if type(value) in (str, bool): return True
    if type(value) in (int, float): return number(value)
    if isinstance(value, list): return all(json_value(v, nullable) for v in value)
    if isinstance(value, dict): return all(isinstance(k, str) and json_value(v, nullable) for k, v in value.items())
    return False


def validate_source(source, stream):
    require(isinstance(source, dict) and identity(source.get('id')) and source.get('stream_id') == stream)
    require(source.get('level_unit') == 'dBm')
    frequencies, levels = source.get('frequencies_hz'), source.get('levels')
    require(isinstance(frequencies, list) and 1 <= len(frequencies) <= 16384)
    require(all(type(v) is int and 0 <= v <= MAX_HZ for v in frequencies))
    require(all(a <= b for a, b in zip(frequencies, frequencies[1:])))
    require(isinstance(levels, list) and len(levels) == len(frequencies) and all(number(v) for v in levels))
    settings = source.get('settings'); require(isinstance(settings, dict))
    recorded = {k: v for k, v in settings.items() if k != 'sequence'}
    require(bool(recorded) and json_value(recorded))
    require('sequence' not in settings or json_value(settings['sequence'], True))
    utc_interval(source.get('interval'))
    return recorded


def validate_average(data, expected, sources, target):
    session, stream = expected
    validate_ids([s['id'] for s in sources], target['id'])
    settings = validate_source(target, stream)
    grid = target['frequencies_hz']; n = len(grid)
    times = []
    for source in sources:
        require(same_json(validate_source(source, stream), settings))
        require(same_json(source['frequencies_hz'], grid))
        times.append(utc_interval(source['interval']))
    require(isinstance(data, dict) and data.get('session_id') == session and data.get('stream_id') == stream)
    require(type(data.get('points')) is int and data['points'] == n and data.get('delta_unit') == 'dB')
    require(same_json(data.get('frequencies_hz'), grid))
    require(all(type(v) is int and 0 <= v <= MAX_HZ for v in data['frequencies_hz']))
    ref = data.get('reference'); require(isinstance(ref, dict))
    require(ref.get('method') == 'mean_power' and type(ref.get('count')) is int and ref['count'] == len(sources))
    require(same_json(ref.get('source_ids'), [s['id'] for s in sources]) and ref.get('level_unit') == 'dBm')
    provenance = ref.get('sources'); require(isinstance(provenance, list) and len(provenance) == len(sources))
    for actual, source in zip(provenance, sources):
        require(isinstance(actual, dict))
        for key in ('id', 'interval', 'settings'): require(same_json(actual.get(key), source[key]))
    start, end = utc_interval(ref.get('interval'))
    require(start == min(t[0] for t in times) and end == max(t[1] for t in times))
    actual = data.get('target'); require(isinstance(actual, dict))
    for key in ('id', 'interval', 'settings', 'level_unit', 'levels'): require(same_json(actual.get(key), target[key]))
    mean, delta = ref.get('levels'), data.get('delta_levels')
    for values in (mean, delta): require(isinstance(values, list) and len(values) == n and all(number(v) for v in values))
    for i in range(n):
        require(min(s['levels'][i] for s in sources) <= mean[i] <= max(s['levels'][i] for s in sources))
        difference = target['levels'][i] - mean[i]
        require(number(difference) and difference == delta[i])
    for key, operation in (('max_increase', max), ('min_change', min)):
        item = data.get(key); require(isinstance(item, dict))
        i = operation(range(n), key=delta.__getitem__)
        require(type(item.get('frequency_hz')) is int and item['frequency_hz'] == grid[i])
        require(number(item.get('delta_db')) and item['delta_db'] == delta[i])
    return data


def source_text(sources):
    times = [utc_interval(s['interval']) for s in sources]
    lines = [f"mean_power · N={len(sources)} · UTC {min(t[0] for t in times).isoformat()} … {max(t[1] for t in times).isoformat()}"]
    lines.extend(f"{s['id']} · UTC {s['interval']['started_at']} … {s['interval']['ended_at']}" for s in sources)
    return '\n'.join(lines)


class AveragePanel(ThresholdPanel):
    def __init__(self, app, notebook):
        self.app = app; self.scope = None; self.generation = 0
        self.future = self.retired = self.data = self.sources = None; self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sweep-average')
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8); notebook.add(frame, text='Усреднённый опорный')
        ttk.Label(frame, text='Исходные dBm; только чтение. Арифметическое среднее линейной мощности с равными весами. Локальное закрепление, без сохранения именованного эталона, калибровки и автоматического детектора.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.count = tk.StringVar(value='3')
        ttk.Label(row, text='N (2…16)').pack(side='left'); ttk.Entry(row, textvariable=self.count, width=5).pack(side='left')
        ttk.Button(row, text='Закрепить N перед последним dBm sweep', command=self.pin).pack(side='left')
        self.button = ttk.Button(row, text='Сравнить с последним dBm sweep', command=self.compare); self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        self.info = tk.StringVar(value='Закрепите N источников; сравнение только по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.canvas = tk.Canvas(frame, background='#07131c', height=300); self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda event: self.draw())
        self.details = tk.Text(frame, height=8, state='disabled', wrap='none'); self.details.pack(fill='both', expand=True)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview); scroll.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scroll.set)
        self.count.trace_add('write', lambda *args: self.clear())

    def clear(self):
        self.abandon(); self.sources = self.data = None
        self.canvas.delete('all'); self.show_details('')
        self.info.set('Опорные источники не закреплены. N предыдущих + последний dBm sweep должны быть в полученном окне.')

    def selected(self):
        snapshot = self.app.snapshot; stream = self.app.selected_stream_id()
        require(self.app.client is not None and bool(self.app.session_id) and bool(stream))
        require((snapshot.session or {}).get('id') == self.app.session_id and snapshot.spectrum_stream_id == stream)
        return [s for s in snapshot.sweeps if isinstance(s, dict) and s.get('stream_id') == stream and s.get('level_unit') == 'dBm']

    def pin(self):
        self.update()
        if self.closed: return
        self.clear()
        try:
            raw = self.count.get(); require(raw.isascii() and raw.isdigit())
            count = int(raw); require(2 <= count <= 16)
            sweeps = self.selected()
            if len(sweeps) < count + 1: raise ValueError(f'Нужны {count + 1} dBm sweeps; доступны {len(sweeps)}. N не уменьшается.')
            sources = deepcopy(sweeps[-count-1:-1]); target = sweeps[-1]
            validate_ids([s['id'] for s in sources], target['id'])
            for source in sources: validate_source(source, self.app.selected_stream_id())
            self.sources = sources
            self.show_details(source_text(sources)); self.info.set('Источники закреплены локально. Новые sweeps не меняют опорный набор.')
        except (ValueError, TypeError, KeyError) as exc: self.info.set(str(exc))

    def compare(self):
        self.update()
        if self.closed or self.future is not None or self.sources is None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            sweeps = self.selected(); require(bool(sweeps))
            target = deepcopy(sweeps[-1]); validate_source(target, self.app.selected_stream_id())
            validate_ids([s['id'] for s in self.sources], target['id'])
        except (ValueError, TypeError, KeyError) as exc:
            self.info.set(str(exc)); return
        self.abandon(); self.data = None; self.canvas.delete('all')
        self.show_details(source_text(self.sources)); self.target = target
        self.expected = (self.app.session_id, self.app.selected_stream_id())
        self.info.set(f"Сравнение с {target['id']}…"); self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0; generation = self.generation
        self.future = self.executor.submit(self.app.client.compare_average_sweeps, self.app.session_id, [s['id'] for s in self.sources], target['id'])
        self.app.root.after(50, lambda: self.finish(generation))

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.abandon(); self.info.set('Превышен срок ожидания 8 секунд; повторите по кнопке.'); return
        if not self.future.done(): self.app.root.after(50, lambda: self.finish(generation)); return
        future, self.future = self.future, None; self.button.configure(state='normal')
        try:
            self.data = deepcopy(validate_average(future.result(), self.expected, self.sources, self.target))
            target = self.data['target']; interval = target['interval']
            self.show_details(source_text(self.sources) + f"\nTarget {target['id']} · UTC {interval['started_at']} … {interval['ended_at']}\n" + '\n'.join(f"{key}: {self.data[key]['delta_db']:+g} dB @ {self.data[key]['frequency_hz']} Hz" for key in ('max_increase', 'min_change')))
            self.info.set('Результат зафиксирован. Голубой: target dBm; оранжевый: mean-power dBm. Нижний график: signed Δ dB, все точки.'); self.draw()
        except Exception as exc:
            self.data = None; self.info.set(f'Усреднённое сравнение недоступно: {exc}')

    def draw(self):
        self.canvas.delete('all')
        if not self.data: return
        data = self.data; frequencies = data['frequencies_hz']
        w, h = max(100, self.canvas.winfo_width()), max(160, self.canvas.winfo_height())
        for section, (traces, unit) in enumerate((([(data['target']['levels'], '#6cd8ff'), (data['reference']['levels'], '#ffb454')], 'dBm'), ([(data['delta_levels'], '#8ce99a')], 'Δ dB'))):
            values = [v for trace, _ in traces for v in trace] + ([0] if section else [])
            low, high = min(values), max(values); scale = max(abs(low), abs(high), 1)
            lower, upper = low / scale, high / scale
            top, bottom = section * h / 2 + 30, (section + 1) * h / 2 - 10
            self.canvas.create_text(10, section*h/2+5, anchor='nw', fill='white', text=f'{unit} · {low:+g} … {high:+g} · {frequencies[0]} … {frequencies[-1]} Hz')
            for trace, color in traces:
                points = []
                for i, value in enumerate(trace):
                    fraction = (frequencies[i]-frequencies[0])/(frequencies[-1]-frequencies[0]) if frequencies[-1] != frequencies[0] else 0.5
                    y = (value/scale-lower)/(upper-lower) if upper != lower else 0.5
                    points.extend((10+fraction*(w-20), bottom-y*(bottom-top)))
                if len(trace) > 1: self.canvas.create_line(*points, fill=color, width=2)
                if len(trace) == 1 or all(points[i:i+2] == points[:2] for i in range(2, len(points), 2)):
                    x, y = points[:2]
                    if color == '#ffb454': self.canvas.create_rectangle(x-6,y-6,x+6,y+6,outline=color,width=2)
                    else: self.canvas.create_oval(x-3,y-3,x+3,y+3,fill=color,outline=color)

    def close(self):
        self.clear(); self.closed = True; self.executor.shutdown(wait=False, cancel_futures=True)
