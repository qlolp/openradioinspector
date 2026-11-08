"""Measurement map: points and antenna sectors as a portable GeoJSON artifact.

Backlog item 11: «Геоинформационная система и карта» — карта с точками
измерений и секторами. The map is built from operator-supplied coordinates
and optional antenna sectors (azimuth, beam width, radius) using the
existing geodesy helpers of :mod:`radioinspector.geoloc`: sector edges and
arcs are real points on the great circle, not a flat-screen fiction.

The result is a standard GeoJSON ``FeatureCollection`` plus a small
summary. Nothing is measured here and nothing is written to the archive:
the map is a derived artifact of the supplied numbers, and clients draw
it with their own simple projections.
"""

from __future__ import annotations

import math
from typing import Any

from .geoloc import destination_point
from .spectral_mask import strict_json_loads

MAP_FORMAT = "openradioinspector-measurement-map"
MAX_MAP_BYTES = 131_072
MAX_MAP_REQUEST_BYTES = 262_144
MAX_POINTS = 50
MAX_LABEL_LENGTH = 80
MAX_RADIUS_M = 200_000.0
#: Число сегментов дуги сектора между двумя краями.
SECTOR_ARC_SEGMENTS = 8


class MeasurementMapError(ValueError):
    """The supplied points or sectors cannot build a measurement map."""


