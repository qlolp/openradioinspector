"""Explicit read-only candidate proposals; the core remains authoritative."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Lock
from time import monotonic
import json
import os
from pathlib import Path
import tempfile
import unicodedata

from .sweep_average import utc_interval

MAX_PROFILE_BYTES = 65_536
MAX_HZ = 9_007_199_254_740_991
SOURCE_KEYS = {'id', 'source_stream_id', 'interval', 'center_frequency_hz', 'level_unit'}
RULE_KEYS = {'start_frequency_hz', 'end_frequency_hz', 'step_hz', 'origin_frequency_hz', 'max_offset_hz'}


def require(ok):
    if not ok:
        raise ValueError('Некорректный профиль или ответ округления частоты')


def identity(value, maximum=None):
    return (isinstance(value, str) and bool(value.strip())
            and (maximum is None or len(value) <= maximum)
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def check_draft(raw):
    require(isinstance(raw, str) and bool(raw.strip()))
    if len(raw.encode('utf-8', errors='strict')) > MAX_PROFILE_BYTES:
        raise ValueError('Профиль превышает 65 536 байт UTF-8')
    return raw


def validate_profile(profile):
    require(isinstance(profile, dict) and set(profile) == {
        'format', 'format_version', 'name', 'frequency_unit', 'method', 'tie_break', 'rules'})
    require(profile['format'] == 'openradioinspector-frequency-rounding'
            and type(profile['format_version']) is int and profile['format_version'] == 1)
    require(profile['frequency_unit'] == 'Hz' and profile['method'] == 'nearest_grid'
            and profile['tie_break'] == 'lower')
    name = profile['name']
    require(isinstance(name, str) and 1 <= len(name) <= 80)
    require(not any(unicodedata.category(c).startswith('C') for c in name))
    require(name == name.strip() and name == unicodedata.normalize('NFC', name))
    rules = profile['rules']
    require(isinstance(rules, list) and 1 <= len(rules) <= 128)
    previous = -1
    for rule in rules:
        require(isinstance(rule, dict) and set(rule) == RULE_KEYS)
        require(all(type(v) is int and 0 <= v <= MAX_HZ for v in rule.values()))
        start, end, step, origin = (rule[k] for k in (
            'start_frequency_hz', 'end_frequency_hz', 'step_hz', 'origin_frequency_hz'))
        require(step >= 1 and previous < start <= end)
        # ceil((start-origin)/step) also works for a negative grid index.
        require(-((origin-start)//step) <= (end-origin)//step)
        previous = end
    return profile


def validate_returned_draft(raw, profile):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value)
            value[key] = item
        return value

    def invalid_constant(_value):
        raise ValueError('Некорректное JSON число')

    requested = json.loads(check_draft(raw), object_pairs_hook=unique,
                           parse_constant=invalid_constant)
    require(isinstance(requested, dict))
    name = requested.get('name')
    require(isinstance(name, str) and not any(unicodedata.category(c).startswith('C') for c in name))
    requested['name'] = unicodedata.normalize('NFC', name).strip()
    validate_profile(requested)
    validate_profile(profile)
    require(requested == profile)


def candidate_source(candidate, session):
    require(identity(session) and isinstance(candidate, dict))
    require('session_id' not in candidate or candidate['session_id'] == session)
    require(SOURCE_KEYS <= candidate.keys())
    require(identity(candidate['id'], 128) and identity(candidate['source_stream_id']))
    require(type(candidate['center_frequency_hz']) is int
            and 1 <= candidate['center_frequency_hz'] <= MAX_HZ)
    require(identity(candidate['level_unit']))
    utc_interval(candidate['interval'])
    return deepcopy({key: candidate[key] for key in SOURCE_KEYS})


def validate_result(data, session, source, raw):
    source = candidate_source(source, session)
    require(isinstance(data, dict) and set(data) == {
        'session_id', 'candidate', 'profile', 'frequency_unit', 'method', 'tie_break', 'result'})
    require(data['session_id'] == session and data['frequency_unit'] == 'Hz'
            and data['method'] == 'nearest_grid' and data['tie_break'] == 'lower')
    candidate = data['candidate']
    require(isinstance(candidate, dict) and set(candidate) == SOURCE_KEYS)
    returned = candidate_source(candidate, session)
    for key in SOURCE_KEYS - {'interval'}:
        require(type(returned[key]) is type(source[key]) and returned[key] == source[key])
    require(utc_interval(returned['interval']) == utc_interval(source['interval']))
    profile = validate_profile(data['profile'])
    validate_returned_draft(raw, profile)
    result = data['result']
    require(isinstance(result, dict) and set(result) == {
        'status', 'rule_index', 'nearest_frequency_hz', 'offset_hz', 'rounded_frequency_hz'})
    expected = dict(status='outside_ranges', rule_index=None, nearest_frequency_hz=None,
                    offset_hz=None, rounded_frequency_hz=None)
    original = source['center_frequency_hz']
    for index, rule in enumerate(profile['rules']):
        start, end = rule['start_frequency_hz'], rule['end_frequency_hz']
        if not start <= original <= end:
            continue
        origin, step = rule['origin_frequency_hz'], rule['step_hz']
        lower, upper = -((origin-start)//step), (end-origin)//step
        below = (original-origin)//step
        points = [origin + min(upper, max(lower, k))*step for k in (below, below+1)]
        nearest = min(points, key=lambda value: (abs(value-original), value))
        offset = nearest-original
        accepted = abs(offset) <= rule['max_offset_hz']
        expected = dict(status=('unchanged' if offset == 0 else 'rounded') if accepted else 'outside_tolerance',
                        rule_index=index, nearest_frequency_hz=nearest, offset_hz=offset,
                        rounded_frequency_hz=nearest if accepted else None)
        break
    for key, value in expected.items():
        require(type(result[key]) is type(value) and result[key] == value)
    return data


def profile_template():
    return json.dumps(dict(format='openradioinspector-frequency-rounding', format_version=1,
        name='НЕ НОРМАТИВ: пример округления', frequency_unit='Hz', method='nearest_grid', tie_break='lower',
        rules=[dict(start_frequency_hz=100000000, end_frequency_hz=110000000,
                    step_hz=100000, origin_frequency_hz=0, max_offset_hz=30000),
               dict(start_frequency_hz=140000000, end_frequency_hz=150000000,
                    step_hz=12500, origin_frequency_hz=0, max_offset_hz=6250)]), ensure_ascii=False, indent=2)


def read_profile_file(path):
    with open(path, 'rb') as file:
        raw = file.read(MAX_PROFILE_BYTES+1)
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError('Файл превышает 65 536 байт')
    return raw.decode('utf-8', errors='strict')


class ExportGuard:
    """Cancellation and final atomic replacement have a single linearization point."""
    def __init__(self):
        self.lock = Lock()
        self.cancelled = False

    def cancel(self):
        with self.lock:
            self.cancelled = True


def save_profile_file(path, profile, overwrite=False, guard=None):
    payload = json.dumps(validate_profile(profile), ensure_ascii=False, indent=2,
                         allow_nan=False).encode('utf-8')
    if len(payload) > MAX_PROFILE_BYTES:
        raise ValueError('Файл превышает 65 536 байт')
    guard = guard or ExportGuard()
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.frequency-rounding-', delete=False) as file:
            temporary = file.name
            if file.write(payload) != len(payload):
                raise OSError('Неполная запись файла')
            file.flush()
            os.fsync(file.fileno())
        with guard.lock:
            if guard.cancelled:
                return False
            if overwrite:
                os.replace(temporary, destination)
            else:
                os.link(temporary, destination)
        return True
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def result_text(data):
    candidate, result = data['candidate'], data['result']
    rule = None if result['rule_index'] is None else data['profile']['rules'][result['rule_index']]
    def hz(value, signed=False):
        return '—' if value is None else (f'{value:+d}' if signed else str(value)) + ' Hz'
    return '\n'.join([
        f"Сеанс {data['session_id']} · кандидат {candidate['id']} · поток {candidate['source_stream_id']}",
        f"UTC {candidate['interval']['started_at']} … {candidate['interval']['ended_at']}",
        f"Профиль: {data['profile']['name']} · статус: {result['status']}",
        f"Исходная частота: {hz(candidate['center_frequency_hz'])}",
        f"Ближайшая точка: {hz(result['nearest_frequency_hz'])} · отклонение: {hz(result['offset_hz'], True)}",
        f"Допуск: {hz(None if rule is None else rule['max_offset_hz'])} · правило: {result['rule_index'] if rule else '—'}",
        f"Производная частота: {hz(result['rounded_frequency_hz'])}",
        f"Исходная единица уровня: {candidate['level_unit']} (без преобразования). Уровень не участвует в расчёте.",
        'Только предложение. Исходный кандидат, архив и настройки прибора не изменены.'])


class FrequencyRoundingPanel:
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = self.retired = self.io_future = self.io_guard = None
        self.data = self.source = self.chosen_source = None
        self.closed = self.snapshot_unavailable = False
        self.server_edit_client = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='frequency-rounding')
        self.io_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rounding-file')
        tk, ttk = app.tk, app.ttk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Округление частоты')
        ttk.Label(frame, text='Read-only предложение ядра. Выберите кандидата текущего сеанса из любого потока. Нет перенастройки прибора, записи в архив или применения к handoff.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.candidate = tk.StringVar()
        self.candidates = ttk.Combobox(row, textvariable=self.candidate, state='readonly', width=40)
        self.candidates.pack(side='left')
        self.button = ttk.Button(row, text='Рассчитать / загрузить', command=self.analyze)
        self.button.pack(side='left')
        ttk.Button(row, text='Очистить результат', command=self.clear).pack(side='left')
        self.retained_info = tk.StringVar()
        ttk.Label(frame, textvariable=self.retained_info, wraplength=1050).pack(fill='x')
        files = ttk.Frame(frame); files.pack(fill='x')
        ttk.Button(files, text='Импорт JSON', command=self.load).pack(side='left')
        ttk.Button(files, text='НЕ НОРМАТИВ: пример', command=self.template).pack(side='left')
        self.save_button = ttk.Button(files, text='Экспорт проверенного профиля', command=self.save, state='disabled')
        self.save_button.pack(side='left')
        self.editor = tk.Text(frame, height=12, wrap='none'); self.editor.pack(fill='both', expand=True)
        self.editor.bind('<<Modified>>', self.edited)
        self.info = tk.StringVar()
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=11, wrap='none', state='disabled'); self.details.pack(fill='both', expand=True)
        self.candidate.trace_add('write', self.choose_candidate)
        self.server_trace = app.server_var.trace_add('write', self.disconnect)
        self.set_draft(profile_template())

    def show_details(self, text):
        self.details.configure(state='normal'); self.details.delete('1.0', 'end')
        self.details.insert('1.0', text); self.details.configure(state='disabled')

    def clear(self):
        self.generation += 1
        if self.io_guard is not None:
            self.io_guard.cancel()
        if self.future is not None:
            self.future.cancel()
            if not self.future.done():
                self.retired = self.future
            self.future = None
        self.data = self.source = None
        self.save_button.configure(state='disabled')
        self.button.configure(state='disabled' if self.snapshot_unavailable else 'normal')
        self.show_details('')
        self.info.set('Нет свежего снимка сервера. Дождитесь успешного обновления.' if self.snapshot_unavailable
                      else 'Черновик не проверен. Расчёт только по кнопке; архив не изменяется.')

    def edited(self, event=None):
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self.clear()

    def set_draft(self, raw):
        self.clear()
        self.editor.delete('1.0', 'end'); self.editor.insert('1.0', raw)
        self.editor.edit_modified(False)

    def template(self):
        if not self.closed:
            self.update(); self.set_draft(profile_template())

    def sources(self):
        snap = self.app.snapshot
        if (self.snapshot_unavailable or self.app.client is None or snap is None
                or not identity(self.app.session_id)
                or (snap.session or {}).get('id') != self.app.session_id):
            return []
        sources = []
        for item in snap.candidates:
            try:
                candidate_source(item, self.app.session_id)
            except (ValueError, TypeError, KeyError):
                continue
            sources.append(item)
        return sources

    def invalidate_choice(self):
        self.chosen_source = None
        self.retained_info.set('')
        self.clear()

    def choose_candidate(self, *args):
        previous = self.chosen_source
        self.invalidate_choice()
        matches = [item for item in self.sources() if item['id'] == self.candidate.get()]
        if len(matches) == 1:
            self.chosen_source = deepcopy(matches[0])
            self.show_choice()
        elif (not matches and previous is not None and previous['id'] == self.candidate.get()
                and not self.snapshot_unavailable and self.app.client is not None
                and self.scope == (id(self.app.client), self.app.session_id)
                and (self.app.snapshot.session or {}).get('id') == self.app.session_id):
            self.chosen_source = previous
            self.show_choice(retained=True)

    def show_choice(self, retained=False):
        chosen = self.chosen_source
        if chosen is None:
            self.retained_info.set('')
            return
        marker = ' · сохранённый снимок (вне текущей страницы)' if retained else ''
        self.retained_info.set(
            f"Кандидат {chosen['id']} · поток {chosen['source_stream_id']}{marker}\n"
            f"Исходная частота: {chosen['center_frequency_hz']} Hz · единица уровня: {chosen['level_unit']}\n"
            f"UTC {chosen['interval']['started_at']} … {chosen['interval']['ended_at']}")

    def disconnect(self, *args):
        if args:
            self.server_edit_client = self.app.client
        self.snapshot_unavailable = True
        self.invalidate_choice()
        self.candidate.set('')
        self.candidates.configure(values=[])

    def snapshot_refreshed(self):
        if self.server_edit_client is not None and self.server_edit_client is self.app.client:
            return
        self.server_edit_client = None
        self.snapshot_unavailable = False
        self.update()

    def update(self):
        if self.closed:
            return
        # Candidates belong to the session, not to the spectrum plot selection.
        scope = (id(self.app.client), self.app.session_id)
        if scope != self.scope:
            self.scope = scope
            self.invalidate_choice(); self.candidate.set('')
        sources = self.sources()
        chosen = self.chosen_source
        if chosen is not None and chosen['id'] != self.candidate.get():
            self.invalidate_choice(); chosen = None
        ids = list(dict.fromkeys(item['id'] for item in sources))
        retained = chosen is not None and chosen['id'] not in ids
        if retained:
            ids.append(chosen['id'])
        self.candidates.configure(values=ids)
        self.show_choice(retained)
        self.button.configure(state='disabled' if self.snapshot_unavailable or self.future is not None else 'normal')

    def analyze(self):
        self.update()
        if self.closed or self.future is not None or self.snapshot_unavailable:
            return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            require(self.app.client is not None)
            source = candidate_source(self.chosen_source, self.app.session_id)
            require(source['id'] == self.candidate.get())
            raw = check_draft(self.editor.get('1.0', 'end-1c'))
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            self.clear(); self.info.set(str(exc)); return
        self.clear()
        self.source, self.raw, self.expected = source, raw, self.app.session_id
        self.deadline = monotonic()+8.0
        generation = self.generation
        self.info.set(f"Расчёт кандидата {source['id']}…")
        self.button.configure(state='disabled')
        self.future = self.executor.submit(self.app.client.candidate_frequency_rounding,
                                           self.app.session_id, source['id'], raw)
        self.app.root.after(50, lambda: self.finish(generation))

    def finish(self, generation):
        if self.closed:
            return
        self.update()
        if generation != self.generation or self.future is None:
            return
        if monotonic() >= self.deadline:
            self.clear(); self.info.set('Превышен срок ожидания 8 секунд.'); return
        if not self.future.done():
            self.app.root.after(50, lambda: self.finish(generation)); return
        future, self.future = self.future, None
        self.button.configure(state='normal')
        try:
            require(self.editor.get('1.0', 'end-1c') == self.raw)
            data = validate_result(future.result(), self.expected, self.source, self.raw)
            self.data = deepcopy(data)
            self.save_button.configure(state='normal')
            self.info.set(f"Проверен профиль «{data['profile']['name']}». Результат зафиксирован.")
            self.show_details(result_text(self.data))
        except Exception as exc:
            self.data = None
            self.save_button.configure(state='disabled')
            self.info.set(f'Округление недоступно: {exc}. Черновик не проверен.')

    def load(self):
        from tkinter import filedialog
        self.update()
        if self.closed or self.io_future is not None:
            return
        generation = self.generation
        path = filedialog.askopenfilename(parent=self.app.root, title='Импорт черновика округления', filetypes=[('JSON', '*.json'), ('Все файлы', '*')])
        self.update()
        if not path or self.closed or generation != self.generation:
            return
        self.clear(); generation = self.generation
        self.io_future = self.io_executor.submit(read_profile_file, path)
        self.app.root.after(50, lambda: self.finish_io(generation, 'load'))

    def save(self):
        from tkinter import filedialog, messagebox
        self.update()
        if self.closed or self.io_future is not None or self.data is None:
            return
        if self.editor.get('1.0', 'end-1c') != self.raw:
            self.clear(); return
        data, generation = deepcopy(self.data), self.generation
        path = filedialog.asksaveasfilename(parent=self.app.root, title=f"Экспорт: {data['profile']['name']}", defaultextension='.json', initialfile='frequency-rounding-profile.json', confirmoverwrite=False, filetypes=[('JSON', '*.json')])
        if not path:
            return
        overwrite = os.path.lexists(path)
        if overwrite and not messagebox.askyesno('Заменить файл?', f'Заменить {path}?', parent=self.app.root):
            return
        self.update()
        if (self.closed or generation != self.generation or self.data is None
                or self.editor.get('1.0', 'end-1c') != self.raw):
            return
        self.io_guard = ExportGuard()
        self.io_future = self.io_executor.submit(save_profile_file, path, data['profile'], overwrite, self.io_guard)
        self.app.root.after(50, lambda: self.finish_io(generation, 'save'))

    def finish_io(self, generation, operation):
        if self.closed:
            return
        self.update()
        if self.io_future is None:
            return
        if not self.io_future.done():
            self.app.root.after(50, lambda: self.finish_io(generation, operation)); return
        future, self.io_future = self.io_future, None
        self.io_guard = None
        try:
            result = future.result()
            if generation != self.generation:
                return
            if operation == 'load':
                self.set_draft(result)
            elif result and self.data is not None:
                self.info.set(f"Сохранён проверенный профиль «{self.data['profile']['name']}»")
        except Exception as exc:
            if generation == self.generation:
                self.info.set(f'Ошибка файла профиля: {exc}')

    def close(self):
        if self.closed:
            return
        self.clear(); self.closed = True
        self.app.server_var.trace_remove('write', self.server_trace)
        if self.io_future is not None:
            self.io_future.cancel()
        self.io_executor.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
