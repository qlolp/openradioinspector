"""Thin, explicit field-estimation client; calculations remain in the core."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from math import isfinite
from time import monotonic

from ..field_strength import MAX_FIELD_REQUEST_BYTES, validate_field_request
from ..spectral_mask import strict_json_loads
from .frequency_counter import FrequencyCounterPanel


def validate_result(data, session, stream, sent):
    def require(ok):
        if not ok: raise ValueError('Ответ оценки поля не соответствует источнику и калибровке')
    require(isinstance(data, dict) and data.get('methodology') == 'field_strength_rms_v2'
            and data.get('unit') == 'V/m')
    for name in ('value', 'value_dbuv_per_m'):
        require(type(data.get(name)) in (int, float) and isfinite(data[name]))
    require(data['value'] > 0)
    uncertainty = data.get('uncertainty_db')
    require('uncertainty_db' in data and (uncertainty is None
            or (type(uncertainty) in (int, float) and isfinite(uncertainty) and uncertainty >= 0)))
    source = data.get('source', {})
    require(isinstance(source, dict) and source.get('session_id') == session
            and source.get('stream_id') == stream and source.get('id') == sent['sweep_id']
            and source.get('frequency_hz') == sent['frequency_hz'])
    calibration = data.get('calibration', {})
    canonical = json.dumps(sent['calibration'], sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    require(isinstance(calibration, dict) and calibration.get('profile') == sent['calibration']
            and calibration.get('sha256') == hashlib.sha256(canonical.encode('utf-8')).hexdigest())
    return data


def result_text(data):
    uncertainty = data['uncertainty_db']
    return (f"{data['value']:.9g} V/m ({data['value_dbuv_per_m']:.6g} dBuV/m) · "
            + ('погрешность неизвестна' if uncertainty is None else f'погрешность {uncertainty:g} dB')
            + f"\nУзкополосная RMS-эквивалентная оценка CW · свип {data['source']['id']}"
            + f" · {data['source']['frequency_hz']} Гц\nКалибровка: {data['calibration']['sha256']}")


class FieldStrengthPanel(FrequencyCounterPanel):
    """Reuse the established bounded request/timer/scope lifecycle."""
    def __init__(self, app, notebook):
        self.app = app; self.scope = None; self.generation = 0
        self.future = self.retired = self.data = None; self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='field-strength')
        self._init_timers(app.root)
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8); notebook.add(frame, text='Поле RMS')
        ttk.Label(frame, text='Узкополосная RMS-эквивалентная оценка CW. Нужен профиль калибровки выбранного тракта и совпадающие записанные настройки RMS/CW/RBW. Без них расчёт недоступен. Охрана и исходники не изменяются.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Label(row, text='Частота исходного бина, Гц').pack(side='left')
        self.frequency = tk.StringVar(); ttk.Entry(row, textvariable=self.frequency, width=20).pack(side='left')
        self.button = ttk.Button(row, text='Оценить последний свип', command=self.analyze); self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        ttk.Label(frame, text='Профиль калибровки JSON · до 64 КиБ · без экстраполяции').pack(anchor='w')
        self.editor = tk.Text(frame, height=10, wrap='none'); self.editor.pack(fill='both', expand=True)
        self.editor.bind('<<Modified>>', self._draft_changed)
        self.frequency.trace_add('write', lambda *_: self.clear())
        self.info = tk.StringVar(value='Загрузите калибровку тракта; расчёт только по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=5, state='disabled', wrap='word'); self.details.pack(fill='both')

    def _draft_changed(self, _event):
        if self.editor.edit_modified():
            self.editor.edit_modified(False); self.clear()

    def clear(self):
        super().clear()
        self.info.set('Оценка только по кнопке; нужен совместимый RMS/CW профиль калибровки.')

    def build_request(self):
        snapshot = self.app.snapshot
        if self.app.client is None or (snapshot.session or {}).get('id') != self.app.session_id:
            raise ValueError('Нет подключённого сеанса')
        choices = [s for s in snapshot.sweeps if s.get('stream_id') == self.app.selected_stream_id()]
        if not choices: raise ValueError('Нет архивного свипа выбранного потока')
        text = self.editor.get('1.0', 'end-1c')
        if len(text.encode('utf-8')) > MAX_FIELD_REQUEST_BYTES: raise ValueError('Профиль больше 64 КиБ')
        payload = {'sweep_id': choices[-1]['id'], 'frequency_hz': int(self.frequency.get()),
                   'calibration': strict_json_loads(text)}
        return validate_field_request(payload)

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается'); return
        self.retired = None
        try: sent = self.build_request()
        except (ValueError, TypeError) as exc:
            self.clear(); self.info.set(f'Оценка недоступна: {exc}'); return
        # The currently consumed edit may still have a queued Tk notification.
        # Later edits set this flag again and invalidate the in-flight result.
        self.editor.edit_modified(False)
        self.clear(); self.sent = sent; self.expected = self.scope
        self.deadline = monotonic() + 6
        self.button.configure(state='disabled'); self.info.set('Выполняется оценка ядром…')
        self.future = self.executor.submit(self.app.client.field_strength_rms_v2, self.app.session_id, sent)
        self._schedule_after(self.app.root, 'request', 20, self.finish, self.generation)

    def finish(self, generation):
        self.update()
        if self.closed or generation != self.generation or self.future is None: return
        if monotonic() > self.deadline:
            self.clear(); self.info.set('Время ожидания истекло'); return
        if not self.future.done():
            self._schedule_after(self.app.root, 'request', 20, self.finish, generation); return
        future = self.future; self.future = None; self.button.configure(state='normal')
        try:
            self.data = validate_result(future.result(), self.expected[1], self.expected[2], self.sent)
            self.info.set(result_text(self.data)); self.show_details(json.dumps(self.data, ensure_ascii=False, indent=2))
        except Exception as exc:
            self.data = None; self.info.set(f'Оценка недоступна: {exc}')