def validate_map_request(payload: Any) -> dict[str, Any]:
    """Exact request shape: the artifact JSON string alone."""
    if not isinstance(payload, dict) or set(payload) != {"map_json"}:
        raise ValueError("Разрешено только поле map_json")
    raw = payload["map_json"]
    if not isinstance(raw, str):
        raise ValueError("map_json должен быть строкой JSON UTF-8")
    try:
        size = len(raw.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError("Некорректный JSON UTF-8 карты") from exc
    if size > MAX_MAP_BYTES:
        raise ValueError(f"Карта должна занимать не более {MAX_MAP_BYTES} байт UTF-8")
    parsed = parse_measurement_map(raw)
    return {"map_json": raw, "name": parsed["name"], "points": parsed["points"]}


def parse_measurement_map(map_json: str) -> dict[str, Any]:
    """Validate the raw artifact and return its canonical name and points."""
    document = strict_json_loads(map_json)
    fields = {"format", "format_version", "name", "points"}
    if not isinstance(document, dict) or set(document) != fields:
        raise ValueError("Некорректные поля карты измерений")
    if (document["format"] != MAP_FORMAT or type(document["format_version"]) is not int
            or document["format_version"] != 1):
        raise ValueError("Неподдерживаемый формат или версия карты измерений")
    name = document["name"]
    if not isinstance(name, str):
        raise ValueError("name карты должен быть строкой")
    import unicodedata
    normalized = unicodedata.normalize("NFC", name)
    if (any(unicodedata.category(c).startswith("C") for c in normalized)
            or normalized != normalized.strip()
            or not 1 <= len(normalized) <= MAX_LABEL_LENGTH):
        raise ValueError("name карты: 1–80 символов NFC без управляющих")
    points = document["points"]
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise ValueError(f"Карта должна содержать от 1 до {MAX_POINTS} точек")
    return {"name": normalized, "points": [_validate_point(point) for point in points]}


def _validate_point(point: Any) -> dict[str, Any]:
    fields = {"latitude_deg", "longitude_deg", "label", "sector"}
    if not isinstance(point, dict) or set(point) != fields:
        raise ValueError("Точка требует только latitude_deg, longitude_deg, label и sector")
    latitude = _finite(point["latitude_deg"], "latitude_deg")
    longitude = _finite(point["longitude_deg"], "longitude_deg")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("latitude_deg должна быть от −90 до 90")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("longitude_deg должна быть от −180 до 180")
    label = point["label"]
    if not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH:
        raise ValueError("label точки: строка не длиннее 80 символов")
    sector = point["sector"]
    if sector is None:
        return {"latitude_deg": latitude, "longitude_deg": longitude,
                "label": label, "sector": None}
    if not isinstance(sector, dict) or set(sector) != {"azimuth_deg", "beamwidth_deg", "radius_m"}:
        raise ValueError("sector требует только azimuth_deg, beamwidth_deg и radius_m")
    azimuth = _finite(sector["azimuth_deg"], "azimuth_deg")
    beamwidth = _finite(sector["beamwidth_deg"], "beamwidth_deg")
    radius = _finite(sector["radius_m"], "radius_m")
    if not 0.0 <= azimuth < 360.0:
        raise ValueError("azimuth_deg должен быть от 0 (включительно) до 360")
    if not 1.0 <= beamwidth <= 180.0:
        raise ValueError("beamwidth_deg должен быть от 1 до 180")
    if not 1.0 <= radius <= MAX_RADIUS_M:
        raise ValueError(f"radius_m должен быть от 1 до {MAX_RADIUS_M}")
    return {"latitude_deg": latitude, "longitude_deg": longitude, "label": label,
            "sector": {"azimuth_deg": azimuth, "beamwidth_deg": beamwidth, "radius_m": radius}}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MeasurementMapError(f"{name} должен быть конечным числом")
    value = float(value)
    if not math.isfinite(value):
        raise MeasurementMapError(f"{name} должен быть конечным числом")
    return value


def build_measurement_map(name: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a GeoJSON FeatureCollection from validated points and sectors.

    Sector polygons are computed with the great-circle helpers of
    :mod:`radioinspector.geoloc`: the apex, the two edges at azimuth ±
    half beam width and an arc of ``SECTOR_ARC_SEGMENTS`` segments. The
    coordinates are exactly the supplied numbers plus derived polygon
    vertices; nothing is interpolated or invented.
    """
    features = []
    for index, point in enumerate(points):
        coordinates = [point["longitude_deg"], point["latitude_deg"]]
        properties: dict[str, Any] = {
            "kind": "measurement_point",
            "index": index,
            "label": point["label"],
            "latitude_deg": point["latitude_deg"],
            "longitude_deg": point["longitude_deg"],
        }
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coordinates},
            "properties": properties,
        })
        sector = point["sector"]
        if sector is None:
            continue
        polygon = _sector_polygon(point, sector)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [polygon]},
            "properties": {
                "kind": "antenna_sector",
                "index": index,
                "label": point["label"],
                "azimuth_deg": sector["azimuth_deg"],
                "beamwidth_deg": sector["beamwidth_deg"],
                "radius_m": sector["radius_m"],
            },
        })
    latitudes = [point["latitude_deg"] for point in points]
    longitudes = [point["longitude_deg"] for point in points]
    return {
        "map": {
            "format": MAP_FORMAT, "format_version": 1, "name": name,
            "points": points,
        },
        "geojson": {"type": "FeatureCollection", "features": features},
        "summary": {
            "point_count": len(points),
            "sector_count": sum(1 for point in points if point["sector"] is not None),
            "bbox": {
                "min_latitude_deg": min(latitudes), "max_latitude_deg": max(latitudes),
                "min_longitude_deg": min(longitudes), "max_longitude_deg": max(longitudes),
            },
        },
    }


def _sector_polygon(point: dict[str, Any], sector: dict[str, Any]) -> list[list[float]]:
    """Apex, edge arcs and closing apex as a GeoJSON ring in [lon, lat]."""
    latitude, longitude = point["latitude_deg"], point["longitude_deg"]
    azimuth, beamwidth, radius = (
        sector["azimuth_deg"], sector["beamwidth_deg"], sector["radius_m"])
    half = beamwidth / 2.0
    ring: list[list[float]] = [[longitude, latitude]]
    start_bearing = azimuth - half
    steps = SECTOR_ARC_SEGMENTS
    for step in range(steps + 1):
        bearing = start_bearing + beamwidth * step / steps
        lat, lon = destination_point(latitude, longitude, bearing, radius)
        ring.append([lon, lat])
    ring.append([longitude, latitude])
    return ring
