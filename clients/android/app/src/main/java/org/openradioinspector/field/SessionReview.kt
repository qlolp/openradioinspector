package org.openradioinspector.field

import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import kotlin.math.abs

internal enum class ReviewKind(val title: String) {
    TRACK("Трек GPS"), DATA("Измерения"), NOTES("Заметки"), GAPS("Пропуски"),
}

internal data class PageCursor(val seq: Long? = null, val backwards: Boolean = false)
internal data class ReviewEntry(val seq: Long, val json: JSONObject, val priorGapSeq: Long = 0)
internal data class ReviewPage(val items: List<ReviewEntry>, val older: Boolean, val newer: Boolean,
    val routeLevels: Map<String, RouteReading> = emptyMap())
internal data class SessionInfo(val profile: String, val started: String, val streamId: String,
    val receiver: Boolean = profile == "ardv1")

internal class SessionReview(private val file: File, private val sessionId: String, private val imported: SessionInfo?) {
    constructor(root: File, sessionId: String) : this(FieldArchive.database(File(root, "field"), sessionId), sessionId, null)

    private fun receiver(profile: String) = imported?.receiver ?: (profile == "ardv1")
    // Existing phone sessions keep inline JSON. Portable documents keep the exact source in the
    // private verified ZIP and only fixed-size offsets/lengths/digests in the SQLite cursor.
    private val payloadColumns get() = if (imported == null) "json,0,NULL" else "source_offset,source_length,source_hash"

    private fun row(cursor: Cursor, column: Int, archive: RandomAccessFile?, table: String): JSONObject {
        if (archive == null) return JSONObject(cursor.getString(column))
        val raw = archive.verifiedFieldText(cursor.getLong(column), cursor.getInt(column + 1), cursor.getBlob(column + 2))
        val json = JSONObject(raw)
        return if (table == "events") json else json.getJSONObject("measurement")
    }

    private fun <T> read(block: (SQLiteDatabase, RandomAccessFile?) -> T): T =
        SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READONLY).use { db ->
            if (imported == null) block(db, null)
            else RandomAccessFile(File(file.parentFile, "source.orifield"), "r").use { block(db, it) }
        }

    private fun info(db: SQLiteDatabase): SessionInfo =
        imported ?: db.rawQuery("SELECT profile, started_at, stream_id FROM sessions WHERE id = ?", arrayOf(sessionId)).use {
            check(it.moveToFirst()) { "Пустой сеанс" }
            SessionInfo(it.getString(0), it.getString(1), it.getString(2))
        }
    fun info(): SessionInfo = read { db, _ -> info(db) }

    private fun inSession(record: JSONObject, id: String, streamId: String): Boolean =
        record.opt("id") == id && record.opt("session_id") == sessionId && record.opt("stream_id") == streamId

    fun page(profile: String, kind: ReviewKind, cursor: PageCursor, fixId: String?, frequencyHz: Long? = null): ReviewPage = read { db, archive ->
        val streamId = info(db).streamId
        val table = if (kind != ReviewKind.DATA) "events" else if (receiver(profile)) "observations" else "sweeps"
        val conditions = mutableListOf<String>()
        val args = mutableListOf<String>()
        if (imported != null) {
            conditions += "stream_id = ?"
            args += streamId
        }
        if (kind != ReviewKind.DATA) {
            conditions += "kind = ?"
            args += when (kind) {
                ReviewKind.TRACK -> "gps"
                ReviewKind.NOTES -> "note"
                ReviewKind.GAPS -> "gap"
                else -> error("Unknown event type")
            }
        } else if (fixId != null) {
            conditions += "fix_id = ?"
            args += fixId
        }
        val direction = if (cursor.backwards) "DESC" else "ASC"
        val limit = if (kind == ReviewKind.TRACK) 128 else 16
        val entries = mutableListOf<ReviewEntry>()
        // The indexed preceding gap also protects archives created before GPS segment IDs.
        val gapStream = if (imported != null) " AND g.stream_id = events.stream_id" else ""
        val priorGap = if (kind == ReviewKind.TRACK)
            "(SELECT COALESCE(MAX(g.seq),0) FROM events g WHERE g.kind = 'gap' AND g.seq < events.seq$gapStream)"
            else "0"
        var boundary = cursor.seq
        var bytesOnPage = 0
        var moreBySize = false
        do {
            val predicates = conditions.toMutableList()
            val parameters = args.toMutableList()
            boundary?.let {
                predicates += if (cursor.backwards) "seq < ?" else "seq > ?"
                parameters += it.toString()
            }
            val where = if (predicates.isEmpty()) "" else predicates.joinToString(" AND ", " WHERE ")
            var scanned = 0
            db.rawQuery("SELECT seq, $payloadColumns, $priorGap, id FROM $table$where ORDER BY seq $direction LIMIT ${limit + 1}",
                parameters.toTypedArray()).use { rows ->
                while (entries.size <= limit && rows.moveToNext()) {
                    scanned++
                    val seq = rows.getLong(0)
                    boundary = seq
                    val byteCount = rows.getInt(2)
                    // A single V1 row may exceed a page. Avoid reading the following row until navigation.
                    if (imported != null && entries.isNotEmpty() &&
                        (entries.size >= limit || bytesOnPage + byteCount > reviewPageBytes)) {
                        moreBySize = true
                        break
                    }
                    val record = row(rows, 1, archive, table)
                    if (!inSession(record, rows.getString(5), streamId)) continue
                    if (kind == ReviewKind.DATA && fixId != null && record.recordedFix(receiver(profile)) != fixId) continue
                    entries += ReviewEntry(seq, record, rows.getLong(4))
                    bytesOnPage += byteCount
                }
            }
        } while (!moreBySize && entries.size <= limit && scanned > limit)
        val extra = moreBySize || entries.size > limit
        val page = entries.take(limit)
        val levels = if (kind == ReviewKind.TRACK && frequencyHz != null && page.isNotEmpty())
            routeLevels(db, archive, page, profile, frequencyHz, streamId) else emptyMap()
        ReviewPage(if (cursor.backwards) page.asReversed() else page,
            if (cursor.backwards) extra else cursor.seq != null,
            if (cursor.backwards) cursor.seq != null else extra, levels)
    }

    private fun routeLevels(db: SQLiteDatabase, archive: RandomAccessFile?, fixes: List<ReviewEntry>, profile: String, hz: Long,
        streamId: String): Map<String, RouteReading> {
        val receiver = receiver(profile)
        val table = if (receiver) "observations" else "sweeps"
        val ids = fixes.map { it.json.getString("id") }
        val placeholders = ids.joinToString(",") { "?" }
        val streamFilter = if (imported != null) " AND stream_id = ?" else ""
        val parameters = if (imported != null) ids + streamId else ids
        val readings = mutableMapOf<String, RouteReading>()
        // Reverse the (fix_id, seq) index. A later off-channel tile cannot hide an earlier covering one.
        db.rawQuery("SELECT fix_id, id, $payloadColumns FROM $table WHERE fix_id IN ($placeholders)$streamFilter ORDER BY fix_id DESC, seq DESC", parameters.toTypedArray()).use { rows ->
            while (rows.moveToNext()) {
                val fixId = rows.getString(0)
                val previous = readings[fixId]
                if (previous is RouteReading.Measured) continue
                val source = row(rows, 2, archive, table)
                if (!inSession(source, rows.getString(1), streamId) || source.recordedFix(receiver) != fixId) continue
                val reading = routeReading(source, receiver, hz) ?: continue
                if (previous == null || reading is RouteReading.Measured ||
                    (reading is RouteReading.MissingLevel && previous is RouteReading.OutsideRange)) readings[fixId] = reading
            }
        }
        return readings
    }

    fun source(profile: String, id: String, observation: Boolean? = null): JSONObject? = read { db, archive ->
        val table = if (observation ?: receiver(profile)) "observations" else "sweeps"
        val streamId = info(db).streamId
        val streamFilter = if (imported != null) " AND stream_id = ?" else ""
        val args = if (imported != null) arrayOf(id, streamId) else arrayOf(id)
        db.rawQuery("SELECT $payloadColumns FROM $table WHERE id = ?$streamFilter", args).use {
            if (it.moveToFirst()) row(it, 0, archive, table).takeIf { record ->
                inSession(record, id, streamId)
            } else null
        }
    }
}

