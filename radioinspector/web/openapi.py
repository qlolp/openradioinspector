"""Additive OpenAPI 3.1 contract for the existing platform, without device I/O.

Domain models are shared by generated SDKs. Extension payloads remain open so
older clients can retain unknown metadata rather than silently discarding it.
Runtime capabilities, leases and server validation remain authoritative.
"""
from __future__ import annotations

import re

from ..contracts import CONTRACT_NAME, CONTRACT_VERSION
from ..request_limits import MAX_CONTROL_REQUEST_BYTES, MAX_MEASUREMENT_REQUEST_BYTES, REQUEST_BODY_TIMEOUT_SECONDS
from ..sweep_quality import MAX_QUALITY_REQUEST_BYTES
from ..sweep_context import MAX_CONTEXT_REQUEST_BYTES
from ..sweep_average import MAX_AVERAGE_REQUEST_BYTES
from ..spectral_mask import MAX_MASK_REQUEST_BYTES
from ..sweep_three_thresholds import MAX_THREE_THRESHOLDS_REQUEST_BYTES
from ..frequency_rounding import MAX_ROUNDING_REQUEST_BYTES
from ..frequency_raster import MAX_ROUNDING_REQUEST_BYTES as MAX_RASTER_REQUEST_BYTES
from ..frequency_counter import MAX_COUNTER_REQUEST_BYTES
from ..field_strength import MAX_FIELD_REQUEST_BYTES, MAX_CALIBRATION_POINTS, MAX_HZ
from ..dvb_parameters import MAX_DVB_REQUEST_BYTES
from ..measurement_map import MAX_MAP_REQUEST_BYTES
from ..signal_list import MAX_IMPORT_REQUEST_BYTES

ANALYSIS_BUDGETS = {
    'sweep-quality': MAX_QUALITY_REQUEST_BYTES, 'sweep-context': MAX_CONTEXT_REQUEST_BYTES,
    'sweep-occupancy': MAX_CONTEXT_REQUEST_BYTES, 'sweep-average-comparison': MAX_AVERAGE_REQUEST_BYTES,
    'sweep-average-change-detection': MAX_AVERAGE_REQUEST_BYTES, 'sweep-mask-analysis': MAX_MASK_REQUEST_BYTES,
    'sweep-three-thresholds': MAX_THREE_THRESHOLDS_REQUEST_BYTES,
    'candidate-frequency-rounding': MAX_ROUNDING_REQUEST_BYTES, 'frequency-rounding': MAX_RASTER_REQUEST_BYTES,
    'frequency-counter': MAX_COUNTER_REQUEST_BYTES, 'dvb-evaluation': MAX_DVB_REQUEST_BYTES,
    'measurement-map': MAX_MAP_REQUEST_BYTES,
    'field-strength-rms-v2': MAX_FIELD_REQUEST_BYTES,
}


def ref(name):
    return {'$ref': '#/components/schemas/' + name}


def obj(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {},
            'required': list(required), 'additionalProperties': True}


def array(items):
    return {'type': 'array', 'items': items}


