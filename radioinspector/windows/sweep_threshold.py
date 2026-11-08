"""Explicit read-only archived threshold view; the core alone computes medians."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic
from .tk_timers import TkTimerOwner


def validate_parameters(window, margin):
    if type(window) is not int or not 3 <= window <= 101 or window % 2 != 1:
        raise ValueError('Окно: нечётное целое 3…101')
    if type(margin) not in (int, float) or not 0 <= margin <= 120 or not isfinite(margin):
        raise ValueError('Запас: конечное число 0…120 dB')
    return window, margin


def validate_threshold(data, expected, source=None):
    def require(ok):
        if not ok:
            raise ValueError('Некорректный ответ адаптивного порога')
    def number(v):
        try:
            return type(v) in (int, float) and isfinite(v)
        except OverflowError:
            return False
    def json_value(v, null=False):
        if v is None: return null
        if type(v) in (str, bool): return True
        if type(v) in (int, float): return number(v)
        if isinstance(v, list): return all(json_value(x, null) for x in v)
        if isinstance(v, dict): return all(isinstance(k, str) and json_value(x, null) for k, x in v.items())
        return False
    session, stream, sweep_id, window, margin = expected
    validate_parameters(window, margin)
    require(isinstance(data, dict))
    require(data.get('session_id') == session and data.get('stream_id') == stream)
    require(data.get('level_unit') == 'dBm' and data.get('delta_unit') == 'dB')
    n = data.get('points')
    require(type(n) is int and 3 <= n <= 16384)
    params = data.get('parameters')
    require(isinstance(params, dict))
    require(params.get('method') == 'rolling_median' and params.get('edge_policy') == 'clip'
            and params.get('comparison') == 'strictly_greater')
    validate_parameters(params.get('window_points'), params.get('margin_db'))
    require(params['window_points'] == window and params['margin_db'] == margin)
    for key in ('frequencies_hz', 'baseline_levels', 'threshold_levels', 'excess_levels'):
        require(isinstance(data.get(key), list) and len(data[key]) == n and all(number(v) for v in data[key]))
    frequencies = data['frequencies_hz']
    require(all(type(v) is int and v >= 0 for v in frequencies))
    require(all(a <= b for a, b in zip(frequencies, frequencies[1:])))
    sweep = data.get('sweep')
    require(isinstance(sweep, dict) and sweep.get('id') == sweep_id and sweep.get('level_unit') == 'dBm')
    levels = sweep.get('levels')
    require(isinstance(levels, list) and len(levels) == n and all(number(v) for v in levels))
    settings = sweep.get('settings')
    require(isinstance(settings, dict))
    recorded = {k: v for k, v in settings.items() if k != 'sequence'}
    require(bool(recorded) and json_value(recorded))
    require('sequence' not in settings or json_value(settings['sequence'], True))
    interval = sweep.get('interval')
    require(isinstance(interval, dict))
    times = []
    for field in ('started_at', 'ended_at'):
        value = interval.get(field)
        require(isinstance(value, str))
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(stamp.utcoffset() == timedelta(0))
        times.append(stamp)
    require(times[0] <= times[1])
    if source is not None:
        require(isinstance(source, dict))
        for key in ('id', 'levels', 'level_unit', 'interval'):
            require(sweep[key] == source.get(key))
        require(frequencies == source.get('frequencies_hz'))
        source_settings = source.get('settings')
        require(isinstance(source_settings, dict))
        require(recorded == {k: v for k, v in source_settings.items() if k != 'sequence'})
    excess = data['excess_levels']
    for i in range(n):
        threshold = data['baseline_levels'][i] + margin
        delta = levels[i] - data['threshold_levels'][i]
        require(isfinite(threshold) and isfinite(delta))
        require(threshold == data['threshold_levels'][i])
        require(delta == excess[i])
        require((delta > 0) == (excess[i] > 0))
    require(type(data.get('exceedance_points')) is int and data['exceedance_points'] == sum(v > 0 for v in excess))
    regions = data.get('regions')
    require(isinstance(regions, list) and type(data.get('region_count')) is int and data['region_count'] == len(regions))
    runs = []
    for i, v in enumerate(excess):
        if v > 0:
            if i == 0 or excess[i - 1] <= 0: runs.append([i, i])
            else: runs[-1][1] = i
    require(len(runs) == len(regions))
    for item, (start, end) in zip(regions, runs):
        require(isinstance(item, dict))
        peak = max(range(start, end + 1), key=excess.__getitem__)
        for key, value in (('start_index', start), ('end_index', end), ('peak_index', peak),
                           ('start_frequency_hz', frequencies[start]), ('end_frequency_hz', frequencies[end]),
                           ('peak_frequency_hz', frequencies[peak]), ('points', end - start + 1)):
            require(type(item.get(key)) is int and item[key] == value)
        require(number(item.get('peak_excess_db')) and item['peak_excess_db'] == excess[peak])
    return data


def threshold_text(data):
    sweep, p = data['sweep'], data['parameters']
    lines = [f"Sweep: {sweep['id']} · UTC {sweep['interval']['started_at']} … {sweep['interval']['ended_at']}",
             f"Окно {p['window_points']} точек · запас {p['margin_db']:g} dB · превышения: {data['exceedance_points']}/{data['points']} · областей: {data['region_count']}"]
    if data['region_count'] > 200:
        lines.append(f"Показаны первые 200 из {data['region_count']} областей; график содержит все точки.")
    for r in data['regions'][:200]:
        lines.append(f"Индексы {r['start_index']}…{r['end_index']} · {r['start_frequency_hz']}…{r['end_frequency_hz']} Hz · {r['points']} точек · пик +{r['peak_excess_db']:g} dB @ {r['peak_frequency_hz']} Hz (индекс {r['peak_index']})")
    return '\n'.join(lines)


class ThresholdPanel(TkTimerOwner):
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = self.retired = self.data = None
        self.closed = False
        self._init_timers(app.root)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sweep-threshold')
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Адаптивный порог')
        ttk.Label(frame, text='Локальная медиана + запас. Не калиброванный шумовой фон и не достоверность обнаружения. Только чтение; результат фиксируется до следующего явного анализа.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.window = tk.StringVar(value='21'); self.margin = tk.StringVar(value='6')
        for label, var in (('Окно (нечётное 3…101)', self.window), ('Запас (0…120 dB)', self.margin)):
            ttk.Label(row, text=label).pack(side='left'); ttk.Entry(row, textvariable=var, width=8).pack(side='left')
        self.button = ttk.Button(row, text='Анализировать последний dBm sweep', command=self.analyze); self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        self.info = tk.StringVar(value='Анализ запускается только по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.canvas = tk.Canvas(frame, background='#07131c', height=260); self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda event: self.draw())
        self.details = tk.Text(frame, height=8, state='disabled', wrap='none')
        self.details.pack(fill='both', expand=True)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview)
        scroll.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scroll.set)

    def show_details(self, value):
        self.details.configure(state='normal'); self.details.delete('1.0', 'end')
        self.details.insert('1.0', value); self.details.configure(state='disabled')

    def update(self):
        if self.closed: return
        scope = (id(self.app.client), self.app.session_id, self.app.selected_stream_id())
        if scope != self.scope:
            self.scope = scope
            self.clear()

    def abandon(self):
        if self.future is not None:
            self.future.cancel()
            if not self.future.done(): self.retired = self.future
            self.future = None
        self.generation += 1
        self.button.configure(state='normal')

    def clear(self):
        self.abandon(); self.data = None; self.canvas.delete('all'); self.show_details('')
        self.info.set('Анализ запускается только по кнопке. Исходные и порог: dBm; превышение: dB.')

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        snapshot = self.app.snapshot
        if (self.app.client is None or not self.app.session_id or not self.app.selected_stream_id()
                or (snapshot.session or {}).get('id') != self.app.session_id
                or snapshot.spectrum_stream_id != self.app.selected_stream_id()): return
        sweeps = [s for s in snapshot.sweeps if isinstance(s, dict)
                  and s.get('level_unit') == 'dBm'
                  and s.get('stream_id') == self.app.selected_stream_id()
                  and isinstance(s.get('id'), str) and bool(s['id'].strip())]
        if not sweeps:
            self.info.set('Нет архивного dBm sweep в выбранном потоке.'); return
        try:
            window, margin = validate_parameters(int(self.window.get()), float(self.margin.get()))
        except (TypeError, ValueError) as exc:
            self.info.set(f'Параметры: {exc}'); return
        self.clear()
        self.source = deepcopy(sweeps[-1])
        sweep_id = self.source['id']
        self.expected = (self.app.session_id, self.app.selected_stream_id(), sweep_id, window, margin)
        self.info.set(f'Анализ sweep {sweep_id}…')
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        self.future = self.executor.submit(self.app.client.sweep_threshold, self.app.session_id, sweep_id, window, margin)
        self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation)

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.abandon(); self.info.set('Порог: превышен срок ожидания 8 секунд; повторите по кнопке.'); return
        if not self.future.done():
            self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation); return
        future, self.future = self.future, None
        self.button.configure(state='normal')
        try:
            self.data = validate_threshold(future.result(), self.expected, self.source)
            self.info.set('Результат зафиксирован для указанного sweep. Голубой: исходные dBm; оранжевый: порог dBm.')
            self.show_details(threshold_text(self.data)); self.draw()
        except Exception as exc:
            self.data = None
            self.info.set('Порог недоступен (404): старый сервер или sweep не найден. Остальные панели доступны.' if getattr(exc, 'status', None) == 404 else f'Порог недоступен: {exc}')

    def draw(self):
        self.canvas.delete('all')
        if not self.data: return
        frequencies = self.data['frequencies_hz']
        traces = [(self.data['sweep']['levels'], '#6cd8ff'), (self.data['threshold_levels'], '#ffb454')]
        values = traces[0][0] + traces[1][0]
        low, high = min(values), max(values)
        scale = max(abs(low), abs(high), 1)
        lower, upper = low / scale, high / scale
        span = upper - lower or 1
        w, h = max(100, self.canvas.winfo_width()), max(100, self.canvas.winfo_height())
        self.canvas.create_text(10, 10, anchor='nw', fill='white', text=f'dBm · {low:g} … {high:g} · {frequencies[0]} … {frequencies[-1]} Hz')
        for values, color in traces:
            points = []
            for i, value in enumerate(values):
                fraction = ((frequencies[i] - frequencies[0]) / (frequencies[-1] - frequencies[0]) if frequencies[-1] != frequencies[0] else 0.5)
                points.extend((10 + fraction * (w - 20), h - 15 - (value / scale - lower) * (h - 50) / span))
            self.canvas.create_line(*points, fill=color, width=2)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._close_timers()
        for future in (self.future, self.retired):
            if future is not None:
                future.cancel()
        self.future = self.retired = self.data = None
        self.generation += 1
        self.executor.shutdown(wait=False, cancel_futures=True)
