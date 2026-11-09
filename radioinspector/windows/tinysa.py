"""Native Tk control panel. Workers receive only HTTP context, never Tcl objects."""
from __future__ import annotations
import base64
import json
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .tk_timers import TkTimerOwner


def parse_value(spec,text):
    kind=spec['type']
    if kind in {'boolean','choice'}:return json.loads(text)
    if kind.endswith('_or_auto') and text.strip()=='auto':return 'auto'
    if kind in {'text','multiline'}:return text
    number=float(text)
    if not math.isfinite(number):raise ValueError('Требуется конечное число')
    if kind.startswith('integer'):
        if not number.is_integer():raise ValueError('Требуется целое число')
        return int(number)
    return number


class InstrumentConnection:
    """One immutable server/session/stream scope; no automatic mutation replay."""
    def __init__(self,client,session,stream):
        self.client=client;self.session=session;self.stream=stream
        self.closed=threading.Event();self.lease=None;self.active=None;self.revision=None
        self._heartbeat=None
        self.client_key=uuid.uuid4().hex

    def request(self,path,payload=None,*,offset=0):return self.client.instrument(self.session,self.stream,path,payload,client_key=self.client_key,offset=offset)

    def operation(self,name,parameters=None):
        if self.closed.is_set():raise ValueError('Контекст панели закрыт')
        payload=dict(operation=name,parameters=parameters or {},request_id=uuid.uuid4().hex)
        if self.lease:payload['lease_id']=self.lease
        if name=='setting' and self.revision is not None:payload['expected_revision']=self.revision
        job=self.request('operations',payload);identifier=job['operation_id']
        if name!='lease.renew':self.active=identifier
        try:
            for _ in range(700):
                value=self.request('operations/'+identifier)
                if value.get('context_current') is False:raise ValueError('Соединение прибора изменилось; подключите панель заново')
                if value['state'] in {'succeeded','failed','cancelled','unknown'}:
                    if value['state']!='succeeded':raise ValueError(value.get('error') or value['state'])
                    result=value.get('result') or {}
                    if self.closed.is_set():
                        if result.get('lease_id'):self._release(result['lease_id'])
                        raise ValueError('Контекст панели изменился')
                    if result.get('lease_id'):
                        self.lease=result['lease_id'];self._start_heartbeat()
                    state=result.get('status',result)
                    self.revision=state.get('revision',self.revision)
                    if state.get('lease_active') is False:self.lease=None
                    return result
                if self.closed.wait(.15):raise ValueError('Ожидание отменено; состояние операции проверьте в ядре')
            raise ValueError('Таймаут ожидания; команда не будет повторена')
        finally:
            if self.active==identifier:self.active=None

    def _start_heartbeat(self):
        if self._heartbeat is not None and self._heartbeat.is_alive():return
        def beat(context):
            while not context.closed.wait(8):
                if not context.lease:return
                try:context.operation('lease.renew')
                except Exception:context.lease=None;return
        self._heartbeat=threading.Thread(target=beat,args=(self,),daemon=True,name='tinysa-lease')
        self._heartbeat.start()

    def _release(self,token):
        try:self.request('operations',dict(operation='lease.release',parameters={},lease_id=token,request_id=uuid.uuid4().hex))
        except Exception:pass  # Server expiry remains authoritative.

    def cancel(self):
        identifier=self.active
        if identifier:return self.request('operations/'+identifier+'/cancel',{})

    def close(self):
        self.closed.set();token=self.lease;self.lease=None
        if token:threading.Thread(target=self._release,args=(token,),daemon=True,name='tinysa-release').start()


def _download(context,identifier,path):
    raw=context.client.instrument_artifact(context.session,context.stream,identifier)
    Path(path).write_bytes(raw)
    return {'saved':str(path),'size_bytes':len(raw)}