def field_strength_schemas():
    """The finite-input/calibration applicability rules remain core-owned."""
    def closed(properties):
        return {**obj(properties, tuple(properties)), 'additionalProperties': False}

    text = {'type': 'string', 'minLength': 1, 'maxLength': 512}
    identity = {'type': 'string', 'minLength': 1}
    number = {'type': 'number', 'format': 'double'}
    hz = {'type': 'integer', 'format': 'uint64', 'minimum': 0, 'maximum': MAX_HZ}
    positive = {**number, 'minimum': 1e-12}
    uncertainty = {'type': ['number', 'null'], 'format': 'double', 'minimum': 0,
                   'description': 'Declared combined uncertainty in dB; null means unknown, never zero.'}
    point = closed({'frequency_hz': hz, 'antenna_factor_db_per_m': number,
        'cable_loss_db': number, 'preamp_gain_db': number, 'level_correction_db': number,
        'uncertainty_db': uncertainty})
    profile = closed({'id': text,
        'version': {'type': 'integer', 'format': 'uint64', 'minimum': 1, 'maximum': MAX_HZ},
        'instrument_profile': text, 'measurement_chain_id': text, 'reference_plane': text,
        'impedance_ohm': positive, 'detector': {'type': 'string', 'enum': ['RMS']},
        'rbw_hz': positive, 'waveform': {'type': 'string', 'enum': ['CW']},
        'valid_from': {'type': 'string', 'format': 'date-time'},
        'valid_until': {'type': 'string', 'format': 'date-time'}, 'uncertainty_basis': text,
        'points': {**array(ref('FieldStrengthCalibrationPoint')), 'minItems': 1,
                   'maxItems': MAX_CALIBRATION_POINTS}})
    profile['description'] = ('Operator-supplied calibration snapshot, not hardware certification. '
        'Points must have strictly increasing integer Hz. No extrapolation. The full source UTC '
        'interval and all seven receiver/chain/plane/impedance/RMS/RBW/CW settings must match.')
    return {
        'FieldStrengthCalibrationPoint': point,
        'FieldStrengthCalibration': profile,
        'FieldStrengthRequest': closed({'sweep_id': text, 'frequency_hz': hz,
                                       'calibration': ref('FieldStrengthCalibration')}),
        'FieldStrengthSource': closed({'id': text, 'session_id': identity, 'stream_id': identity,
            'interval': ref('TimeInterval'), 'frequency_hz': hz,
            'bin_index': {'type': 'integer', 'format': 'uint64', 'minimum': 0},
            'level': number, 'level_unit': text, 'settings': ref('JSONObject')}),
        'FieldStrengthCalibrationEvidence': closed({'profile': ref('FieldStrengthCalibration'),
            'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
            'coefficients': ref('FieldStrengthCalibrationPoint')}),
        'FieldStrengthResult': closed({
            'methodology': {'type': 'string', 'enum': ['field_strength_rms_v2']},
            'value': {**number, 'minimum': 0}, 'unit': {'type': 'string', 'enum': ['V/m']},
            'value_dbuv_per_m': number, 'uncertainty_db': uncertainty,
            'warnings': array({'type': 'string'}), 'source': ref('FieldStrengthSource'),
            'calibration': ref('FieldStrengthCalibrationEvidence'), 'applicability': text}),
    }


