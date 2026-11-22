package org.openradioinspector.field

import android.content.ContentValues
import android.database.sqlite.SQLiteDatabase
import android.location.Location
import android.os.SystemClock
import org.json.JSONArray
import org.json.JSONObject
import java.io.Closeable
import java.io.File
import java.time.Instant
import java.util.UUID

internal fun FieldConfig.nativePlan(): JSONObject = JSONObject().put("kind", kind).apply {
    when (kind) {
        "rf_explorer_survey", "rf_explorer_sweep", "tinysa_rx", "ardv1_range" -> {
            if (kind != "rf_explorer_sweep" || startHz != 0L || stopHz != 0L) {
                put("start_hz", startHz)
                put("stop_hz", stopHz)
            }
        }
        "ardv1_single" -> put("frequency_hz", startHz)
        "ardv1_list" -> put("frequencies_hz", JSONArray(frequenciesHz))
    }
    if (kind == "ardv1_range" || kind == "rf_explorer_survey") put("step_hz", stepHz)
    if (profile == "ardv1") {
        put("mode", mode)
        put("continuous", continuous)
        put("settle_ms", settleMs)
    }
    if (profile == "tinysa") {
        put("points", points)
        put("rbw_hz", rbwHz)
    }
}

internal data class CommittedSample(
    val frequencies: List<Long>, val values: List<Double>, val unit: String,
    val progress: Long, val total: Long, val receiver: FieldSample?,
)

