"""Windows DVB-T panel: verdicts apart from values, no demodulation claims."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from math import isfinite
from time import monotonic

from ..dvb_parameters import (
    CODE_RATES, DVB_METHODOLOGY, GUARD_INTERVALS, PARAMETER_FIELDS, THRESHOLD_FIELDS,
    DvbEvaluationError, evaluate_dvb_parameters,
)


def validate_result(data, session, sent):
    def require(ok, message='Некорректный ответ DVB-T'):
        if not ok:
            raise ValueError(message)
    require(isinstance(data, dict))
    require(data.get('session_id') == session, 'Ответ другого сеанса')
    require(data.get('methodology') == DVB_METHODOLOGY)
    require(data.get('result_code') in ('norm', 'not_norm'))
    require(data.get('guard_interval') == sent['guard_interval'], 'Ответ другого защитного интервала')
    require(data.get('code_rate') == sent['code_rate'], 'Ответ другой скорости кодирования')
    require(data.get('parameters') == sent['parameters'], 'Ответ других параметров')
    require(data.get('thresholds') == sent['thresholds'], 'Ответ других порогов')
    verdicts = data.get('verdicts')
    require(isinstance(verdicts, dict) and set(verdicts) == set(PARAMETER_FIELDS)
            and all(type(v) is bool for v in verdicts.values()), 'Некорректные вердикты')
    values = data.get('values')
    require(isinstance(values, dict) and set(values) == set(PARAMETER_FIELDS) | {'guard_interval_ratio', 'code_rate_ratio'}
            and all(type(v) in (int, float) and isfinite(v) for v in values.values()),
            'Значения результата не совпадают с методикой dvb_t_parameters')
    return data


def result_text(data):
    lines = [f"Код результата: {data['result_code']} · защитный интервал {data['guard_interval']} · скорость кодирования {data['code_rate']}"]
    for name in PARAMETER_FIELDS:
        verdict = 'в норме' if data['verdicts'][name] else 'вне нормы'
        lines.append(f"{name}: {data['parameters'][name]} — {verdict}")
    lines.append('Вердикт относится только к введённым числам и порогам; не измерение и не нормативное заключение.')
    return '\n'.join(lines)


class DvbEvaluationPanel:
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = self.retired = self.data = None
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dvb-evaluation')
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='DVB-T')
        ttk.Label(frame, text='Сверка MER, BER после Витерби и после РС с пороговыми значениями. Равенство порогу — норма. Это оценка введённых чисел: демодуляция IQ и измерение MER/BER прибором не выполняются. Архив не изменяется.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.entries = {}
        for label, key, default in (('MER, дБ', 'mer_db', '20'),
                                    ('BER после Витерби', 'ber_after_viterbi', '0.000001'),
                                    ('BER после РС', 'ber_after_rs', '0')):
            ttk.Label(row, text=label).pack(side='left')
            var = tk.StringVar(value=default); self.entries[key] = var
            ttk.Entry(row, textvariable=var, width=11).pack(side='left')
        row2 = ttk.Frame(frame); row2.pack(fill='x')
        for label, key, default in (('Порог MER', 'min_mer_db', '15'),
                                    ('Порог BER Витерби', 'max_ber_after_viterbi', '0.001'),
                                    ('Порог BER РС', 'max_ber_after_rs', '1e-9')):
            ttk.Label(row2, text=label).pack(side='left')
            var = tk.StringVar(value=default); self.entries[key] = var
            ttk.Entry(row2, textvariable=var, width=11).pack(side='left')
        row3 = ttk.Frame(frame); row3.pack(fill='x')
        ttk.Label(row3, text='Защитный интервал').pack(side='left')
        self.guard = ttk.Combobox(row3, state='readonly', width=8, values=list(GUARD_INTERVALS))
        self.guard.pack(side='left'); self.guard.set('1/4')
        ttk.Label(row3, text='Скорость кодирования').pack(side='left')
        self.code = ttk.Combobox(row3, state='readonly', width=8, values=list(CODE_RATES))
        self.code.pack(side='left'); self.code.set('2/3')
        self.button = ttk.Button(row3, text='Сравнить с порогами', command=self.analyze)
        self.button.pack(side='left')
        ttk.Button(row3, text='Очистить', command=self.clear).pack(side='left')
        self.info = tk.StringVar(value='Оценка запускается только по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=8, state='disabled', wrap='none')
        self.details.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview)
        scrollbar.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scrollbar.set)

    def show_details(self, value):
        self.details.configure(state='normal'); self.details.delete('1.0', 'end')
        self.details.insert('1.0', value); self.details.configure(state='disabled')

    def clear(self):
        self.abandon(); self.data = None
        self.show_details('')
        self.info.set('Оценка запускается только по кнопке; архив не изменяется.')

    def abandon(self):
        if self.future is not None:
            self.future.cancel()
            if not self.future.done(): self.retired = self.future
            self.future = None
        self.generation += 1
        self.button.configure(state='normal')

    def build_request(self):
        values = {}
        for key in PARAMETER_FIELDS:
            raw = self.entries[key].get().strip()
            try:
                values[key] = float(raw)
            except ValueError as exc:
                raise ValueError(f'{key}: конечное число') from exc
            if not isfinite(values[key]):
                raise ValueError(f'{key}: конечное число')
        limits = {}
        for key in THRESHOLD_FIELDS:
            raw = self.entries[key].get().strip()
            try:
                limits[key] = float(raw)
            except ValueError as exc:
                raise ValueError(f'{key}: конечное число') from exc
            if not isfinite(limits[key]):
                raise ValueError(f'{key}: конечное число')
        guard = self.guard.get()
        if guard not in GUARD_INTERVALS:
            raise ValueError('Некорректный защитный интервал')
        code_rate = self.code.get()
        if code_rate not in CODE_RATES:
            raise ValueError('Некорректная скорость кодирования')
        return {'parameters': values, 'thresholds': limits,
                'guard_interval': guard, 'code_rate': code_rate}

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            sent = self.build_request()
        except (ValueError, TypeError) as exc:
            self.info.set(str(exc)); return
        if self.app.client is None or not self.app.session_id:
            self.info.set('Нет активного сеанса.'); return
        self.clear()
        self.sent = sent
        self.expected = self.app.session_id
        self.info.set('Оценка параметров выполняется ядром…')
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        self.future = self.executor.submit(
            self.app.client.dvb_evaluation, self.app.session_id, sent)
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
            data = validate_result(future.result(), self.expected, self.sent)
            self.data = deepcopy(data)
            self.show_details(result_text(self.data))
            self.info.set(f"Код результата: {data['result_code']}.")
        except Exception as exc:
            self.data = None
            self.info.set(f'Оценка недоступна: {exc}')

    def update(self):
        if self.closed: return
        scope = (id(self.app.client), self.app.session_id)
        if scope != self.scope:
            self.scope = scope
            self.clear()

    def close(self):
        self.clear(); self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
