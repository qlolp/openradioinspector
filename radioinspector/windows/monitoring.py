"""Thin project workspace; every request runs on the existing HTTP executor."""
import json
from functools import partial


class MonitoringPanel:
    def __init__(self, app, notebook):
        self.app, self.notebook = app, notebook
        tk, ttk = app.tk, app.ttk
        self.frame = ttk.Frame(notebook, padding=8)
        notebook.add(self.frame, text='Проект наблюдения')
        self.mode = tk.StringVar(value='research')
        modes = ttk.Frame(self.frame); modes.pack(fill='x')
        for label, value in [('Радиомониторинг', 'monitoring'), ('Исследование', 'research')]:
            ttk.Radiobutton(modes, text=label, value=value, variable=self.mode, command=self.change_mode).pack(side='left')
        ttk.Label(self.frame, text='Переключение экрана не останавливает приборы. Выберите подготовленный двухприборный сеанс.', wraplength=1000).pack(fill='x')
        row = ttk.Frame(self.frame); row.pack(fill='x')
        self.name, self.site = tk.StringVar(value='Наблюдение'), tk.StringVar(value='Объект')
        ttk.Entry(row, textvariable=self.name).pack(side='left')
        ttk.Entry(row, textvariable=self.site).pack(side='left')
        ttk.Button(row, text='Создать проект', command=self.create).pack(side='left')
        self.choice = ttk.Combobox(self.frame, state='readonly'); self.choice.pack(fill='x')
        self.items = []
        buttons = ttk.Frame(self.frame); buttons.pack(fill='x')
        ttk.Button(buttons, text='Обновить проекты', command=self.refresh).pack(side='left')
        ttk.Button(buttons, text='Проверить готовность', command=self.preflight).pack(side='left')
        self.output = tk.Text(self.frame, height=12, state='disabled'); self.output.pack(fill='both', expand=True)
        self.tabs = list(notebook.tabs())

    def selected(self):
        index = self.choice.current()
        return self.items[index]['id'] if 0 <= index < len(self.items) else None

    def render(self, data):
        if isinstance(data.get('items'), list):
            previous = self.selected()
            self.items = data['items']
            self.choice.configure(values=[f"{x['name']} · {x['site']} · {x['state']}" for x in self.items])
            if self.items:
                self.choice.current(next((i for i,x in enumerate(self.items) if x['id'] == previous), 0))
        self.output.configure(state='normal'); self.output.delete('1.0', 'end')
        self.output.insert('1.0', json.dumps(data, ensure_ascii=False, indent=2)); self.output.configure(state='disabled')

    def call(self, path='', payload=None):
        if self.app.client:
            operation = partial(self.app.client.monitoring, path, payload)
            self.app._submit_action('Проект наблюдения', operation, self.render)

    def refresh(self): self.call()
    def create(self):
        if self.app.session_id:
            self.call(payload=dict(name=self.name.get(), site=self.site.get(), session_id=self.app.session_id))
    def preflight(self):
        if self.selected(): self.call('/' + self.selected() + '/preflight')
    def change_mode(self):
        for tab in self.tabs:
            label = self.notebook.tab(tab, 'text')
            visible = self.mode.get() == 'research' or label in ('Проект наблюдения', 'Потоки', 'Спектр и водопад', 'Кандидаты', 'Исследования')
            if visible: self.notebook.add(tab)
            else: self.notebook.hide(tab)
