"""Additive v1 resources. Viewing a workspace has no device side effects."""
from urllib.parse import unquote
from ..contracts import api_payload


def dispatch(handler, method, route, payload=None, query=None):
    prefix = '/api/v1/monitoring-projects'
    if route != prefix and not route.startswith(prefix + '/'):
        return False
    service = handler.monitoring
    tail = [unquote(x) for x in route[len(prefix):].strip('/').split('/') if x]
    query = query or {}
    try:
        if method == 'GET' and not tail:
            data = service.store.list('project', limit=int(query.get('limit', ['200'])[0]), after=query.get('after', [''])[0])
            data = [{k: p[k] for k in ('id', 'name', 'site', 'state', 'active_session_id', 'baseline_id', 'autostart')} for p in data]
            handler._send_json(api_payload(dict(items=data, next_cursor=data[-1]['id'] if data else None)))
        elif method == 'POST' and not tail:
            handler._send_json(api_payload(service.create(payload)), 201)
        elif method == 'GET' and len(tail) == 1:
            handler._send_json(api_payload(service.project(tail[0])))
        elif method == 'GET' and len(tail) == 2 and tail[1] == 'preflight':
            handler._send_json(api_payload(service.preflight(tail[0])))
        else:
            handler._send_api_error(404, 'monitoring_route_not_found', 'Маршрут проекта не найден')
    except KeyError as exc:
        handler._send_api_error(404, 'monitoring_not_found', str(exc))
    except (ValueError, TypeError, StopIteration) as exc:
        handler._send_api_error(400, 'invalid_monitoring_request', str(exc))
    return True
