package org.openradioinspector.field

import android.database.sqlite.SQLiteDatabase
import org.json.JSONObject
import java.io.File
import java.util.UUID

internal enum class ReviewKind(val title: String) {
    TRACK("Трек GPS"), DATA("Измерения"), NOTES("Заметки"), GAPS("Пропуски"),
}

internal data class PageCursor(val seq: Long? = null, val backwards: Boolean = false)
internal data class ReviewEntry(val seq: Long, val json: JSONObject)
internal data class ReviewPage(val items: List<ReviewEntry>, val older: Boolean, val newer: Boolean)
internal data class SessionInfo(val profile: String, val started: String)

internal class SessionReview(root: File, id: String) {
    private val file = File(root, "field/${UUID.fromString(id)}/capture.sqlite")
    private fun <T> read(block: (SQLiteDatabase) -> T): T =
        SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READONLY).use(block)

    fun info(): SessionInfo = read { db ->
        db.rawQuery("SELECT profile, started_at FROM sessions LIMIT 1", null).use {
            check(it.moveToFirst()) { "Пустой сеанс" }
            SessionInfo(it.getString(0), it.getString(1))
        }
    }

    fun page(profile: String, kind: ReviewKind, cursor: PageCursor, fixId: String?): ReviewPage = read { db ->
        val table = if (kind != ReviewKind.DATA) "events" else if (profile == "ardv1") "observations" else "sweeps"
        val conditions = mutableListOf<String>()
        val args = mutableListOf<String>()
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
        cursor.seq?.let {
            conditions += if (cursor.backwards) "seq < ?" else "seq > ?"
            args += it.toString()
        }
        val where = if (conditions.isEmpty()) "" else conditions.joinToString(" AND ", " WHERE ")
        val direction = if (cursor.backwards) "DESC" else "ASC"
        val limit = if (kind == ReviewKind.TRACK) 128 else 16
        val entries = mutableListOf<ReviewEntry>()
        db.rawQuery("SELECT seq, json FROM $table$where ORDER BY seq $direction LIMIT ${limit + 1}",
            args.toTypedArray()).use { rows ->
            while (rows.moveToNext()) entries += ReviewEntry(rows.getLong(0), JSONObject(rows.getString(1)))
        }
        val extra = entries.size > limit
        val page = entries.take(limit)
        ReviewPage(if (cursor.backwards) page.asReversed() else page,
            if (cursor.backwards) extra else cursor.seq != null,
            if (cursor.backwards) cursor.seq != null else extra)
    }

    fun source(profile: String, id: String): JSONObject? = read { db ->
        val table = if (profile == "ardv1") "observations" else "sweeps"
        db.rawQuery("SELECT json FROM $table WHERE id = ?", arrayOf(id)).use {
            if (it.moveToFirst()) JSONObject(it.getString(0)) else null
        }
    }
}

internal fun reviewTrack(entry: ReviewEntry) = with(entry.json) {
    TrackPoint(getString("id"), getDouble("latitude"), getDouble("longitude"), getString("time_utc"))
}
