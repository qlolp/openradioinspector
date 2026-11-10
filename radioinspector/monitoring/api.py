"""Additive v1 resources. Viewing a workspace has no device side effects."""
from urllib.parse import unquote
from ..contracts import api_payload
from ..device_workers import WorkerError


def dispatch(handler, method, route, payload=None, query=None):
    prefix = '/api/v1/monitoring-projects'
    if route != prefix and not route.startswith(prefix + '/'):
        return False
    service = handler.monitoring
    tail = [unquote(x) for x in route[len(prefix):].strip('/').split('/') if x]
    query = query or {}
    try:
        if len(tail)==3 and tail[1] in ('manual','search'):
            identifier,group,action=tail
            if group=='search' and action=='status' and method=='GET':
                handler._send_json(api_payload(service.search_status(identifier)))
            elif method=='POST' and (group,action) in {
                ('manual','acquire'),('manual','renew'),('manual','return'),('manual','tune'),('search','start'),('search','mark')}:
                handler._send_json(api_payload(getattr(service,group+'_'+action)(identifier,payload)))
            else: raise ValueError('Неизвестное действие ручного управления')
        elif method=='GET' and len(tail)==2 and tail[1]=='history':
            handler._send_json(api_payload(service.history(tail[0],query)))
        elif method=='GET' and len(tail)==3 and tail[1]=='history':
            handler._send_json(api_payload(service.history_original(tail[0],tail[2])))
        elif method=='GET' and len(tail)==4 and tail[1]=='history' and tail[3]=='comparison':
            handler._send_json(api_payload(service.history_comparison(tail[0],tail[2])))
        elif method=='GET' and len(tail)==4 and tail[1]=='events' and tail[3]=='sources':
            handler._send_json(api_payload(service.event_sources(tail[0],tail[2],query)))
        elif method == 'GET' and tail == ['audio-inputs']:
            from ..tinysa.audio import inputs
            handler._send_json(api_payload(inputs()))
        elif len(tail) == 2 and tail[1] in ('arm','disarm','configure','status'):
            if method == 'GET' and tail[1] == 'status':
                handler._send_json(api_payload(service.guard_status(tail[0])))
            elif method == 'POST' and tail[1] != 'status':
                handler._send_json(api_payload(getattr(service,tail[1])(tail[0],payload)))
            else: raise ValueError('Неверный метод управления охраной')
        elif method == 'GET' and len(tail) == 4 and tail[1] == 'events' and tail[3] == 'audio':
            event = service.store.get('event', tail[2])
            if event['project_id'] != tail[0]: raise KeyError('Событие другого проекта')
            ref = event.get('analysis',{}).get('audio')
            if not ref: raise KeyError('Для события нет аудиозаписи')
            item = service.sessions.get_instrument_artifact(ref['session_id'],ref['stream_id'],ref['id'])
            handler.send_response(200); handler.send_header('Content-Type','audio/wav')
            handler.send_header('Content-Length',str(len(item['payload'])))
            handler.send_header('Cache-Control','no-store'); handler.end_headers()
            handler.wfile.write(item['payload'])
        elif method == 'GET' and not tail:
            data = service.store.list('project', limit=int(query.get('limit', ['200'])[0]), after=query.get('after', [''])[0])
            data = [{k: p[k] for k in ('id', 'name', 'site', 'state', 'active_session_id', 'baseline_id', 'autostart')} for p in data]
            handler._send_json(api_payload(dict(items=data, next_cursor=data[-1]['id'] if data else None)))
        elif method == 'POST' and not tail:
            handler._send_json(api_payload(service.create(payload)), 201)
        elif len(tail) == 2 and tail[1] in ('baselines', 'allowed', 'events'):
            identifier, action = tail
            service.project(identifier)
            kind = {'baselines':'baseline', 'allowed':'allowed', 'events':'event'}[action]
            if method == 'GET':
                items = service.store.list(kind, identifier, limit=int(query.get('limit',['200'])[0]), after=query.get('after',[''])[0])
                if kind == 'baseline':
                    items = [{k:v for k,v in x.items() if k not in ('frequencies_hz','p50_dbm','p95_dbm','mad_db','source_refs')} | {'source_count':len(x['source_refs'])} for x in items]
                elif kind == 'event':
                    items = [{k:v for k,v in x.items() if k != 'source_refs'} for x in items]
                handler._send_json(api_payload(dict(items=items, next_cursor=items[-1]['id'] if items else None)))
            elif action == 'baselines':
                handler._send_json(api_payload(service.create_baseline(identifier, payload)), 201)
            elif action == 'allowed':
                handler._send_json(api_payload(service.add_allowed(identifier, payload)), 201)
            else:
                raise ValueError('События создаёт только детектор')
        elif method == 'POST' and len(tail) == 2 and tail[1] == 'analyze':
            if payload: raise ValueError('Анализ новых свипов принимает пустой объект')
            handler._send_json(api_payload(service.analyze(tail[0])))
        elif method == 'POST' and len(tail) == 4 and tail[1] == 'events' and tail[3] == 'review':
            handler._send_json(api_payload(service.review_event(tail[0], tail[2], payload)))
        elif method == 'GET' and len(tail) == 3 and tail[1] in ('events','baselines'):
            service.project(tail[0])
            data = service.store.get('event' if tail[1]=='events' else 'baseline', tail[2])
            if data['project_id'] != tail[0]: raise KeyError('Запись другого проекта')
            handler._send_json(api_payload(data))
        elif method == 'GET' and len(tail) == 1:
            handler._send_json(api_payload(service.project(tail[0])))
        elif method == 'GET' and len(tail) == 2 and tail[1] == 'preflight':
            handler._send_json(api_payload(service.preflight(tail[0])))
        else:
            handler._send_api_error(404, 'monitoring_route_not_found', 'Маршрут проекта не найден')
    except KeyError as exc:
        handler._send_api_error(404, 'monitoring_not_found', str(exc))
    except WorkerError as exc:
        handler._send_api_error(409, 'monitoring_conflict', str(exc))
    except (ValueError, TypeError, StopIteration) as exc:
        handler._send_api_error(400, 'invalid_monitoring_request', str(exc))
    return True