/** Accessed only from the service's ordered archive executor. */
internal class FieldArchive private constructor(
    private val db: SQLiteDatabase, val sessionId: String, private val streamId: String,
) : Closeable {
    private val gpsCoverage = GpsCoverage(SystemClock.elapsedRealtime())
    private var pass = 1L
    private var lastMeasurement: Pair<String, String>? = null
    private val recentFixes = ArrayDeque<Pair<FieldFix, Long>>()

    private fun envelope(kind: String): JSONObject = JSONObject()
        .put("kind", kind).put("id", UUID.randomUUID().toString())
        .put("session_id", sessionId).put("stream_id", streamId)
        .put("time_utc", Instant.now().toString())

    private fun appendEvent(event: JSONObject) {
        db.insertOrThrow("events", null, ContentValues().apply {
            put("id", event.getString("id"))
            put("kind", event.getString("kind"))
            put("json", event.toString())
        })
    }

    fun note(text: String) {
        val event = envelope("note").put("text", text)
        lastMeasurement?.let { (key, id) -> event.put(key, id) }
        appendEvent(event)
    }

    fun gap(reason: String) = appendEvent(envelope("gap").put("reason", reason))

    fun gpsTimeout(): Boolean {
        if (!gpsCoverage.check(SystemClock.elapsedRealtime())) return false
        recentFixes.clear()
        gap("GPS: нет свежих координат 15 с")
        return true
    }

    fun gpsDisabled() {
        if (gpsCoverage.lose(SystemClock.elapsedRealtime())) {
            recentFixes.clear()
            gap("GPS отключён")
        }
    }

    fun gps(location: Location): FieldFix {
        gpsTimeout()
        val elapsed = location.elapsedRealtimeNanos / 1_000_000
        val now = SystemClock.elapsedRealtime()
        val fresh = fixApplies(location.time, System.currentTimeMillis(), elapsed, now) &&
            gpsCoverage.receive(elapsed, now)
        val segment = if (fresh) "$streamId-gps-${gpsCoverage.segment}" else UUID.randomUUID().toString()
        val utc = Instant.ofEpochMilli(location.time).toString()
        val accuracy = if (location.hasAccuracy()) location.accuracy.toDouble() else null
        val event = envelope("gps").put("time_utc", utc)
            .put("latitude", location.latitude).put("longitude", location.longitude)
            .put("accuracy_m", accuracy).put("segment_id", segment).put("fresh", fresh)
        if (location.hasAltitude()) event.put("altitude_m", location.altitude)
        appendEvent(event)
        val fix = FieldFix(location.latitude, location.longitude, accuracy, utc, event.getString("id"), segment, fresh)
        if (fresh) recentFixes.addLast(fix to elapsed)
        while (recentFixes.size > 32) recentFixes.removeFirst()
        return fix
    }

    fun measurement(raw: String, config: FieldConfig): CommittedSample {
        val source = JSONObject(raw)
        val interval = source.getJSONObject("interval")
        val utc = interval.getString("ended_at")
        val utcMs = Instant.parse(utc).toEpochMilli()
        val now = SystemClock.elapsedRealtime()
        val fixId = recentFixes.lastOrNull { (fix, elapsed) ->
            fixApplies(Instant.parse(fix.utc).toEpochMilli(), utcMs, elapsed, now)
        }?.first?.id
        val id = UUID.randomUUID().toString()
        source.put("id", id).put("session_id", sessionId).put("stream_id", streamId)
        val receiver = config.profile == "ardv1"
        val context = source.getJSONObject(if (receiver) "metadata" else "settings")
        if (fixId != null) context.put("field_fix_id", fixId)
        for (key in if (receiver) listOf("raw_squelch_status", "raw_mode_status") else listOf("kind")) {
            if (source.has(key)) context.put(key, source.remove(key))
        }
        val frequencies = if (receiver) listOf(source.getLong("frequency_hz")) else
            source.getJSONArray("frequencies_hz").let { array -> List(array.length()) { array.getLong(it) } }
        val values = if (receiver) listOf(source.getDouble("level")) else
            source.getJSONArray("levels").let { array -> List(array.length()) { array.getDouble(it) } }
        val survey = context.optJSONObject("survey")
        val progress = survey?.getLong("tile") ?: (context.optLong("scan_index", 0) + 1)
        val total = survey?.getLong("tiles") ?: context.optLong("scan_total", 1)
        val measuredPass = survey?.getLong("pass") ?: context.optLong("scan_pass", pass)
        (survey ?: context).put("pass_id", "$streamId-$measuredPass")
        val unit = source.getString("level_unit")
        require(frequencies.isNotEmpty() && frequencies.size == values.size) { "Invalid native sample geometry" }
        db.insertOrThrow(if (receiver) "observations" else "sweeps", null, ContentValues().apply {
            put("id", id)
            put("fix_id", fixId)
            put("json", source.toString())
        })
        if (progress == total) pass = measuredPass + 1
        lastMeasurement = (if (receiver) "observation_id" else "sweep_id") to id
        return CommittedSample(frequencies, values, unit, progress, total,
            if (receiver) FieldSample(frequencies.single(), values.single(), utc, id, fixId) else null)
    }

    fun finish(interruption: String?) {
        db.beginTransaction()
        try {
            if (interruption != null) gap(interruption)
            db.execSQL("UPDATE sessions SET closed_at=? WHERE id=?", arrayOf(Instant.now().toString(), sessionId))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    override fun close() = db.close()

    companion object {
        fun database(root: File, id: String): File {
            require(UUID.fromString(id).toString() == id) { "Invalid session ID" }
            return File(File(root, id), "capture.sqlite")
        }

        fun list(root: File): List<String> = root.listFiles().orEmpty()
            .filter { it.isDirectory && runCatching { database(root, it.name).isFile }.getOrDefault(false) }
            .sortedByDescending { it.lastModified() }.map { it.name }

        fun recover(file: File) {
            SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READWRITE).use { db ->
                db.rawQuery("SELECT id,stream_id FROM sessions WHERE closed_at IS NULL", null).use {
                    if (it.moveToFirst()) FieldArchive(db, it.getString(0), it.getString(1))
                        .finish("Capture interrupted before the service could close it")
                }
            }
        }

        fun create(root: File, id: String, device: FieldDevice, config: FieldConfig): FieldArchive {
            val file = database(root, id)
            check(file.parentFile!!.mkdirs()) { "Cannot create capture directory" }
            val options = SQLiteDatabase.OpenParams.Builder()
                .addOpenFlags(SQLiteDatabase.CREATE_IF_NECESSARY or SQLiteDatabase.ENABLE_WRITE_AHEAD_LOGGING)
                .setSynchronousMode("FULL").build()
            val db = SQLiteDatabase.openDatabase(file, options)
            try {
                db.beginTransaction()
                val streamId = UUID.randomUUID().toString()
                try {
                    db.execSQL("CREATE TABLE sessions(id TEXT PRIMARY KEY, stream_id TEXT NOT NULL, closed_at TEXT, profile TEXT NOT NULL, started_at TEXT NOT NULL, plan_json TEXT NOT NULL, device TEXT NOT NULL)")
                    for (table in listOf("sweeps", "observations")) {
                        db.execSQL("CREATE TABLE $table(seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, fix_id TEXT, json TEXT NOT NULL)")
                        db.execSQL("CREATE INDEX ${table}_fix ON $table(fix_id,seq)")
                    }
                    db.execSQL("CREATE TABLE events(seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, json TEXT NOT NULL)")
                    db.execSQL("CREATE INDEX events_kind ON events(kind,seq)")
                    val plan = config.nativePlan().put("baud", config.checkedBaud())
                        .put("gps", config.gps).put("continuous", config.continuous)
                    db.insertOrThrow("sessions", null, ContentValues().apply {
                        put("id", id)
                        put("stream_id", streamId)
                        put("started_at", Instant.now().toString())
                        put("profile", config.profile)
                        put("device", device.label)
                        put("plan_json", plan.toString())
                    })
                    db.setTransactionSuccessful()
                } finally {
                    db.endTransaction()
                }
                return FieldArchive(db, id, streamId)
            } catch (e: Throwable) {
                db.close()
                throw e
            }
        }
    }
}