class TinySAPanel(TkTimerOwner):
    def __init__(self,app,notebook):
        self.app=app;self.closed=False;self.scope=None;self.connection=None;self.future=None;self.generation=0
        self.cap=None;self.artifacts=[];self.inputs={};self.image=None
        self.release_pending=None
        self.artifact_offset=0
        self.executor=ThreadPoolExecutor(max_workers=3,thread_name_prefix='tinysa-client')
        self._init_timers(app.root)
        tk,ttk=app.tk,app.ttk
        outer=ttk.Frame(notebook);notebook.add(outer,text='tinySA · lab / экран')
        viewport=tk.Canvas(outer,highlightthickness=0);scroll=ttk.Scrollbar(outer,orient='vertical',command=viewport.yview)
        scroll.pack(side='right',fill='y');viewport.pack(side='left',fill='both',expand=True);viewport.configure(yscrollcommand=scroll.set)
        frame=ttk.Frame(viewport,padding=8);window=viewport.create_window((0,0),window=frame,anchor='nw')
        frame.bind('<Configure>',lambda _e:viewport.configure(scrollregion=viewport.bbox('all')))
        viewport.bind('<Configure>',lambda e:viewport.itemconfigure(window,width=e.width))
        self.info=tk.StringVar(value='Выберите работающий tinySA serial-поток; ядро владеет USB.')
        ttk.Label(frame,textvariable=self.info,wraplength=1100).pack(fill='x')
        self.rf=tk.BooleanVar(value=False)
        ttk.Checkbutton(frame,text='Разрешаю возможную RF-генерацию в lab; стенд подготовлен',variable=self.rf).pack(anchor='w')
        row=ttk.Frame(frame);row.pack(fill='x')
        for label,fn in [('Подключить панель',self.connect),('Войти в lab',self.acquire),('Monitoring',lambda:self.run('monitor.resume')),('Освободить lab',lambda:self.run('lease.release')),('RF/CAL off',lambda:self.run('output.off')),('Отменить',self.cancel)]:ttk.Button(row,text=label,command=fn).pack(side='left')
        body=ttk.Frame(frame);body.pack(fill='both',expand=True)
        left=ttk.Frame(body);left.pack(side='left',fill='both',expand=True)
        right=ttk.Frame(body);right.pack(side='right',fill='both',expand=True)
        self.screen=tk.Canvas(left,width=480,height=320,background='black',highlightthickness=0);self.screen.pack(anchor='nw')
        self.screen.bind('<ButtonPress-1>',self.touch);self.screen.bind('<ButtonRelease-1>',self.release_touch)
        row=ttk.Frame(left);row.pack(fill='x')
        for label,op,params in [('Экран','capture',{'save':False}),('Сохранить PNG','capture',{'save':True}),('Меню','menu.home',{}),('Отпустить','release',{})]:ttk.Button(row,text=label,command=lambda o=op,p=params:self.run(o,p)).pack(side='left')
        self.keypad=tk.StringVar(value='100M');ttk.Entry(left,textvariable=self.keypad).pack(fill='x')
        ttk.Button(left,text='Число в клавиатуру прибора',command=lambda:self.run('keypad',{'text':self.keypad.get()})).pack()
        self.menu=tk.Listbox(left,height=6,exportselection=False);self.menu.pack(fill='both',expand=True)
        self.menu.bind('<<ListboxSelect>>',self.menu_selected)
        self.setting=tk.StringVar();self.setting_box=ttk.Combobox(right,textvariable=self.setting,state='readonly');self.setting_box.pack(fill='x');self.setting_box.bind('<<ComboboxSelected>>',lambda _e:self.setting_fields())
        self.setting_frame=ttk.Frame(right);self.setting_frame.pack(fill='x')
        ttk.Button(right,text='Применить настройку',command=self.apply_setting).pack()
        self.operation_name=tk.StringVar();self.operation_box=ttk.Combobox(right,textvariable=self.operation_name,state='readonly');self.operation_box.pack(fill='x');self.operation_box.bind('<<ComboboxSelected>>',lambda _e:self.operation_fields())
        # Scroll forms: service/measurement/generator operations share server schemas.
        self.form=ttk.Frame(right);self.form.pack(fill='x')
        ttk.Button(right,text='Выполнить операцию',command=self.apply_operation).pack()
        self.result=tk.Text(right,height=7,wrap='word',state='disabled');self.result.pack(fill='both',expand=True)
        row=ttk.Frame(right);row.pack(fill='x')
        ttk.Button(row,text='Артефакты',command=lambda:self.artifact_page(0)).pack(side='left')
        ttk.Button(row,text='Сохранить файл…',command=self.download).pack(side='left')
        ttk.Button(row,text='Прослушать WAV',command=self.play).pack(side='left')
        ttk.Button(row,text='Новее',command=lambda:self.artifact_page(-100)).pack(side='left')
        ttk.Button(row,text='Старее',command=lambda:self.artifact_page(100)).pack(side='left')
        self.artifact_box=ttk.Combobox(right,state='readonly');self.artifact_box.pack(fill='x')
        ttk.Label(frame,text='ACK ≠ физический RF off. LO/PULSE выключайте на экране. Wiki включает новые Ultra+: наличие проверяйте на приборе. LISTEN требует аналогового аудиовхода ядра.',wraplength=1100).pack(fill='x')
        self._schedule_after(app.root,'scope',200,self.tick)

    def update(self):
        if self.closed:return
        scope=(id(self.app.client),self.app.server_var.get(),self.app.session_id,self.app.selected_stream_id())
        if scope!=self.scope:
            if self.connection:self.connection.close()
            self.scope=scope;self.generation+=1;self.future=None;self.connection=None;self.cap=None;self.release_pending=None;self.artifact_offset=0
            self.screen.delete('all');self.image=None;self.info.set('Контекст изменился; подключите панель заново.')

    def tick(self):
        if self.closed:return
        self.update();self._schedule_after(self.app.root,'scope',200,self.tick)

    def connect(self):
        self.update()
        if not self.app.client or not self.app.session_id or not self.app.selected_stream_id():return
        if self.connection:self.connection.close()
        self.generation+=1;self.future=None
        self.connection=InstrumentConnection(self.app.client,self.app.session_id,self.app.selected_stream_id())
        self.submit(self.connection.request,'capabilities',kind='capabilities')

    def submit(self,fn,*args,kind='result',**kwargs):
        if self.closed or self.future is not None:return
        self.future=self.executor.submit(fn,*args,**kwargs);self.pending=(self.generation,kind);self.info.set('Операция ядра…')
        self._schedule_after(self.app.root,'result',60,self.poll)

    def poll(self):
        future=self.future
        if self.closed or future is None:return
        if not future.done():self._schedule_after(self.app.root,'result',60,self.poll);return
        self.future=None;generation,kind=self.pending
        if generation!=self.generation:return
        try:
            result=future.result()
            if kind=='capabilities':
                self.cap=result['capabilities'];self.setting_keys=list(self.cap['settings']);self.operation_keys=list(self.cap['operations'])
                self.setting_box.configure(values=[self.cap['settings'][k]['label'] for k in self.setting_keys]);self.setting_box.current(0)
                self.operation_box.configure(values=[self.cap['operations'][k]['label'] for k in self.operation_keys]);self.operation_box.current(0)
                self.setting_fields();self.operation_fields();self.menu.delete(0,'end')
                for item in self.cap['menu']:self.menu.insert('end',' → '.join(item['path']))
                self.run('inspect')
            elif kind=='artifacts':
                self.artifacts=result['items'];self.artifact_box.configure(values=[f"{x['kind']} · {x['filename']} · {x['size_bytes']}" for x in self.artifacts])
                if self.artifacts:self.artifact_box.current(0)
            if result.get('preview'):
                self.image=self.app.tk.PhotoImage(data=result['preview']['data_base64']);self.screen.delete('all');self.screen.create_image(0,0,image=self.image,anchor='nw')
            redacted={**result,**({'lease_id':'(аренда этой панели)'} if result.get('lease_id') else {})}
            self.result.configure(state='normal');self.result.delete('1.0','end');self.result.insert('1.0',json.dumps(redacted,ensure_ascii=False,indent=2));self.result.configure(state='disabled')
            self.info.set('Готово. '+('Lab-аренда этой панели активна.' if self.connection and self.connection.lease else 'RF readback не подтверждён.'))
            if result.get('artifact') and self.connection:
                self.artifact_offset=0;self.submit(self.connection.request,'artifacts',kind='artifacts')
        except Exception as exc:self.info.set(str(exc))
        finally:
            if self.release_pending==(self.connection,self.generation) and self.future is None:
                self.release_pending=None;self.run('release')

    def add_input(self,parent,spec):
        tk,ttk=self.app.tk,self.app.ttk
        ttk.Label(parent,text=spec['label']+(' · lab' if spec.get('lab') else '')).pack(anchor='w')
        if spec['type']=='multiline':
            widget=tk.Text(parent,height=3);widget.insert('1.0',str(spec.get('default','')));getter=lambda:widget.get('1.0','end-1c')
        else:
            variable=tk.StringVar()
            if spec['type'] in {'choice','boolean'}:
                values=spec.get('choices',[False,True]);widget=ttk.Combobox(parent,textvariable=variable,state='readonly',values=[json.dumps(v) for v in values]);variable.set(json.dumps(spec.get('default',values[0])))
            else:widget=ttk.Entry(parent,textvariable=variable);variable.set(str(spec.get('default',spec.get('min','auto'))))
            getter=variable.get
        widget.pack(fill='x');return spec,getter

    def setting_fields(self):
        if not self.cap:return
        for child in self.setting_frame.winfo_children():child.destroy()
        self.setting_input=self.add_input(self.setting_frame,self.cap['settings'][self.setting_keys[self.setting_box.current()]])

    def operation_fields(self):
        if not self.cap:return
        for child in self.form.winfo_children():child.destroy()
        op=self.operation_keys[self.operation_box.current()]
        self.inputs={k:self.add_input(self.form,v) for k,v in self.cap['operations'][op]['fields'].items()}

    def apply_setting(self):
        if not self.cap:return
        try:spec,getter=self.setting_input;self.run('setting',dict(name=self.setting_keys[self.setting_box.current()],value=parse_value(spec,getter())))
        except Exception as exc:self.info.set(str(exc))

    def apply_operation(self):
        if not self.cap:return
        try:self.run(self.operation_keys[self.operation_box.current()],{k:parse_value(s,g()) for k,(s,g) in self.inputs.items()})
        except Exception as exc:self.info.set(str(exc))

    def run(self,name,params=None):
        if self.connection:self.submit(self.connection.operation,name,params)

    def acquire(self):
        if not self.rf.get():self.info.set('Подтвердите возможную RF-генерацию в lab.');return
        self.run('lease.acquire',{'acknowledge_rf':True})

    def touch(self,event):
        if self.connection and self.connection.lease and 0<=event.x<480 and 0<=event.y<320:self.run('touch',{'x':event.x,'y':event.y})

    def release_touch(self,_event):
        if self.connection and self.connection.lease:
            if self.future is not None:self.release_pending=(self.connection,self.generation)
            else:self.run('release')

    def cancel(self):
        if self.connection:self.executor.submit(self.connection.cancel)
        self.stop_playback()

    @staticmethod
    def stop_playback():
        try:
            import winsound
            winsound.PlaySound(None,0)
        except ImportError:pass

    def menu_selected(self,_event):
        if self.cap and self.menu.curselection():self.info.set('Экран: '+' → '.join(self.cap['menu'][self.menu.curselection()[0]]['path']))

    def download(self):
        index=self.artifact_box.current()
        if not self.connection or not 0<=index<len(self.artifacts):return
        from tkinter.filedialog import asksaveasfilename
        item=self.artifacts[index];path=asksaveasfilename(parent=self.app.root,initialfile=item['filename'])
        if path:self.submit(_download,self.connection,item['id'],path)

    def artifact_page(self,delta):
        if not self.connection or self.future is not None:return
        self.artifact_offset=max(0,self.artifact_offset+delta)
        context=self.connection;offset=self.artifact_offset
        self.submit(context.request,'artifacts',kind='artifacts',offset=offset)

    def play(self):
        index=self.artifact_box.current()
        if not self.connection or not 0<=index<len(self.artifacts) or self.artifacts[index]['mime_type']!='audio/wav':self.info.set('Выберите WAV в списке артефактов.');return
        context=self.connection;identifier=self.artifacts[index]['id']
        def playback():
            import winsound
            raw=context.client.instrument_artifact(context.session,context.stream,identifier)
            winsound.PlaySound(raw,winsound.SND_MEMORY)
            return {'playback':'completed'}
        self.submit(playback)

    def close(self):
        if self.closed:return
        self.closed=True;self._close_timers()
        if self.connection:self.connection.close()
        self.stop_playback()
        self.executor.shutdown(wait=False,cancel_futures=True)