internal sealed interface RouteReading {
    val sourceId: String
    data class Measured(override val sourceId: String, val frequencyHz: Long, val level: Double, val unit: String) : RouteReading
    data class MissingLevel(override val sourceId: String, val frequencyHz: Long) : RouteReading
    data class OutsideRange(override val sourceId: String) : RouteReading
}

private fun JSONObject.recordedFix(receiver: Boolean) =
    optJSONObject(if (receiver) "metadata" else "settings")?.opt("field_fix_id") as? String

internal fun routeReading(source: JSONObject, receiver: Boolean, hz: Long): RouteReading? {
    val id = source.getString("id")
    val unit = source.getString("level_unit")
    if (unit != (if (receiver) "s_meter_count" else "dBm")) return null
    if (receiver) {
        if (source.getLong("frequency_hz") != hz) return RouteReading.OutsideRange(id)
        val count = source.opt("level") as? Number ?: return RouteReading.MissingLevel(id, hz)
        return RouteReading.Measured(id, hz, count.toDouble(), unit)
    }
    val frequencies = source.getJSONArray("frequencies_hz")
    val levels = source.getJSONArray("levels")
    val n = frequencies.length()
    if (n == 0 || n != levels.length()) return null
    if (hz !in frequencies.getLong(0)..frequencies.getLong(n - 1)) return RouteReading.OutsideRange(id)
    // Archived grids are ascending. The first minimum is the lower bin on an exact tie.
    val bin = (0 until n).minBy { abs(frequencies.getLong(it) - hz) }
    return RouteReading.Measured(id, frequencies.getLong(bin), levels.getDouble(bin), unit)
}

internal fun reviewTrack(entry: ReviewEntry) = with(entry.json) {
    TrackPoint(getString("id"), getDouble("latitude"), getDouble("longitude"), getString("time_utc"),
        if (isNull("segment_id")) null else getString("segment_id"), entry.priorGapSeq, optBoolean("fresh", true))
}
