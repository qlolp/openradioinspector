"""Explicit archived comparison; subtraction is exclusively performed by the core."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic


def eligible_sweeps(app):
    if ((app.snapshot.session or {}).get('id') != app.session_id
            or app.snapshot.spectrum_stream_id != app.selected_stream_id()):
        return []
    return [s for s in app.snapshot.sweeps if s.get('level_unit') == 'dBm']


def comparison_text(data):
    lines = []
    for key in ('reference', 'target'):
        item = data[key]
        lines.append(f"{key}: {item['id']} · UTC {item['interval']} · исходные {item['level_unit']}")
    for key, label in (('max_increase', 'Максимальное изменение'), ('min_change', 'Минимальное изменение')):
        item = data[key]
        lines.append(f"{label}: {item['delta_db']:+g} dB @ {item['frequency_hz']} Hz")
    return '\n'.join(lines)


def validate_comparison(data, expected):
    def require(ok):
        if not ok:
            raise ValueError('Некорректный ответ сравнения')
    def number(value):
        return type(value) in (int, float) and isfinite(value)
    def json_value(value, allow_null=False):
        if value is None:
            return allow_null
        if type(value) in (str, bool):
            return True
        if type(value) in (int, float):
            return number(value)
        if isinstance(value, list):
            return all(json_value(v, allow_null) for v in value)
        if isinstance(value, dict):
            return all(isinstance(k, str) and json_value(v, allow_null) for k, v in value.items())
        return False
    require(isinstance(data, dict))
    session, stream, reference, target = expected
    require(data.get('session_id') == session and data.get('stream_id') == stream)
    n = data.get('points')
    require(type(n) is int and 1 <= n <= 16384 and data.get('delta_unit') == 'dB')
    for key in ('frequencies_hz', 'delta_levels'):
        require(isinstance(data.get(key), list) and len(data[key]) == n)
        require(all(number(v) for v in data[key]))
    frequencies = data['frequencies_hz']
    require(all(type(v) is int and v >= 0 for v in frequencies))
    require(all(a <= b for a, b in zip(frequencies, frequencies[1:])))
    for key, identity in (('reference', reference), ('target', target)):
        item = data.get(key)
        require(isinstance(item, dict))
        require(item.get('id') == identity and item.get('level_unit') == 'dBm')
        settings = item.get('settings')
        require(isinstance(settings, dict))
        recorded = {k: v for k, v in settings.items() if k != 'sequence'}
        require(bool(recorded) and json_value(recorded))
        if 'sequence' in settings:
            require(json_value(settings['sequence'], allow_null=True))
        levels = item.get('levels')
        require(isinstance(levels, list) and len(levels) == n and all(number(v) for v in levels))
        interval = item.get('interval')
        require(isinstance(interval, dict))
        times = []
        for field in ('started_at', 'ended_at'):
            value = datetime.fromisoformat(interval[field].replace('Z', '+00:00'))
            require(value.utcoffset() == timedelta(0))
            times.append(value)
        require(times[0] <= times[1])
    for key in ('max_increase', 'min_change'):
        item = data.get(key)
        require(isinstance(item, dict) and number(item.get('delta_db')))
        index = (max if key == 'max_increase' else min)(range(n), key=data['delta_levels'].__getitem__)
        require(item.get('delta_db') == data['delta_levels'][index])
        require(type(item.get('frequency_hz')) is int and item['frequency_hz'] == frequencies[index])
    return data


class ComparisonPanel:
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.reference = None
        self.generation = 0
        self.future = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sweep-comparison')
        self.retired = None
        self.closed = False
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Сравнение sweeps')
        self.box = ttk.Combobox(frame, state='readonly', width=100)
        self.box.pack(fill='x')
        ttk.Button(frame, text='Закрепить выбранный dBm sweep', command=self.pin).pack()
        self.button = ttk.Button(frame, text='Сравнить с последним', command=self.compare)
        self.button.pack()
        ttk.Button(frame, text='Очистить', command=self.clear).pack()
        self.pin_label = tk.StringVar(value='Опорный sweep не закреплён.')
        ttk.Label(frame, textvariable=self.pin_label, wraplength=1050).pack(fill='x')
        from .sweep_references import NamedReferenceCatalog
        self.catalog = NamedReferenceCatalog(self, frame)
        self.reference_interval = None
        self.info = tk.StringVar(value='Выберите архивный sweep и закрепите его.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.canvas = tk.Canvas(frame, background='#07131c', height=260)
        self.canvas.pack(fill='both', expand=True)
        self.data = None
        self.canvas.bind('<Configure>', lambda event: self.draw())

    def update(self):
        scope = (id(self.app.client), self.app.session_id, self.app.selected_stream_id())
        if scope != self.scope:
            self.scope = scope
            self.clear()
        if hasattr(self, "catalog"):
            self.catalog.update()
        sweeps = eligible_sweeps(self.app)
        self.ids = [s['id'] for s in sweeps]
        if hasattr(self, 'pin_label') and self.reference:
            suffix = ' · вне текущего окна sweeps' if self.reference not in self.ids else ''
            self.pin_label.set(f'Опорный sweep: {self.reference} · UTC {self.reference_interval} · dBm{suffix}')
        selected = self.box.get()
        self.box.configure(values=self.ids)
        if selected not in self.ids:
            self.box.set(self.ids[-1] if self.ids else '')

    def abandon(self):
        if self.future is not None:
            self.future.cancel()
            if not self.future.done():
                self.retired = self.future
            self.future = None
        self.button.configure(state='normal')

    def clear(self):
        self.abandon()
        self.generation += 1
        self.reference = None
        self.reference_interval = None
        if hasattr(self, 'pin_label'):
            self.pin_label.set('Опорный sweep не закреплён.')
        self.data = None
        self.canvas.delete('all')
        self.info.set('Опорный sweep не закреплён. Исходные dBm; разность dB.')
        if hasattr(self, 'catalog'):
            self.catalog.controls()

    def pin(self):
        self.update()
        if self.box.get() in self.ids:
            self.clear()
            self.reference = self.box.get()
            item = next(s for s in eligible_sweeps(self.app) if s['id'] == self.reference)
            self.reference_interval = item.get('interval')
            self.update()
            self.info.set(f"Опорный sweep: {self.reference} · UTC {item.get('interval')} · исходные dBm. Закрепление сохраняется вне текущего окна sweeps.")

    def compare(self):
        self.update()
        if self.closed or self.future is not None or not self.reference:
            return
        snapshot = self.app.snapshot
        if ((snapshot.session or {}).get('id') != self.app.session_id
                or snapshot.spectrum_stream_id != self.app.selected_stream_id() or not snapshot.sweeps):
            return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.')
            return
        self.retired = None
        client, session = self.app.client, self.app.session_id
        if client is None or session is None:
            return
        target = self.app.snapshot.sweeps[-1]['id']
        generation = self.generation
        self.data = None
        self.canvas.delete('all')
        self.info.set(f'Сравнение {self.reference} → {target}…')
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        self.expected = (session, self.app.selected_stream_id(), self.reference, target)
        self.future = self.executor.submit(client.compare_sweeps, session, self.reference, target)
        self.app.root.after(50, lambda: self.finish(generation))

    def finish(self, generation):
        if self.closed:
            return
        self.update()
        if generation != self.generation or self.future is None:
            return
        if monotonic() >= self.deadline:
            self.abandon()
            self.generation += 1
            self.info.set('Сравнение: превышен срок ожидания 8 секунд; повторите запрос.')
            return
        if not self.future.done():
            self.app.root.after(50, lambda: self.finish(generation))
            return
        future, self.future = self.future, None
        self.button.configure(state='normal')
        if generation != self.generation:
            return
        try:
            data = validate_comparison(future.result(), self.expected)
            self.info.set(comparison_text(data))
            self.data = data
            self.draw()
        except Exception as exc:
            self.info.set(f'Сравнение недоступно: {exc}')

    def draw(self):
        self.canvas.delete('all')
        if not self.data:
            return
        values = self.data['delta_levels']
        frequencies = self.data['frequencies_hz']
        if not values:
            return
        w, h = max(100, self.canvas.winfo_width()), max(100, self.canvas.winfo_height())
        low, high = min(min(values), 0), max(max(values), 0)
        scale = max(abs(low), abs(high), 1)
        lower, upper = low / scale, high / scale
        span = upper - lower or 1
        self.canvas.create_text(10, 10, anchor='nw', fill='white', text=f'Δ target − reference, dB · {low:g} … {high:g} · {frequencies[0]} … {frequencies[-1]} Hz')
        points = []
        for index, value in enumerate(values):
            fraction = ((frequencies[index] - frequencies[0]) / (frequencies[-1] - frequencies[0]) if frequencies[-1] != frequencies[0] else 0.5)
            points.extend((10 + fraction * (w - 20), h - 15 - (value / scale - lower) * (h - 50) / span))
        if len(values) > 1:
            self.canvas.create_line(*points, fill='#6cd8ff')
        else:
            x, y = points
            self.canvas.create_oval(x-3, y-3, x+3, y+3, fill='#6cd8ff')

    def close(self):
        self.closed = True
        if hasattr(self, "catalog"):
            self.catalog.close()
        self.abandon()
        self.generation += 1
        self.executor.shutdown(wait=False, cancel_futures=True)
