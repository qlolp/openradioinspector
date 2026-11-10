"""Thin monitoring workspace: the core owns all capture and receiver control."""
import io
import json
import time
from datetime import datetime, timezone
import sys
import wave
from functools import partial
from pathlib import Path
from urllib.parse import quote

from ..contracts import parse_time
from .spectrum import waterfall_color


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
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text='Проект наблюдения')
        self.scroll_canvas = tk.Canvas(self.frame, highlightthickness=0, height=220)
        scroll = ttk.Scrollbar(self.frame, orient='vertical', command=self.scroll_canvas.yview)
        self.scroll_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.scroll_canvas.pack(side='left', fill='both', expand=True)
        self.content = ttk.Frame(self.scroll_canvas, padding=8)
        content_window = self.scroll_canvas.create_window(0, 0, anchor='nw', window=self.content)
        self.content.bind('<Configure>', lambda _event: self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox('all')))
        self.scroll_canvas.bind('<Configure>', lambda event: self.scroll_canvas.itemconfigure(content_window, width=event.width))
        self.mode = tk.StringVar(value='research')
        modes = ttk.Frame(self.content); modes.pack(fill='x')
        for label, value in [('Радиомониторинг', 'monitoring'), ('Исследование', 'research')]:
            ttk.Radiobutton(modes, text=label, value=value, variable=self.mode, command=self.change_mode).pack(side='left')
        ttk.Label(self.content, text='Переключение экрана не останавливает приборы. Охраной и записью управляет ядро.', wraplength=900).pack(fill='x')
        self.workspace = ttk.Notebook(self.content)
        events_page, receiver_page, prepare_page, self.diagnostics_page = [ttk.Frame(self.workspace, padding=4) for _ in range(4)]
        for page, title in [(events_page, 'События'), (receiver_page, 'Приёмник и аудио'), (prepare_page, 'Подготовка'), (self.diagnostics_page, 'Диагностика')]:
            self.workspace.add(page, text=title)
        row = ttk.Frame(prepare_page); row.pack(fill='x')
        self.name, self.site = tk.StringVar(value='Наблюдение'), tk.StringVar(value='Объект')
        ttk.Entry(row, textvariable=self.name, width=22).pack(side='left')
        ttk.Entry(row, textvariable=self.site, width=22).pack(side='left')
        ttk.Button(row, text='Создать проект', command=self.create).pack(side='left')
        ttk.Button(modes, text='Обновить проекты', command=self.refresh).pack(side='right')
        self.choice = ttk.Combobox(self.content, state='readonly'); self.choice.pack(fill='x')
        self.choice.bind('<<ComboboxSelected>>', self.project_changed)
        buttons = ttk.Frame(self.content); buttons.pack(fill='x')
        for title, command in [('Проверить готовность', self.preflight), ('На охрану', self.arm),
                               ('Снять с охраны', self.disarm), ('Статус', self.status)]:
            ttk.Button(buttons, text=title, command=command).pack(side='left')
        self.status_text = tk.StringVar(value='Статус не запрошен; оборудование не проверено.')
        ttk.Label(self.content, textvariable=self.status_text, wraplength=900).pack(fill='x')
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
        self._build_research(receiver_page)
        self._bind_scroll(self.content)
        self.tabs = list(notebook.tabs())

    def _bind_scroll(self, widget):
        if widget.winfo_class() in ('TFrame', 'TLabelframe', 'TLabel', 'Canvas'):
            def wheel(event):
                step = -1 if getattr(event, 'num', None) == 4 else 1 if getattr(event, 'num', None) == 5 else (-1 if event.delta > 0 else 1)
                self.scroll_canvas.yview_scroll(step, 'units')
                return 'break'
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'): widget.bind(sequence, wheel)
        for child in widget.winfo_children(): self._bind_scroll(child)

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
        self._clear_research()
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
            if path.endswith('/disarm'): self._clear_research()
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

    def _build_research(self, receiver_page):
        tk, ttk = self.app.tk, self.app.ttk
        self.leases = {}
        self.history_items, self.history_cursor = [], None
        self.search_data, self.comparison = {}, None
        self.manual_text = tk.StringVar(value='Ручное владение не получено. Потерянный token сбрасывается снятием с охраны.')
        self.coverage_text = tk.StringVar(value='Покрытие неизвестно. Обновите статус поиска.')
        self.manual_frequency = tk.StringVar(value='145500000')
        self.search_span, self.position_label = tk.StringVar(value='200000'), tk.StringVar()
        search_page, history_page = ttk.Frame(self.workspace, padding=4), ttk.Frame(self.workspace, padding=4)
        self.workspace.add(search_page, text='Поиск источника')
        self.workspace.add(history_page, text='История')
        self.search_page, self.history_page = search_page, history_page
        for page, role, title in [(receiver_page, 'receiver', 'Ручной AR-DV1'), (search_page, 'analyzer', 'Ручной tinySA')]:
            row = ttk.Frame(page); row.pack(fill='x')
            ttk.Label(row, text=title).pack(side='left')
            for label, action in [('Взять вручную', 'acquire'), ('Продлить на 30 с', 'renew'), ('Вернуть автоматике', 'return')]:
                ttk.Button(row, text=label, command=partial(self.manual, role, action)).pack(side='left')
            ttk.Label(page, textvariable=self.manual_text, wraplength=900).pack(fill='x')
        row = ttk.Frame(receiver_page); row.pack(fill='x')
        ttk.Label(row, text='Частота, целые Гц').pack(side='left')
        ttk.Entry(row, textvariable=self.manual_frequency, width=18).pack(side='left')
        ttk.Button(row, text='Ручная перестройка AR-DV1', command=self.manual_tune).pack(side='left')
        ttk.Label(row, text='Режим выбран выше').pack(side='left')
        row = ttk.Frame(search_page); row.pack(fill='x')
        ttk.Label(row, text='Узкая полоса, Гц').pack(side='left')
        ttk.Entry(row, textvariable=self.search_span, width=12).pack(side='left')
        ttk.Button(row, text='Искать выбранное событие', command=self.start_search).pack(side='left')
        ttk.Button(row, text='Обновить поиск', command=self.search_status).pack(side='left')
        ttk.Label(search_page, textvariable=self.coverage_text, wraplength=900).pack(fill='x')
        row = ttk.Frame(search_page); row.pack(fill='x')
        ttk.Entry(row, textvariable=self.position_label, width=26).pack(side='left')
        ttk.Button(row, text='Отметить положение', command=self.mark_position).pack(side='left')
        self.marks_text = tk.StringVar()
        ttk.Label(search_page, textvariable=self.marks_text, wraplength=900).pack(fill='x')
        self.search_canvas = tk.Canvas(search_page, height=110, background='#07131c')
        self.search_canvas.pack(fill='both', expand=True)
        self.search_canvas.bind('<Configure>', lambda _event: self.draw_search())
        self.history_start, self.history_end = tk.StringVar(), tk.StringVar()
        self.history_min, self.history_max, self.history_event = tk.StringVar(), tk.StringVar(), tk.StringVar()
        row = ttk.Frame(history_page); row.pack(fill='x')
        for label, variable, width in [('От UTC', self.history_start, 22), ('До UTC', self.history_end, 22), ('Событие', self.history_event, 18)]:
            ttk.Label(row, text=label).pack(side='left'); ttk.Entry(row, textvariable=variable, width=width).pack(side='left')
        row = ttk.Frame(history_page); row.pack(fill='x')
        for label, variable in [('От, Гц', self.history_min), ('До, Гц', self.history_max)]:
            ttk.Label(row, text=label).pack(side='left'); ttk.Entry(row, textvariable=variable, width=14).pack(side='left')
        ttk.Button(row, text='Событие из списка', command=self.history_from_event).pack(side='left')
        ttk.Button(row, text='Загрузить', command=self.load_history).pack(side='left')
        ttk.Button(row, text='Следующая страница', command=lambda: self.load_history(next_page=True)).pack(side='left')
        self.history_text = tk.StringVar(value='До 64 свипов × 320 точек: отображение рассчитывает ядро; исходные данные сохранены.')
        ttk.Label(history_page, textvariable=self.history_text, wraplength=900).pack(fill='x')
        row = ttk.Frame(history_page); row.pack(fill='x')
        self.history_choice = ttk.Combobox(row, state='readonly', width=48); self.history_choice.pack(side='left', fill='x', expand=True)
        self.history_choice.bind('<<ComboboxSelected>>', self.history_selected)
        ttk.Button(row, text='Исходный свип', command=self.raw_sweep).pack(side='left')
        ttk.Button(row, text='Сравнить с эталоном', command=self.compare_sweep).pack(side='left')
        row = ttk.Frame(history_page); row.pack(fill='x')
        ttk.Button(row, text='Источники события', command=self.event_sources).pack(side='left')
        ttk.Button(row, text='Ещё источники', command=lambda: self.event_sources(next_page=True)).pack(side='left')
        self.history_views = ttk.Notebook(history_page); self.history_views.pack(fill='both', expand=True)
        canvases = []
        for title in ('Водопад UTC', 'Спектр', 'Эталон / текущий'):
            page = ttk.Frame(self.history_views); self.history_views.add(page, text=title)
            canvas = tk.Canvas(page, height=120, background='#07131c'); canvas.pack(fill='both', expand=True)
            canvases.append(canvas)
        self.waterfall_canvas, self.spectrum_canvas, self.comparison_canvas = canvases
        self.comparison_text = tk.StringVar()
        ttk.Label(history_page, textvariable=self.comparison_text, wraplength=900).pack(fill='x')
        for canvas in canvases: canvas.bind('<Configure>', lambda _event: self.draw_history())
        self.source_cursor = None
        self.workspace.bind('<<NotebookTabChanged>>', lambda _event: self._refresh_manual())
        for variable in (self.history_start, self.history_end, self.history_min, self.history_max, self.history_event):
            variable.trace_add('write', self._history_filters_changed)

    def _clear_research(self):
        self.leases = {}
        self.history_items, self.history_cursor = [], None
        self.search_data, self.comparison = {}, None
        self.source_cursor = None
        self.history_event.set('')
        self.history_choice.configure(values=[]); self.history_choice.set('')
        self.comparison_text.set('')
        self.history_text.set('До 64 свипов × 320 точек; выберите фильтры для текущего проекта.')
        self.coverage_text.set('Покрытие неизвестно. Потерянный token: снимите охрану для сброса владения.')
        self.marks_text.set('')
        self._refresh_manual()
        self.draw_search(); self.draw_history()

    def _refresh_manual(self):
        parts = []
        for role, lease in self.leases.items():
            remaining = lease['deadline'] - time.monotonic()
            expiry = datetime.fromtimestamp(time.time()+remaining, timezone.utc).strftime('%H:%M:%S UTC')
            parts.append(f"{role}: {'срок истёк; автоматика приостановлена, явно верните управление' if remaining <= 0 else 'до ' + expiry + '; затем пауза до явного возврата'}")
        self.manual_text.set('; '.join(parts) or 'Ручное владение не получено. Потерянный token: снимите охрану для сброса.')

    def _research(self, action, payload, callback, *, query=None, guard=None):
        self._sync_context()
        if not self.selected() or not self.app.client: return
        path = '/' + quote(self.selected(), safe='') + '/' + action
        operation = partial(self.app.client.monitoring, path, payload, **({'query': query} if query is not None else {}))
        def completed(data):
            if guard is None or guard(): callback(data)
        self._submit(operation, completed)

    def _lease_token(self, role, *, current=True):
        self._sync_context(); self._refresh_manual()
        lease = self.leases.get(role)
        if not lease:
            self.render({'error': f'Явно возьмите {role} вручную. Потерянный token: снимите охрану для сброса.'}); return None
        if current and lease['deadline'] <= time.monotonic():
            self.render({'error': 'Срок ручного владения истёк; автоматика остаётся на паузе. Явно верните управление.'}); return None
        return lease['token']

    def manual(self, role, action):
        self._sync_context()
        requested = time.monotonic()
        if action == 'acquire':
            if role in self.leases:
                self._refresh_manual(); self.render({'error': 'Владение уже получено: продлите срок или явно верните управление.'}); return
            payload = {'role': role}
        else:
            token = self._lease_token(role, current=action != 'return')
            if token is None: return
            payload = {'token': token}
        def completed(data):
            if action in ('acquire', 'renew'):
                self.leases[role] = dict(token=data.get('token', payload.get('token')), deadline=requested + data['expires_seconds'])
            else:
                self.leases.pop(role, None)
                self.coverage_text.set('Широкий обзор восстанавливается; обновите поиск до подтверждения wide.' if data.get('state') == 'restoring' else 'Управление возвращено; обновите статус поиска.')
            self._refresh_manual()
            self.render({k: v for k, v in data.items() if k != 'token'})
        self._research('manual/' + action, payload, completed)

    def manual_tune(self):
        token = self._lease_token('receiver')
        if token is None: return
        try:
            frequency = int(self.manual_frequency.get())
            if frequency <= 0: raise ValueError()
        except ValueError:
            self.render({'error': 'Частота должна быть положительным целым числом Гц.'}); return
        self._research('manual/tune', dict(token=token, frequency_hz=frequency, mode=self.receiver_mode.get()), self.render)

    def start_search(self):
        token = self._lease_token('analyzer')
        if token is None: return
        event = self.selected_event()
        if not event:
            self.render({'error': 'Выберите событие в списке перед поиском источника.'}); return
        try:
            span = int(self.search_span.get())
            if not 1000 <= span <= 100_000_000: raise ValueError()
        except ValueError:
            self.render({'error': 'Узкая полоса должна быть целой: 1000…100000000 Гц.'}); return
        self._research('search/start', dict(token=token, event_id=event, span_hz=span), self.receive_search)

    def search_status(self): self._research('search/status', None, self.receive_search)

    def mark_position(self):
        token = self._lease_token('analyzer')
        if token is None: return
        label = self.position_label.get().strip()
        if not label:
            self.render({'error': 'Введите название положения.'}); return
        def completed(data):
            self.marks_text.set(f"{data.get('label', '')} · {data.get('created_at', '')} · {data.get('level', '—')} dBm · {data.get('frequency_hz', '—')} Гц")
            self.render(data)
        self._research('search/mark', dict(token=token, label=label), completed)

    def receive_search(self, data):
        self.search_data = data
        descriptions = {'narrow': 'УЗКИЙ поиск: широкий обзор приостановлен', 'wide': 'Широкий обзор подтверждён ядром',
                        'restoring': 'Широкий обзор восстанавливается', 'paused': 'Широкий обзор приостановлен',
                        'error': 'Ошибка обзора; проверьте статус', 'stopped': 'Обзор остановлен'}
        coverage = data.get('coverage', 'unknown')
        self.coverage_text.set(f"{descriptions.get(coverage, 'Покрытие неизвестно')} · событие {data.get('event_id', '—')} · максимум ядра: {data.get('maximum_level', '—')} dBm")
        marks = data.get('marks', [])[-8:]
        self.marks_text.set('; '.join(f"{m.get('label', '')} · {m.get('created_at', '')} · {m.get('level', '—')} dBm" for m in marks)[:900])
        self._refresh_manual(); self.draw_search(); self.render(data)

    def _filter_signature(self):
        return tuple(v.get() for v in (self.history_start, self.history_end, self.history_min, self.history_max, self.history_event))

    def _history_filters_changed(self, *_args):
        self.history_cursor, self.source_cursor = None, None
        self.history_items, self.comparison = [], None
        self.history_choice.configure(values=[]); self.history_choice.set('')
        self.comparison_text.set('')
        self.draw_history()

    def history_from_event(self):
        identifier = self.selected_event()
        if identifier:
            self.history_event.set(identifier)
            self.workspace.select(self.history_page)

    def load_history(self, next_page=False):
        self._sync_context()
        if next_page and not self.history_cursor: return
        signature = self._filter_signature()
        query = {'limit': 64}
        try:
            for key, value in zip(('start', 'end', 'min_hz', 'max_hz', 'event_id'), signature):
                if not value.strip(): continue
                if key in ('start', 'end'):
                    parse_time(value)
                    if not value.endswith('Z') and '+00:00' not in value: raise ValueError('Используйте время UTC с Z или +00:00')
                    query[key] = value
                elif key in ('min_hz', 'max_hz'):
                    query[key] = int(value)
                    if query[key] < 0: raise ValueError('Частоты должны быть неотрицательными')
                else: query[key] = value.strip()
            if 'start' in query and 'end' in query and parse_time(query['start']) > parse_time(query['end']): raise ValueError('Начало позже окончания')
            if query.get('min_hz', 0) > query.get('max_hz', 20_000_000_000): raise ValueError('Неверный диапазон частот')
        except (ValueError, TypeError) as exc:
            self.render({'error': str(exc) or 'Неверные фильтры истории'}); return
        if next_page: query['after'] = self.history_cursor
        def completed(data):
            self.history_items = data.get('items', [])[:64]
            self.history_cursor = data.get('next_cursor')
            self.history_choice.configure(values=[f"{s['interval']['started_at']} · {s['id']}" for s in self.history_items])
            if self.history_items: self.history_choice.current(0)
            else: self.history_choice.set('')
            self.comparison = None; self.comparison_text.set('')
            self.history_text.set(f"Свипов: {len(self.history_items)} (до 64); максимум 320 точек на свип, расчёт ядра. "
                                  + ('Есть следующая страница.' if self.history_cursor else 'Конец выборки.'))
            self.draw_history(); self.render(data)
        self._research('history', None, completed, query=query, guard=lambda: self._filter_signature() == signature)

    def _history_item(self):
        index = self.history_choice.current()
        return self.history_items[index] if 0 <= index < len(self.history_items) else None

    def history_selected(self, _event=None):
        self.comparison = None; self.comparison_text.set('')
        self.draw_history()

    def raw_sweep(self):
        self._sync_context()
        item = self._history_item()
        if item:
            identifier = item['id']
            def completed(data):
                self.render(data); self.workspace.select(self.diagnostics_page)
            self._research('history/' + quote(identifier, safe=''), None, completed,
                           guard=lambda: bool(self._history_item() and self._history_item()['id'] == identifier))

    def compare_sweep(self):
        self._sync_context()
        item = self._history_item()
        if item:
            identifier = item['id']
            def completed(data):
                self.comparison = data
                self.comparison_text.set(f"Эталон {data.get('baseline_id', '—')} · детектор {data.get('detector_version', '—')} · "
                    + ('p50: жёлтый; p95: зелёный; текущий: голубой. dBm' if data.get('compatible') else str(data.get('reason') or 'Сравнение несовместимо')))
                self.draw_history(); self.render(data)
                self.history_views.select(2)
            self._research('history/' + quote(identifier, safe='') + '/comparison', None, completed,
                           guard=lambda: bool(self._history_item() and self._history_item()['id'] == identifier))

    def event_sources(self, next_page=False):
        self._sync_context()
        identifier = self.history_event.get().strip()
        if not identifier:
            self.render({'error': 'Укажите событие для просмотра источников.'}); return
        if next_page and not self.source_cursor: return
        query = {'limit': 64}
        if next_page: query['after'] = self.source_cursor
        def completed(data):
            self.source_cursor = data.get('next_cursor')
            self.render(data); self.workspace.select(self.diagnostics_page)
        self._research('events/' + quote(identifier, safe='') + '/sources', None, completed, query=query,
                       guard=lambda: self.history_event.get().strip() == identifier)

    @staticmethod
    def _canvas_box(canvas):
        return max(canvas.winfo_width(), 480), max(canvas.winfo_height(), 120)

    @staticmethod
    def _utc(value):
        return parse_time(value).timestamp()

    def _lines(self, canvas, frequencies, groups):
        """Map core-provided samples to pixels; no analysis or baseline calculation."""
        width, height = self._canvas_box(canvas)
        if not frequencies: return
        values = [v for _tag, _color, levels in groups for v in levels]
        if not values: return
        left, right = min(frequencies), max(frequencies)
        low, high = min(values)-3, max(values)+3
        for tag, color, levels in groups:
            points = []
            for frequency, level in zip(frequencies, levels):
                points.extend([60+(frequency-left)/max(1, right-left)*(width-80), 15+(high-level)/max(1,high-low)*(height-45)])
            if len(points) >= 4: canvas.create_line(*points, fill=color, tags=tag)
            elif points: canvas.create_oval(points[0]-2,points[1]-2,points[0]+2,points[1]+2,fill=color,tags=tag)
        for text, x, y in [(f'{left} Гц',60,height-10), (f'{right} Гц',width-65,height-10), (f'{high:.1f}',26,15), (f'{low:.1f}',26,height-30)]:
            canvas.create_text(x,y,text=text,fill='#c7d6e7')

    def draw_search(self):
        canvas = self.search_canvas; canvas.delete('all')
        series = self.search_data.get('series', [])[:256]
        if not series: return
        width, height = self._canvas_box(canvas)
        times = [self._utc(s['started_at']) for s in series]
        levels = [s['peak_level'] for s in series]
        first, last, low, high = min(times), max(times), min(levels)-3, max(levels)+3
        points = []
        for stamp, level in zip(times, levels):
            points.extend([60+(stamp-first)/max(1,last-first)*(width-80),15+(high-level)/max(1,high-low)*(height-45)])
        if len(points) >= 4: canvas.create_line(*points, fill='#52d6e8', tags='search-series')
        elif points: canvas.create_oval(points[0]-2,points[1]-2,points[0]+2,points[1]+2,fill='#52d6e8',tags='search-series')
        canvas.create_text(25,15,text=f'{high:.1f}',fill='white')
        canvas.create_text(25,height-35,text=f'{low:.1f}',fill='white')
        canvas.create_text(width/2,height-10,text=f"UTC {series[0]['started_at']} … {series[-1]['started_at']} · dBm",fill='white')

    def draw_history(self):
        for canvas in (self.waterfall_canvas, self.spectrum_canvas, self.comparison_canvas): canvas.delete('all')
        rows = self.history_items[:64]
        if rows:
            canvas = self.waterfall_canvas
            width, height = self._canvas_box(canvas)
            times = [(self._utc(s['interval']['started_at']),self._utc(s['interval']['ended_at'])) for s in rows]
            first, last = min(t[0] for t in times), max(t[1] for t in times)
            all_hz = [hz for s in rows for hz in s.get('frequencies_hz', [])[:320]]
            if all_hz:
                left, right = min(all_hz), max(all_hz)
                for sweep, (start, end) in zip(rows,times):
                    frequencies, levels = sweep.get('frequencies_hz', [])[:320], sweep.get('levels', [])[:320]
                    y1=15+(start-first)/max(.001,last-first)*(height-50)
                    y2=max(y1+1,15+(end-first)/max(.001,last-first)*(height-50))
                    for i,(frequency,level) in enumerate(zip(frequencies,levels)):
                        lower = frequency if i==0 else (frequencies[i-1]+frequency)/2
                        upper = frequency if i==len(frequencies)-1 else (frequency+frequencies[i+1])/2
                        x1=65+(lower-left)/max(1,right-left)*(width-80)
                        x2=max(x1+1,65+(upper-left)/max(1,right-left)*(width-80))
                        canvas.create_rectangle(x1,y1,x2,y2,outline='',fill=waterfall_color(level,-140,-20),tags=('history-cell','sweep:'+sweep['id']))
                canvas.create_text(width/2,height-25,text=f'{left} … {right} Гц · цвет: −140 … −20 dBm',fill='white')
                canvas.create_text(width/2,height-10,text='UTC ' + datetime.fromtimestamp(first,timezone.utc).isoformat() + ' … ' + datetime.fromtimestamp(last,timezone.utc).isoformat(),fill='white')
                for stamp, y in [(first,15),(last,height-35)]:
                    canvas.create_text(3,y,anchor='w',text=datetime.fromtimestamp(stamp,timezone.utc).strftime('%H:%M:%S'),fill='white')
        item = self._history_item()
        if item:
            self._lines(self.spectrum_canvas,item.get('frequencies_hz', [])[:320],
                        [('spectrum-current','#52d6e8',item.get('levels', [])[:320])])
        comparison = self.comparison
        if comparison and comparison.get('compatible'):
            items = comparison.get('items', [])[:320]
            self._lines(self.comparison_canvas,[p['frequency_hz'] for p in items],
                        [('baseline-p50','#ffd36e',[p['baseline_p50_dbm'] for p in items]),
                         ('baseline-p95','#5de19a',[p['baseline_p95_dbm'] for p in items]),
                         ('comparison-current','#52d6e8',[p['current_dbm'] for p in items])])
