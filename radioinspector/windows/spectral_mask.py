"""Portable masks remain raw drafts until explicit, read-only core analysis."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic
import json
import os
from pathlib import Path
import tempfile
import unicodedata
from .sweep_threshold import ThresholdPanel

MAX_MASK_BYTES = 65536
MAX_HZ = 9_007_199_254_740_991


def check_draft(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('Пустой черновик маски')
    if len(raw.encode('utf-8')) > MAX_MASK_BYTES:
        raise ValueError('Маска превышает 65 536 байт UTF-8')
    return raw


def validate_artifact(mask):
    def require(ok):
        if not ok: raise ValueError('Некорректная проверенная маска')
    require(isinstance(mask, dict) and set(mask) == {'format', 'format_version', 'name', 'frequency_unit', 'level_unit', 'interpolation', 'vertices'})
    require(mask['format'] == 'openradioinspector-spectral-mask' and type(mask['format_version']) is int and mask['format_version'] == 1)
    require(mask['frequency_unit'] == 'Hz' and mask['level_unit'] == 'dBm' and mask['interpolation'] == 'linear_dbm')
    name = mask['name']
    require(isinstance(name, str) and 1 <= len(name) <= 80)
    require(not any(unicodedata.category(c).startswith('C') for c in name))
    require(name == name.strip() and name == unicodedata.normalize('NFC', name))
    vertices = mask['vertices']
    require(isinstance(vertices, list) and 2 <= len(vertices) <= 128)
    previous = -1
    for vertex in vertices:
        require(isinstance(vertex, dict) and set(vertex) == {'frequency_hz', 'limit_dbm'})
        hz, level = vertex['frequency_hz'], vertex['limit_dbm']
        require(type(hz) is int and previous < hz <= MAX_HZ)
        try: valid = type(level) in (int, float) and isfinite(level)
        except OverflowError: valid = False
        require(valid)
        previous = hz
    return mask


def read_mask_file(path):
    with open(path, 'rb') as file:
        raw = file.read(MAX_MASK_BYTES + 1)
    if len(raw) > MAX_MASK_BYTES: raise ValueError('Файл превышает 65 536 байт')
    return raw.decode('utf-8', errors='strict')


def save_mask_file(path, mask, overwrite=False):
    """Same-directory temporary file; failures never truncate the destination."""
    payload = json.dumps(validate_artifact(mask), ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_MASK_BYTES: raise ValueError('Файл превышает 65 536 байт')
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.spectral-mask-', delete=False) as file:
            temporary = file.name
            if file.write(payload) != len(payload): raise OSError('Неполная запись файла')
            file.flush()
            os.fsync(file.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            # Atomic create-if-absent also protects a destination created after the dialog.
            os.link(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary): os.unlink(temporary)


def mask_template(source):
    frequencies = source.get('frequencies_hz', [])
    if (not frequencies or type(frequencies[0]) is not int or type(frequencies[-1]) is not int
            or not 0 <= frequencies[0] < frequencies[-1] <= MAX_HZ):
        raise ValueError('Для примера нужны две разные крайние частоты источника')
    return json.dumps(dict(format='openradioinspector-spectral-mask', format_version=1,
        name='НЕ НОРМАТИВ: горизонтальный пример', frequency_unit='Hz', level_unit='dBm', interpolation='linear_dbm',
        vertices=[dict(frequency_hz=hz, limit_dbm=-80) for hz in (frequencies[0], frequencies[-1])]), ensure_ascii=False, indent=2)


def same_json(left, right):
    """JSON numeric equality never aliases booleans, including nested values."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right): return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


def validate_mask_result(data, expected, source=None):
    def require(ok):
        if not ok:
            raise ValueError('Некорректный ответ спектральной маски')
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
    session, stream, sweep_id = expected
    require(isinstance(data, dict))
    require(data.get('session_id') == session and data.get('stream_id') == stream)
    require(data.get('level_unit') == 'dBm' and data.get('delta_unit') == 'dB')
    n = data.get('points')
    require(type(n) is int and 1 <= n <= 16384)
    mask = validate_artifact(data.get('mask'))
    require(data.get('comparison') == 'strictly_greater')
    for key in ('frequencies_hz', 'threshold_levels', 'excess_levels'):
        require(isinstance(data.get(key), list) and len(data[key]) == n and all(number(v) for v in data[key]))
    frequencies = data['frequencies_hz']
    require(all(type(v) is int and 0 <= v <= MAX_HZ for v in frequencies))
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
            require(same_json(sweep[key], source.get(key)))
        require(same_json(frequencies, source.get('frequencies_hz')))
        source_settings = source.get('settings')
        require(isinstance(source_settings, dict))
        require(same_json(settings, source_settings))
    excess = data['excess_levels']
    require(mask['vertices'][0]['frequency_hz'] <= frequencies[0]
            and frequencies[-1] <= mask['vertices'][-1]['frequency_hz'])
    for i in range(n):
        delta = levels[i] - data['threshold_levels'][i]
        require(isfinite(delta) and delta == excess[i])
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


