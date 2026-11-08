"""Windows signal-list panel: export archived candidates, import as new ones."""
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from ..signal_list import MAX_SIGNAL_LIST_BYTES, SIGNAL_LIST_FORMAT, parse_signal_list


def validate_export(data, session):
    def require(ok, message='Некорректный ответ экспорта'):
        if not ok:
            raise ValueError(message)
    require(isinstance(data, dict))
    require(data.get('session_id') == session, 'Ответ другого сеанса')
    require(type(data.get('exported')) is int and data['exported'] >= 1)
    listing = data.get('list')
    require(isinstance(listing, dict) and listing.get('format') == SIGNAL_LIST_FORMAT)
    require(isinstance(listing.get('entries'), list) and len(listing['entries']) == data['exported'])
    return data


def validate_import(data, session, stream, sent_count):
    def require(ok, message='Некорректный ответ импорта'):
        if not ok:
            raise ValueError(message)
    require(isinstance(data, dict))
    require(data.get('session_id') == session, 'Ответ другого сеанса')
    require(data.get('stream_id') == stream, 'Ответ другого потока')
    require(data.get('imported') == sent_count, 'Число импортированных не совпало')
    ids = data.get('candidate_ids')
    require(isinstance(ids, list) and len(ids) == sent_count)
    return data


def result_text(action, data):
    if action == 'export':
        return (f"Экспортировано сигналов: {data['exported']} («{data['list']['name']}»).\n"
                'Исходные записи архива не изменялись.')
    return (f"Импортировано сигналов: {data['imported']} («{data['name']}») в поток {data['stream_id']}.\n"
            'Импортированные сигналы начинаются с состояния new; единицы измерения не конвертируются.')


from .tk_timers import TkTimerOwner


class SignalListPanel(TkTimerOwner):
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = None
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='signal-list')
        self._init_timers(app.root)
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Список сигналов')
        ttk.Label(frame, text='Экспорт сериализует архивных кандидатов сеанса; импорт создаёт новых кандидатов через обычный жизненный цикл. Единицы измерения не конвертируются: dBm и s_meter_count никогда не пересчитываются друг в друга.', wraplength=1050).pack(fill='x')
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Label(row, text='Имя списка').pack(side='left')
        self.name = tk.StringVar(value='Список сигналов')
        ttk.Entry(row, textvariable=self.name, width=24).pack(side='left')
        self.button = ttk.Button(row, text='Выгрузить список', command=self.export)
        self.button.pack(side='left')
        self.import_button = ttk.Button(row, text='Загрузить список', command=self.import_list)
        self.import_button.pack(side='left')
        ttk.Label(frame, text='Черновик списка · JSON (экспорт заполнит; можно вставить чужой список)').pack(fill='x')
        self.editor = tk.Text(frame, height=10, wrap='none'); self.editor.pack(fill='both', expand=True)
        self.info = tk.StringVar(value='Обмен выполняется только по кнопкам.')
        ttk.Label(frame, textvariable=self.info, wraplength=1050).pack(fill='x')
        self.details = tk.Text(frame, height=5, state='disabled', wrap='none')
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
        self.button.configure(state="normal"); self.import_button.configure(state="normal")
        self.show_details('')
        self.info.set('Обмен выполняется только по кнопкам; архив изменяется только при импорте.')

    def selected_stream(self):
        snapshot = self.app.snapshot
        stream = self.app.selected_stream_id()
        if (self.app.client is None or not self.app.session_id or not stream
                or (snapshot.session or {}).get('id') != self.app.session_id):
            raise ValueError('Нет активного сеанса и потока')
        return stream

    def run(self, action):
        self.update()
        if self.closed or self.future is not None: return
        client = self.app.client
        session = self.app.session_id
        if client is None or not session:
            self.info.set('Нет активного сеанса.'); return
        try:
            stream = self.selected_stream()
            if action == 'import':
                raw = self.editor.get('1.0', 'end-1c')
                if not raw.strip():
                    raise ValueError('Вставьте список сигналов в черновик')
                if len(raw.encode('utf-8')) > MAX_SIGNAL_LIST_BYTES:
                    raise ValueError('Список превышает 262 144 байт')
                artifact = parse_signal_list(raw)
                sent_count = len(artifact['entries'])
                target = self.executor.submit(client.signal_list_import, session, stream, raw)
            else:
                sent_count = None
                target = self.executor.submit(client.signal_list_export, session, self.name.get().strip() or 'Список сигналов', stream)
        except (ValueError, TypeError) as exc:
            self.info.set(str(exc)); return
        self.info.set('Запрос выполняется ядром…')
        self.button.configure(state='disabled'); self.import_button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        self.future = target
        self._schedule_after(self.app.root, 'request', 50, self.finish, generation, action, session, stream, sent_count)

    def export(self):
        self.run('export')

    def import_list(self):
        self.run('import')

    def finish(self, generation, action, session, stream, sent_count):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.future = None
            self.info.set('Превышен срок ожидания 8 секунд.')
            self.button.configure(state='normal'); self.import_button.configure(state='normal')
            return
        if not self.future.done():
            self._schedule_after(self.app.root, 'request', 50, self.finish, generation, action, session, stream, sent_count); return
        future, self.future = self.future, None
        self.button.configure(state='normal'); self.import_button.configure(state='normal')
        try:
            data = future.result()
            if action == 'export':
                validated = validate_export(data, session)
                self.editor.delete('1.0', 'end')
                self.editor.insert('1.0', __import__('json').dumps(validated['list'], ensure_ascii=False, indent=2))
            else:
                validated = validate_import(data, session, stream, sent_count)
            self.show_details(result_text(action, validated))
            self.info.set('Обмен завершён.')
        except Exception as exc:
            self.info.set(f'Обмен недоступен: {exc}')

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
