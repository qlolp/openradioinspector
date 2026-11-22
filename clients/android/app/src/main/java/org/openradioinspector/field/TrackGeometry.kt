package org.openradioinspector.field

import kotlin.math.cos
import kotlin.math.hypot

internal data class TrackPoint(val id: String, val latitude: Double, val longitude: Double, val utc: String)
internal data class MapPoint(val x: Double, val y: Double)

internal fun projectTrack(track: List<TrackPoint>, width: Double, height: Double): List<MapPoint> {
    if (track.isEmpty() || width <= 0 || height <= 0) return emptyList()
    val referenceLatitude = track.map { it.latitude }.average()
    val longitudeScale = cos(Math.toRadians(referenceLatitude))
    var previousLongitude = track.first().longitude
    var longitudeOffset = 0.0
    val local = track.map { point ->
        // Unwrap each GPS segment across the antimeridian.
        longitudeOffset += ((point.longitude - previousLongitude + 540.0) % 360.0) - 180.0
        previousLongitude = point.longitude
        MapPoint(longitudeOffset * longitudeScale, referenceLatitude - point.latitude)
    }
    val left = local.minOf { it.x }
    val top = local.minOf { it.y }
    val spanX = local.maxOf { it.x } - left
    val spanY = local.maxOf { it.y } - top
    val scale = minOf(width / spanX, height / spanY).let { if (it.isFinite()) it * 0.88 else 0.0 }
    return local.map {
        MapPoint(width * 0.5 + (it.x - left - spanX * 0.5) * scale,
            height * 0.5 + (it.y - top - spanY * 0.5) * scale)
    }
}

internal fun closestPoint(points: List<MapPoint>, x: Double, y: Double): Int? =
    points.indices.minByOrNull { hypot(points[it].x - x, points[it].y - y) }