def mask_text(data):
    sweep = data['sweep']
    lines = [f"Маска: {data['mask']['name']} · источник sweep {sweep['id']} · UTC {sweep['interval']['started_at']} … {sweep['interval']['ended_at']}",
             f"Превышения: {data['exceedance_points']}/{data['points']} · показано {min(200, data['region_count'])} из {data['region_count']} областей; график: все точки"]
    for r in data['regions'][:200]:
        lines.append(f"Индексы {r['start_index']}…{r['end_index']} · {r['start_frequency_hz']}…{r['end_frequency_hz']} Hz · {r['points']} точек · пик +{r['peak_excess_db']:g} dB @ {r['peak_frequency_hz']} Hz (индекс {r['peak_index']})")
    return '\n'.join(lines)


def validate_returned_draft(raw, mask):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('Повторный ключ в маске')
            result[key] = value
        return result
    requested = json.loads(raw, object_pairs_hook=unique)
    name = requested.get('name')
    if not isinstance(name, str) or any(unicodedata.category(c).startswith('C') for c in name):
        raise ValueError('Некорректное имя маски')
    requested['name'] = unicodedata.normalize('NFC', name).strip()
    validate_artifact(requested)
    if requested != mask: raise ValueError('Ответ содержит другую маску')


class SpectralMaskPanel(ThresholdPanel):
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = self.retired = self.data = None
        self.io_future = None
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='spectral-mask')
        self.io_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='mask-file')
        self._init_timers(app.root)
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Спектральная маска')
        ttk.Label(frame, text='Табличная маска, linear dBm. Черновик не проверен: загрузка и редактирование не обращаются к серверу. Анализ только по кнопке; архив не изменяется.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        for label, command in [('Загрузить JSON', self.load), ('НЕ НОРМАТИВ: пример −80 dBm', self.template)]:
            ttk.Button(row, text=label, command=command).pack(side='left')
        self.button = ttk.Button(row, text='Анализировать последний dBm sweep', command=self.analyze)
        self.button.pack(side='left')
        self.save_button = ttk.Button(row, text='Сохранить проверенную маску', command=self.save, state='disabled')
        self.save_button.pack(side='left')
        ttk.Button(row, text='Очистить результат', command=self.clear).pack(side='left')
        self.editor = tk.Text(frame, height=9, wrap='none'); self.editor.pack(fill='both', expand=True)
        self.editor.bind('<<Modified>>', self.edited)
        self.info = tk.StringVar(value='Черновик не проверен. Анализ только по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.canvas = tk.Canvas(frame, background='#07131c', height=230); self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda event: self.draw())
        self.details = tk.Text(frame, height=7, state='disabled', wrap='none'); self.details.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview)
        scrollbar.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scrollbar.set)

    def clear(self):
        self.abandon(); self.data = None
        self.save_button.configure(state='disabled')
        self.canvas.delete('all'); self.show_details('')
        self.info.set('Черновик не проверен. Анализ только по кнопке; архив не изменяется.')

    def edited(self, event=None):
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self.clear()

    def set_draft(self, raw):
        self.clear()
        self.editor.delete('1.0', 'end'); self.editor.insert('1.0', raw)
        self.editor.edit_modified(False)

    def selected_source(self):
        snapshot = self.app.snapshot
        if (self.app.client is None or not self.app.session_id or not self.app.selected_stream_id()
                or (snapshot.session or {}).get('id') != self.app.session_id
                or snapshot.spectrum_stream_id != self.app.selected_stream_id()):
            raise ValueError('Нет источника в выбранном сеансе/потоке')
        sweeps = [s for s in snapshot.sweeps if isinstance(s, dict) and s.get('level_unit') == 'dBm'
                  and s.get('stream_id') == self.app.selected_stream_id()
                  and isinstance(s.get('id'), str) and s['id'].strip()]
        if not sweeps: raise ValueError('Нет архивного dBm sweep в выбранном потоке')
        return deepcopy(sweeps[-1])

    def template(self):
        self.update()
        try: self.set_draft(mask_template(self.selected_source()))
        except ValueError as exc: self.info.set(str(exc))

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            raw = check_draft(self.editor.get('1.0', 'end-1c'))
            source = self.selected_source()
        except (ValueError, UnicodeError) as exc:
            self.clear(); self.info.set(str(exc)); return
        self.clear()
        self.source, self.raw = source, raw
        self.expected = (self.app.session_id, self.app.selected_stream_id(), source['id'])
        self.info.set(f"Черновик не проверен; анализ sweep {source['id']}…")
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        self.future = self.executor.submit(self.app.client.sweep_mask_analysis, self.app.session_id, source['id'], raw)
        self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation)

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.abandon(); self.info.set('Превышен срок ожидания 8 секунд. Черновик не проверен.'); return
        if not self.future.done():
            self._schedule_after(self.app.root, 'analysis', 50, self.finish, generation); return
        future, self.future = self.future, None
        self.button.configure(state='normal')
        try:
            data = validate_mask_result(future.result(), self.expected, self.source)
            validate_returned_draft(self.raw, data['mask'])
            self.data = deepcopy(data)
            self.save_button.configure(state='normal')
            self.info.set(f"Проверена маска «{data['mask']['name']}», sweep {data['sweep']['id']}. Голубой: исходные dBm; оранжевый: маска dBm. Результат зафиксирован.")
            self.show_details(mask_text(self.data)); self.draw()
        except Exception as exc:
            self.data = None
            self.info.set('Маска недоступна (404): старый сервер или sweep не найден. Черновик не проверен.' if getattr(exc, 'status', None) == 404 else f'Маска недоступна: {exc}. Черновик не проверен.')

    def load(self):
        from tkinter import filedialog
        if self.closed or self.io_future is not None: return
        path = filedialog.askopenfilename(parent=self.app.root, title='Загрузить черновик маски', filetypes=[('JSON', '*.json'), ('Все файлы', '*')])
        if not path: return
        self.update(); self.clear()
        generation = self.generation
        self.io_future = self.io_executor.submit(read_mask_file, path)
        self._schedule_after(self.app.root, 'io', 50, self.finish_io, generation, 'load')

    def save(self):
        from tkinter import filedialog, messagebox
        self.update()
        if self.closed or self.io_future is not None or self.data is None: return
        data = deepcopy(self.data)
        generation = self.generation
        path = filedialog.asksaveasfilename(parent=self.app.root, title=f"Сохранить проверенную маску: {data['mask']['name']} / {data['sweep']['id']}", defaultextension='.json', initialfile='spectral-mask.json', confirmoverwrite=False, filetypes=[('JSON', '*.json')])
        if not path: return
        overwrite = os.path.lexists(path)
        if overwrite and not messagebox.askyesno('Заменить файл?', f'Заменить {path}?', parent=self.app.root): return
        self.update()
        if generation != self.generation or self.data is None: return
        self.io_future = self.io_executor.submit(save_mask_file, path, data['mask'], overwrite)
        self._schedule_after(self.app.root, 'io', 50, self.finish_io, generation, 'save')

    def finish_io(self, generation, operation):
        if self.closed: return
        self.update()
        if self.io_future is None: return
        if not self.io_future.done():
            self._schedule_after(self.app.root, 'io', 50, self.finish_io, generation, operation); return
        future, self.io_future = self.io_future, None
        try:
            result = future.result()
            if generation != self.generation: return
            if operation == 'load': self.set_draft(result)
            else: self.info.set(f"Сохранена проверенная маска «{self.data['mask']['name']}», sweep {self.data['sweep']['id']}")
        except Exception as exc:
            if generation == self.generation: self.info.set(f'Ошибка файла маски: {exc}')

    def draw(self):
        # Tk lines need two points; distinguish both single-bin values with markers.
        if self.data and self.data['points'] == 1:
            self.canvas.delete('all')
            self.canvas.create_text(10, 10, anchor='nw', fill='white', text=f"{self.data['frequencies_hz'][0]} Hz · исходный {self.data['sweep']['levels'][0]:g} dBm · маска {self.data['threshold_levels'][0]:g} dBm")
            source, threshold = self.data['sweep']['levels'][0], self.data['threshold_levels'][0]
            scale = max(abs(source), abs(threshold), 1)
            lower, upper = min(source, threshold) / scale, max(source, threshold) / scale
            w, h = max(100, self.canvas.winfo_width()), max(100, self.canvas.winfo_height())
            x = w / 2
            def y(value):
                fraction = (value / scale - lower) / (upper - lower) if upper != lower else 0.5
                return h - 15 - fraction * (h - 50)
            source_y, threshold_y = y(source), y(threshold)
            # A hollow square remains visible around the filled dot when values coincide.
            self.canvas.create_rectangle(x - 6, threshold_y - 6, x + 6, threshold_y + 6,
                                         outline='#ffb454', width=2)
            self.canvas.create_oval(x - 3, source_y - 3, x + 3, source_y + 3,
                                    fill='#6cd8ff', outline='#6cd8ff')
            return
        super().draw()

    def close(self):
        if self.closed:
            return
        super().close()
        if self.io_future is not None: self.io_future.cancel()
        self.io_future = None
        self.io_executor.shutdown(wait=False, cancel_futures=True)
