"""Explicit, scoped named-reference catalog. All HTTP runs outside the Tk thread."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from time import monotonic
import unicodedata


def normalize_name(value):
    if not isinstance(value, str):
        raise ValueError('Имя должно быть строкой')
    value = unicodedata.normalize('NFC', value)
    if any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('Имя: 1–80 символов, без управляющих символов')
    value = value.strip()
    if not 1 <= len(value) <= 80:
        raise ValueError('Имя: 1–80 символов, без управляющих символов')
    return value


def validate_record(data, session, stream):
    def require(ok):
        if not ok:
            raise ValueError('Некорректный сохранённый опорный sweep')
    require(isinstance(data, dict))
    require(data.get('session_id') == session)
    require(isinstance(data.get('stream_id'), str) and bool(data['stream_id']))
    require(stream is None or data['stream_id'] == stream)
    for key in ('id', 'sweep_id'):
        require(isinstance(data.get(key), str) and bool(data[key]))
    require(normalize_name(data.get('name')) == data['name'])
    require(data.get('level_unit') == 'dBm')
    require(type(data.get('points')) is int and 1 <= data['points'] <= 16384)
    def utc(value):
        require(isinstance(value, str))
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.utcoffset() == timedelta(0))
        return parsed
    utc(data.get('created_at'))
    interval = data.get('interval')
    require(isinstance(interval, dict))
    require(utc(interval.get('started_at')) <= utc(interval.get('ended_at')))
    return data


def validate_catalog(data, session, stream):
    if (not isinstance(data, dict) or data.get('session_id') != session or data.get('stream_id') != stream
            or data.get('storage_mode') not in ('sqlite', 'memory')
            or type(data.get('persistent')) is not bool
            or (data['storage_mode'] == 'memory' and data['persistent'])
            or type(data.get('limit')) is not int or data['limit'] != 100
            or not isinstance(data.get('items'), list) or len(data['items']) > 100):
        raise ValueError('Некорректный каталог опорных sweeps')
    items = [validate_record(item, session, stream) for item in data['items']]
    for key in ('id', 'name'):
        if len({item[key] for item in items}) != len(items):
            raise ValueError('Повторяющиеся записи каталога')
    return data


class NamedReferenceCatalog:
    def __init__(self, panel, frame):
        self.panel = panel
        self.scope = None
        self.generation = 0
        self.future = None
        self.retired = None
        self.closed = False
        self.uncertain = False
        self.verified = False
        self.items = []
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sweep-references')
        tk, ttk = panel.app.tk, panel.app.ttk
        ttk.Label(frame, text='Имя архивного опорного sweep (1–80 символов)').pack(fill='x')
        self.name = tk.StringVar()
        ttk.Entry(frame, textvariable=self.name).pack(fill='x')
        self.save_button = ttk.Button(frame, text='Сохранить закреплённый опорный', command=self.save)
        self.save_button.pack()
        self.box = ttk.Combobox(frame, state='readonly', width=100)
        self.box.pack(fill='x')
        self.refresh_button = ttk.Button(frame, text='Обновить каталог', command=self.refresh)
        self.refresh_button.pack()
        self.use_button = ttk.Button(frame, text='Использовать выбранный сохранённый', command=self.use)
        self.use_button.pack()
        self.storage = tk.StringVar(value='Хранилище каталога ещё не проверено.')
        ttk.Label(frame, textvariable=self.storage, wraplength=1050).pack(fill='x')
        self.status = tk.StringVar(value='Каталог загружается при выборе сеанса/потока или по кнопке.')
        ttk.Label(frame, textvariable=self.status, wraplength=1050).pack(fill='x')

    def controls(self):
        busy = self.closed or self.future is not None or (self.retired is not None and not self.retired.done())
        valid_scope = self.scope is not None and bool(self.scope[1])
        self.save_button.configure(state='disabled' if busy or not valid_scope or not self.panel.reference or self.uncertain or not self.verified else 'normal')
        self.refresh_button.configure(state='disabled' if busy or not valid_scope else 'normal')
        matching = self.scope is not None and any(r['stream_id'] == self.scope[2] for r in self.items)
        self.use_button.configure(state='disabled' if busy or not matching else 'normal')

    def abandon(self):
        self.generation += 1
        if self.future is not None:
            self.future.cancel()
            if not self.future.done():
                self.retired = self.future
            self.future = None

    def update(self):
        if self.closed:
            return
        scope = self.panel.scope
        if scope != self.scope:
            was_write = self.future is not None and self.operation == 'save'
            self.abandon()
            self.scope = scope
            self.items = []
            self.box.configure(values=[])
            self.box.set('')
            self.name.set('')
            self.uncertain = False
            self.verified = False
            self.storage.set('Хранилище каталога ещё не проверено.')
            self.status.set('Область изменена. Предыдущая запись могла сохраниться на сервере; проверьте её каталог.' if was_write else 'Выбран новый сервер, сеанс или поток.')
            self.refresh(sync_scope=False)
        self.controls()

    def start(self, operation, function, *args):
        if self.closed or self.future is not None:
            return
        if self.retired is not None and not self.retired.done():
            self.status.set('Предыдущий запрос ещё завершается. Затем обновите каталог; запись могла сохраниться.')
            return
        self.retired = None
        self.operation = operation
        self.deadline = monotonic() + 8.0
        self.expected_scope = self.scope
        generation = self.generation
        self.future = self.executor.submit(function, *args)
        self.status.set('Сохранение опорного…' if operation == 'save' else 'Загрузка каталога…')
        self.controls()
        self.panel.app.root.after(50, lambda: self.finish(generation))

    def refresh(self, *, sync_scope=True):
        if sync_scope and not self.closed:
            self.panel.update()
        if self.closed or not self.scope or not self.scope[1] or self.panel.app.client is None:
            return
        self.start('list', self.panel.app.client.list_sweep_references, self.scope[1], self.scope[2])

    def save(self):
        self.panel.update()
        if (self.closed or self.future is not None or self.uncertain or not self.verified or not self.panel.reference
                or not self.scope or not self.scope[1] or self.panel.app.client is None):
            return
        try:
            name = normalize_name(self.name.get())
        except ValueError as exc:
            self.status.set(str(exc))
            return
        if any(r['name'] == name and r['sweep_id'] == self.panel.reference for r in self.items):
            self.status.set('Это имя и опорный sweep уже сохранены в каталоге; повторный запрос не отправлен.')
            return
        self.expected_save = (name, self.panel.reference)
        self.start('save', self.panel.app.client.save_sweep_reference, self.scope[1], name, self.panel.reference)

    def use(self):
        self.panel.update()
        if self.closed or self.future is not None:
            return
        index = self.box.current()
        if not 0 <= index < len(self.items):
            return
        record = self.items[index]
        if self.scope is None or record['stream_id'] != self.scope[2]:
            self.status.set('Сначала выберите поток сохранённого опорного sweep.')
            return
        self.panel.clear()
        self.panel.reference = record['sweep_id']
        self.panel.reference_interval = dict(record['interval'])
        self.panel.update()
        self.panel.info.set(f"Сохранённый опорный: {record['name']} · {record['sweep_id']}. Сравните по кнопке.")

    def show_items(self):
        self.box.configure(values=[f"{r['name']} · {r['sweep_id']} · UTC {r['interval']['started_at']} · dBm" for r in self.items])
        if self.items:
            self.box.current(0)
        else:
            self.box.set('')

    def finish(self, generation):
        if self.closed:
            return
        self.panel.update()
        if generation != self.generation or self.future is None:
            return
        if monotonic() >= self.deadline:
            write = self.operation == 'save'
            self.abandon()
            self.uncertain = write or self.uncertain
            if not write:
                self.verified = False
                self.items = []
                self.show_items()
                self.storage.set('Каталог не проверен. Обновите его перед сохранением опорного.')
            self.status.set('Срок ожидания 8 секунд истёк. Результат записи неизвестен; она могла сохраниться. Обновите каталог, автоматического повтора нет.' if write else 'Каталог: срок ожидания 8 секунд истёк. Обновите по кнопке.')
            self.controls()
            return
        if not self.future.done():
            self.panel.app.root.after(50, lambda: self.finish(generation))
            return
        future, self.future = self.future, None
        try:
            data = future.result()
            _, session, stream = self.expected_scope
            if self.operation == 'list':
                catalog = validate_catalog(data, session, stream)
                self.items = catalog['items']
                self.storage.set('SQLite: каталог сохраняется после перезапуска сервера.' if catalog['persistent'] else
                                 ('SQLite (:memory:): ' if catalog['storage_mode'] == 'sqlite' else 'Memory: ') + 'временный каталог; исчезает после перезапуска сервера.')
                self.uncertain = False
                self.verified = True
                self.status.set(f'Каталог: {len(self.items)} записей; максимум 100 на сеанс.')
            else:
                record = validate_record(data, session, stream)
                if (record['name'], record['sweep_id']) != self.expected_save:
                    raise ValueError('Ответ записи не соответствует запросу')
                items = [r for r in self.items if r['id'] != record['id']] + [record]
                if len(items) > 100 or len({r['name'] for r in items}) != len(items):
                    raise ValueError('Некорректное обновление каталога')
                self.items = items
                self.status.set('Опорный сохранён. Повтор того же имени и sweep безопасен; другие записи не изменены.')
            self.show_items()
        except Exception as exc:
            if self.operation == 'list':
                self.verified = False
                self.items = []
                self.show_items()
                self.storage.set('Каталог не проверен. Обновите его перед сохранением опорного.')
            status = getattr(exc, 'status', None)
            if self.operation == 'save' and status not in (400, 404, 409):
                self.uncertain = True
                self.status.set(f'Результат записи неизвестен: {exc}. Запись могла сохраниться; обновите каталог. Автоматического повтора нет.')
            elif status == 404:
                self.status.set('Каталог или sweep недоступен (404). Локальное закрепление и сравнение остаются доступны.')
            else:
                self.status.set(f'Каталог недоступен: {exc}')
        self.controls()

    def close(self):
        self.closed = True
        self.abandon()
        self.executor.shutdown(wait=False, cancel_futures=True)