def schemas():
    text = {'type': 'string'}
    identifier = {'type': 'string', 'minLength': 1}
    utc = {'type': 'string', 'format': 'date-time',
           'description': 'UTC interval; preserve six microsecond digits and normalize legacy offsets exactly.'}
    hz = {'type': 'integer', 'minimum': 0, 'description': 'Exact integer Hz; never derive through float32.'}
    number = {'type': 'number', 'format': 'double', 'description': 'Finite IEEE-754 float64 in the original unit.'}
    nullable_text = {'type': ['string', 'null']}
    raw = obj()
    interval = obj({'started_at': utc, 'ended_at': utc}, ('started_at', 'ended_at'))
    stream = obj({'id': identifier, 'session_id': identifier, 'device': text,
        'role': {'type': 'string', 'enum': ['spectrum_analyzer', 'receiver', 'simulator']},
        'level_unit': identifier, 'port': nullable_text, 'settings': raw,
        'capabilities': array(text), 'state': text}, ('id', 'session_id', 'device', 'role', 'level_unit'))
    session = obj({'id': identifier, 'name': text, 'created_at': utc,
        'started_at': {'type': ['string', 'null'], 'format': 'date-time'},
        'ended_at': {'type': ['string', 'null'], 'format': 'date-time'},
        'streams': array(ref('DeviceStream')), 'state': text, 'metadata': raw,
        'contract_version': {'type': 'integer', 'const': CONTRACT_VERSION}},
        ('id', 'name', 'streams', 'state', 'created_at'))
    sweep = obj({'id': identifier, 'session_id': identifier, 'stream_id': identifier,
        'interval': ref('TimeInterval'), 'frequencies_hz': array(hz), 'levels': array(number),
        'level_unit': identifier, 'settings': raw},
        ('id', 'session_id', 'stream_id', 'interval', 'frequencies_hz', 'levels', 'level_unit'))
    observation = obj({'id': identifier, 'session_id': identifier, 'stream_id': identifier,
        'interval': ref('TimeInterval'), 'frequency_hz': hz, 'mode': identifier,
        'level': {'type': ['number', 'null'], 'format': 'double'}, 'level_unit': identifier,
        'squelch_open': {'type': ['boolean', 'null']}, 'decoded_mode': nullable_text,
        'audio_uri': nullable_text, 'metadata': raw},
        ('id', 'session_id', 'stream_id', 'interval', 'frequency_hz', 'mode', 'level_unit'))
    return {'JSONObject': raw, 'TimeInterval': interval, 'DeviceStream': stream,
        'Session': session, 'Sweep': sweep, 'ReceiverObservation': observation,
        'CoreCandidateInfo': obj({
            'mode': {'type': 'string', 'enum': ['shadow', 'replay', 'live']},
            'native_connector_enabled': {'type': 'boolean'},
            'compatibility_complete': {'type': 'boolean'},
            'release_accepted': {'type': 'boolean'}, 'hardware_validation': text,
            'history_worker_configured': {'type': 'boolean'},
            'unsupported_features': array(text), 'resource_limits': ref('JSONObject')}, ('mode',)),
        'CoreCapabilities': obj({'api_version': text, 'contract_version': {'type': 'integer'},
            'features': ref('JSONObject'), 'candidate': ref('CoreCandidateInfo')}, ('features',)),
        'SweepSummary': obj({'id':identifier,'session_id':identifier,'stream_id':identifier,
            'interval':ref('TimeInterval'),'level_unit':identifier,'point_count':{'type':'integer','minimum':1},
            'min_frequency_hz':hz,'max_frequency_hz':hz,'peak_frequency_hz':hz,'peak_level':number},
            ('id','session_id','stream_id','interval','level_unit','point_count','min_frequency_hz','max_frequency_hz','peak_frequency_hz','peak_level')),
        'SweepSummaryPage': obj({'items':array(ref('SweepSummary')), 'next_cursor':nullable_text},('items','next_cursor')),
        'AudioSegmentInput': obj({'id':identifier,'stream_id':identifier,'frequency_hz':hz,'mode':identifier,
            'interval':ref('TimeInterval'),'data_base64':text,'sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'}},
            ('id','stream_id','frequency_hz','mode','interval','data_base64','sha256')),
        'ArtifactSummary': obj({'id':identifier,'session_id':identifier,'stream_id':identifier,'kind':text,
            'schema_version':{'type':'integer','const':1},'mime_type':text,'filename':text,'sha256':text,
            'created_at':utc,'metadata':raw,'size_bytes':{'type':'integer','minimum':0}},
            ('id','session_id','stream_id','kind','schema_version','mime_type','filename','sha256','created_at','metadata','size_bytes')),
        'NewSession': obj({'id': identifier, 'name': text, 'streams': array(raw), 'metadata': raw}),
        'Candidate': obj({'id': identifier, 'session_id': identifier, 'source_stream_id': identifier,
            'center_frequency_hz': hz, 'bandwidth_hz': {'type': ['integer', 'null']},
            'interval': ref('TimeInterval'), 'level_unit': identifier,
            'peak_level': {'type': ['number', 'null'], 'format': 'double'},
            'state': {'type': 'string', 'enum': ['new', 'queued', 'investigating', 'resolved', 'dismissed']}},
            ('id', 'session_id', 'source_stream_id', 'center_frequency_hz', 'level_unit', 'state')),
        'Investigation': obj({'id': identifier, 'session_id': identifier, 'candidate_id': identifier,
            'target_stream_id': identifier, 'mode': nullable_text, 'state': text,
            'created_at': utc, 'observation_ids': array(identifier), 'error': nullable_text},
            ('id', 'session_id', 'candidate_id', 'target_stream_id', 'state')),
        'SessionEvent': obj({'sequence': {'type': 'integer', 'minimum': 0},
            'event_type': identifier, 'occurred_at': utc, 'session_id': nullable_text, 'payload': raw},
            ('sequence', 'event_type', 'occurred_at', 'payload')),
        'MonitoringProject': obj({'id': identifier, 'name': text, 'site': text, 'state': text,
            'active_session_id': nullable_text, 'baseline_id': nullable_text,
            'autostart': {'type': 'boolean'}, 'session_ids': array(identifier)}, ('id', 'name', 'site', 'state')),
        **field_strength_schemas(),
        'Envelope': obj({'contract': {'type': 'string', 'const': CONTRACT_NAME},
            'contract_version': {'type': 'integer', 'const': CONTRACT_VERSION}, 'data': {}},
            ('contract', 'contract_version', 'data')),
        'ErrorEnvelope': obj({'contract': {'type': 'string', 'const': CONTRACT_NAME},
            'contract_version': {'type': 'integer', 'const': CONTRACT_VERSION},
            'error': obj({'code': text, 'message': text}, ('code', 'message'))},
            ('contract', 'contract_version', 'error'))}


