"""Read-only archived edge/level review, never a hardware diagnosis."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from time import monotonic
import json
from .sweep_average import identity, number, json_value, utc_interval, MAX_HZ
from .sweep_context import SweepContextPanel, scope_identity
from .spectral_mask import same_json

DISCLAIMER = 'Эвристика архивных уровней: не диагностирует перегрузку или помехи, не удаляет отсчёты. Исходные данные не изменяются.'


def require(ok):
    if not ok: raise ValueError('Некорректный отчёт качества sweep')


def validate_parameters(edge_margin_db, high_level_dbm):
    require(number(edge_margin_db) and 0 < edge_margin_db <= 200)
    require(number(high_level_dbm) and -200 <= high_level_dbm <= 100)
    return edge_margin_db, high_level_dbm


def validate_source(source, session, stream):
    require(scope_identity(session) and scope_identity(stream) and isinstance(source, dict))
    require(identity(source.get('id')) and source.get('stream_id') == stream)
    require('session_id' not in source or source['session_id'] == session)
    require(source.get('level_unit') == 'dBm' and 'settings' in source and json_value(source['settings'], True))
    require(len(json.dumps({key: source.get(key) for key in ('id', 'interval', 'level_unit', 'settings')}, ensure_ascii=False, allow_nan=False).encode('utf-8')) <= 65536)
    utc_interval(source.get('interval'))
    grid, levels = source.get('frequencies_hz'), source.get('levels')
    require(isinstance(grid, list) and 2 <= len(grid) <= 16384)
    require(all(type(v) is int and 0 <= v <= MAX_HZ for v in grid))
    require(all(a < b for a, b in zip(grid, grid[1:])))
    require(isinstance(levels, list) and len(levels) == len(grid) and all(number(v) for v in levels))
    return source


def validate_quality(data, expected, source=None):
    session, sweep_id, edge_margin, high_level = expected
    validate_parameters(edge_margin, high_level)
    require(scope_identity(session) and identity(sweep_id) and isinstance(data, dict))
    require(set(data) == {'session_id', 'stream_id', 'sweep', 'parameters', 'points', 'peak_index', 'peak_frequency_hz', 'peak_level_dbm', 'high_level_count', 'edge_assessments', 'flags', 'assessment', 'method', 'hardware_diagnosis', 'raw_modified'})
    require(data.get('session_id') == session and scope_identity(data.get('stream_id')))
    require(data.get('method') == 'archived_edge_level_review_v1' and data.get('hardware_diagnosis') is False and data.get('raw_modified') is False)
    params = data.get('parameters')
    require(isinstance(params, dict) and set(params) == {'edge_margin_db', 'high_level_dbm'})
    validate_parameters(params['edge_margin_db'], params['high_level_dbm'])
    require(params['edge_margin_db'] == edge_margin and params['high_level_dbm'] == high_level)
    sweep = data.get('sweep')
    require(isinstance(sweep, dict) and sweep.get('id') == sweep_id and sweep.get('level_unit') == 'dBm')
    require(set(sweep) == {'id', 'interval', 'level_unit', 'settings'})
    require('settings' in sweep and json_value(sweep['settings'], True)); utc_interval(sweep.get('interval'))
    require(len(json.dumps(sweep, ensure_ascii=False, allow_nan=False).encode('utf-8')) <= 65536)
    n, peak, count = data.get('points'), data.get('peak_index'), data.get('high_level_count')
    require(type(n) is int and 2 <= n <= 16384 and type(peak) is int and 0 <= peak < n)
    require(type(count) is int and 0 <= count <= n)
    require(type(data.get('peak_frequency_hz')) is int and 0 <= data['peak_frequency_hz'] <= MAX_HZ)
    require(number(data.get('peak_level_dbm')))
    require((count > 0) == (data['peak_level_dbm'] >= high_level))
    edges = data.get('edge_assessments'); require(isinstance(edges, list) and len(edges) == 2)
    flags = []
    for e, name, index, neighbor in zip(edges, ('first', 'last'), (0, n-1), (1, n-2)):
        require(isinstance(e, dict) and e.get('edge') == name)
        require(set(e) == {'edge', 'index', 'frequency_hz', 'level_dbm', 'neighbor_index', 'neighbor_level_dbm', 'delta_db', 'flagged'})
        require(type(e.get('index')) is int and e['index'] == index and type(e.get('neighbor_index')) is int and e['neighbor_index'] == neighbor)
        require(type(e.get('frequency_hz')) is int and 0 <= e['frequency_hz'] <= MAX_HZ)
        require(all(number(e.get(key)) for key in ('level_dbm', 'neighbor_level_dbm', 'delta_db')))
        require(e['delta_db'] == e['level_dbm'] - e['neighbor_level_dbm'])
        require(type(e.get('flagged')) is bool and e['flagged'] == (e['delta_db'] >= edge_margin))
        require(max(e['level_dbm'], e['neighbor_level_dbm']) <= data['peak_level_dbm'])
        if e['flagged']: flags.append(name + '_edge_jump')
    require(edges[0]['frequency_hz'] < edges[1]['frequency_hz'])
    require(edges[0]['frequency_hz'] <= data['peak_frequency_hz'] <= edges[1]['frequency_hz'])
    known = {}
    for e in edges:
        for index, level in ((e['index'], e['level_dbm']), (e['neighbor_index'], e['neighbor_level_dbm'])):
            require(index not in known or known[index] == level)
            known[index] = level
        if peak == e['index']:
            require(data['peak_frequency_hz'] == e['frequency_hz'])
    if peak in known: require(data['peak_level_dbm'] == known[peak])
    known[peak] = data['peak_level_dbm']
    require(all(index >= peak or level < data['peak_level_dbm'] for index, level in known.items()))
    known_frequencies = {e['index']: e['frequency_hz'] for e in edges}
    require(peak not in known_frequencies or known_frequencies[peak] == data['peak_frequency_hz'])
    known_frequencies[peak] = data['peak_frequency_hz']
    ordered = sorted(known_frequencies.items())
    require(all(frequency_b - frequency_a >= index_b - index_a
                for (index_a, frequency_a), (index_b, frequency_b) in zip(ordered, ordered[1:])))
    require(sum(v >= high_level for v in known.values()) <= count <= sum(v >= high_level for v in known.values()) + n - len(known))
    if len(known) == n: require(peak == max(range(n), key=known.__getitem__))
    if count: flags.append('level_threshold')
    require(data.get('flags') == flags and data.get('assessment') == ('review' if flags else 'no_flags'))
    if source is not None:
        validate_source(source, session, data['stream_id'])
        require(source['id'] == sweep_id and len(source['levels']) == n)
        require(same_json(sweep['settings'], source['settings']) and utc_interval(sweep['interval']) == utc_interval(source['interval']))
        levels, grid = source['levels'], source['frequencies_hz']
        require(peak == max(range(n), key=levels.__getitem__) and data['peak_frequency_hz'] == grid[peak] and data['peak_level_dbm'] == levels[peak])
        require(count == sum(v >= high_level for v in levels))
        for e in edges:
            require(e['frequency_hz'] == grid[e['index']] and e['level_dbm'] == levels[e['index']] and e['neighbor_level_dbm'] == levels[e['neighbor_index']])
    return data


def result_text(data):
    sweep, p = data['sweep'], data['parameters']
    return '\n'.join([DISCLAIMER, f"Session {data['session_id']} · Stream {data['stream_id']} · Sweep {sweep['id']}",
        f"UTC {sweep['interval']['started_at']} … {sweep['interval']['ended_at']}",
        f"Край ≥ {p['edge_margin_db']} dB · уровень ≥ {p['high_level_dbm']} dBm · {data['high_level_count']}/{data['points']} отсчётов",
        f"Пик {data['peak_level_dbm']} dBm @ {data['peak_frequency_hz']} Hz (индекс {data['peak_index']})",
        f"Оценка: {data['assessment']} · flags: {', '.join(data['flags']) or 'none'}",
        *[f"{e['edge']}: индекс {e['index']} · {e['frequency_hz']} Hz · {e['level_dbm']} dBm · сосед {e['neighbor_level_dbm']} dBm · Δ {e['delta_db']} dB · flagged={e['flagged']}" for e in data['edge_assessments']],
        'Settings: ' + json.dumps(sweep['settings'], ensure_ascii=False, allow_nan=False)])


class SweepQualityPanel(SweepContextPanel):
    """Reuse context selection, scope invalidation and timer ownership, not analysis."""
    def __init__(self, app, notebook):
        self.app = app; self.scope = None; self.generation = 0; self.closed = False
        self.future = self.retired = self.data = self.source = self.chosen_source = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sweep-quality')
        self._init_timers(app.root)
        tk, ttk = app.tk, app.ttk
        frame = ttk.Frame(notebook, padding=8); notebook.add(frame, text='Качество sweep')
        ttk.Label(frame, text=DISCLAIMER, wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.anchor = tk.StringVar(); self.edge_margin = tk.StringVar(value='20'); self.high_level = tk.StringVar(value='0')
        self.anchors = ttk.Combobox(row, textvariable=self.anchor, state='readonly', width=35); self.anchors.pack(side='left')
        for label, var in (('Край dB (0…200]', self.edge_margin), ('Уровень dBm [-200…100]', self.high_level)):
            ttk.Label(row, text=label).pack(side='left'); ttk.Entry(row, textvariable=var, width=7).pack(side='left')
        self.button = ttk.Button(row, text='Проверить выбранный sweep', command=self.analyze); self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        self.retained_info = tk.StringVar(); ttk.Label(frame, textvariable=self.retained_info).pack(fill='x')
        self.info = tk.StringVar(); ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=14, state='disabled', wrap='word'); self.details.pack(fill='both', expand=True)
        self.anchor.trace_add('write', self.choose_anchor)
        for var in (self.edge_margin, self.high_level): var.trace_add('write', lambda *args: self.clear())
        self.server_trace = app.server_var.trace_add('write', self.disconnect)
        self.clear()

    def sources(self):
        if getattr(self, 'snapshot_unavailable', False): return []
        snap = self.app.snapshot; stream = self.app.selected_stream_id()
        if self.app.client is None or snap is None or (snap.session or {}).get('id') != self.app.session_id or snap.spectrum_stream_id != stream: return []
        result = []
        for source in snap.sweeps:
            try: validate_source(source, self.app.session_id, stream)
            except (ValueError, TypeError, KeyError): continue
            result.append(source)
        return result

    def clear(self):
        self.abandon(); self.data = self.source = None; self.show_details('')
        self.info.set('Выберите архивный sweep и запустите проверку по кнопке.')
        if getattr(self, 'snapshot_unavailable', False): self.button.configure(state='disabled')

    def analyze(self):
        self.update()
        if self.closed or self.future is not None or getattr(self, 'snapshot_unavailable', False): return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            params = validate_parameters(float(self.edge_margin.get()), float(self.high_level.get()))
            source = deepcopy(self.chosen_source)
            validate_source(source, self.app.session_id, self.app.selected_stream_id())
            require(source['id'] == self.anchor.get())
        except (ValueError, TypeError, KeyError) as exc:
            self.clear(); self.info.set(str(exc)); return
        self.clear(); self.source = source
        self.expected = (self.app.session_id, source['id'], *params)
        self.info.set('Проверка архива…'); self.button.configure(state='disabled')
        self.deadline = monotonic()+8
        self.future = self.executor.submit(self.app.client.sweep_quality, *self.expected)
        self._schedule_after(self.app.root, 'analysis', 50, self.finish, self.generation)

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.clear(); self.info.set('Превышен срок ожидания 8 секунд.'); return
        if not self.future.done(): self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation); return
        future, self.future = self.future, None; self.button.configure(state='normal')
        try:
            self.data = deepcopy(validate_quality(future.result(), self.expected, self.source))
            self.show_details(result_text(self.data)); self.info.set(DISCLAIMER)
        except Exception as exc:
            self.data = None; self.info.set(f'Отчёт недоступен: {exc}. Остальные панели доступны.')
