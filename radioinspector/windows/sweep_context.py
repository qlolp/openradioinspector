"""Explicit read-only archived N1/N2 context, with frozen anchor provenance."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from time import monotonic
import json
from .sweep_average import validate_source, utc_interval
from .spectral_mask import same_json
from .sweep_threshold import ThresholdPanel


def require(ok):
    if not ok: raise ValueError('Некорректный архивный контекст sweep')


def scope_identity(value):
    """Session/stream IDs have no sweep-source length limit."""
    return (isinstance(value, str) and bool(value.strip())
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def validate_scoped_source(source, session, stream):
    require(scope_identity(session) and scope_identity(stream))
    settings = validate_source(source, stream)
    require('session_id' not in source or source['session_id'] == session)
    return settings


def validate_counts(before, after):
    require(all(type(v) is int and 0 <= v <= 16 for v in (before, after)))
    return before, after


def validate_context(data, expected, source, before, after):
    validate_counts(before, after)
    session, stream = expected
    settings = validate_scoped_source(source, session, stream)
    require(isinstance(data, dict) and data.get('session_id') == session and data.get('stream_id') == stream)
    require(data.get('anchor_id') == source['id'] and data.get('order') == 'started_at_then_id')
    require(data.get('level_unit') == 'dBm')
    grid = source['frequencies_hz']; n = len(grid)
    require(type(data.get('points')) is int and data['points'] == n)
    require(same_json(data.get('frequencies_hz'), grid) and all(type(v) is int for v in data['frequencies_hz']))
    require(same_json(data.get('requested'), {'before': before, 'after': after}))
    for group in ('requested', 'available', 'missing'):
        values = data.get(group)
        require(isinstance(values, dict) and set(values) == {'before', 'after'})
        validate_counts(values['before'], values['after'])
    available, missing = data['available'], data['missing']
    for side, count in (('before', before), ('after', after)):
        require(available[side] <= count and missing[side] == count-available[side])
    require(type(data.get('complete')) is bool and data['complete'] == (not any(missing.values())))
    items = data.get('items')
    require(isinstance(items, list) and len(items) == available['before']+1+available['after'] and n*len(items) <= 131072)
    seen = set(); previous = None
    for offset, item in zip(range(-available['before'], available['after']+1), items):
        require(isinstance(item, dict) and type(item.get('offset')) is int and item['offset'] == offset)
        actual = dict(item, stream_id=stream, frequencies_hz=grid)
        require(same_json(validate_source(actual, stream), settings))
        require(item['id'] not in seen); seen.add(item['id'])
        key = (utc_interval(item['interval'])[0], item['id'].encode('utf-8'))
        require(previous is None or previous < key); previous = key
        if offset == 0:
            require(utc_interval(item['interval']) == utc_interval(source['interval']))
            for field in ('id', 'levels', 'level_unit', 'settings'):
                require(same_json(item.get(field), source[field]))
    return data


def result_text(data):
    lines = [f"Session {data['session_id']} · Stream {data['stream_id']} · Anchor {data['anchor_id']}",
             f"N1/N2 requested {data['requested']['before']}/{data['requested']['after']} · available {data['available']['before']}/{data['available']['after']} · missing {data['missing']['before']}/{data['missing']['after']} · complete={data['complete']}"]
    for item in data['items']:
        lines.append(f"{item['offset']:+d} · {item['id']} · UTC {item['interval']['started_at']} … {item['interval']['ended_at']} · dBm\nSettings: " + json.dumps(item['settings'], ensure_ascii=False, allow_nan=False, sort_keys=True))
    return '\n'.join(lines)


class SweepContextPanel(ThresholdPanel):
    def __init__(self, app, notebook):
        self.app = app; self.scope = None; self.generation = 0
        self.future = self.retired = self.data = self.source = self.chosen_source = None; self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sweep-context')
        tk, ttk = app.tk, app.ttk
        frame = ttk.Frame(notebook, padding=8); notebook.add(frame, text='Архивный контекст N1/N2')
        ttk.Label(frame, text='Разовый снимок архива вокруг выбранного sweep. N1/N2=2 — примеры. Не автоматический захват обнаружения; только чтение.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.anchor = tk.StringVar(); self.before = tk.StringVar(value='2'); self.after = tk.StringVar(value='2')
        self.anchors = ttk.Combobox(row, textvariable=self.anchor, state='readonly', width=35); self.anchors.pack(side='left')
        for title, var in (('N1 до (0…16)', self.before), ('N2 после (0…16)', self.after)):
            ttk.Label(row, text=title).pack(side='left'); ttk.Entry(row, textvariable=var, width=5).pack(side='left')
        self.button = ttk.Button(row, text='Зафиксировать / загрузить', command=self.analyze); self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        self.retained_info = tk.StringVar(); ttk.Label(frame, textvariable=self.retained_info, wraplength=1050).pack(fill='x')
        self.info = tk.StringVar(); ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.selected = tk.StringVar(); self.selection = ttk.Combobox(frame, textvariable=self.selected, state='readonly', width=70); self.selection.pack(fill='x')
        self.selection.bind('<<ComboboxSelected>>', lambda event: self.draw())
        self.canvas = tk.Canvas(frame, background='#07131c', height=260); self.canvas.pack(fill='both', expand=True); self.canvas.bind('<Configure>', lambda event: self.draw())
        self.details = tk.Text(frame, height=9, state='disabled', wrap='none'); self.details.pack(fill='both', expand=True)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview); scroll.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scroll.set)
        self.anchor.trace_add('write', self.choose_anchor)
        for var in (self.before, self.after): var.trace_add('write', lambda *args: self.clear())
        self.server_trace = app.server_var.trace_add('write', self.disconnect)
        self.clear()

    def sources(self):
        if getattr(self, 'snapshot_unavailable', False): return []
        snap = self.app.snapshot; stream = self.app.selected_stream_id()
        if (self.app.client is None or not self.app.session_id or not stream or snap is None
                or (snap.session or {}).get('id') != self.app.session_id or snap.spectrum_stream_id != stream): return []
        result = []
        for item in snap.sweeps:
            try: validate_scoped_source(item, self.app.session_id, stream)
            except (ValueError, TypeError, KeyError): continue
            result.append(item)
        return result

    def disconnect(self, *args):
        """A failed refresh invalidates even retained choices until a fresh snapshot arrives."""
        self.snapshot_unavailable = True
        self.invalidate_choice()
        self.anchor.set('')
        self.anchors.configure(values=[])

    def snapshot_refreshed(self):
        self.snapshot_unavailable = False
        self.button.configure(state='disabled' if self.future is not None else 'normal')
        self.update()

    def choose_anchor(self, *args):
        self.invalidate_choice()
        matches = [s for s in self.sources() if s['id'] == self.anchor.get()]
        if len(matches) == 1: self.chosen_source = deepcopy(matches[0])

    def invalidate_choice(self, *args):
        self.chosen_source = None
        if hasattr(self, 'retained_info'): self.retained_info.set('')
        self.clear()

    def update(self):
        if self.closed: return
        scope = (id(self.app.client), self.app.session_id, self.app.selected_stream_id())
        if scope != self.scope:
            self.scope = scope; self.invalidate_choice(); self.anchor.set('')
        sources = self.sources(); selected = self.anchor.get()
        chosen = getattr(self, 'chosen_source', None)
        if chosen is not None and chosen['id'] != selected:
            self.invalidate_choice(); chosen = None
        matches = [s for s in sources if s['id'] == selected]
        if chosen is None and len(matches) == 1:
            chosen = self.chosen_source = deepcopy(matches[0])
        ids = [s['id'] for s in sources]
        retained = chosen is not None and chosen['id'] not in ids
        if retained: ids.append(chosen['id'])
        self.anchors.configure(values=ids)
        if hasattr(self, 'retained_info'):
            self.retained_info.set(f"Выбран anchor {chosen['id']} · сохранённый снимок (вне текущей страницы)" if retained else '')
        # The chosen full source survives polling/count edits independently of the result pin.

    def clear(self):
        super().clear(); self.source = None
        self.selection.configure(values=[]); self.selected.set('')
        self.info.set('Выберите anchor и загрузите N1/N2 явно. Автоматический захват не реализован.')
        if getattr(self, 'snapshot_unavailable', False):
            self.button.configure(state='disabled')
            self.info.set('Нет свежего снимка сервера. Загрузка недоступна до успешного обновления.')

    def analyze(self):
        self.update()
        if self.closed or self.future is not None or getattr(self, 'snapshot_unavailable', False): return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            counts = validate_counts(int(self.before.get()), int(self.after.get()))
            source = deepcopy(getattr(self, 'chosen_source', None))
            require(isinstance(source, dict) and source['id'] == self.anchor.get())
            validate_scoped_source(source, self.app.session_id, self.app.selected_stream_id())
        except (ValueError, TypeError, KeyError) as exc:
            self.clear(); self.info.set(str(exc)); return
        self.clear(); self.source = source; self.counts = counts
        self.expected = (self.app.session_id, self.app.selected_stream_id())
        self.info.set(f"Зафиксирован anchor {source['id']}; загрузка…"); self.button.configure(state='disabled')
        self.deadline = monotonic()+8; generation = self.generation
        self.future = self.executor.submit(self.app.client.sweep_context, self.app.session_id, source['id'], *counts)
        self.app.root.after(50, lambda: self.finish(generation))

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.clear(); self.info.set('Превышен срок ожидания 8 секунд.'); return
        if not self.future.done(): self.app.root.after(50, lambda: self.finish(generation)); return
        future, self.future = self.future, None; self.button.configure(state='normal')
        try:
            self.data = deepcopy(validate_context(future.result(), self.expected, self.source, *self.counts))
            self.selection.configure(values=[f"{s['offset']:+d} · {s['id']}" for s in self.data['items']])
            self.selection.current(self.data['available']['before'])
            self.show_details(result_text(self.data)); self.info.set('Архивный снимок зафиксирован. Выберите строку для полного исходного спектра dBm.'); self.draw()
        except Exception as exc:
            self.data = None; self.info.set(f'Контекст недоступен: {exc}. Остальные панели доступны.')

    def draw(self):
        self.canvas.delete('all')
        if not self.data: return
        index = self.selection.current()
        if not isinstance(index, int) or not 0 <= index < len(self.data['items']): return
        item = self.data['items'][index]; grid = self.data['frequencies_hz']; levels = item['levels']
        low, high = min(levels), max(levels); scale = max(abs(low), abs(high), 1); lower, upper = low/scale, high/scale
        w, h = max(160, self.canvas.winfo_width()), max(160, self.canvas.winfo_height())
        self.canvas.create_text(10, 5, anchor='nw', fill='#6cd8ff', text=f"Anchor {self.data['anchor_id']} · выбран {item['offset']:+d} {item['id']} · dBm\n{grid[0]} … {grid[-1]} Hz · {low} … {high} dBm")
        points = []
        for hz, level in zip(grid, levels):
            x = 20+((hz-grid[0])/(grid[-1]-grid[0]) if grid[-1] != grid[0] else .5)*(w-40)
            y = h-20-((level/scale-lower)/(upper-lower) if upper != lower else .5)*(h-75)
            points.extend((x,y))
        if len(levels) > 1: self.canvas.create_line(*points, fill='#6cd8ff', width=2)
        if len(levels) == 1 or all(points[i:i+2] == points[:2] for i in range(2,len(points),2)):
            x,y = points[:2]; self.canvas.create_oval(x-3,y-3,x+3,y+3,fill='#6cd8ff',outline='#6cd8ff')

    def close(self):
        self.clear(); self.closed = True
        if hasattr(self, 'server_trace'): self.app.server_var.trace_remove('write', self.server_trace)
        self.executor.shutdown(wait=False, cancel_futures=True)
