"""Windows phonogram panel: five-second pre-history, runtime state only."""
from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from time import monotonic

PRE_HISTORY_SECONDS = 5.0


def validate_status(data, session, stream):
    def require(ok, message='Некорректное состояние фонограммы'):
        if not ok:
            raise ValueError(message)
    require(isinstance(data, dict))
    require(data.get('session_id') == session, 'Ответ другого сеанса')
    require(data.get('stream_id') == stream, 'Ответ другого потока')
    require(type(data.get('recording')) is bool)
    for key in ('pre_history_seconds', 'buffered_seconds', 'recorded_seconds'):
        value = data.get(key)
        require(type(value) in (int, float) and isfinite(value), f'Некорректное {key}')
    require(abs(data['pre_history_seconds'] - PRE_HISTORY_SECONDS) < 1e-9,
            'Предыстория должна быть ровно 5 секунд')
    last = data.get('last')
    if last is not None:
        require(isinstance(last, dict))
        for key in ('pre_history_seconds', 'recorded_seconds', 'duration_seconds'):
            value = last.get(key)
            require(type(value) in (int, float) and isfinite(value), f'Некорректное last.{key}')
        for key in ('pre_history_samples', 'recorded_samples', 'sample_rate_hz'):
            require(type(last.get(key)) is int and last[key] >= 0, f'Некорректное last.{key}')
        require(abs(last['duration_seconds'] - (last['pre_history_seconds'] + last['recorded_seconds'])) < 1e-9,
                'Длительность фонограммы не равна сумме частей')
    return data


def status_text(data):
    lines = [f"Предыстория: {data['pre_history_seconds']} с · буфер: {data['buffered_seconds']:.2f} с · запись: {'идёт' if data['recording'] else 'остановлена'} · записано: {data['recorded_seconds']:.2f} с"]
    if data.get('last'):
        last = data['last']
        lines.append(f"Последняя фонограмма: {last['duration_seconds']:.2f} с = {last['pre_history_seconds']:.2f} с предыстории + {last['recorded_seconds']:.2f} с записи · {last['sample_rate_hz']} Гц")
    lines.append('Запись — runtime-состояние; в архив ничего не пишется.')
    return '\n'.join(lines)


from .tk_timers import TkTimerOwner


class PhonogramPanel(TkTimerOwner):
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = None
        self.closed = False
        self.state = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='phonogram')
        self._init_timers(app.root)
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Фонограмма')
        ttk.Label(frame, text='Запись фонограммы с пятисекундной предысторией: окно последних 5 секунд буфера становится началом файла. Отсчёты поступают из аудиопотока или через API; панель их не генерирует. Runtime-состояние: в архив ничего не пишется.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.button = ttk.Button(row, text='Начать запись (предыстория 5 с)', command=self.start)
        self.button.pack(side='left')
        self.stop_button = ttk.Button(row, text='Остановить запись', command=self.stop)
        self.stop_button.pack(side='left')
        ttk.Button(row, text='Обновить состояние', command=self.refresh).pack(side='left')
        self.info = tk.StringVar(value='Состояние запрашивается по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=6, state='disabled', wrap='none')
        self.details.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview)
        scrollbar.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scrollbar.set)

    def show_details(self, value):
        self.details.configure(state='normal'); self.details.delete('1.0', 'end')
        self.details.insert('1.0', value); self.details.configure(state='disabled')

    def clear(self):
        self.generation += 1
        if self.future is not None: self.future.cancel()
        self.future = None
        self.button.configure(state="normal"); self.stop_button.configure(state="normal")
        self.state = None
        self.show_details('')
        self.info.set('Состояние запрашивается по кнопке; архив не изменяется.')

    def selected_stream(self):
        snapshot = self.app.snapshot
        stream = self.app.selected_stream_id()
        if (self.app.client is None or not self.app.session_id or not stream
                or (snapshot.session or {}).get('id') != self.app.session_id):
            raise ValueError('Нет активного сеанса и потока')
        return stream

    def call(self, action):
        self.update()
        if self.closed or self.future is not None: return
        try:
            stream = self.selected_stream()
        except (ValueError, TypeError) as exc:
            self.info.set(str(exc)); return
        self.info.set('Запрос выполняется ядром…')
        self.button.configure(state='disabled')
        self.stop_button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        client = self.app.client
        session = self.app.session_id
        if action == 'status':
            self.future = self.executor.submit(client.phonogram_status, session, stream)
        elif action == 'start':
            self.future = self.executor.submit(client.phonogram_start, session, stream)
        else:
            self.future = self.executor.submit(client.phonogram_stop, session, stream)
        self._schedule_after(self.app.root, 'request', 50, self.finish, generation, session, stream)

    def start(self):
        self.call('start')

    def stop(self):
        self.call('stop')

    def refresh(self):
        self.call('status')

    def finish(self, generation, session, stream):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.future = None
            self.info.set('Превышен срок ожидания 8 секунд.')
            self.button.configure(state='normal'); self.stop_button.configure(state='normal')
            return
        if not self.future.done():
            self._schedule_after(self.app.root, 'request', 50, self.finish, generation, session, stream); return
        future, self.future = self.future, None
        self.button.configure(state='normal'); self.stop_button.configure(state='normal')
        try:
            data = validate_status(future.result(), session, stream)
            self.state = data
            self.show_details(status_text(data))
            self.info.set('Состояние обновлено.')
        except Exception as exc:
            self.state = None
            self.info.set(f'Запрос недоступен: {exc}')

    def update(self):
        if self.closed: return
        scope = (id(self.app.client), self.app.session_id, self.app.selected_stream_id())
        if scope != self.scope:
            self.scope = scope
            self.clear()

    def close(self):
        if self.closed: return
        self.closed = True
        self._close_timers()
        self.generation += 1
        if self.future is not None: self.future.cancel()
        self.future = None
        self.executor.shutdown(wait=False, cancel_futures=True)
