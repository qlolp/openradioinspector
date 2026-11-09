"""Windows measurement-map panel: GeoJSON from supplied coordinates only."""
import json

from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from time import monotonic

from ..measurement_map import MAX_MAP_BYTES, parse_measurement_map


def validate_result(data, session):
    def require(ok, message='Некорректный ответ карты'):
        if not ok:
            raise ValueError(message)
    require(isinstance(data, dict))
    require(data.get('session_id') == session, 'Ответ другого сеанса')
    geojson = data.get('geojson')
    require(isinstance(geojson, dict) and geojson.get('type') == 'FeatureCollection')
    features = geojson.get('features')
    require(isinstance(features, list) and features)
    summary = data.get('summary')
    require(isinstance(summary, dict))
    for key in ('point_count', 'sector_count'):
        require(type(summary.get(key)) is int and summary[key] >= 0, f'Некорректное {key}')
    bbox = summary.get('bbox')
    require(isinstance(bbox, dict)
            and all(type(v) in (int, float) and isfinite(v) for v in bbox.values()))
    for feature in features:
        require(isinstance(feature, dict) and isinstance(feature.get('geometry'), dict)
                and isinstance(feature.get('properties'), dict))
    return data


def result_text(data):
    summary = data['summary']
    return (f"Точек: {summary['point_count']}, секторов: {summary['sector_count']}.\n"
            f"Границы: широта {summary['bbox']['min_latitude_deg']}…{summary['bbox']['max_latitude_deg']}, "
            f"долгота {summary['bbox']['min_longitude_deg']}…{summary['bbox']['max_longitude_deg']}.\n"
            'Карта — производный артефакт от введённых координат: не измерение и не привязка GPS.\n'
            + json.dumps(data['geojson'], ensure_ascii=False, indent=2))


from .tk_timers import TkTimerOwner


class MeasurementMapPanel(TkTimerOwner):
    def __init__(self, app, notebook):
        self.app = app
        self.scope = None
        self.generation = 0
        self.future = None
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='measurement-map')
        self._init_timers(app.root)
        ttk, tk = app.ttk, app.tk
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text='Карта измерений')
        ttk.Label(frame, text='GeoJSON: точки измерений и антенные секторы (азимут, ширина луча, радиус). Секторы строятся ядром по дуге большого круга. Координаты вводятся явно; это производный артефакт, не измерение и не привязка GPS. Архив не изменяется.', wraplength=1050).pack(fill='x')
        ttk.Label(frame, text='Черновик карты · JSON').pack(fill='x')
        self.editor = tk.Text(frame, height=10, wrap='none'); self.editor.pack(fill='both', expand=True)
        row = ttk.Frame(frame); row.pack(fill='x')
        self.button = ttk.Button(row, text='Построить карту', command=self.analyze)
        self.button.pack(side='left')
        ttk.Button(row, text='Очистить', command=self.clear).pack(side='left')
        self.info = tk.StringVar(value='Карта строится только по кнопке.')
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
        self.button.configure(state="normal")
        self.show_details('')
        self.info.set('Карта строится только по кнопке; архив не изменяется.')

    def analyze(self):
        self.update()
        if self.closed or self.future is not None: return
        if self.app.client is None or not self.app.session_id:
            self.info.set('Нет активного сеанса.'); return
        raw = self.editor.get('1.0', 'end-1c')
        try:
            if not raw.strip():
                raise ValueError('Вставьте карту измерений в черновик')
            if len(raw.encode('utf-8')) > MAX_MAP_BYTES:
                raise ValueError('Карта превышает 131 072 байт')
            parse_measurement_map(raw)
        except (ValueError, TypeError) as exc:
            self.info.set(str(exc)); return
        self.info.set('Карта строится ядром…')
        self.button.configure(state='disabled')
        self.deadline = monotonic() + 8.0
        generation = self.generation
        session = self.app.session_id
        self.future = self.executor.submit(self.app.client.measurement_map, session, raw)
        self._schedule_after(self.app.root, 'request', 50, self.finish, generation, session)

    def finish(self, generation, session):
        if self.closed: return
        self.update()
        if generation != self.generation or self.future is None: return
        if monotonic() >= self.deadline:
            self.future = None
            self.info.set('Превышен срок ожидания 8 секунд.')
            self.button.configure(state='normal')
            return
        if not self.future.done():
            self._schedule_after(self.app.root, 'request', 50, self.finish, generation, session); return
        future, self.future = self.future, None
        self.button.configure(state='normal')
        try:
            data = validate_result(future.result(), session)
            self.show_details(result_text(data))
            self.info.set('Карта построена.')
        except Exception as exc:
            self.info.set(f'Карта недоступна: {exc}')

    def update(self):
        if self.closed: return
        scope = (id(self.app.client), self.app.session_id)
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
