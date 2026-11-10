from uuid import uuid4
from ..contracts import format_time, utc_now

def record(project_id=None, **fields):
    now = format_time(utc_now())
    return dict(id=uuid4().hex, project_id=project_id, created_at=now, **fields)


def text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{label}: требуется непустая строка до {maximum} символов')
    return value.strip()


