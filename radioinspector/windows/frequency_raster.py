"""Portable frequency raster tables and read-only rounding of candidate frequencies."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from math import isfinite
from time import monotonic
import json
import os
from pathlib import Path
import tempfile
import unicodedata

MAX_RASTER_BYTES = 65536
MAX_HZ = 9_007_199_254_740_991
MAX_ENTRIES = 64
MAX_CANDIDATES = 256


def check_draft(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('Пустой черновик таблицы')
    if len(raw.encode('utf-8')) > MAX_RASTER_BYTES:
        raise ValueError('Таблица превышает 65 536 байт UTF-8')
    return raw


def _canonical_name(value):
    if not isinstance(value, str):
        raise ValueError('Имя должно быть строкой')
    value = unicodedata.normalize('NFC', value)
    if any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('Имя: 1–80 символов, без управляющих символов')
    value = value.strip()
    if not 1 <= len(value) <= 80:
        raise ValueError('Имя: 1–80 символов, без управляющих символов')
    return value


def _canonicalize(table):
    """NFC+trim names so a loose draft can be compared with the echoed artifact."""
    if not isinstance(table, dict):
        raise ValueError('Некорректная проверенная таблица')
    result = dict(table)
    result['name'] = _canonical_name(result.get('name'))
    entries = result.get('entries')
    if isinstance(entries, list):
        canonical = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('Некорректная проверенная таблица')
            row = dict(entry)
            row['name'] = _canonical_name(row.get('name'))
            canonical.append(row)
        result['entries'] = canonical
    return result


def validate_artifact(table):
    def require(ok):
        if not ok: raise ValueError('Некорректная проверенная таблица')
    require(isinstance(table, dict) and set(table) == {'format', 'format_version', 'name', 'frequency_unit', 'entries'})
    require(table['format'] == 'openradioinspector-frequency-raster' and type(table['format_version']) is int and table['format_version'] == 1)
    require(table['frequency_unit'] == 'Hz')
    require(_canonical_name(table['name']) == table['name'])
    entries = table['entries']
    require(isinstance(entries, list) and 1 <= len(entries) <= MAX_ENTRIES)
    previous_stop = -1
    names = set()
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {'name', 'start_hz', 'stop_hz', 'grid_step_hz'})
        require(_canonical_name(entry['name']) == entry['name'] and entry['name'] not in names)
        names.add(entry['name'])
        start, stop, step = entry['start_hz'], entry['stop_hz'], entry['grid_step_hz']
        require(type(start) is int and type(stop) is int and 0 <= start < stop <= MAX_HZ)
        require(type(step) is int and 1 <= step <= stop - start)
        require(start > previous_stop)
        previous_stop = stop
    return table


def read_raster_file(path):
    with open(path, 'rb') as file:
        raw = file.read(MAX_RASTER_BYTES + 1)
    if len(raw) > MAX_RASTER_BYTES: raise ValueError('Файл превышает 65 536 байт')
    return raw.decode('utf-8', errors='strict')


def save_raster_file(path, table, overwrite=False):
    """Same-directory temporary file; failures never truncate the destination."""
    payload = json.dumps(validate_artifact(table), ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_RASTER_BYTES: raise ValueError('Файл превышает 65 536 байт')
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.frequency-raster-', delete=False) as file:
            temporary = file.name
            if file.write(payload) != len(payload): raise OSError('Неполная запись файла')
            file.flush()
            os.fsync(file.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
            os.unlink(temporary)
        temporary = None
        return str(destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def raster_template(candidates):
    """Non-normative 100 kHz example covering the selected candidate frequencies."""
    frequencies = [item['center_frequency_hz'] for item in candidates
                   if isinstance(item, dict) and type(item.get('center_frequency_hz')) is int]
    if not frequencies: raise ValueError('Нет выбранных кандидатов с целыми частотами')
    start = max(0, min(frequencies) - 1_000_000)
    stop = min(MAX_HZ, max(frequencies) + 1_000_000)
    if stop - start < 100_000: raise ValueError('Диапазон примера уже шага сетки')
    return json.dumps({'format': 'openradioinspector-frequency-raster', 'format_version': 1,
                       'name': 'НЕНОРМАТИВНЫЙ пример 100 кГц', 'frequency_unit': 'Hz',
                       'entries': [{'name': 'пример', 'start_hz': start, 'stop_hz': stop,
                                    'grid_step_hz': 100_000}]}, ensure_ascii=False, indent=2)


def validate_result(data, expected_session, candidate_ids):
    def require(ok):
        if not ok: raise ValueError('Некорректный ответ округления')
    require(isinstance(data, dict) and data.get('session_id') == expected_session)
    require(data.get('order') == 'request')
    table = validate_artifact(data.get('table'))
    items = data.get('items')
    require(isinstance(items, list) and len(items) == data.get('count') == len(candidate_ids))
    require(type(data.get('matched_count')) is int and type(data.get('unmatched_count')) is int)
    matched = 0
    for item, candidate_id in zip(items, candidate_ids):
        require(isinstance(item, dict) and set(item) == {'candidate_id', 'frequency_hz', 'matched',
                                                        'rounded_frequency_hz', 'delta_hz',
                                                        'grid_step_hz', 'entry_name'})
        require(item['candidate_id'] == candidate_id)
        require(type(item['frequency_hz']) is int and 0 <= item['frequency_hz'] <= MAX_HZ)
        if item['matched'] is True:
            matched += 1
            entry = next((row for row in table['entries'] if row['name'] == item['entry_name']), None)
            require(entry is not None)
            require(type(item['rounded_frequency_hz']) is int and type(item['delta_hz']) is int)
            require(item['grid_step_hz'] == entry['grid_step_hz'])
            require(item['rounded_frequency_hz'] - item['frequency_hz'] == item['delta_hz'])
            require(entry['start_hz'] <= item['rounded_frequency_hz'] <= entry['stop_hz'])
            require((item['rounded_frequency_hz'] - entry['start_hz']) % entry['grid_step_hz'] == 0)
        else:
            require(item['matched'] is False)
            require(item['rounded_frequency_hz'] is None and item['delta_hz'] is None)
            require(item['grid_step_hz'] is None and item['entry_name'] is None)
    require(matched == data['matched_count'])
    require(matched + data['unmatched_count'] == len(items))
    return data


def validate_returned_draft(raw, table):
    """The echoed table must canonicalize to exactly the accepted raw draft."""
    if not isinstance(raw, str):
        raise ValueError('Черновик должен быть строкой')
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError('Некорректный JSON черновика') from exc
    require_equal(validate_artifact(_canonicalize(parsed)), table)


def require_equal(left, right):
    if left != right:
        raise ValueError('Таблица ответа не совпадает с отправленным черновиком')


def result_text(data):
    lines = [f"Таблица: «{data['table']['name']}». Кандидатов: {data['count']}; "
             f"совпало: {data['matched_count']}; вне таблицы: {data['unmatched_count']}. "
             f"Исходные частоты архива не изменялись."]
    for item in data['items']:
        if item['matched']:
            delta = item['delta_hz']
            lines.append(f"{item['candidate_id']}: {item['frequency_hz']} Гц → {item['rounded_frequency_hz']} Гц "
                         f"(Δ {delta:+d} Гц, {item['entry_name']}, шаг {item['grid_step_hz']} Гц)")
        else:
            lines.append(f"{item['candidate_id']}: {item['frequency_hz']} Гц — вне диапазонов таблицы, не округлялась")
    return '\n'.join(lines)


from .tk_timers import TkTimerOwner


class FrequencyRasterPanel(TkTimerOwner):
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = self.retired = self.data = None
        self.io_future = None
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='frequency-raster')
        self.io_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='raster-file')
        self._init_timers(app.root)
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Округление частот')
        ttk.Label(frame, text='Таблица сетки: диапазоны с шагом. Округление только по кнопке; исходные частоты кандидатов не изменяются. Частоты вне всех записей не округляются. Не калибровка и не сверка с нормативной сеткой каналов.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        for label, command in [('Загрузить JSON', self.load), ('НЕ НОРМАТИВ: пример 100 кГц', self.template)]:
            ttk.Button(row, text=label, command=command).pack(side='left')
        self.button = ttk.Button(row, text='Округлить выбранных кандидатов', command=self.analyze)
        self.button.pack(side='left')
        self.save_button = ttk.Button(row, text='Сохранить проверенную таблицу', command=self.save, state='disabled')
        self.save_button.pack(side='left')
        ttk.Button(row, text='Очистить результат', command=self.clear).pack(side='left')
        pick = ttk.Frame(frame); pick.pack(fill='x')
        ttk.Label(pick, text='Кандидаты сеанса (выбор: Ctrl/Shift)').pack(side='left')
        self.picker = tk.Listbox(pick, selectmode='extended', height=5, exportselection=False)
        self.picker.pack(side='left', fill='x', expand=True)
        self.editor = tk.Text(frame, height=9, wrap='none'); self.editor.pack(fill='both', expand=True)
        self.editor.bind('<<Modified>>', self.edited)
        self.info = tk.StringVar(value='Черновик не проверен. Округление только по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=8, state='disabled', wrap='none'); self.details.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview)
        scrollbar.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scrollbar.set)

    def show_details(self, value):
        self.details.configure(state='normal'); self.details.delete('1.0', 'end')
        self.details.insert('1.0', value); self.details.configure(state='disabled')

    def abandon(self):
        if self.future is not None:
            self.future.cancel()
            if not self.future.done(): self.retired = self.future
            self.future = None
        self.generation += 1
        self.button.configure(state='normal')

    def clear(self):
        self.abandon(); self.data = None
        self.save_button.configure(state='disabled')
        self.show_details('')
        self.info.set('Черновик не проверен. Округление только по кнопке; архив не изменяется.')

    def edited(self, event=None):
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self.clear()

    def set_draft(self, raw):
        self.clear()
        self.editor.delete('1.0', 'end'); self.editor.insert('1.0', raw)
        self.editor.edit_modified(False)

    def refresh_picker(self):
        selected = self.selected_ids()
        self.picker.delete(0, 'end')
        self.candidates = []
        if self.app.client is None or not self.app.session_id: return
        for candidate in self.app.snapshot.candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get('id'), str):
                continue
            self.picker.insert('end', f"{candidate['id']} · {candidate.get('center_frequency_hz')} Гц")
            self.candidates.append(candidate)
        for index, candidate in enumerate(self.candidates):
            if candidate['id'] in selected:
                self.picker.selection_set(index)

    def selected_ids(self):
        return [self.candidates[index]['id'] for index in self.picker.curselection()
                if index < len(getattr(self, 'candidates', []))]

    def selected_candidates(self):
        ids = self.selected_ids()
        if not ids: raise ValueError('Выберите хотя бы одного кандидата')
        if len(ids) > MAX_CANDIDATES: raise ValueError('Не более 256 кандидатов за раз')
        return ids

    def template(self):
        self.update()
        try:
            chosen = [item for item in getattr(self, 'candidates', []) if item['id'] in set(self.selected_ids())]
            self.set_draft(raster_template(chosen))
        except ValueError as exc: self.info.set(str(exc))

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            raw = check_draft(self.editor.get('1.0', 'end-1c'))
            candidate_ids = self.selected_candidates()
            if self.app.client is None or not self.app.session_id:
                raise ValueError('Нет активного сеанса')
        except (ValueError, UnicodeError) as exc:
            self.clear(); self.info.set(str(exc)); return
        self.clear()
        self.raw, self.candidate_ids = raw, candidate_ids
        self.expected = self.app.session_id
        self.info.set('Черновик не проверен; округление выполняется ядром…')
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        self.future = self.executor.submit(self.app.client.frequency_rounding,
                                           self.app.session_id, candidate_ids, raw)
        self._schedule_after(self.app.root, 'request', 50, self.finish, generation)

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.abandon(); self.info.set('Превышен срок ожидания 8 секунд. Черновик не проверен.'); return
        if not self.future.done():
            self._schedule_after(self.app.root, 'request', 50, self.finish, generation); return
        future, self.future = self.future, None
        self.button.configure(state='normal')
        try:
            data = validate_result(future.result(), self.expected, self.candidate_ids)
            validate_returned_draft(self.raw, data['table'])
            self.data = deepcopy(data)
            self.save_button.configure(state='normal')
            self.info.set(f"Проверена таблица «{data['table']['name']}». Результат зафиксирован; кандидаты в архиве не изменялись.")
            self.show_details(result_text(self.data))
        except Exception as exc:
            self.data = None
            self.info.set(f'Округление недоступно: {exc}. Черновик не проверен.')

    def load(self):
        from tkinter import filedialog
        if self.closed or self.io_future is not None: return
        path = filedialog.askopenfilename(parent=self.app.root, title='Загрузить черновик таблицы', filetypes=[('JSON', '*.json'), ('Все файлы', '*')])
        if not path: return
        self.update(); self.clear()
        generation = self.generation
        self.io_future = self.io_executor.submit(read_raster_file, path)
        self._schedule_after(self.app.root, 'file', 50, self.finish_io, generation, 'load')

    def save(self):
        from tkinter import filedialog, messagebox
        self.update()
        if self.closed or self.io_future is not None or self.data is None: return
        data = deepcopy(self.data)
        generation = self.generation
        path = filedialog.asksaveasfilename(parent=self.app.root, title=f"Сохранить проверенную таблицу: {data['table']['name']}", defaultextension='.json', initialfile='frequency-raster.json', confirmoverwrite=False, filetypes=[('JSON', '*.json')])
        if not path: return
        overwrite = os.path.lexists(path)
        if overwrite and not messagebox.askyesno('Заменить файл?', f'Заменить {path}?', parent=self.app.root): return
        self.update()
        if generation != self.generation or self.data is None: return
        self.io_future = self.io_executor.submit(save_raster_file, path, data['table'], overwrite)
        self._schedule_after(self.app.root, 'file', 50, self.finish_io, generation, 'save')

    def finish_io(self, generation, operation):
        if self.closed: return
        self.update()
        if self.io_future is None: return
        if not self.io_future.done():
            self._schedule_after(self.app.root, 'file', 50, self.finish_io, generation, operation); return
        future, self.io_future = self.io_future, None
        try:
            result = future.result()
            if generation != self.generation: return
            if operation == 'load': self.set_draft(result)
            else: self.info.set(f"Сохранена проверенная таблица «{self.data['table']['name']}»")
        except Exception as exc:
            if generation == self.generation: self.info.set(f'Ошибка файла таблицы: {exc}')

    def update(self):
        if self.closed: return
        scope = (id(self.app.client), self.app.session_id)
        if scope != self.scope:
            self.scope = scope
            self.clear()
            self.refresh_picker()
            return
        listing = [item.get('id') for item in self.app.snapshot.candidates
                   if isinstance(item, dict) and isinstance(item.get('id'), str)]
        if listing != [item['id'] for item in getattr(self, 'candidates', [])]:
            self.refresh_picker()

    def close(self):
        if self.closed: return
        self.closed = True
        self._close_timers()
        self.generation += 1
        for future in (self.future, self.retired, self.io_future):
            if future is not None: future.cancel()
        self.future = self.retired = self.io_future = self.data = None
        self.io_executor.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False, cancel_futures=True)
