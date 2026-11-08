"""Three independent horizontal levels; measurements are computed only by core."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from time import monotonic
import json
from .sweep_average import number, validate_source, utc_interval
from .spectral_mask import same_json, SpectralMaskPanel
from .sweep_threshold import ThresholdPanel

COLORS = ('#ffb454', '#b69cff', '#8ce99a')
DASHES = ((8, 4), (3, 3), (10, 3, 2, 3))


def require(ok):
    if not ok: raise ValueError('Некорректные три горизонтальных порога')


def validate_levels(levels):
    require(isinstance(levels, list) and len(levels) == 3 and all(number(v) for v in levels))
    return levels


def validate_three_thresholds(data, expected, source, levels):
    session, stream = expected
    validate_levels(levels); validate_source(source, stream)
    grid = source['frequencies_hz']; n = len(grid)
    require(isinstance(data, dict) and data.get('session_id') == session and data.get('stream_id') == stream)
    require(data.get('method') == 'three_horizontal' and data.get('comparison') == 'strictly_greater')
    require(data.get('level_unit') == 'dBm' and data.get('delta_unit') == 'dB')
    require(type(data.get('points')) is int and data['points'] == n)
    require(same_json(data.get('frequencies_hz'), grid) and all(type(v) is int for v in data['frequencies_hz']))
    sweep = data.get('sweep'); require(isinstance(sweep, dict))
    for key in ('id', 'interval', 'levels', 'level_unit', 'settings'):
        require(key in sweep and same_json(sweep[key], source[key]))
    utc_interval(sweep['interval'])
    lines = data.get('lines'); require(isinstance(lines, list) and len(lines) == 3)
    for index, (line, level) in enumerate(zip(lines, levels), 1):
        require(isinstance(line, dict) and line.get('id') == f'line-{index}')
        require(number(line.get('level_dbm')) and line['level_dbm'] == level)
        excess = line.get('excess_levels')
        require(isinstance(excess, list) and len(excess) == n and all(number(v) for v in excess))
        for raw, delta in zip(source['levels'], excess):
            difference = raw - level
            require(number(difference) and difference == delta)
        require(type(line.get('exceedance_points')) is int and line['exceedance_points'] == sum(v > 0 for v in excess))
        # Validate the server's contiguous regions, never produce a client detector result.
        runs = []
        for i, value in enumerate(excess):
            if value > 0:
                if i == 0 or excess[i-1] <= 0: runs.append([i, i])
                else: runs[-1][1] = i
        regions = line.get('regions')
        require(isinstance(regions, list) and type(line.get('region_count')) is int
                and line['region_count'] == len(regions) == len(runs))
        for region, (start, end) in zip(regions, runs):
            require(isinstance(region, dict))
            peak = max(range(start, end+1), key=excess.__getitem__)
            for key, value in (('start_index', start), ('end_index', end), ('peak_index', peak),
                               ('start_frequency_hz', grid[start]), ('end_frequency_hz', grid[end]),
                               ('peak_frequency_hz', grid[peak]), ('points', end-start+1)):
                require(type(region.get(key)) is int and region[key] == value)
            require(number(region.get('peak_excess_db')) and region['peak_excess_db'] == excess[peak])
    return data


def result_text(data):
    sweep = data['sweep']
    text = [f"Session {data['session_id']} · Stream {data['stream_id']}",
            f"Sweep {sweep['id']} · UTC {sweep['interval']['started_at']} … {sweep['interval']['ended_at']}",
            'Settings: ' + json.dumps(sweep['settings'], ensure_ascii=False, allow_nan=False, sort_keys=True)]
    for line in data['lines']:
        coincident = [other['id'] for other in data['lines'] if other['id'] != line['id'] and other['level_dbm'] == line['level_dbm']]
        text.append(f"{line['id']}: {line['level_dbm']} dBm · строго > · превышения {line['exceedance_points']}/{data['points']} · областей {line['region_count']}" + (f" · совпадает с {', '.join(coincident)}" if coincident else ''))
        text.append(f"Превышение отдельно в dB; показаны первые {min(200, line['region_count'])} из {line['region_count']} областей.")
        for r in line['regions'][:200]:
            text.append(f"Индексы {r['start_index']}…{r['end_index']} · {r['start_frequency_hz']}…{r['end_frequency_hz']} Hz · {r['points']} точек · пик +{r['peak_excess_db']} dB @ {r['peak_frequency_hz']} Hz (индекс {r['peak_index']})")
    return '\n'.join(text)


class ThreeThresholdsPanel(ThresholdPanel):
    selected_source = SpectralMaskPanel.selected_source

    def __init__(self, app, notebook):
        self.app = app; self.scope = None; self.generation = 0
        self.future = self.retired = self.data = None; self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='three-thresholds')
        self._init_timers(app.root)
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8); notebook.add(frame, text='Три горизонтальных порога')
        ttk.Label(frame, text='НЕ НОРМАТИВ: −90, −80, −70 dBm — только примеры. Три независимых уровня в любом порядке, совпадения допустимы. Только чтение; без автоматического детектора.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.level_vars = [tk.StringVar(value=str(v)) for v in (-90, -80, -70)]
        for i, var in enumerate(self.level_vars, 1):
            ttk.Label(row, text=f'line-{i} dBm').pack(side='left'); ttk.Entry(row, textvariable=var, width=10).pack(side='left')
        self.button = ttk.Button(row, text='Анализировать последний dBm sweep', command=self.analyze); self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        self.info = tk.StringVar(value='Анализ запускается только по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.canvas = tk.Canvas(frame, background='#07131c', height=300); self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda event: self.draw())
        self.details = tk.Text(frame, height=8, state='disabled', wrap='none'); self.details.pack(fill='both', expand=True)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview); scroll.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scroll.set)
        for var in self.level_vars: var.trace_add('write', lambda *args: self.clear())

    def clear(self):
        super().clear(); self.source = self.submitted_levels = None
        self.info.set('Три независимых порога dBm; превышения отдельно в dB. Анализ только по кнопке.')

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            levels = validate_levels([float(var.get()) for var in self.level_vars])
            source = self.selected_source(); validate_source(source, self.app.selected_stream_id())
        except (ValueError, TypeError, KeyError) as exc:
            self.clear(); self.info.set(str(exc)); return
        self.clear(); self.source = source; self.submitted_levels = deepcopy(levels)
        self.expected = (self.app.session_id, self.app.selected_stream_id())
        self.info.set(f"Анализ sweep {source['id']}…"); self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0; generation = self.generation
        self.future = self.executor.submit(self.app.client.sweep_three_thresholds, self.app.session_id, source['id'], levels)
        self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation)

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.clear(); self.info.set('Превышен срок ожидания 8 секунд; повторите по кнопке.'); return
        if not self.future.done(): self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation); return
        future, self.future = self.future, None; self.button.configure(state='normal')
        try:
            self.data = deepcopy(validate_three_thresholds(future.result(), self.expected, self.source, self.submitted_levels))
            self.show_details(result_text(self.data)); self.info.set('Результат зафиксирован. Все исходные точки и три независимых порога dBm.'); self.draw()
        except Exception as exc:
            self.data = None; self.info.set(f'Три порога недоступны: {exc}. Остальные панели доступны.')

    def draw(self):
        self.canvas.delete('all')
        if not self.data: return
        data = self.data; grid = data['frequencies_hz']; raw = data['sweep']['levels']
        values = raw + [line['level_dbm'] for line in data['lines']]
        low, high = min(values), max(values); scale = max(abs(low), abs(high), 1)
        lower, upper = low/scale, high/scale
        w, h = max(160, self.canvas.winfo_width()), max(160, self.canvas.winfo_height())
        self.canvas.create_text(10, 5, anchor='nw', fill='#6cd8ff', text=f'Исходные dBm · {grid[0]} … {grid[-1]} Hz · {low} … {high} dBm')
        def y(v): return h-20 - ((v/scale-lower)/(upper-lower) if upper != lower else .5)*(h-110)
        points = []
        for hz, value in zip(grid, raw):
            x = 20 + ((hz-grid[0])/(grid[-1]-grid[0]) if grid[-1] != grid[0] else .5)*(w-40)
            points.extend((x, y(value)))
        for i, line in enumerate(data['lines']):
            color = COLORS[i]; level = line['level_dbm']; cy = y(level)
            equal = [other['id'] for other in data['lines'] if other['id'] != line['id'] and other['level_dbm'] == level]
            self.canvas.create_text(10, 23+19*i, anchor='nw', fill=color, text=f"{line['id']}: {level} dBm" + (f" · совпадает: {', '.join(equal)}" if equal else ''))
            self.canvas.create_line(20, cy, w-20, cy, fill=color, width=2, dash=DASHES[i])
            # Nested hollow markers retain all three identities even at identical levels.
            radius = 6+4*i
            self.canvas.create_rectangle(w/2-radius, cy-radius, w/2+radius, cy+radius, outline=color, width=2)
        if len(raw) > 1: self.canvas.create_line(*points, fill='#6cd8ff', width=2)
        if len(raw) == 1 or all(points[i:i+2] == points[:2] for i in range(2, len(points), 2)):
            x, cy = points[:2]; self.canvas.create_oval(x-3, cy-3, x+3, cy+3, fill='#6cd8ff', outline='#6cd8ff')

    def close(self):
        super().close()
        self.source = self.submitted_levels = None
