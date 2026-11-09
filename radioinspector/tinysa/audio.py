"""Core-owned analog LISTEN capture; tinySA USB is not an audio device."""
import io
import threading
import time
import wave

_owner=threading.Lock()

def _backend():
    try:import sounddevice
    except (ImportError,OSError) as exc:
        raise ValueError('Нет аудиовхода ядра: установите audio extra / PortAudio и подключите аналоговый LISTEN') from exc
    return sounddevice

def inputs():
    try:
        backend=_backend()
        devices=[dict(index=i,name=str(x['name']),input_channels=int(x['max_input_channels']),
                      default_sample_rate_hz=float(x['default_samplerate']))
                 for i,x in enumerate(backend.query_devices()) if x['max_input_channels']>0]
        return dict(available=bool(devices),devices=devices,reason=None if devices else 'В ядре нет доступных аналоговых аудиовходов',source='analog_input',usb_pcm=False)
    except Exception as exc:return dict(available=False,devices=[],reason=str(exc),source='analog_input',usb_pcm=False)

def capture(parameters,*,check,backend=None):
    backend=backend or _backend()
    seconds=parameters['seconds'];rate=parameters['sample_rate_hz'];device=parameters['device']
    if type(seconds) not in (int,float) or not .1<=seconds<=30 or type(rate) is not int or rate not in (8000,12000,24000,48000) or type(device) is not int or not 0<=device<=1024:
        raise ValueError('Недопустимый формат аналоговой записи')
    if not _owner.acquire(blocking=False):raise ValueError('Аудиовход ядра уже занят')
    chunks=[];received=0;target=round(seconds*rate)*2;done=threading.Event();errors=[]
    def callback(indata,frames,_time,status):
        nonlocal received
        if status:errors.append('Audio input overflow/status: '+str(status));done.set();return
        if done.is_set():return
        block=bytes(indata)
        if len(block)!=frames*2:errors.append('Audio input frame length mismatch');done.set();return
        # The bounded requested prefix is accepted exactly once. Never overwrite
        # already accepted blocks or silently keep only the newest callback.
        accepted=block[:target-received]
        chunks.append(accepted);received+=len(accepted)
        if received>=target:done.set()
    try:
        with backend.RawInputStream(samplerate=rate,device=device,channels=1,dtype='int16',callback=callback,blocksize=1024):
            deadline=time.monotonic()+seconds+5
            while True:
                check()
                if done.wait(.05):break
                if time.monotonic()>=deadline:raise ValueError('Аудиовход не передал полный запрошенный интервал')
        check()
        if errors:raise ValueError(errors[0])
        if received!=target:raise ValueError('Неполная аналоговая запись')
        result=io.BytesIO()
        with wave.open(result,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate);wav.writeframes(b''.join(chunks))
        return result.getvalue()
    finally:_owner.release()
