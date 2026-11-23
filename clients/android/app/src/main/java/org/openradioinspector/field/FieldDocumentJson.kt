package org.openradioinspector.field

import org.json.JSONObject
import java.time.LocalDateTime
import java.time.ZoneOffset

internal fun JSONObject.text(key: String): String =
    (opt(key) as? String)?.takeIf { it.isNotBlank() } ?: throw IllegalArgumentException("Недопустимое поле $key")

internal fun JSONObject.count(key: String): Long =
    (opt(key) as? Long)?.takeIf { it >= 0 } ?: throw IllegalArgumentException("Недопустимое целое $key")

private val civilTime = Regex("""[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}""")
private val zoneOffset = Regex("""[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]""")

// RFC 3339 permits arbitrary fractional precision. Compare verified digits in chunks, padding
// the shorter fraction with zeros, so source precision survives the complete V1 record range.
internal class FieldTimestamp(private val seconds: Long, private val digits: Int,
    private val fraction: (Int, Int) -> String) : Comparable<FieldTimestamp> {
    override fun compareTo(other: FieldTimestamp): Int {
        if (seconds != other.seconds) return seconds.compareTo(other.seconds)
        for (start in 0 until maxOf(digits, other.digits) step 4096) {
            fun part(stamp: FieldTimestamp) = if (start >= stamp.digits) "" else
                stamp.fraction(start, minOf(start + 4096, stamp.digits))
            val a = part(this)
            val b = part(other)
            for (i in 0 until maxOf(a.length, b.length)) {
                val difference = a.getOrElse(i) { '0' } - b.getOrElse(i) { '0' }
                if (difference != 0) return difference
            }
        }
        return 0
    }
}

internal fun FieldRowObject.timestamp(key: String): FieldTimestamp {
    val length = textLength(key)
    require(length >= 20)
    val local = textPage(key, 0, 19)
    require(civilTime.matches(local) && !local.startsWith("0000-")) { "Недопустимое время UTC" }
    val utc = textPage(key, length - 1, length) == "Z"
    val zoneStart = length - if (utc) 1 else 6
    require(zoneStart >= 19)
    val offset = if (utc) 0 else {
        val zone = textPage(key, zoneStart, length)
        require(zoneOffset.matches(zone)) { "Недопустимый часовой пояс" }
        val seconds = (zone.substring(1, 3).toInt() * 60 + zone.substring(4).toInt()) * 60
        if (zone.first() == '-') -seconds else seconds
    }
    val digits = if (zoneStart == 19) 0 else {
        require(zoneStart > 20 && textPage(key, 19, 20) == ".")
        zoneStart - 20
    }
    fun part(start: Int, end: Int) = textPage(key, 20 + start, 20 + end)
    for (start in 0 until digits step 4096)
        require(part(start, minOf(start + 4096, digits)).all { it in '0'..'9' }) { "Недопустимая доля секунды" }
    return FieldTimestamp(LocalDateTime.parse(local).toEpochSecond(ZoneOffset.UTC) - offset, digits, ::part)
}
