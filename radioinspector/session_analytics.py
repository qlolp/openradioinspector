"""Read-only аналитика архива сеансов без изменения writer-контура.

Расчёт использует блокировку ``SessionService`` и тот же SQLite connection, но
не создаёт измерений, событий или побочных состояний. Исходные единицы потоков
сохраняются раздельно и никогда не преобразуются друг в друга.
"""

from __future__ import annotations

from typing import Any, Optional

from .contracts import parse_time, to_primitive
from .session_service import SessionService
from .session_store import SQLiteSessionStore


def analytics_summary(
    sessions: SessionService, session_id: str, *, stream_id: Optional[str] = None
) -> dict[str, Any]:
    """Возвращает агрегаты всего архива, а не только последней HTTP-выборки."""
    with sessions._lock:
        sessions._validate_stream_query_locked(session_id, stream_id)
        store = sessions._store
        if store is not None:
            data = _sqlite_analytics(store, session_id, stream_id=stream_id)
        else:
            data = _memory_analytics(sessions, session_id, stream_id=stream_id)
        latest = data["sweeps"]["latest"]
        if latest is None:
            latest_peak = None
        else:
            levels = latest["levels"]
            peak_index = max(range(len(levels)), key=levels.__getitem__)
            latest_peak = {
                "sweep_id": latest["id"],
                "frequency_hz": latest["frequencies_hz"][peak_index],
                "level": levels[peak_index],
                "level_unit": latest["level_unit"],
            }
        data["sweeps"]["latest_peak"] = latest_peak
        observations = data["observations"]
        known = observations["squelch_known"]
        observations["squelch_open_ratio"] = (
            observations["squelch_open"] / known if known else None
        )
        return {
            "session_id": session_id,
            "stream_id": stream_id,
            "archive_wide": True,
            **data,
        }


def _memory_analytics(
    sessions: SessionService, session_id: str, *, stream_id: Optional[str]
) -> dict[str, Any]:
    sweeps = sorted(
        (
            item for item in sessions._sweeps
            if item.session_id == session_id
            and (stream_id is None or item.stream_id == stream_id)
        ),
        key=lambda item: (item.interval.started_at, item.id),
    )
    observations = sorted(
        (
            item for item in sessions._observations
            if item.session_id == session_id
            and (stream_id is None or item.stream_id == stream_id)
        ),
        key=lambda item: (item.interval.started_at, item.id),
    )

    def bounds(items: list[Any]) -> tuple[Optional[str], Optional[str]]:
        if not items:
            return None, None
        return (
            to_primitive(items[0].interval.started_at),
            to_primitive(items[-1].interval.started_at),
        )

    first_sweep, last_sweep = bounds(sweeps)
    first_observation, last_observation = bounds(observations)
    return {
        "sweeps": {
            "count": len(sweeps),
            "first_started_at": first_sweep,
            "last_started_at": last_sweep,
            "level_units": sorted({item.level_unit for item in sweeps}),
            "latest": to_primitive(sweeps[-1]) if sweeps else None,
        },
        "observations": {
            "count": len(observations),
            "first_started_at": first_observation,
            "last_started_at": last_observation,
            "level_units": sorted({item.level_unit for item in observations}),
            "latest": to_primitive(observations[-1]) if observations else None,
            "squelch_known": sum(item.squelch_open is not None for item in observations),
            "squelch_open": sum(item.squelch_open is True for item in observations),
        },
    }


def _sqlite_analytics(
    store: SQLiteSessionStore, session_id: str, *, stream_id: Optional[str]
) -> dict[str, Any]:
    clause, parameters = store._filters(session_id, stream_id)
    # Нормализация нужна для старых строк без долей секунды: лексикографические
    # MIN/MAX тогда совпадают с хронологией ISO-UTC и не материализуют архив.
    sort_time = (
        "CASE WHEN instr(started_at, '.') = 0 "
        "THEN replace(started_at, 'Z', '.000000Z') ELSE started_at END"
    )
    with store._lock:
        sweep_totals = store._connection.execute(
            f"""
            SELECT COUNT(*) AS count,
                   MIN({sort_time}) AS first_started_at,
                   MAX({sort_time}) AS last_started_at
            FROM sweeps WHERE {clause}
            """,
            parameters,
        ).fetchone()
        sweep_units = [
            row["level_unit"]
            for row in store._connection.execute(
                f"SELECT DISTINCT level_unit FROM sweeps WHERE {clause} ORDER BY level_unit",
                parameters,
            ).fetchall()
        ]
        observation_totals = store._connection.execute(
            f"""
            SELECT COUNT(*) AS count,
                   MIN({sort_time}) AS first_started_at,
                   MAX({sort_time}) AS last_started_at,
                   COALESCE(SUM(squelch_open IS NOT NULL), 0) AS squelch_known,
                   COALESCE(SUM(squelch_open = 1), 0) AS squelch_open
            FROM observations WHERE {clause}
            """,
            parameters,
        ).fetchone()
        observation_units = [
            row["level_unit"]
            for row in store._connection.execute(
                f"SELECT DISTINCT level_unit FROM observations WHERE {clause} ORDER BY level_unit",
                parameters,
            ).fetchall()
        ]
        latest_sweep = store.list_sweeps(session_id, stream_id=stream_id, limit=1)
        latest_observation = store.list_observations(
            session_id, stream_id=stream_id, limit=1
        )

    def bounds(row: Any) -> tuple[Optional[str], Optional[str]]:
        first, last = row["first_started_at"], row["last_started_at"]
        if first is None or last is None:
            return None, None
        return to_primitive(parse_time(first)), to_primitive(parse_time(last))

    first_sweep, last_sweep = bounds(sweep_totals)
    first_observation, last_observation = bounds(observation_totals)
    return {
        "sweeps": {
            "count": sweep_totals["count"],
            "first_started_at": first_sweep,
            "last_started_at": last_sweep,
            "level_units": sweep_units,
            "latest": to_primitive(latest_sweep[0]) if latest_sweep else None,
        },
        "observations": {
            "count": observation_totals["count"],
            "first_started_at": first_observation,
            "last_started_at": last_observation,
            "level_units": observation_units,
            "latest": to_primitive(latest_observation[0]) if latest_observation else None,
            "squelch_known": observation_totals["squelch_known"],
            "squelch_open": observation_totals["squelch_open"],
        },
    }
