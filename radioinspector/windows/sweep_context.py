"""Explicit read-only archived N1/N2 context, with frozen anchor provenance."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from time import monotonic
import json
import os
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
        self.result_json = None; self.exporting = False; self.occupancy_threshold = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sweep-context')
        self._init_timers(app.root)
        tk, ttk = app.tk, app.ttk
        frame = ttk.Frame(notebook, padding=8); notebook.add(frame, text='Архивный контекст N1/N2')
        ttk.Label(frame, text='Разовый снимок архива вокруг выбранного sweep. N1/N2=2 — примеры. Не автоматический захват обнаружения; только чтение.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.anchor = tk.StringVar(); self.before = tk.StringVar(value='2'); self.after = tk.StringVar(value='2')
        self.threshold = tk.StringVar(value='-80')
        self.anchors = ttk.Combobox(row, textvariable=self.anchor, state='readonly', width=35); self.anchors.pack(side='left')
        for title, var in (('N1 до (0…16)', self.before), ('N2 после (0…16)', self.after)):
            ttk.Label(row, text=title).pack(side='left'); ttk.Entry(row, textvariable=var, width=5).pack(side='left')
        self.button = ttk.Button(row, text='Зафиксировать / загрузить', command=self.analyze); self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Label(row, text='Порог dBm').pack(side='left')
        ttk.Entry(row, textvariable=self.threshold, width=7).pack(side='left')
        self.occupancy_button = ttk.Button(row, text='Занятость', command=lambda: self.analyze(True)); self.occupancy_button.pack(side='left')
        self.export_button = ttk.Button(row, text='Экспорт JSON…', command=self.export_result, state='disabled'); self.export_button.pack(side='left')
        self.retained_info = tk.StringVar(); ttk.Label(frame, textvariable=self.retained_info, wraplength=1050).pack(fill='x')
        self.info = tk.StringVar(); ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.selected = tk.StringVar(); self.selection = ttk.Combobox(frame, textvariable=self.selected, state='readonly', width=70); self.selection.pack(fill='x')
        self.selection.bind('<<ComboboxSelected>>', lambda event: self.draw())
        self.canvas = tk.Canvas(frame, background='#07131c', height=260); self.canvas.pack(fill='both', expand=True); self.canvas.bind('<Configure>', lambda event: self.draw())
        self.details = tk.Text(frame, height=9, state='disabled', wrap='none'); self.details.pack(fill='both', expand=True)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview); scroll.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scroll.set)
        self.anchor.trace_add('write', self.choose_anchor)
        for var in (self.before, self.after, self.threshold): var.trace_add('write', lambda *args: self.clear())
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
        self.refresh_controls()

    def refresh_controls(self):
        if hasattr(self, 'occupancy_button'):
            self.occupancy_button.configure(state='disabled' if self.closed or self.future is not None or getattr(self, 'snapshot_unavailable', False) else 'normal')
        if hasattr(self, 'export_button'):
            self.export_button.configure(state='normal' if not self.closed and self.future is None and self.data is not None and getattr(self, 'result_json', None) is not None and not getattr(self, 'exporting', False) else 'disabled')

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
        self.result_json = None; self.occupancy_threshold = None
        self.selection.configure(values=[]); self.selected.set('')
        self.info.set('Выберите anchor и загрузите N1/N2 явно. Автоматический захват не реализован.')
        if getattr(self, 'snapshot_unavailable', False):
            self.button.configure(state='disabled')
            self.info.set('Нет свежего снимка сервера. Загрузка недоступна до успешного обновления.')
        self.refresh_controls()

    def analyze(self, with_occupancy=False):
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
            threshold = None
            if with_occupancy:
                from .sweep_occupancy import finite_number
                threshold = float(self.threshold.get())
                require(finite_number(threshold))
        except (ValueError, TypeError, KeyError) as exc:
            self.clear(); self.info.set(str(exc)); return
        self.clear(); self.source = source; self.counts = counts
        self.occupancy_threshold = threshold
        self.expected = (self.app.session_id, self.app.selected_stream_id())
        self.info.set(f"Зафиксирован anchor {source['id']}; загрузка…"); self.button.configure(state='disabled')
        self.deadline = monotonic()+8; generation = self.generation
        if with_occupancy:
            self.future = self.executor.submit(self.app.client.sweep_occupancy, self.app.session_id, source['id'], *counts, threshold)
        else:
            self.future = self.executor.submit(self.app.client.sweep_context, self.app.session_id, source['id'], *counts)
        self.refresh_controls()
        self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation)

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.clear(); self.info.set('Превышен срок ожидания 8 секунд.'); return
        if not self.future.done(): self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation); return
        future, self.future = self.future, None; self.button.configure(state='normal')
        try:
            data = future.result()
            if self.occupancy_threshold is not None:
                from .sweep_occupancy import validate_occupancy
                validate_occupancy(data, self.expected, self.source, *self.counts, self.occupancy_threshold)
                self.result_json = getattr(data, 'data_json', None)
            self.data = deepcopy(validate_context(data, self.expected, self.source, *self.counts))
            self.selection.configure(values=[f"{s['offset']:+d} · {s['id']}" for s in self.data['items']])
            self.selection.current(self.data['available']['before'])
            self.show_details(result_text(self.data)); self.info.set('Архивный снимок зафиксирован. Выберите строку для полного исходного спектра dBm.'); self.draw()
            if self.occupancy_threshold is not None:
                from .sweep_occupancy import occupancy_text
                self.show_details(occupancy_text(self.data) + '\n\n' + result_text(self.data))
        except Exception as exc:
            self.data = None; self.result_json = None; self.info.set(f'Контекст недоступен: {exc}. Остальные панели доступны.')
        self.refresh_controls()

    def export_result(self):
        from tkinter import TclError, filedialog, messagebox
        from .sweep_occupancy import save_occupancy_file
        if self.closed or self.exporting: return
        self.update()
        if self.future is not None or self.data is None or self.result_json is None: return
        generation, data, data_json = self.generation, self.data, self.result_json
        self.exporting = True; self.refresh_controls()

        def current():
            if self.closed: return False
            self.update()
            return not self.closed and self.generation == generation and self.future is None and self.data is data

        try:
            path = filedialog.asksaveasfilename(parent=self.app.root, title='Экспорт занятости и исходных свипов',
                defaultextension='.json', initialfile='sweep-occupancy.json', filetypes=[('JSON', '*.json')])
            if not current() or not path: return
            overwrite = os.path.lexists(path)
            if overwrite:
                approved = messagebox.askyesno('Заменить файл?', f'Заменить {path}?', parent=self.app.root)
                if not current() or not approved: return
            save_occupancy_file(path, data, data_json, overwrite)
            self.info.set(f'Занятость и исходные измерения сохранены: {path}')
        except TclError:
            return
        except (OSError, ValueError, TypeError) as exc:
            if current(): self.info.set(f'Не удалось экспортировать результат: {exc}')
        finally:
            self.exporting = False
            if not self.closed: self.refresh_controls()

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
        if self.closed:
            return
        super().close()
        if hasattr(self, 'server_trace'): self.app.server_var.trace_remove('write', self.server_trace)
