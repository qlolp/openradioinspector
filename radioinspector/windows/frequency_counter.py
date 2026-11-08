"""Built-in frequency counter on archived sweeps; honest resolution bound."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from math import isfinite
from time import monotonic

MAX_HZ = 9_007_199_254_740_991


def validate_request(sweep_id, resolution_hz, start_hz, stop_hz):
    def require(ok, message):
        if not ok:
            raise ValueError(message)
    require(isinstance(sweep_id, str) and bool(sweep_id.strip()) and len(sweep_id) <= 128,
            'Нужен ID архивного свипа не длиннее 128 символов')
    require(type(resolution_hz) is int and 1 <= resolution_hz <= MAX_HZ,
            'Разрешение: положительное целое число Гц')
    require(type(start_hz) is int and type(stop_hz) is int and 0 <= start_hz < stop_hz <= MAX_HZ,
            'Окно: целые Гц, 0 <= start < stop')
    return sweep_id, resolution_hz, start_hz, stop_hz


def validate_result(data, session, sent):
    def require(ok, message='Некорректный результат частотомера'):
        if not ok:
            raise ValueError(message)
    require(isinstance(data, dict))
    require(data.get('session_id') == session, 'Ответ другого сеанса')
    sweep = data.get('sweep')
    require(isinstance(sweep, dict) and sweep.get('id') == sent[0], 'Ответ другого свипа')
    window = data.get('window')
    require(isinstance(window, dict)
            and window.get('start_hz') == sent[2] and window.get('stop_hz') == sent[3],
            'Ответ другого окна измерения')
    require(data.get('resolution_hz') == sent[1], 'Ответ другого разрешения')
    require(data.get('method') in ('parabolic', 'peak_bin'))
    require(type(data.get('peak_index')) is int and data['peak_index'] >= 0)
    require(type(data.get('peak_frequency_hz')) is int and 0 <= data['peak_frequency_hz'] <= MAX_HZ)
    for key in ('peak_level_dbm', 'measured_frequency_hz', 'uncertainty_hz'):
        value = data.get(key)
        require(type(value) in (int, float) and isfinite(value), f'Некорректное {key}')
    require(type(data.get('raster_hz')) is int and data['raster_hz'] >= 0)
    require(sent[2] <= data['measured_frequency_hz'] <= sent[3], 'Измеренная частота вне окна')
    require(data['uncertainty_hz'] >= 0, 'Погрешность не может быть отрицательной')
    values = data.get('values')
    require(isinstance(values, dict)
            and set(values) == {'measured_frequency_hz', 'resolution_hz', 'uncertainty_hz'}
            and all(type(v) in (int, float) and isfinite(v) for v in values.values()),
            'Значения результата не совпадают с методикой frequency_counter')
    return data


def result_text(data):
    return (f"{data['measured_frequency_hz']} Гц ± {data['uncertainty_hz']} Гц · метод {data['method']}\n"
            f"Пик: {data['peak_frequency_hz']} Гц ({data['peak_level_dbm']} dBm), индекс {data['peak_index']}\n"
            f"Сетка: {data['raster_hz']} Гц · разрешение: {data['resolution_hz']} Гц\n"
            f"Свип: {data['sweep']['id']} · окно: {data['window']['start_hz']}…{data['window']['stop_hz']} Гц\n"
            "Погрешность — граница max(разрешение, сетка)/2. Исходные бины архива не изменялись.\n"
            "Значения готовы для записи в результаты измерений (методика frequency_counter).")


class FrequencyCounterPanel:
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = self.retired = self.data = None
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='frequency-counter')
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Частотомер')
        ttk.Label(frame, text='Измерение частоты пика архивного dBm свипа в окне. Параболическая интерполяция только при равномерной сетке; погрешность — граница max(разрешение, сетка)/2. Оценка по записанным данным, не показание аппаратного счётчика. Чтение без записи в архив.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Label(row, text='Окно: начало, Гц').pack(side='left')
        self.start = tk.StringVar(value='100'); ttk.Entry(row, textvariable=self.start, width=12).pack(side='left')
        ttk.Label(row, text='конец, Гц').pack(side='left')
        self.stop = tk.StringVar(value='400'); ttk.Entry(row, textvariable=self.stop, width=12).pack(side='left')
        ttk.Label(row, text='Разрешение, Гц').pack(side='left')
        self.resolution = tk.StringVar(value='10'); ttk.Entry(row, textvariable=self.resolution, width=8).pack(side='left')
        self.button = ttk.Button(row, text='Измерить частоту', command=self.analyze)
        self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        self.info = tk.StringVar(value='Измерение запускается только по кнопке.')
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
        self.info.set('Измерение запускается только по кнопке; архив не изменяется.')

    def abandon(self):
        if self.future is not None:
            self.future.cancel()
            if not self.future.done(): self.retired = self.future
            self.future = None
        self.generation += 1
        self.button.configure(state='normal')

    def selected_sweep(self):
        snapshot = self.app.snapshot
        if (self.app.client is None or not self.app.session_id or not self.app.selected_stream_id()
                or (snapshot.session or {}).get('id') != self.app.session_id):
            raise ValueError('Нет активного сеанса и потока')
        sweeps = [item for item in snapshot.sweeps if isinstance(item, dict)
                  and item.get('level_unit') == 'dBm'
                  and item.get('stream_id') == self.app.selected_stream_id()
                  and isinstance(item.get('id'), str) and item['id'].strip()]
        if not sweeps:
            raise ValueError('Нет архивного dBm свипа выбранного потока')
        return sweeps[-1]['id']

    def build_request(self):
        for name, text in (('начало', self.start), ('конец', self.stop), ('разрешение', self.resolution)):
            raw = text.get().strip()
            if not raw.isdigit():
                raise ValueError(f'{name}: целое неотрицательное число')
        sweep_id = self.selected_sweep()
        return validate_request(sweep_id, int(self.resolution.get().strip()),
                                int(self.start.get().strip()), int(self.stop.get().strip()))

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.retired is not None and not self.retired.done():
            self.info.set('Предыдущий запрос завершается; повторите позже.'); return
        self.retired = None
        try:
            sent = self.build_request()
        except (ValueError, TypeError) as exc:
            self.clear(); self.info.set(str(exc)); return
        self.clear()
        self.sent = sent
        self.expected = self.app.session_id
        self.info.set('Измерение выполняется ядром…')
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        self.future = self.executor.submit(
            self.app.client.frequency_counter, self.app.session_id, *sent)
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
            self.info.set(f"Измерено: {data['measured_frequency_hz']} Гц ± {data['uncertainty_hz']} Гц.")
        except Exception as exc:
            self.data = None
            self.info.set(f'Измерение недоступно: {exc}')

    def update(self):
        if self.closed: return
        scope = (id(self.app.client), self.app.session_id, self.app.selected_stream_id())
        if scope != self.scope:
            self.scope = scope
            self.clear()

    def close(self):
        self.clear(); self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
