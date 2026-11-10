"""Thin monitoring workspace: the core owns all capture and receiver control."""
import io
import json
import sys
import wave
from functools import partial
from pathlib import Path
from urllib.parse import quote


RECEIVER_MODES = ('AUTO', 'FM', 'AM', 'DSTAR', 'YSF', 'ALINCO', 'NXDN', 'P25', 'DPMR', 'DMR')


def _checked_wav(raw):
    if not isinstance(raw, bytes) or len(raw) > 8 * 1024 * 1024:
        raise ValueError('Некорректная или слишком большая WAV-запись')
    with wave.open(io.BytesIO(raw), 'rb') as wav:
        if wav.getnframes() <= 0:
            raise ValueError('Аудиозапись пуста')
    return raw


def _download(client, ref):
    return _checked_wav(client.instrument_artifact(ref['session_id'], ref['stream_id'], ref['id']))


def _play(raw):
    import winsound
    winsound.PlaySound(raw, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    return {'playback': 'completed'}


def _save(raw, path):
    Path(path).write_bytes(raw)
    return {'saved': str(path)}


class MonitoringPanel:
    def __init__(self, app, notebook):
        self.app, self.notebook = app, notebook
        self._client = app.client
        self._revision = getattr(app, '_revision', 0)
        self.generation = 0
        self.items, self.events, self.devices = [], [], []
        self.event = None
        self.active = None
        tk, ttk = app.tk, app.ttk
        self.frame = ttk.Frame(notebook, padding=8)
        notebook.add(self.frame, text='Проект наблюдения')
        self.mode = tk.StringVar(value='research')
        modes = ttk.Frame(self.frame); modes.pack(fill='x')
        for label, value in [('Радиомониторинг', 'monitoring'), ('Исследование', 'research')]:
            ttk.Radiobutton(modes, text=label, value=value, variable=self.mode, command=self.change_mode).pack(side='left')
        ttk.Label(self.frame, text='Переключение экрана не останавливает приборы. Охраной и записью управляет ядро.', wraplength=900).pack(fill='x')
        self.workspace = ttk.Notebook(self.frame)
        events_page, receiver_page, prepare_page, self.diagnostics_page = [ttk.Frame(self.workspace, padding=4) for _ in range(4)]
        for page, title in [(events_page, 'События'), (receiver_page, 'Приёмник и аудио'), (prepare_page, 'Подготовка'), (self.diagnostics_page, 'Диагностика')]:
            self.workspace.add(page, text=title)
        row = ttk.Frame(prepare_page); row.pack(fill='x')
        self.name, self.site = tk.StringVar(value='Наблюдение'), tk.StringVar(value='Объект')
        ttk.Entry(row, textvariable=self.name, width=22).pack(side='left')
        ttk.Entry(row, textvariable=self.site, width=22).pack(side='left')
        ttk.Button(row, text='Создать проект', command=self.create).pack(side='left')
        ttk.Button(modes, text='Обновить проекты', command=self.refresh).pack(side='right')
        self.choice = ttk.Combobox(self.frame, state='readonly'); self.choice.pack(fill='x')
        self.choice.bind('<<ComboboxSelected>>', self.project_changed)
        buttons = ttk.Frame(self.frame); buttons.pack(fill='x')
        for title, command in [('Проверить готовность', self.preflight), ('На охрану', self.arm),
                               ('Снять с охраны', self.disarm), ('Статус', self.status)]:
            ttk.Button(buttons, text=title, command=command).pack(side='left')
        self.status_text = tk.StringVar(value='Статус не запрошен; оборудование не проверено.')
        ttk.Label(self.frame, textvariable=self.status_text, wraplength=900).pack(fill='x')
        self.workspace.pack(fill='both', expand=True)
        receiver = ttk.LabelFrame(receiver_page, text='Приёмник и аналоговый вход LISTEN на сервере ядра', padding=4)
        receiver.pack(fill='x')
        first = ttk.Frame(receiver); first.pack(fill='x')
        ttk.Label(first, text='Режим AR-DV1').pack(side='left')
        self.receiver_mode = tk.StringVar(value='AUTO')
        ttk.Combobox(first, textvariable=self.receiver_mode, values=RECEIVER_MODES, state='readonly', width=10).pack(side='left')
        ttk.Button(first, text='Применить режим', command=self.configure_mode).pack(side='left')
        ttk.Button(first, text='Аудиовходы ядра', command=self.audio_inputs).pack(side='left')
        ttk.Button(first, text='Без аудио', command=self.disable_audio).pack(side='left')
        second = ttk.Frame(receiver); second.pack(fill='x')
        self.audio_choice = ttk.Combobox(second, state='readonly', width=34); self.audio_choice.pack(side='left')
        self.audio_confirm = tk.BooleanVar(value=False)
        self.audio_choice.bind('<<ComboboxSelected>>', lambda _event: self.audio_confirm.set(False))
        ttk.Checkbutton(second, text='Подтверждаю вход AR-DV1', variable=self.audio_confirm).pack(side='left')
        ttk.Button(second, text='Применить вход и режим', command=self.configure_receiver).pack(side='left')
        self.audio_text = tk.StringVar(value='Выберите вход явно. USB AR-DV1 не передаёт PCM. AUTO не включает TETRA.')
        ttk.Label(receiver, textvariable=self.audio_text, wraplength=900).pack(fill='x')
        extra = ttk.Frame(prepare_page); extra.pack(fill='x')
        self.confirm = tk.BooleanVar(value=False)
        ttk.Checkbutton(extra, text='Подтверждаю обычную обстановку', variable=self.confirm).pack(side='left')
        ttk.Button(extra, text='Зафиксировать эталон', command=self.baseline).pack(side='left')
        ttk.Button(extra, text='Обработать новые свипы', command=lambda: self.scoped('analyze', {})).pack(side='left')
        allowed = ttk.Frame(prepare_page); allowed.pack(fill='x')
        self.allowed_name = tk.StringVar(value='Разрешённый сигнал')
        self.allowed_start = tk.StringVar(value='100000000'); self.allowed_end = tk.StringVar(value='100100000')
        for label, variable in [('Название', self.allowed_name), ('От, Гц', self.allowed_start), ('До, Гц', self.allowed_end)]:
            ttk.Label(allowed, text=label).pack(side='left')
            ttk.Entry(allowed, textvariable=variable, width=16).pack(side='left')
        self.watch_changes = tk.BooleanVar(value=False)
        ttk.Checkbutton(allowed, text='Следить за изменениями', variable=self.watch_changes).pack(side='left')
        ttk.Button(allowed, text='Разрешить', command=self.allow_signal).pack(side='left')
        event_buttons = ttk.Frame(events_page); event_buttons.pack(fill='x')
        for title, command in [('Обновить события', lambda: self.scoped('events')),
                               ('Подробности', self.event_detail), ('Слушать WAV', self.listen_audio),
                               ('Сохранить WAV', self.save_audio)]:
            ttk.Button(event_buttons, text=title, command=command).pack(side='left')
        event_frame = ttk.Frame(events_page); event_frame.pack(fill='both', expand=True)
        columns = ('time', 'kind', 'frequency', 'state', 'analysis', 'audio')
        self.event_tree = ttk.Treeview(event_frame, columns=columns, show='headings', height=5)
        for key, title, width in zip(columns, ['UTC', 'Событие', 'Частота, Гц', 'Состояние', 'Приёмник', 'WAV'], [180, 155, 110, 90, 140, 60]):
            self.event_tree.heading(key, text=title); self.event_tree.column(key, width=width, minwidth=55)
        scroll = ttk.Scrollbar(event_frame, orient='vertical', command=self.event_tree.yview)
        self.event_tree.configure(yscrollcommand=scroll.set)
        self.event_tree.pack(side='left', fill='both', expand=True); scroll.pack(side='right', fill='y')
        self.event_tree.bind('<<TreeviewSelect>>', self.event_changed)
        self.event_tree.bind('<Double-1>', lambda _event: self.event_detail())
        self.event_text = tk.StringVar(value='Выберите событие; подробности открывают сохранённые данные ядра.')
        ttk.Label(events_page, textvariable=self.event_text, wraplength=900).pack(fill='x')
        ttk.Label(self.diagnostics_page, text='Подробности / диагностический JSON (до 24 000 символов)').pack(fill='x')
        self.output = tk.Text(self.diagnostics_page, height=7, state='disabled', wrap='word')
        self.output.pack(fill='both', expand=True)
        self.tabs = list(notebook.tabs())

    def _sync_context(self):
        revision = getattr(self.app, '_revision', 0)
        if self.app.client is self._client and revision == self._revision:
            return
        self._client, self._revision = self.app.client, revision
        self.items, self.devices = [], []
        self.choice.configure(values=[]); self.choice.set('')
        self.audio_choice.configure(values=[]); self.audio_choice.set('')
        self.audio_confirm.set(False)
        self.project_changed()

    def selected(self):
        index = self.choice.current()
        return self.items[index]['id'] if 0 <= index < len(self.items) else None

    def project_changed(self, _event=None):
        self.generation += 1
        self.active, self.event, self.events = None, None, []
        self.audio_confirm.set(False)
        self.receiver_mode.set('AUTO')
        self.audio_text.set('Настройки выбранного проекта не запрошены. Обновите статус; вход выбирается явно.')
        self.confirm.set(False)
        for item in self.event_tree.get_children(): self.event_tree.delete(item)
        self.status_text.set('Статус не запрошен; оборудование не проверено.')
        self.event_text.set('Выберите событие; подробности открывают сохранённые данные ядра.')
        self.render({'message': 'Выберите «Статус» для актуальных настроек проекта.'})

    def render(self, data):
        if data.get('error') and 'project' not in data:
            self.status_text.set(str(data['error'])[:900])
            self.workspace.select(self.diagnostics_page)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if len(text) > 24000: text = text[:24000] + '\n… диагностический вывод сокращён'
        self.output.configure(state='normal'); self.output.delete('1.0', 'end')
        self.output.insert('1.0', text); self.output.configure(state='disabled')

    def _context(self):
        return (self.app.client, getattr(self.app, '_revision', 0), self.generation, self.selected())

    def _valid(self, context):
        return not getattr(self.app, '_closed', False) and context == self._context()

    def _submit(self, operation, callback, *, event_id=None):
        context = self._context()
        def completed(data):
            if self._valid(context) and (event_id is None or self.selected_event() == event_id):
                callback(data)
        self.app._submit_action('Проект наблюдения', operation, completed)

    def call(self, path='', payload=None):
        self._sync_context()
        if self.app.client:
            self._submit(partial(self.app.client.monitoring, path, payload), partial(self.receive, path, payload))

    def receive(self, path, payload, data):
        if path == '' and payload is None:
            previous = self.selected()
            self.items = data.get('items', [])[:200]
            self.choice.configure(values=[f"{x['name']} · {x['site']} · {x['state']}" for x in self.items])
            if self.items:
                self.choice.current(next((i for i, x in enumerate(self.items) if x['id'] == previous), 0))
            else: self.choice.set('')
            if self.selected() != previous: self.project_changed()
        elif path == '/audio-inputs':
            self.devices = data.get('devices', [])[:200] if data.get('available') else []
            self.audio_choice.configure(values=[f"{d['index']} · {d['name']}" for d in self.devices])
            self.audio_choice.set(''); self.audio_confirm.set(False)
            self.audio_text.set('Выберите и подтвердите аналоговый вход AR-DV1.' if self.devices else str(data.get('reason') or 'Аудиовходы недоступны')[:900])
        elif path.endswith('/events'):
            self.events = data.get('items', [])[:200]
            self.event = None
            for item in self.event_tree.get_children(): self.event_tree.delete(item)
            for event in self.events:
                analysis = event.get('analysis') or {}
                self.event_tree.insert('', 'end', iid=event['id'], values=(event.get('last_seen', ''), event.get('kind', ''),
                    event.get('center_frequency_hz', ''), event.get('state', ''), analysis.get('state', 'не запрошен'),
                    'есть' if analysis.get('audio') else 'нет'))
        elif '/events/' in path:
            self.event = data
            self.workspace.select(self.diagnostics_page)
        elif path.endswith('/status'):
            self.active = bool(data.get('active')) or data.get('project', {}).get('state') == 'armed'
            project = data.get('project', {})
            self.receiver_mode.set(project.get('receiver_mode', 'AUTO'))
            worker_errors = '; '.join(str(w.get('error') or w.get('last_error')) for w in data.get('workers', []) if w.get('error') or w.get('last_error'))
            self.status_text.set(f"Охрана: {'активна' if data.get('active') else 'выключена'}; приёмник: "
                f"{'занят' if data.get('receiver_busy') else 'свободен'}; аудио: {'настроено' if data.get('audio_configured') else 'не выбрано'}. "
                f"Оборудование: {'не проверено' if data.get('hardware_validation') == 'not_run' else data.get('hardware_validation', 'неизвестно')}. "
                f"{data.get('error') or worker_errors or ''}"[:900])
            configured = project.get('audio')
            if configured: self.audio_text.set('Настроенный вход ядра: ' + str(configured.get('name', ''))[:800])
            else: self.audio_text.set('Аудио не настроено. Выберите и подтвердите вход AR-DV1 явно.')
        elif path.endswith(('/arm', '/disarm', '/configure')):
            self.active = data.get('state') == 'armed'
            self.status_text.set('Команда выполнена. Запросите «Статус» для состояния исполнителей; оборудование не проверено.')
        self.render(data)

    def refresh(self): self.call()

    def create(self):
        self._sync_context()
        if self.app.session_id:
            self.call(payload=dict(name=self.name.get(), site=self.site.get(), session_id=self.app.session_id))
        else: self.render({'error': 'Выберите подготовленный двухприборный сеанс.'})

    def preflight(self): self.scoped('preflight')
    def arm(self): self.scoped('arm', {})
    def disarm(self): self.scoped('disarm', {})
    def status(self): self.scoped('status')
    def audio_inputs(self): self.call('/audio-inputs')

    def scoped(self, action, payload=None):
        self._sync_context()
        if self.selected(): self.call('/' + quote(self.selected(), safe='') + '/' + action, payload)
        else: self.render({'error': 'Выберите проект и обновите список для текущего сервера.'})

    def _configurable(self):
        self._sync_context()
        if self.active:
            self.render({'error': 'Сначала снимите проект с охраны, затем изменяйте настройки.'})
            return False
        return bool(self.selected())

    def configure_mode(self):
        if self._configurable(): self.scoped('configure', {'receiver_mode': self.receiver_mode.get()})

    def configure_receiver(self):
        if not self._configurable(): return
        index = self.audio_choice.current()
        if not 0 <= index < len(self.devices) or not self.audio_confirm.get():
            self.render({'error': 'Явно выберите аудиовход ядра и подтвердите подключение AR-DV1.'}); return
        device = self.devices[index]
        self.scoped('configure', dict(receiver_mode=self.receiver_mode.get(),
                    audio=dict(device=device['index'], name=device['name'], confirm=True)))

    def disable_audio(self):
        if self._configurable(): self.scoped('configure', {'audio': None})

    def baseline(self):
        self.scoped('baselines', dict(name=self.name.get(), location=self.site.get(), confirm=self.confirm.get()))

    def allow_signal(self):
        try:
            payload = dict(name=self.allowed_name.get(), start_hz=int(self.allowed_start.get()),
                           end_hz=int(self.allowed_end.get()), watch_changes=self.watch_changes.get())
        except ValueError:
            self.render({'error': 'Частоты должны быть целыми Гц'}); return
        self.scoped('allowed', payload)

    def selected_event(self):
        selected = self.event_tree.selection()
        return selected[0] if selected else None

    def event_changed(self, _event=None):
        self.event = next((e for e in self.events if e['id'] == self.selected_event()), None)
        if self.event:
            analysis = self.event.get('analysis') or {}
            self.event_text.set(f"{self.event.get('center_frequency_hz', '—')} Гц · {self.event.get('kind', '')} · "
                f"{analysis.get('state', 'приёмник ещё не исследовал')} · WAV: {'есть' if analysis.get('audio') else 'нет'}. "
                f"{analysis.get('audio_error') or analysis.get('error') or ''}"[:900])
            self.render(self.event)

    def event_detail(self):
        self._sync_context()
        identifier = self.selected_event()
        if self.selected() and identifier:
            path = '/' + quote(self.selected(), safe='') + '/events/' + quote(identifier, safe='')
            self._submit(partial(self.app.client.monitoring, path), partial(self.receive, path, None), event_id=identifier)

    def _audio_ref(self):
        self._sync_context()
        identifier = self.selected_event()
        event = self.event if self.event and self.event['id'] == identifier else next((e for e in self.events if e['id'] == identifier), {})
        ref = (event.get('analysis') or {}).get('audio')
        if not ref:
            self.event_text.set('У выбранного события нет сохранённой WAV-записи.')
            self.render({'message': 'У выбранного события нет сохранённой WAV-записи.'}); return None
        return dict(ref)

    def listen_audio(self):
        ref = self._audio_ref()
        if not ref: return
        if sys.platform != 'win32':
            self.workspace.select(self.diagnostics_page)
            self.render({'message': 'Прослушивание через winsound доступно в Windows. Сохраните WAV для внешнего проигрывателя.'}); return
        identifier = self.selected_event()
        def downloaded(raw):
            self._submit(partial(_play, raw), self.render, event_id=identifier)
        self._submit(partial(_download, self.app.client, ref), downloaded, event_id=identifier)

    def save_audio(self):
        ref = self._audio_ref()
        if not ref: return
        identifier = self.selected_event()
        def downloaded(raw):
            from tkinter.filedialog import asksaveasfilename
            context = self._context()
            path = asksaveasfilename(parent=self.app.root, initialfile='event-' + identifier + '.wav',
                                      defaultextension='.wav', filetypes=[('WAV', '*.wav')])
            if path and self._valid(context) and self.selected_event() == identifier:
                self._submit(partial(_save, raw, path), self.render, event_id=identifier)
        self._submit(partial(_download, self.app.client, ref), downloaded, event_id=identifier)

    def change_mode(self):
        for tab in self.tabs:
            label = self.notebook.tab(tab, 'text')
            visible = self.mode.get() == 'research' or label in ('Проект наблюдения', 'Потоки', 'Спектр и водопад', 'Кандидаты', 'Исследования')
            if visible: self.notebook.add(tab)
            else: self.notebook.hide(tab)
