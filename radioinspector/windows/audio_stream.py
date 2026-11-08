"""Windows audio-stream panel: network delivery of demodulated PCM, runtime only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic

CHUNK_BYTES = 8192


def validate_status(data, session, stream):
    def require(ok, message='Некорректное состояние сетевой трансляции'):
        if not ok:
            raise ValueError(message)
    require(isinstance(data, dict))
    require(data.get('session_id') == session, 'Ответ другого сеанса')
    require(data.get('stream_id') == stream, 'Ответ другого потока')
    for key in ('subscribers', 'max_subscribers', 'queued_blocks', 'dropped_blocks'):
        value = data.get(key)
        require(type(value) is int and not isinstance(value, bool) and value >= 0,
                f'Некорректное {key}')
    require(0 <= data['subscribers'] <= data['max_subscribers'],
            'Число подписчиков вне диапазона')
    return data


def status_text(data):
    return (f"Подписчиков: {data['subscribers']} из {data['max_subscribers']} · "
            f"в очередях {data['queued_blocks']} блоков · "
            f"потеряно {data['dropped_blocks']} блоков.\n"
            "Трансляция — runtime-доставка: запись и архив не изменяются.")


def pump_text(stats, running):
    if stats.get('error') and not running:
        line = f"Трансляция прервана: {stats['error']}"
    elif running:
        line = "Трансляция открыта: получено {received} байт PCM16LE ({blocks} блоков).".format(
            received=stats['received_bytes'], blocks=stats['blocks'])
    else:
        line = "Трансляция завершена: получено {received} байт PCM16LE ({blocks} блоков).".format(
            received=stats['received_bytes'], blocks=stats['blocks'])
    return (line + "\nБлоки PCM16LE поступают по мере того, как рекордер принимает отсчёты; "
            "остановленный клиент теряет только собственную очередь.")


class AudioStreamPanel:
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = None
        self.closed = False
        self.state = None
        self.pump_generation = None
        self.pump_stats = None
        self.stop_event = None
        self.pump_future = None
        self.connection = None
        self._connection_lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-stream')
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Аудио по сети')
        ttk.Label(frame, text='Трансляция демодулированного аудио по сети (RemoteMD): блоки PCM16LE поступают клиенту по мере того, как рекордер принимает отсчёты. Панель не генерирует аудио и не пишет архив: поток — доставка, а не запись. Остановленный клиент теряет только собственную очередь (потери считаются).', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        self.connect_button = ttk.Button(row, text='Подключить трансляцию', command=self.connect)
        self.connect_button.pack(side='left')
        self.disconnect_button = ttk.Button(row, text='Отключить', command=self.disconnect)
        self.disconnect_button.pack(side='left')
        ttk.Button(row, text='Обновить статус', command=self.refresh).pack(side='left')
        self.info = tk.StringVar(value='Трансляция запускается кнопкой; статус запрашивается по кнопке.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=6, state='disabled', wrap='none')
        self.details.pack(fill='both', expand=True)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.details.yview)
        scrollbar.pack(side='right', fill='y'); self.details.configure(yscrollcommand=scrollbar.set)

    def show_details(self, value):
        self.details.configure(state='normal'); self.details.delete('1.0', 'end')
        self.details.insert('1.0', value); self.details.configure(state='disabled')

    def clear(self):
        self.state = None
        self.show_details('')
        self.info.set('Трансляция запускается кнопкой; архив не изменяется.')

    def selected_stream(self):
        snapshot = self.app.snapshot
        stream = self.app.selected_stream_id()
        if (self.app.client is None or not self.app.session_id or not stream
                or (snapshot.session or {}).get('id') != self.app.session_id):
            raise ValueError('Нет активного сеанса и потока')
        return stream

    def connect(self):
        self.update()
        if self.closed or self.pump_future is not None or self.future is not None:
            return
        try:
            stream = self.selected_stream()
        except (ValueError, TypeError) as exc:
            self.info.set(str(exc)); return
        session = self.app.session_id
        self.generation += 1
        generation = self.generation
        stats = {'received_bytes': 0, 'blocks': 0, 'error': None, 'done': False}
        self.stop_event = Event()
        self.pump_generation = generation
        self.pump_stats = stats
        self.info.set('Подключение к аудиопотоку…')
        self.connect_button.configure(state='disabled')
        self.disconnect_button.configure(state='normal')
        self.pump_future = self.executor.submit(
            self._pump, session, stream, generation, stats, self.stop_event)
        self.app.root.after(200, lambda: self._display_pump(generation, stats))

    def _pump(self, session, stream, generation, stats, stop_event):
        connection = None
        try:
            connection = self.app.client.audio_stream_open(session, stream)
            with self._connection_lock:
                if generation != self.generation:
                    connection.close()
                    return
                self.connection = connection
            while not stop_event.is_set():
                block = connection.read_block(CHUNK_BYTES)
                if not block:
                    break
                stats['received_bytes'] += len(block)
                stats['blocks'] += 1
        except Exception as exc:
            if not stop_event.is_set():
                stats['error'] = str(exc)
        finally:
            if connection is not None:
                connection.close()
            with self._connection_lock:
                if self.connection is connection:
                    self.connection = None
            stats['done'] = True

    def _display_pump(self, generation, stats):
        if self.closed or generation != self.generation:
            return
        running = self.pump_future is not None and not stats['done']
        self.show_details(pump_text(stats, running))
        if running:
            self.app.root.after(200, lambda: self._display_pump(generation, stats))
            return
        self.pump_future = None
        self.connect_button.configure(state='normal')
        self.disconnect_button.configure(state='disabled')
        self.info.set('Трансляция завершена.' if not stats.get('error')
                      else f"Трансляция недоступна: {stats['error']}")

    def disconnect(self):
        self.stop_pump()
        if self.pump_stats is not None:
            self.info.set('Трансляция отключена.')

    def stop_pump(self):
        if self.stop_event is not None:
            self.stop_event.set()
        with self._connection_lock:
            connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()

    def call_status(self):
        self.update()
        if self.closed or self.future is not None or self.pump_future is not None:
            return
        try:
            stream = self.selected_stream()
        except (ValueError, TypeError) as exc:
            self.info.set(str(exc)); return
        self.info.set('Запрос выполняется ядром…')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        self.future = self.executor.submit(self.app.client.audio_stream_status,
                                           self.app.session_id, stream)
        self.app.root.after(50, lambda: self.finish(generation, self.app.session_id, stream))

    def refresh(self):
        self.call_status()

    def finish(self, generation, session, stream):
        if self.closed:
            return
        self.update()
        if generation != self.generation or self.future is None:
            return
        if monotonic() >= self.deadline:
            self.future = None
            self.info.set('Превышен срок ожидания 8 секунд.')
            return
        if not self.future.done():
            self.app.root.after(50, lambda: self.finish(generation, session, stream))
            return
        future, self.future = self.future, None
        try:
            data = validate_status(future.result(), session, stream)
            self.state = data
            self.show_details(status_text(data))
            self.info.set('Статус обновлён.')
        except Exception as exc:
            self.state = None
            self.info.set(f'Запрос недоступен: {exc}')

    def update(self):
        if self.closed:
            return
        scope = (id(self.app.client), self.app.session_id)
        if scope != self.scope:
            self.scope = scope
            self.generation += 1
            self.stop_pump()
            self.pump_future = None
            self.pump_stats = None
            self.future = None
            self.clear()
            self.connect_button.configure(state='normal')
            self.disconnect_button.configure(state='disabled')

    def close(self):
        self.closed = True
        self.stop_pump()
        self.executor.shutdown(wait=False, cancel_futures=True)
