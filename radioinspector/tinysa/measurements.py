"""Derived host results. Never overwrite source arrays or claim device metrology."""
import math
import numpy as np
from ..trace import Trace
from ..measurements import occupied_bandwidth_beta2,bandwidth_at_level,center_frequency_centroid


def analyze(data,method,*,rbw_hz):
    hz=np.asarray(data['frequencies_hz'],dtype=float);levels=np.asarray(data['levels'],dtype=float)
    if data.get('level_unit')!='dBm' or hz.ndim!=1 or levels.shape!=hz.shape or len(hz)<2 or not np.isfinite(hz).all() or not np.isfinite(levels).all() or np.any(np.diff(hz)<=0):
        raise ValueError('Нужна исходная dBm-трасса с возрастающей сеткой')
    if isinstance(rbw_hz,bool) or not isinstance(rbw_hz,(int,float)) or not math.isfinite(rbw_hz) or rbw_hz<=0:
        raise ValueError('Для оценки нужен известный RBW, Гц')
    uncertainty=max(float(np.max(np.diff(hz))),float(rbw_hz))
    trace=Trace(hz,levels)
    if method=='peak':
        i=int(np.argmax(levels));result=dict(name='Sampled peak frequency',value=int(hz[i]),unit='Hz',uncertainty=uncertainty,details=dict(level_dbm=float(levels[i])))
    elif method=='channel_power':
        # Integrate a PSD estimate over each actual interval. Bin sum would
        # depend on point count and double-count overlapping RBW filters.
        reference=float(np.max(levels))
        linear=np.power(10.0,(levels-reference)/10)
        total=float(np.sum((linear[:-1]+linear[1:])*.5*np.diff(hz))/rbw_hz)
        result=dict(name='Channel power estimate',value=reference+10*math.log10(total),unit='dBm',uncertainty=None,
                    details=dict(rbw_hz=rbw_hz,integration='trapezoid_over_actual_grid',enbw_correction='not_calibrated'))
    elif method=='occupied_bandwidth':result=occupied_bandwidth_beta2(trace,frequency_uncertainty_hz=uncertainty).as_dict()
    elif method=='bandwidth_3db':result=bandwidth_at_level(trace,n_db=3,frequency_uncertainty_hz=uncertainty).as_dict()
    elif method=='centroid':result=center_frequency_centroid(trace,frequency_uncertainty_hz=uncertainty).as_dict()
    else:raise ValueError('Неизвестный метод')
    result.setdefault('warnings',[]).extend(['Оценка ядра по исходным отсчётам; не результат встроенного MEASURE',
        'RBW и шкала уровня требуют аппаратной проверки; точность прибора не подтверждена'])
    result['method']=method;result['hardware_validation']='not_run'
    # JSON must remain portable across all three clients.
    def finite(value):
        if isinstance(value,dict):return {k:finite(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)):return [finite(v) for v in value]
        if isinstance(value,float) and not math.isfinite(value):return None
        return value
    return finite(result)
