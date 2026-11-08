"""Measurement results with explicit methodology; values stay apart from the code."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic
import json

from ..measurement_results import METHODOLOGIES, RESULT_CODES


def validate_record(data, session, stream):
    def require(ok):
        if not ok:
            raise ValueError('Некорректный результат измерения')
    require(isinstance(data, dict))
    require(data.get('session_id') == session)
    require(isinstance(data.get('stream_id'), str) and bool(data['stream_id']))
    require(stream is None or data['stream_id'] == stream)
    require(isinstance(data.get('id'), str) and bool(data['id']))
    methodology = data.get('methodology')
    require(methodology in METHODOLOGIES)
    require(data.get('result_code') in RESULT_CODES)
    frequency = data.get('frequency_hz')
    require(type(frequency) is int and 0 <= frequency <= 9_007_199_254_740_991)
    def utc(value):
        require(isinstance(value, str))
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.utcoffset() == timedelta(0))
        return parsed
    interval = data.get('interval')
    require(isinstance(interval, dict))
    require(utc(interval.get('started_at')) <= utc(interval.get('ended_at')))
    require(isinstance(data.get('created_at'), str))
    values = data.get('values')
    if data['result_code'] == 'not_measured':
        require(values == {})
    else:
        fields = METHODOLOGIES[methodology]['fields']
        require(isinstance(values, dict) and set(values) == set(fields))
        for name in fields:
            value = values[name]
            require(type(value) in (int, float) and isfinite(value))
    return data


def validate_payload(body, stream):
    def require(ok):
        if not ok:
            raise ValueError('Некорректный запрос результата измерения')
    require(isinstance(body, dict))
    require(set(body) == {'methodology', 'result_code', 'frequency_hz', 'interval', 'values', 'stream_id'})
    require(body['stream_id'] == stream)
    methodology = body['methodology']
    require(methodology in METHODOLOGIES)
    require(body['result_code'] in RESULT_CODES)
    frequency = body['frequency_hz']
    require(type(frequency) is int and 0 <= frequency <= 9_007_199_254_740_991)
    interval = body['interval']
    require(isinstance(interval, dict) and set(interval) == {'started_at', 'ended_at'})
    for key in ('started_at', 'ended_at'):
        value = interval[key]
        require(isinstance(value, str) and bool(value))
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.utcoffset() == timedelta(0))
    require(datetime.fromisoformat(interval['started_at'].replace('Z', '+00:00'))
            <= datetime.fromisoformat(interval['ended_at'].replace('Z', '+00:00')))
    values = body['values']
    if body['result_code'] == 'not_measured':
        require(values == {})
    else:
        fields = METHODOLOGIES[methodology]['fields']
        require(isinstance(values, dict) and set(values) == set(fields))
        for name in fields:
            value = values[name]
            require(type(value) in (int, float) and isfinite(value))
    return body


def result_text(items):
    lines = [f'Результатов: {len(items)}. Методика указана явно; код результата отделён от значений.']
    for record in items:
        values = ', '.join(f'{name}={value}' for name, value in sorted(record['values'].items())) or 'без значений'
        lines.append(f"[{record['methodology']}] {record['result_code']} · {record['frequency_hz']} Гц · "
                     f"{record['interval']['started_at']}…{record['interval']['ended_at']} · {values}")
    return '\n'.join(lines)


class MeasurementResultPanel:
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = self.retired = None
        self.closed = False
        self.items = []
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='measurement-results')
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Результаты измерений')
        ttk.Label(frame, text='Методика — явное поле результата; код результата (norm / not_norm / not_measured) хранится отдельно от числовых значений. Запись привязана к сеансу, потоку и интервалу UTC. Исходные измерения архива не изменяются.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Label(row, text='Методика').pack(side='left')
        self.methodology = ttk.Combobox(row, state='readonly', width=28,
                                        values=sorted(METHODOLOGIES))
        self.methodology.pack(side='left')
        self.methodology.set(sorted(METHODOLOGIES)[0])
        ttk.Label(row, text='Код').pack(side='left')
        self.code = ttk.Combobox(row, state='readonly', width=12, values=list(RESULT_CODES))
        self.code.pack(side='left')
        self.code.set('norm')
        self.frequency = tk.StringVar(value='101800000')
        ttk.Label(row, text='Частота, Гц').pack(side='left')
        ttk.Entry(row, textvariable=self.frequency, width=14).pack(side='left')
        self.started = tk.StringVar(value='2026-11-08T12:00:00Z')
        self.ended = tk.StringVar(value='2026-11-08T12:00:01Z')
        ttk.Label(row, text='Начало UTC').pack(side='left')
        ttk.Entry(row, textvariable=self.started, width=22).pack(side='left')
        ttk.Label(row, text='Окончание UTC').pack(side='left')
        ttk.Entry(row, textvariable=self.ended, width=22).pack(side='left')
        ttk.Label(frame, text='Значения результата · JSON (точно поля методики; для not_measured — {})').pack(fill='x')
        self.editor = tk.Text(frame, height=3, wrap='none'); self.editor.pack(fill='x')
        self.editor.insert('1.0', '{}')
        row2 = ttk.Frame(frame); row2.pack(fill='x')
        self.button = ttk.Button(row2, text='Записать результат', command=self.analyze)
        self.button.pack(side='left')
        ttk.Button(row2, text='Обновить список', command=self.refresh).pack(side='left')
        ttk.Button(row2, text='Очистить', command=self.clear).pack(side='left')
        self.info = tk.StringVar(value='Запись выполняется только по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=9, state='disabled', wrap='none')
        self.details.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview)
        scrollbar.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scrollbar.set)

    def show_details(self, value):
        self.details.configure(state='normal'); self.details.delete('1.0', 'end')
        self.details.insert('1.0', value); self.details.configure(state='disabled')

    def clear(self):
        self.abandon()
        self.show_details('')
        self.info.set('Запись выполняется только по кнопке; архив не изменяется.')

    def abandon(self):
        if self.future is not None:
            self.future.cancel()
            if not self.future.done(): self.retired = self.future
            self.future = None
        self.generation += 1
        self.button.configure(state='normal')

    def selected_stream(self):
        snapshot = self.app.snapshot
        stream = self.app.selected_stream_id()
        if (self.app.client is None or not self.app.session_id or not stream
                or (snapshot.session or {}).get('id') != self.app.session_id):
            raise ValueError('Нет активного сеанса и потока')
        return stream

    def build_payload(self):
        methodology = self.methodology.get()
        if methodology not in METHODOLOGIES:
            raise ValueError('Выберите методику из каталога')
        code = self.code.get()
        if code not in RESULT_CODES:
            raise ValueError('Некорректный код результата')
        raw = self.frequency.get().strip()
        if not raw.isdigit():
            raise ValueError('Частота: целое неотрицательное число Гц')
        try:
            values = json.loads(self.editor.get('1.0', 'end-1c') or '{}')
        except ValueError as exc:
            raise ValueError('Значения должны быть JSON-объектом') from exc
        body = {
            'methodology': methodology, 'result_code': code, 'frequency_hz': int(raw),
            'interval': {'started_at': self.started.get().strip(), 'ended_at': self.ended.get().strip()},
            'values': values, 'stream_id': self.selected_stream(),
        }
        return validate_payload(body, body['stream_id'])

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            body = self.build_payload()
        except (ValueError, TypeError) as exc:
            self.info.set(str(exc)); return
        self.info.set('Запись результата выполняется ядром…')
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        stream = body['stream_id']
        self.expected = (self.app.session_id, stream)
        self.future = self.executor.submit(
            self.app.client.save_measurement_result, self.app.session_id, body)
        self.app.root.after(50, lambda: self.finish(generation))

    def finish(self, generation):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.abandon(); self.info.set('Превышен срок ожидания 8 секунд.'); return
        if not self.future.done():
            self.app.root.after(50, lambda: self.finish(generation)); return
        future, self.future = self.future, None
        self.button.configure(state='normal')
        try:
            record = future.result()
            validate_record(record, self.expected[0], self.expected[1])
            self.items = [item for item in self.items if item['id'] != record['id']] + [record]
            self.show_details(result_text(self.items))
            self.info.set(f"Результат {record['id']} записан: {record['methodology']} / {record['result_code']}.")
        except Exception as exc:
            self.info.set(f'Запись недоступна: {exc}')

    def refresh(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.app.client is None or not self.app.session_id:
            self.info.set('Нет активного сеанса.'); return
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        session = self.app.session_id
        self.future = self.executor.submit(
            self.app.client.list_measurement_results, session)
        self.app.root.after(50, lambda: self.finish_refresh(generation, session))

    def finish_refresh(self, generation, session):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.abandon(); self.info.set('Превышен срок ожидания 8 секунд.'); return
        if not self.future.done():
            self.app.root.after(50, lambda: self.finish_refresh(generation, session)); return
        future, self.future = self.future, None
        self.button.configure(state='normal')
        try:
            items = future.result()
            self.items = [validate_record(item, session, None) for item in items]
            self.show_details(result_text(self.items))
            self.info.set(f'Загружено результатов: {len(self.items)}.')
        except Exception as exc:
            self.info.set(f'Список недоступен: {exc}')

    def update(self):
        if self.closed: return
        scope = (id(self.app.client), self.app.session_id)
        if scope != self.scope:
            self.scope = scope
            self.items = []
            self.clear()

    def close(self):
        self.clear(); self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