def document():
    paths = {}

    def add(path, method='get', response=None, *, body=None, status=200, queries=(), mime='application/json'):
        path = '/api/v1' + path
        parameters = [{'name': name, 'in': 'path', 'required': True, 'schema': {'type': 'string'}}
                      for name in re.findall(r'\{([^}]+)\}', path)]
        parameters += [{'name': name, 'in': 'query', 'required': False,
                        'schema': {'type': 'integer', 'minimum': 0} if name in ('limit', 'after_sequence', 'offset')
                        else {'type': 'string'}} for name in queries]
        data = response if response is not None else ref('JSONObject')
        result = {'allOf': [ref('Envelope'), {'properties': {'data': data}}]}
        if mime != 'application/json':
            result = {'type': 'string', 'format': 'binary'}
        operation = {'operationId': method+'_'+re.sub(r'[^a-zA-Z0-9]+', '_', path).strip('_'),
            'parameters': parameters, 'responses': {str(status): {'description': 'Successful response',
                'content': {mime: {'schema': result}}}}}
        for code in (400, 403, 404, 408, 409, 413, 415, 500, 503, 507):
            operation['responses'][str(code)] = {'description': 'Structured platform error',
                'content': {'application/json': {'schema': ref('ErrorEnvelope')}}}
        if method == 'post':
            operation['requestBody'] = {'required': body is not None,
                'content': {'application/json': {'schema': body or ref('JSONObject')}}}
            maximum = ANALYSIS_BUDGETS.get(path.rsplit('/', 1)[-1], MAX_CONTROL_REQUEST_BYTES)
            if path.endswith('/signal-list/import'):
                maximum = MAX_IMPORT_REQUEST_BYTES
            if path.endswith(('/sweeps', '/sweeps/batch', '/observations', '/observations/batch', '/phonogram/frames', '/audio-segments')):
                maximum = MAX_MEASUREMENT_REQUEST_BYTES
            operation['x-ori-max-request-bytes'] = maximum
            operation['x-ori-body-deadline-seconds'] = REQUEST_BODY_TIMEOUT_SECONDS
        paths.setdefault(path, {})[method] = operation

    for route in ('health', 'capabilities', 'archive/capacity', 'task-scheduler'):
        add('/'+route, response=ref('CoreCapabilities') if route == 'capabilities' else None)
    add('/openapi.json')
    paths['/api/v1/openapi.json']['get']['responses']['200']['content']['application/json']['schema'] = \
        obj({'openapi': {'type': 'string'}, 'info': obj(), 'paths': obj()}, ('openapi', 'info', 'paths'))
    add('/measurement-catalog')
    add('/task-scheduler', 'post', body=ref('JSONObject'))
    add('/task-scheduler/tasks')
    add('/task-scheduler/tasks', 'post', body=ref('JSONObject'), status=201)
    add('/task-scheduler/tasks/{name}/cancel', 'post')
    add('/sessions', response=array(ref('Session')))
    add('/sessions', 'post', ref('Session'), body=ref('NewSession'), status=201)
    add('/sessions/{id}/sweep-summaries', response=ref('SweepSummaryPage'),
        queries=('stream_id','limit','after','start','end','min_hz','max_hz'))
    paths['/api/v1/sessions/{id}/sweep-summaries']['get']['description'] = \
        'Newest UTC start and exact UTF-8 ID first. Cursor freezes the ingestion watermark and filters. Frequency filters select intersecting complete source sweeps; peaks summarize the whole source.'
    add('/sessions/{id}/audio-segments','post',ref('ArtifactSummary'),body=ref('AudioSegmentInput'),status=201)
    add('/workers', response=array(ref('JSONObject')))
    add('/events/history', response=array(ref('SessionEvent')), queries=('after', 'limit'))
    add('/events', mime='text/event-stream', queries=('after',))
    base = '/sessions/{id}'
    add(base, response=ref('Session'))
    for action in ('start', 'stop', 'workers/start', 'workers/stop'):
        add(base+'/'+action, 'post')
    add(base+'/workers', response=array(ref('JSONObject')))
    add(base+'/streams/{stream_id}')
    for action in ('start', 'stop', 'tune'):
        add(base+'/streams/{stream_id}/'+action, 'post')
    for kind, model in (('sweeps', 'Sweep'), ('observations', 'ReceiverObservation')):
        add(base+'/'+kind, response=array(ref(model)), queries=('stream_id', 'limit'))
        add(base+'/'+kind, 'post', ref(model), body=ref('JSONObject'), status=201)
        add(base+'/'+kind+'/batch', 'post', body=ref('JSONObject'), status=201)
    for kind, model, item in (('candidates', 'Candidate', 'candidate_id'),
                              ('investigations', 'Investigation', 'investigation_id')):
        add(base+'/'+kind, response=array(ref(model)), queries=('state',))
        add(base+'/'+kind+'/{'+item+'}', response=ref(model))
        add(base+'/'+kind+'/by-id/{'+item+'}', response=ref(model))
    add(base+'/candidates', 'post', ref('Candidate'), body=ref('JSONObject'), status=201)
    add(base+'/candidates/{candidate_id}/investigations', 'post', ref('Investigation'), body=ref('JSONObject'), status=201)
    add(base+'/candidates/{candidate_id}/dismiss', 'post')
    add(base+'/investigations/next')
    add(base+'/investigations/start-next', 'post')
    for action in ('start', 'complete', 'fail', 'cancel'):
        add(base+'/investigations/{investigation_id}/'+action, 'post')
    for policy in ('automatic-detection', 'automatic-handoff', 'automatic-completion'):
        add(base+'/'+policy)
        add(base+'/'+policy, 'post', body=ref('JSONObject'))
    for analysis in ('candidate-frequency-rounding', 'sweep-average-comparison',
                     'sweep-average-change-detection', 'sweep-three-thresholds', 'sweep-context',
                     'sweep-occupancy', 'frequency-rounding', 'sweep-quality', 'frequency-counter',
                     'dvb-evaluation', 'measurement-map', 'sweep-mask-analysis'):
        add(base+'/'+analysis, 'post', body=ref('JSONObject'))
    add(base+'/field-strength-rms-v2', 'post', ref('FieldStrengthResult'), body=ref('FieldStrengthRequest'))
    add(base+'/sweep-comparison', queries=('reference_id', 'target_id'))
    add(base+'/sweep-threshold', queries=('sweep_id', 'window_points', 'margin_db'))
    add(base+'/sweep-references', queries=('limit', 'offset'))
    add(base+'/sweep-references', 'post', body=ref('JSONObject'), status=201)
    add(base+'/measurement-results', queries=('limit', 'methodology'))
    add(base+'/measurement-results', 'post', body=ref('JSONObject'), status=201)
    add(base+'/analytics/summary', queries=('stream_id',))
    add(base+'/archive/export', queries=('stream_id',))
    add(base+'/archive/export.ndjson', queries=('stream_id',), mime='application/x-ndjson')
    for part in ('phonogram/status', 'audio/status'):
        add(base+'/'+part, queries=('stream_id',))
    for action in ('start', 'stop', 'frames'):
        add(base+'/phonogram/'+action, 'post')
    add(base+'/phonogram/wav', mime='audio/wav', queries=('stream_id',))
    add(base+'/audio/stream', mime='audio/L16', queries=('stream_id',))
    add(base+'/signal-list/export', queries=('name', 'stream_id'))
    add(base+'/signal-list/import', 'post', body=ref('JSONObject'), status=201)

    instrument = base+'/streams/{stream_id}/instrument'
    for part in ('capabilities', 'settings', 'control-lease', 'operations', 'operations/{operation_id}',
                 'artifacts', 'artifacts/{artifact_id}'):
        add(instrument+'/'+part)
    add(instrument+'/screen', status=202)
    add(instrument+'/artifacts/{artifact_id}/content', mime='application/octet-stream')
    for action in ('operations', 'settings', 'control-lease'):
        add(instrument+'/'+action, 'post', body=ref('JSONObject'), status=202)
    add(instrument+'/operations/{operation_id}/cancel', 'post', body=ref('JSONObject'))

    project = '/monitoring-projects'
    add(project, queries=('limit', 'after'))
    add(project, 'post', ref('MonitoringProject'), body=ref('JSONObject'), status=201)
    add(project+'/audio-inputs')
    project += '/{project_id}'
    add(project, response=ref('MonitoringProject'))
    for part in ('preflight', 'status', 'settings', 'storage', 'alerts', 'baselines', 'allowed', 'events'):
        add(project+'/'+part, queries=('limit', 'after') if part in ('alerts', 'baselines', 'allowed', 'events') else ())
    for action in ('arm', 'disarm', 'configure', 'analyze', 'settings', 'preparation',
                   'preparation/start', 'preparation/stop', 'manual/acquire', 'manual/renew',
                   'manual/return', 'manual/tune', 'search/start', 'search/mark'):
        add(project+'/'+action, 'post')
    for kind in ('baselines', 'allowed'):
        add(project+'/'+kind, 'post', body=ref('JSONObject'), status=201)
    add(project+'/search/status')
    add(project+'/baselines/{baseline_id}')
    add(project+'/events/{event_id}')
    add(project+'/events/{event_id}/review', 'post', body=ref('JSONObject'))
    add(project+'/events/{event_id}/sources', queries=('after', 'limit'))
    add(project+'/events/{event_id}/audio', mime='audio/wav')
    add(project+'/alerts/{alert_id}/ack', 'post')
    add(project+'/history', queries=('limit', 'after', 'start', 'end', 'min_hz', 'max_hz', 'event_id'))
    add(project+'/history/{sweep_id}', response=ref('Sweep'))
    add(project+'/history/{sweep_id}/comparison')
    return {'openapi': '3.1.0', 'info': {'title': 'OpenRadioInspector platform', 'version': '1.0.0',
        'description': 'Backward-compatible v1. Hardware ACK is not physical RF readback. '
                       'Client workspace changes never disarm guard. Original units and source IDs are retained.'},
        'servers': [{'url': '/'}], 'paths': paths, 'components': {'schemas': schemas()},
        'x-ori-contract': CONTRACT_NAME, 'x-ori-contract-version': CONTRACT_VERSION}
