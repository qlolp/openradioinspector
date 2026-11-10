"""Thin monitoring workspace: the core owns all capture and receiver control."""
import io
import json
import math
import time
from datetime import datetime, timezone
import sys
import wave
from functools import partial
from pathlib import Path
from urllib.parse import quote

from ..contracts import parse_time
from .spectrum import waterfall_color
from .tk_timers import TkTimerOwner
from .client import PlatformClientError


RECEIVER_MODES = ('AUTO', 'FM', 'AM', 'DSTAR', 'YSF', 'ALINCO', 'NXDN', 'P25', 'DPMR', 'DMR')


def _checked_wav(raw):
    if not isinstance(raw, bytes) or len(raw) > 8 * 1024 * 1024:
        raise ValueError('Некорректная или слишком большая WAV-запись')
    with wave.open(io.BytesIO(raw), 'rb') as wav:
        if wav.getnframes() <= 0:
            raise ValueError('Аудиозапись пуста')
    return raw


def _download(client, ref):
    return _checked_wav(client.monitoring_event_audio(ref['project_id'], ref['event_id']))


def _play(raw):
    import winsound
    winsound.PlaySound(raw, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
    return {'playback': 'completed'}


def _save(raw, path):
    Path(path).write_bytes(raw)
    return {'saved': str(path)}


VERDICT_LABELS = {'unreviewed':'Не просмотрено', 'authorized':'Разрешённый сигнал',
                  'false_positive':'Ложное срабатывание', 'investigate':'Исследовать'}
DELIVERY_LABELS = {'disabled':'отключена', 'pending':'ожидается', 'delivering':'отправляется',
                   'sent':'доставлено', 'failed':'ошибка доставки'}


BALANCED_DETECTOR = dict(margin_db=6.0, impulse_db=12.0, hysteresis_db=3.0, confirmations=3, window_sweeps=5, dwell_seconds=10.0)

def _public_data(value, secret=''):
    if isinstance(value, dict):
        return {key:_public_data(item, secret) for key,item in value.items() if str(key).lower() not in ('secret','token')}
    if isinstance(value, list): return [_public_data(item, secret) for item in value]
    if isinstance(value, str) and secret: return value.replace(secret, '[скрыто]')
    return value


def _post_settings(client, path, payload):
    secret = payload.get('webhook', {}).get('secret', '')
    try:
        return _public_data(client.monitoring(path, payload), secret)
    except Exception as exc:
        message = str(exc).replace(secret, '[скрыто]') if secret else str(exc)
        raise PlatformClientError(message, status=getattr(exc, 'status', None), code=getattr(exc, 'code', 'settings_error')) from None


def _poll_project(client, project, health, collecting=False, searching=False):
    result = {}
    resources = ('alerts', 'storage', 'status') if health else ('alerts',)
    if health and collecting: resources += ('preflight',)
    if searching: resources += ('search/status',)
    for resource in resources:
        try:
            result[resource] = client.monitoring('/' + quote(project, safe='') + '/' + resource,
                **({'query':{'limit':50}} if resource == 'alerts' else {}))
        except Exception as exc: result[resource] = {'poll_error':str(exc)}
    return result


def _follow_session(client, identifier):
    try: return {'session':client.get_session(identifier)}
    except Exception: return {'error':'Не удалось загрузить новый сеанс охраны; обновите статус проекта.'}


class MonitoringPanel(TkTimerOwner):
    def __init__(self, app, notebook):
        self.app, self.notebook = app, notebook
        self._client = app.client
        self.closed = False
        self._background_token=None
        self._operator_epoch=0
        self._operator_actions=[]
        self._followed_runs, self._following_runs = set(), set()
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
        self.preparation_baseline_row = extra
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
        self._build_operations(events_page)
        self._build_acceptance(prepare_page)
        self._init_timers(app.root)
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
        if self.app.client is self._client:
            return
        self._client = self.app.client
        self._followed_runs, self._following_runs = set(), set()
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
        self._background_token=None
        self._operator_epoch+=1
        self._operator_actions.clear()
        self._following_runs.clear()
        self._clear_research()
        self._reset_operations()
        self._reset_acceptance()
        self.active, self.event, self.events = None, None, []
        self.audio_confirm.set(False)
        self.receiver_mode.set('AUTO')
        self.audio_text.set('Настройки выбранного проекта не запрошены. Обновите статус; вход выбирается явно.')
        self.confirm.set(False)
        for item in self.event_tree.get_children(): self.event_tree.delete(item)
        self.status_text.set('Статус не запрошен; оборудование не проверено.')
        self.event_text.set('Выберите событие; подробности открывают сохранённые данные ядра.')
        self.render({'message': 'Выберите «Статус» для актуальных настроек проекта.'})
        self._arm_operations_poll(0)

    def render(self, data):
        if data.get('error') and 'project' not in data:
            self.status_text.set(str(data['error'])[:900])
            self.workspace.select(self.diagnostics_page)
        text = json.dumps(_public_data(data), ensure_ascii=False, indent=2)
        if len(text) > 24000: text = text[:24000] + '\n… диагностический вывод сокращён'
        self.output.configure(state='normal'); self.output.delete('1.0', 'end')
        self.output.insert('1.0', text); self.output.configure(state='disabled')

    def _context(self):
        return (self.app.client, self.generation, self.selected())

    def _valid(self, context):
        return not self.closed and not getattr(self.app, '_closed', False) and context == self._context()

    def _submit(self, operation, callback, *, event_id=None, background=False):
        if self.closed or getattr(self.app, '_closed', False) or not self.app.client:
            return False
        if getattr(self.app,'_action_running',False):
            if background:return False
            if len(self._operator_actions)>=32:
                self.render({'error':'Очередь действий клиента заполнена; дождитесь ответа'});return False
            self._operator_actions.append((self._context(),operation,callback,event_id))
            self._operator_epoch+=1
            self.status_text.set('Действие ожидает завершения предыдущего запроса; повторно нажимать не нужно.')
            self._schedule_after(self.app.root,'operator-actions',100,self._drain_operator_actions)
            return True
        if background and self._background_token is not None:return False
        token=object()
        if background:self._background_token=token
        else:self._operator_epoch+=1
        epoch=self._operator_epoch;context = self._context()
        def completed(data):
            if background:
                if self._background_token is token:self._background_token=None
                if epoch!=self._operator_epoch:return
            if self._valid(context) and (event_id is None or self.selected_event() == event_id):
                callback(data)
        submit = getattr(self.app, '_submit_project_read' if background else '_submit_project_action', self.app._submit_action)
        submit('Проект наблюдения', operation, completed)
        return True

    def _drain_operator_actions(self):
        if self.closed or getattr(self.app,'_closed',False):self._operator_actions.clear();return
        if getattr(self.app,'_action_running',False):
            self._schedule_after(self.app.root,'operator-actions',100,self._drain_operator_actions);return
        while self._operator_actions:
            context,operation,callback,event_id=self._operator_actions.pop(0)
            if self._valid(context):
                self._submit(operation,callback,event_id=event_id)
                break
        if self._operator_actions:self._schedule_after(self.app.root,'operator-actions',100,self._drain_operator_actions)

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
                self.event_tree.insert('', 'end', iid=event['id'], values=self._event_values(event))
        elif '/events/' in path:
            self.event = data
            self.workspace.select(self.diagnostics_page)
        elif path.endswith('/preflight'):
            self._show_preflight(data)
        elif path.endswith('/baselines'):
            self.confirm.set(False)
            if self.collecting_baseline: self.preparation_text.set('Эталон зафиксирован. Приём продолжается до явной остановки сбора или постановки на охрану; аппаратная проверка не выполнена.')
        elif path.endswith('/status'):
            self._show_status(data, update_inputs=True)
        elif path.endswith(('/arm', '/disarm', '/configure')):
            if path.endswith(('/arm','/disarm')): self.collecting_baseline = False
            if path.endswith('/disarm'): self._clear_research()
            self.active = data.get('state') == 'armed'
            self.status_text.set('Команда выполнена. Запросите «Статус» для состояния исполнителей; оборудование не проверено.')
        self._update_preparation_controls()
        self.render(data)
        if path.endswith('/arm'): self._maybe_follow(data)

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
        if not self._configurable(): return
        if not self.confirm.get():
            self.render({'error':'Явно подтвердите обычную обстановку перед фиксацией эталона.'}); return
        if self.collection_ready is False:
            self.render({'error':'Ядро ещё не подтвердило достаточный сбор эталона; обновите готовность.'}); return
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
            self.verdict.set(VERDICT_LABELS.get(self.event.get('disposition'), 'Не просмотрено'))
            self.verdict_comment.set(self.event.get('operator_comment', ''))
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
        return dict(project_id=self.selected(), event_id=identifier)

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
                if role == 'analyzer' and data.get('state') == 'restoring':
                    self.search_data = dict(self.search_data, active=False, coverage='restoring')
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

    def receive_search(self, data, *, diagnostic=True):
        self.search_data = data
        descriptions = {'narrow': 'УЗКИЙ поиск: широкий обзор приостановлен', 'wide': 'Широкий обзор подтверждён ядром',
                        'restoring': 'Широкий обзор восстанавливается', 'paused': 'Широкий обзор приостановлен',
                        'error': 'Ошибка обзора; проверьте статус', 'stopped': 'Обзор остановлен',
                        'unavailable': 'Широкий обзор недоступен; проверьте состояние анализатора'}
        coverage = data.get('coverage', 'unknown')
        self.coverage_text.set(f"{descriptions.get(coverage, 'Покрытие неизвестно')} · событие {data.get('event_id', '—')} · максимум ядра: {data.get('maximum_level', '—')} dBm")
        marks = data.get('marks', [])[-8:]
        self.marks_text.set('; '.join(f"{m.get('label', '')} · {m.get('created_at', '')} · {m.get('level', '—')} dBm" for m in marks)[:900])
        self._refresh_manual(); self.draw_search()
        if diagnostic: self.render(data)

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

    def _build_operations(self, events_page):
        tk, ttk = self.app.tk, self.app.ttk
        self.settings_loaded = False
        self.autostart, self.webhook_enabled, self.beep_enabled = [tk.BooleanVar(value=False) for _ in range(3)]
        self.minimum_free, self.minimum_retention = tk.StringVar(value='268435456'), tk.StringVar(value='3600')
        self.webhook_url, self.webhook_secret = tk.StringVar(), tk.StringVar()
        self.settings_text = tk.StringVar(value='Настройки не загружены. Обновите их перед сохранением.')
        self.storage_text = tk.StringVar(value='Прогноз архива не получен.')
        self.alert_summary = tk.StringVar(value='Уведомления не загружены.')
        self.alert_text = tk.StringVar()
        self.alerts, self.alert_cursor, self.alert_page_after = [], None, None
        self.seen_alerts, self.alerts_initialized = {}, False
        self.next_health_poll = 0
        self.verdict, self.verdict_comment = tk.StringVar(value='Не просмотрено'), tk.StringVar()
        row = ttk.Frame(events_page); row.pack(fill='x', before=self.event_tree.master)
        ttk.Combobox(row, state='readonly', values=list(VERDICT_LABELS.values()), textvariable=self.verdict, width=25).pack(side='left')
        ttk.Entry(row, textvariable=self.verdict_comment, width=30).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Сохранить вердикт', command=self.review_event).pack(side='left')
        operations = ttk.Frame(self.workspace, padding=4); self.workspace.add(operations, text='Эксплуатация')
        pages = ttk.Notebook(operations); pages.pack(fill='both', expand=True)
        self.operations_tabs = pages
        settings_page, storage_page, alerts_page = [ttk.Frame(pages, padding=4) for _ in range(3)]
        for page,title in [(settings_page,'Настройки'),(storage_page,'Архив'),(alerts_page,'Уведомления')]: pages.add(page,text=title)
        self.operations_page, self.alerts_page = operations, alerts_page
        ttk.Checkbutton(settings_page, text='Автоматически восстановить охрану после перезапуска ядра', variable=self.autostart).pack(anchor='w')
        for label,variable in [('Резерв свободного места, байт',self.minimum_free),('Минимальный срок хранения, секунд',self.minimum_retention)]:
            row = ttk.Frame(settings_page); row.pack(fill='x')
            ttk.Label(row,text=label).pack(side='left'); ttk.Entry(row,textvariable=variable,width=20).pack(side='left')
        ttk.Checkbutton(settings_page,text='Включить webhook уведомлений',variable=self.webhook_enabled).pack(anchor='w')
        row = ttk.Frame(settings_page); row.pack(fill='x')
        ttk.Label(row,text='HTTP(S) URL').pack(side='left'); ttk.Entry(row,textvariable=self.webhook_url,width=65).pack(side='left',fill='x',expand=True)
        row = ttk.Frame(settings_page); row.pack(fill='x')
        ttk.Label(row,text='Новый секрет').pack(side='left')
        ttk.Entry(row,textvariable=self.webhook_secret,show='•',width=35).pack(side='left')
        ttk.Label(row,text='Пустое поле сохраняет текущий секрет.').pack(side='left')
        row = ttk.Frame(settings_page); row.pack(fill='x')
        ttk.Button(row,text='Обновить настройки',command=self.load_settings).pack(side='left')
        ttk.Button(row,text='Сохранить настройки',command=self.save_settings).pack(side='left')
        ttk.Label(settings_page,textvariable=self.settings_text,wraplength=900).pack(fill='x')
        ttk.Button(storage_page,text='Обновить прогноз',command=self.storage).pack(anchor='w')
        ttk.Label(storage_page,textvariable=self.storage_text,wraplength=900).pack(fill='x')
        ttk.Label(storage_page,text='Прогноз рассчитывает ядро. При неизвестной скорости срок хранения неизвестен. Автоматическое удаление выключено.',wraplength=900).pack(fill='x')
        row = ttk.Frame(alerts_page); row.pack(fill='x')
        ttk.Button(row,text='Последние уведомления',command=self.load_alerts).pack(side='left')
        ttk.Button(row,text='Следующая страница',command=lambda:self.load_alerts(next_page=True)).pack(side='left')
        ttk.Button(row,text='Подтвердить оператором',command=self.ack_alert).pack(side='left')
        ttk.Checkbutton(row,text='Звук новых тревог на этом ПК',variable=self.beep_enabled).pack(side='left')
        ttk.Label(alerts_page,textvariable=self.alert_summary,wraplength=900).pack(fill='x')
        columns = ('utc','severity','message','ack','delivery')
        alert_rows = ttk.Frame(alerts_page); alert_rows.pack(fill='both',expand=True)
        self.alert_tree = ttk.Treeview(alert_rows,columns=columns,show='headings',height=4)
        for key,label,width in zip(columns,['UTC','Важность','Сообщение','Подтверждение','Доставка'],[180,90,320,170,140]):
            self.alert_tree.heading(key,text=label); self.alert_tree.column(key,width=width,minwidth=60)
        alert_scroll = ttk.Scrollbar(alert_rows,orient='vertical',command=self.alert_tree.yview)
        self.alert_tree.configure(yscrollcommand=alert_scroll.set)
        self.alert_tree.pack(side='left',fill='both',expand=True); alert_scroll.pack(side='right',fill='y')
        self.alert_tree.bind('<<TreeviewSelect>>',self.alert_selected)
        ttk.Label(alerts_page,textvariable=self.alert_text,wraplength=900).pack(fill='x')

    def _reset_operations(self):
        self._tk_timers.cancel('operations')
        self.alerts, self.alert_cursor, self.alert_page_after = [], None, None
        self.seen_alerts, self.alerts_initialized = {}, False
        self.next_health_poll, self.settings_loaded = 0, False
        self.autostart.set(False); self.webhook_enabled.set(False)
        self.minimum_free.set('268435456'); self.minimum_retention.set('3600')
        self.webhook_url.set(''); self.webhook_secret.set('')
        self.verdict.set('Не просмотрено'); self.verdict_comment.set('')
        self.settings_text.set('Настройки не загружены. Обновите их перед сохранением.')
        self.storage_text.set('Прогноз архива не получен.')
        self.alert_summary.set('Уведомления не загружены. Проверка каждые 5 секунд при подключении и выбранном проекте.')
        self.alert_text.set('')
        for identifier in self.alert_tree.get_children(): self.alert_tree.delete(identifier)

    def _arm_operations_poll(self, delay=5000):
        if not self.closed and not getattr(self.app,'_closed',False) and self.app.client and self.selected():
            self._schedule_after(self.app.root,'operations',delay,self.poll_operations)
        else: self._tk_timers.cancel('operations')

    def poll_operations(self):
        if self.closed or getattr(self.app,'_closed',False):
            self._close_timers(); return
        self._sync_context()
        self._arm_operations_poll()
        if not self.app.client or not self.selected() or getattr(self.app,'_action_running',False): return
        health = time.monotonic() >= self.next_health_poll
        if health: self.next_health_poll = time.monotonic()+15
        def completed(data):
            alerts = data.get('alerts',{})
            if alerts.get('poll_error'): self.alert_summary.set('Не удалось проверить уведомления: '+str(alerts['poll_error'])[:500])
            else: self._receive_alerts(alerts,polling=True)
            if 'storage' in data:
                if data['storage'].get('poll_error'): self.storage_text.set('Прогноз не обновлён: '+str(data['storage']['poll_error'])[:500])
                else: self._show_storage(data['storage'])
            if 'status' in data and not data['status'].get('poll_error'): self._show_status(data['status'],update_inputs=False)
            if 'preflight' in data:
                if data['preflight'].get('poll_error'): self.collection_text.set('Прогресс не обновлён: '+str(data['preflight']['poll_error'])[:500])
                else: self._show_preflight(data['preflight'])
            if 'search/status' in data:
                search = data['search/status']
                if search.get('poll_error'): self.coverage_text.set('Не удалось обновить поиск: '+str(search['poll_error'])[:500])
                else: self.receive_search(search,diagnostic=False)
            self._refresh_manual()
        searching = bool(self.search_data.get('active')) or self.search_data.get('coverage') == 'restoring'
        self._submit(partial(_poll_project,self.app.client,self.selected(),health,self.collecting_baseline,searching),completed,background=True)

    def close(self):
        if self.closed: return
        self.closed = True
        self.generation += 1
        self._close_timers()
        self.leases.clear()
        self._operator_actions.clear()

    def load_settings(self):
        self._research('settings',None,self._receive_settings)

    def _receive_settings(self, data):
        data = _public_data(data)
        self.settings_loaded = True
        self.autostart.set(bool(data.get('autostart')))
        archive, webhook = data.get('archive',{}), data.get('webhook',{})
        self.minimum_free.set(str(archive.get('minimum_free_bytes',268435456)))
        self.minimum_retention.set(str(archive.get('minimum_retention_seconds',3600)))
        self.webhook_enabled.set(bool(webhook.get('enabled')))
        self.webhook_url.set(webhook.get('url','')); self.webhook_secret.set('')
        self.settings_text.set(('Секрет webhook настроен. ' if webhook.get('secret_configured') else 'Секрет webhook не настроен. ')
            +'Параметры детектора — на вкладке «Детектор».')
        self.render(data)

    def save_settings(self):
        self._sync_context()
        if not self.settings_loaded:
            self.render({'error':'Сначала обновите настройки выбранного проекта.'}); return
        if getattr(self.app,'_action_running',False): return
        try:
            reserve, retention = int(self.minimum_free.get()), int(self.minimum_retention.get())
            if not 1048576<=reserve<=10**15 or not 60<=retention<=31536000: raise ValueError()
        except ValueError:
            self.render({'error':'Резерв: 1048576…1000000000000000 байт; срок: 60…31536000 секунд, целые числа.'}); return
        hook = dict(enabled=self.webhook_enabled.get(),url=self.webhook_url.get().strip())
        secret = self.webhook_secret.get()
        if secret: hook['secret'] = secret
        payload = dict(autostart=self.autostart.get(),archive=dict(minimum_free_bytes=reserve,minimum_retention_seconds=retention),webhook=hook)
        if self.selected() and self.app.client:
            path = '/'+quote(self.selected(),safe='')+'/settings'
            if self._submit(partial(_post_settings,self.app.client,path,payload),self._receive_settings): self.webhook_secret.set('')

    def storage(self):
        def completed(data): self._show_storage(data); self.render(data)
        self._research('storage',None,completed)

    @staticmethod
    def _bytes_text(value):
        return 'неизвестно' if value is None else f'{value/1024**2:.1f} МиБ'

    def _show_storage(self, data):
        forecast = data.get('estimated_retention_seconds')
        rate = data.get('bytes_per_second')
        self.storage_text.set(f"Архив: {'готов' if data.get('ready') else 'не готов'}. Свободно на диске: {self._bytes_text(data.get('filesystem_free_bytes'))}; "
            f"резерв: {self._bytes_text(data.get('minimum_free_bytes'))}; доступно для архива: {self._bytes_text(data.get('available_bytes'))}.\n"
            +f"Скорость: {'неизвестна' if rate is None else str(round(rate))+' байт/с'}; прогноз хранения: {'неизвестен' if forecast is None else format(forecast/3600,'.1f')+' ч'}; "
            +f"требуется: {data.get('required_retention_seconds','—')} с. Автоматическое удаление выключено.")

    def load_alerts(self, next_page=False):
        if next_page and not self.alert_cursor: return
        after = self.alert_cursor if next_page else None
        query = {'limit':50}
        if after: query['after'] = after
        def completed(data):
            self.alert_page_after = after
            self._receive_alerts(data,polling=False)
            self.render(data)
        self._research('alerts',None,completed,query=query)

    def _receive_alerts(self, data, *, polling):
        items = data.get('items',[])[:100]
        first_page = polling or self.alert_page_after is None
        if first_page:
            unseen = [item for item in items if item['id'] not in self.seen_alerts and not item.get('acknowledged_at')]
            if self.alerts_initialized and unseen and self.beep_enabled.get(): self.app.root.bell()
            for item in reversed(items): self.seen_alerts[item['id']] = True
            while len(self.seen_alerts)>1000: self.seen_alerts.pop(next(iter(self.seen_alerts)))
            self.alerts_initialized = True
        self.alert_summary.set(f"Проверены последние {len(items)} уведомлений; "
            + ('открыта предыдущая страница.' if polling and self.alert_page_after else 'доставка webhook показана отдельно от подтверждения оператора.'))
        if polling and self.alert_page_after: return
        selected = self.alert_tree.selection()
        self.alerts, self.alert_cursor = items, data.get('next_cursor')
        for identifier in self.alert_tree.get_children(): self.alert_tree.delete(identifier)
        for item in self.alerts:
            delivery = item.get('delivery',{})
            self.alert_tree.insert('', 'end',iid=item['id'],values=(item.get('created_at',''),item.get('severity',''),item.get('message',''),
                item.get('acknowledged_at') or 'не подтверждено',DELIVERY_LABELS.get(delivery.get('state'),'неизвестна')))
        if selected and self.alert_tree.exists(selected[0]): self.alert_tree.selection_set(selected[0]); self.alert_selected()
        else: self.alert_text.set('Выберите уведомление для попыток доставки, следующего повтора и ошибки.')

    def alert_selected(self, _event=None):
        selection = self.alert_tree.selection()
        item = next((item for item in self.alerts if selection and item['id']==selection[0]),None)
        if not item: return
        delivery = item.get('delivery',{})
        self.alert_text.set(f"{item.get('kind','')} · событие {item.get('event_id') or '—'} · {str(item.get('message',''))[:300]}\n"
            +f"Доставка: {DELIVERY_LABELS.get(delivery.get('state'),'неизвестна')}; попытки: {delivery.get('attempts',0)}; "
            +f"следующий повтор UTC: {delivery.get('next_attempt_at') or '—'}; ошибка: {str(delivery.get('last_error') or '—')[:300]}")

    def ack_alert(self):
        selection = self.alert_tree.selection()
        if not selection: return
        identifier = selection[0]
        def completed(data):
            updated = []
            for item in self.alerts:
                updated.append(dict(item,acknowledged_at=data.get('acknowledged_at') or 'подтверждено') if item['id']==identifier else item)
            self._receive_alerts(dict(items=updated,next_cursor=self.alert_cursor),polling=False)
            self.render(data)
        self._research('alerts/'+quote(identifier,safe='')+'/ack',{},completed)

    @staticmethod
    def _event_values(event):
        analysis = event.get('analysis') or {}
        return (event.get('last_seen',''),event.get('kind',''),event.get('center_frequency_hz',''),
            VERDICT_LABELS.get(event.get('disposition'),event.get('state','')),analysis.get('state','не запрошен'),'есть' if analysis.get('audio') else 'нет')

    def review_event(self):
        identifier = self.selected_event()
        if not identifier: return
        disposition = next((key for key,label in VERDICT_LABELS.items() if label==self.verdict.get()),None)
        if disposition is None: return
        payload = dict(disposition=disposition,comment=self.verdict_comment.get()[:1000])
        def completed(data):
            self.events = [data if event['id']==identifier else event for event in self.events]
            if self.event_tree.exists(identifier): self.event_tree.item(identifier,values=self._event_values(data))
            self.event_changed(); self.render(data)
        self._research('events/'+quote(identifier,safe='')+'/review',payload,completed,
            guard=lambda:self.selected_event()==identifier)

    def _show_status(self, data, *, update_inputs):
        self.active = bool(data.get('active')) or data.get('project',{}).get('state') in ('armed','searching','degraded')
        project = data.get('project',{})
        if project.get('state') or project.get('coverage'):
            self.collecting_baseline = project.get('state') == 'collecting' or project.get('coverage') == 'preparation'
        worker_errors = '; '.join(str(w.get('error') or w.get('last_error')) for w in data.get('workers',[]) if w.get('error') or w.get('last_error'))
        degraded = project.get('state') == 'degraded'
        coverage = {'wide':'широкий обзор', 'unavailable':'широкий обзор недоступен', 'narrow':'узкий поиск',
                    'restoring':'широкий обзор восстанавливается', 'paused':'обзор на паузе', 'stopped':'обзор остановлен',
                    'preparation':'сбор эталона', 'error':'ошибка обзора'}.get(project.get('coverage'),'покрытие неизвестно')
        guard = 'работает с отказом прибора' if degraded else ('активна' if data.get('active') else 'выключена')
        self.status_text.set(f"Охрана: {guard}; {coverage}; приёмник: {'занят' if data.get('receiver_busy') else 'свободен'}; "
            +f"аудио: {'настроено' if data.get('audio_configured') else 'не выбрано'}. Оборудование: "
            +f"{'не проверено' if data.get('hardware_validation')=='not_run' else data.get('hardware_validation','неизвестно')}. "
            +('Очередь неисправного прибора на паузе, автоматический повтор команд не выполняется. ' if degraded else '')
            +str(data.get('error') or worker_errors or '')[:500])
        if update_inputs:
            self.receiver_mode.set(project.get('receiver_mode','AUTO'))
            configured = project.get('audio')
            self.audio_text.set(('Настроенный вход ядра: '+str(configured.get('name',''))[:800]) if configured else 'Аудио не настроено. Выберите и подтвердите вход AR-DV1 явно.')
        self._update_preparation_controls()
        if data.get('active') or self.collecting_baseline: self._maybe_follow(project)

    def _maybe_follow(self, project):
        identifier = project.get('active_session_id')
        key = (self.selected(),identifier)
        if not identifier or key in self._followed_runs or key in self._following_runs or not hasattr(self.app,'_use_created_session'): return
        if self.app.session_id == identifier:
            self._followed_runs.add(key); return
        original_selection = self.app.session_id
        def completed(data):
            self._following_runs.discard(key)
            if data.get('error'):
                self.status_text.set(data['error']); return
            self._followed_runs.add(key)
            if self.app.session_id == original_selection: self.app._use_created_session(data['session'])
        if self._submit(partial(_follow_session,self.app.client,identifier),completed): self._following_runs.add(key)

    def _build_acceptance(self, prepare_page):
        tk, ttk = self.app.tk, self.app.ttk
        self.antenna_notes = tk.StringVar()
        self.receive_path_confirmed, self.location_confirmed = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        self.preparation_project, self.preparation_saved = None, None
        self.collecting_baseline, self.collection_ready = False, None
        self.preparation_text = tk.StringVar(value='Подготовка не загружена; аппаратная проверка не выполнена.')
        self.collection_text = tk.StringVar(value='Прогресс эталона не получен. Нужны число свипов и длительность, подтверждённые ядром.')
        self.preflight_text = tk.StringVar()
        wizard = ttk.LabelFrame(prepare_page,text='Подготовка наблюдения',padding=4)
        wizard.pack(fill='x',before=self.preparation_baseline_row)
        ttk.Label(wizard,text='1. Антенна, приёмный тракт и место установки',wraplength=900).pack(fill='x')
        ttk.Entry(wizard,textvariable=self.antenna_notes,width=70).pack(fill='x')
        row = ttk.Frame(wizard); row.pack(fill='x')
        ttk.Checkbutton(row,text='Приёмный тракт проверен оператором',variable=self.receive_path_confirmed).pack(side='left')
        ttk.Checkbutton(row,text='Место установки подтверждено',variable=self.location_confirmed).pack(side='left')
        row = ttk.Frame(wizard); row.pack(fill='x')
        ttk.Button(row,text='Обновить подготовку',command=self.load_preparation).pack(side='left')
        ttk.Button(row,text='Сохранить подготовку',command=self.save_preparation).pack(side='left')
        ttk.Label(wizard,textvariable=self.preparation_text,wraplength=900).pack(fill='x')
        row = ttk.Frame(wizard); row.pack(fill='x')
        self.preparation_start_button = ttk.Button(row,text='2. Запустить приём и сбор эталона',command=self.start_preparation,state='disabled')
        self.preparation_start_button.pack(side='left')
        self.preparation_stop_button = ttk.Button(row,text='Остановить сбор',command=self.stop_preparation,state='disabled')
        self.preparation_stop_button.pack(side='left')
        ttk.Button(row,text='Обновить прогресс',command=self.preflight).pack(side='left')
        ttk.Label(wizard,text='Профиль только приёма: команды генерации не отправляются. Физическое состояние RF не проверено.',wraplength=900).pack(fill='x')
        ttk.Label(wizard,textvariable=self.collection_text,wraplength=900).pack(fill='x')
        self.collection_bar = ttk.Progressbar(wizard,maximum=100)
        self.collection_bar.pack(fill='x')
        ttk.Label(wizard,textvariable=self.preflight_text,wraplength=900).pack(fill='x')
        ttk.Label(wizard,text='3. Дождитесь готовности ядра и явно подтвердите обычную обстановку для эталона ниже.',wraplength=900).pack(fill='x')
        for variable in (self.antenna_notes,self.receive_path_confirmed,self.location_confirmed):
            variable.trace_add('write',lambda *_args:self._update_preparation_controls())
        detector_page = ttk.Frame(self.operations_tabs,padding=4)
        self.operations_tabs.add(detector_page,text='Детектор')
        self.detector_values = {key:tk.StringVar(value=str(value)) for key,value in BALANCED_DETECTOR.items()}
        self.detector_loaded = False
        self.detector_text = tk.StringVar(value='Алгоритм robust-city-v1; оборудование не проверено. Загрузите настроенные значения из ядра.')
        ttk.Label(detector_page,text='Инженерные параметры обнаружения. Сохранение доступно после снятия с охраны.',wraplength=900).pack(fill='x')
        fields = [('margin_db','Порог превышения','дБ'),('impulse_db','Порог импульса','дБ'),('hysteresis_db','Гистерезис','дБ'),
                  ('confirmations','Число подтверждений','свипов'),('window_sweeps','Окно подтверждения','свипов'),('dwell_seconds','Исследование приёмником','с')]
        for key,label,unit in fields:
            row = ttk.Frame(detector_page); row.pack(fill='x')
            ttk.Label(row,text=label,width=28).pack(side='left')
            ttk.Entry(row,textvariable=self.detector_values[key],width=10).pack(side='left')
            ttk.Label(row,text=unit).pack(side='left')
        row = ttk.Frame(detector_page); row.pack(fill='x')
        ttk.Button(row,text='Обновить параметры',command=self.load_detector).pack(side='left')
        ttk.Button(row,text='Сбалансированный профиль',command=self.balanced_detector).pack(side='left')
        ttk.Button(row,text='Сохранить параметры',command=self.save_detector).pack(side='left')
        ttk.Label(detector_page,textvariable=self.detector_text,wraplength=900).pack(fill='x')

    def _reset_acceptance(self):
        self.preparation_project, self.preparation_saved = None, None
        self.collecting_baseline, self.collection_ready = False, None
        self.antenna_notes.set(''); self.receive_path_confirmed.set(False); self.location_confirmed.set(False)
        self.preparation_start_button.configure(state='disabled')
        self.preparation_text.set('Подготовка не загружена; аппаратная проверка не выполнена.')
        self.collection_text.set('Прогресс эталона не получен. Обновите готовность выбранного проекта.')
        self.collection_bar.configure(value=0); self.preflight_text.set('')
        self.detector_loaded = False
        for key,value in BALANCED_DETECTOR.items(): self.detector_values[key].set(str(value))
        self.detector_text.set('Алгоритм robust-city-v1; оборудование не проверено. Загрузите настроенные значения из ядра.')

    def _preparation_values(self):
        return dict(antenna_notes=self.antenna_notes.get().strip(),receive_path_confirmed=self.receive_path_confirmed.get(),
                    location_confirmed=self.location_confirmed.get())

    def _update_preparation_controls(self):
        values = self._preparation_values()
        project = self.preparation_project or {}
        adapters = tuple(project.get('profile',{}).get(role,{}).get('settings',{}).get('adapter') for role in ('analyzer','receiver'))
        simulated = adapters == ('tinysa-sim','ardv1-sim')
        confirmed = values['receive_path_confirmed'] and values['location_confirmed']
        declared = self.preparation_saved == values and bool(values['antenna_notes']) and confirmed
        ready = bool(not self.active and not self.collecting_baseline and (simulated or declared))
        self.preparation_start_button.configure(state='normal' if ready else 'disabled')
        self.preparation_stop_button.configure(state='normal' if self.collecting_baseline and not self.active else 'disabled')
        return ready

    def load_preparation(self):
        self._sync_context()
        if self.selected() and self.app.client:
            self._submit(partial(self.app.client.monitoring,'/'+quote(self.selected(),safe='')),self._receive_preparation)

    def _receive_preparation(self, data):
        project = data.get('project',data)
        preparation = project.get('preparation') or {}
        self.preparation_project = project
        if project.get('state') in ('armed','searching','degraded'): self.active = True
        elif project.get('state') in ('draft','ready','collecting','interrupted'): self.active = False
        self.collecting_baseline = project.get('state') == 'collecting' or project.get('coverage') == 'preparation'
        self.antenna_notes.set(preparation.get('antenna_notes',''))
        self.receive_path_confirmed.set(bool(preparation.get('receive_path_confirmed')))
        self.location_confirmed.set(bool(preparation.get('location_confirmed')))
        self.preparation_saved = self._preparation_values()
        self._update_preparation_controls()
        adapters = tuple(project.get('profile',{}).get(role,{}).get('settings',{}).get('adapter') for role in ('analyzer','receiver'))
        declaration = ('Симуляция: физические декларации неприменимы.' if adapters == ('tinysa-sim','ardv1-sim')
            else ('Подтверждения оператора получены.' if self.receive_path_confirmed.get() and self.location_confirmed.get() else 'Для аппаратного приёма нужны оба подтверждения.'))
        self.preparation_text.set('Подготовка сохранена в ядре; аппаратная проверка не выполнена. '+declaration)
        self.render(data)

    def save_preparation(self):
        if not self._configurable(): return
        if self.collecting_baseline:
            self.render({'error':'Остановите сбор перед изменением подготовки.'}); return
        values = self._preparation_values()
        if not 1<=len(values['antenna_notes'])<=1000:
            self.render({'error':'Опишите антенну и приёмный тракт: 1…1000 символов.'}); return
        self._research('preparation',values,self._receive_preparation)

    def start_preparation(self):
        if not self._configurable(): return
        if not self._update_preparation_controls():
            self.render({'error':'Сохраните заметки и оба подтверждения оператора перед аппаратным сбором эталона.'}); return
        def completed(data):
            self.collecting_baseline, self.collection_ready = True, False
            self.next_health_poll = 0
            self.preparation_project = data.get('project',self.preparation_project)
            self.preparation_text.set('Запрошен приём в новом сеансе сбора эталона; аппаратная проверка не выполнена. Статусы исполнителей — в диагностике.')
            self.collection_text.set('Сбор начат. Обновите прогресс; готовность определяет ядро по числу свипов и длительности.')
            self._update_preparation_controls()
            self.render(data)
            self._maybe_follow(data.get('project',{}))
        self._research('preparation/start',{},completed)

    def stop_preparation(self):
        if not self._configurable(): return
        def completed(data):
            self.collecting_baseline = False
            self.preparation_project = data.get('project',self.preparation_project)
            self.preparation_text.set('Сбор остановлен. Исходные измерения сохранены; аппаратная проверка не выполнена.')
            self._update_preparation_controls(); self.render(data)
        self._research('preparation/stop',{},completed)

    def _show_preflight(self, data):
        collection = data.get('baseline_collection')
        if isinstance(collection,dict):
            self.collection_ready = collection.get('ready') is True
            sweeps, elapsed = collection.get('sweeps',0), collection.get('elapsed_seconds',0)
            minimum_sweeps, minimum_seconds = collection.get('minimum_sweeps',20), collection.get('minimum_seconds',60)
            self.collection_text.set(f"Свипы: {sweeps} / {minimum_sweeps}; длительность: {elapsed:g} / {minimum_seconds:g} с. "
                +('Ядро подтвердило готовность к фиксации эталона.' if self.collection_ready else 'Сбор эталона ещё не готов.'))
            progress = min(sweeps/max(1,minimum_sweeps),elapsed/max(1,minimum_seconds))
            self.collection_bar.configure(value=max(0,min(100,progress*100)))
        else:
            self.collection_ready = None
            self.collection_text.set('Ядро не вернуло прогресс сбора эталона; проверьте диагностический ответ.')
        failed = [str(check.get('detail') or check.get('name','')) for check in data.get('checks',[]) if not check.get('ok')]
        self.preflight_text.set(('Проверки ядра пройдены. ' if data.get('ready') else 'Подготовка ещё не готова. ')
            +'; '.join(failed)[:700]+' Оборудование не проверено.')

    def load_detector(self):
        self._research('settings',None,self._receive_detector)

    def _receive_detector(self, data):
        parameters = data.get('detector',{})
        self.detector_loaded = all(key in parameters for key in BALANCED_DETECTOR)
        for key in BALANCED_DETECTOR:
            if key in parameters: self.detector_values[key].set(str(parameters[key]))
        self.detector_text.set('Алгоритм robust-city-v1; оборудование не проверено. Настроено в ядре: '
            +f"порог {parameters.get('margin_db','—')} дБ; импульс {parameters.get('impulse_db','—')} дБ; "
            +f"гистерезис {parameters.get('hysteresis_db','—')} дБ; подтверждения {parameters.get('confirmations','—')} из {parameters.get('window_sweeps','—')} свипов; "
            +f"приёмник {parameters.get('dwell_seconds','—')} с.")
        self.render(data)

    def balanced_detector(self):
        for key,value in BALANCED_DETECTOR.items(): self.detector_values[key].set(str(value))
        self.detector_text.set('Черновик: 6 / 12 / 3 дБ; 3 из 5 свипов; 10 с. Нажмите «Сохранить параметры». Алгоритм robust-city-v1; оборудование не проверено.')

    def save_detector(self):
        if not self._configurable(): return
        if not self.detector_loaded:
            self.render({'error':'Сначала обновите параметры детектора из ядра.'}); return
        try:
            parameters = {key:(int(variable.get()) if key in ('confirmations','window_sweeps') else float(variable.get()))
                          for key,variable in self.detector_values.items()}
            bounds = dict(margin_db=(1,60),impulse_db=(1,120),hysteresis_db=(0,20),dwell_seconds=(1,30))
            if any(not math.isfinite(parameters[key]) or not low<=parameters[key]<=high for key,(low,high) in bounds.items()): raise ValueError()
            if not 1<=parameters['confirmations']<=parameters['window_sweeps']<=15: raise ValueError()
            if parameters['impulse_db']<parameters['margin_db'] or parameters['hysteresis_db']>parameters['margin_db']: raise ValueError()
        except (ValueError,OverflowError):
            self.render({'error':'Проверьте параметры: порог 1…60 дБ; импульс 1…120 дБ и не ниже порога; гистерезис 0…20 дБ и не выше порога; подтверждения 1…окно ≤15 целых свипов; время 1…30 с.'}); return
        self._research('settings',{'detector':parameters},self._receive_detector)
