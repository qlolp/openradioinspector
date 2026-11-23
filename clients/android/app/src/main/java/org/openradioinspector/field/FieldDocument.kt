package org.openradioinspector.field

import android.database.sqlite.SQLiteDatabase
import org.json.JSONArray
import org.json.JSONObject
import java.io.Closeable
import java.io.File
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.LinkOption

internal data class DocumentChannel(val streamId: String, val device: String, val profile: String, val receiver: Boolean) {
    val title get() = "$device · ${if (receiver) "S-метр" else "Спектр"} · $streamId"
}

internal class FieldDocument private constructor(private val directory: File, val sessionId: String,
    val channels: List<DocumentChannel>, private val started: String) : Closeable {
    fun reader(channel: DocumentChannel) = SessionReview(File(directory, "review.sqlite"), sessionId,
        SessionInfo(channel.profile, started, channel.streamId, channel.receiver))
    override fun close() { DocumentStaging.release(directory) }

    companion object {
        fun open(cache: File, input: InputStream, checkpoint: () -> Unit = {}): FieldDocument {
            val work = DocumentStaging.create(cache)
            var published = false
            try {
                val copy = File(work, "source.orifield")
                copy.outputStream().buffered().use { output ->
                    val block = ByteArray(32 * 1024)
                    var total = 0L
                    while (true) {
                        checkpoint()
                        val n = input.read(block)
                        if (n < 0) break
                        require(n > 0)
                        total += n
                        require(total <= mobileDocumentLimit) { "Размер файла превышает 128 МиБ" }
                        output.write(block, 0, n)
                    }
                }
                val zip = StoredFieldZip(copy)
                require(zip.size(fieldManifest) <= 256 * 1024) { "Манифест превышает 256 КиБ" }
                val manifest = zip.read(fieldManifest) { FieldDocumentJson(strictUtf8(it.readBytes())).parse() }
                val dbFile = File(work, "review.sqlite")
                val indexed = SQLiteDatabase.openOrCreateDatabase(dbFile, null).use { db ->
                    db.maximumSize = 384L * 1024 * 1024
                    db.beginTransaction()
                    try {
                        val index = DocumentIndex(db, zip, manifest, checkpoint)
                        val channels = index.build()
                        checkpoint()
                        db.setTransactionSuccessful()
                        FieldDocument(work, index.session, channels, manifest.opt("started_at") as? String ?: "")
                    } finally { db.endTransaction() }
                }
                checkpoint()
                published = true
                return indexed
            } finally {
                if (!published) DocumentStaging.release(work)
            }
        }
    }
}

// The Field activity and its capture service share one process. Never reclaim another open review.
private object DocumentStaging {
    private val active = mutableSetOf<File>()
    private val filenames = listOf("source.orifield", "review.sqlite", "review.sqlite-journal", "review.sqlite-wal", "review.sqlite-shm")

    @Synchronized fun create(cache: File): File {
        val area = File(cache, "field-document-review")
        check(area.mkdir() || Files.isDirectory(area.toPath(), LinkOption.NOFOLLOW_LINKS)) { "Не удалось создать временный каталог" }
        for (stage in area.listFiles().orEmpty()) {
            if (stage.name.startsWith("open-") && stage !in active) erase(stage)
        }
        return Files.createTempDirectory(area.toPath(), "open-").toFile().also { active += it }
    }

    @Synchronized fun release(stage: File) {
        if (active.remove(stage)) {
            erase(stage)
            stage.parentFile?.delete() // Only succeeds after the last open document closes.
        }
    }

    private fun erase(stage: File) {
        if (Files.isDirectory(stage.toPath(), LinkOption.NOFOLLOW_LINKS)) {
            for (name in filenames) File(stage, name).delete()
        }
        stage.delete()
    }
}

private class DocumentIndex(private val db: SQLiteDatabase, private val zip: StoredFieldZip,
    private val manifest: JSONObject, private val checkpoint: () -> Unit) {
    val session = manifest.text("session_id")
    private val streams = linkedMapOf<String, DocumentChannel>()
    private val sweeps = mutableSetOf<String>()
    private val observations = mutableSetOf<String>()

    fun build(): List<DocumentChannel> {
        require(manifest.text("format") == "openradioinspector-field" && manifest.count("format_version") == 1L) {
            "Неподдерживаемый формат выезда"
        }
        val descriptors = manifest.getJSONArray("streams")
        val ids = manifest.getJSONArray("stream_ids")
        require(ids.length() in 1..256 && descriptors.length() == ids.length())
        for (i in 0 until descriptors.length()) {
            val descriptor = descriptors.getJSONObject(i)
            descriptor.getJSONObject("settings")
            val id = descriptor.text("id")
            val channel = DocumentChannel(id, descriptor.text("device"), descriptor.text("profile"), false)
            require(streams.put(id, channel) == null) { "Повтор потока" }
        }
        require((0 until ids.length()).map { ids.get(it) }.toSet() == streams.keys)
        val files = manifest.getJSONObject("files")
        require(files.keys().asSequence().toSet() == fieldPayloads.toSet())
        for (table in listOf("sweeps", "observations")) {
            db.execSQL("CREATE TABLE $table(seq INTEGER PRIMARY KEY,stream_id TEXT NOT NULL,id TEXT NOT NULL,fix_id TEXT,json TEXT NOT NULL,UNIQUE(stream_id,id))")
            db.execSQL("CREATE INDEX ${table}_fix ON $table(stream_id,fix_id,seq)")
        }
        db.execSQL("CREATE TABLE events(seq INTEGER PRIMARY KEY,stream_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,json TEXT NOT NULL,UNIQUE(stream_id,kind,id))")
        db.execSQL("CREATE INDEX events_kind ON events(stream_id,kind,seq)")
        db.execSQL("CREATE INDEX events_track ON events(kind,seq)")
        db.execSQL("CREATE TABLE refs(stream_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL)")
        for (name in fieldPayloads) {
            checkpoint()
            val receipt = files.getJSONObject(name)
            val digest = receipt.text("sha256")
            require(digest.matches(Regex("[0-9a-f]{64}")) && receipt.count("bytes") == zip.size(name))
            val count = zip.read(name, digest) {
                val lines = FieldLines(it)
                when (name) {
                    "measurements.ndjson" -> measurements(lines)
                    "events.ndjson" -> events(lines)
                    else -> track(lines)
                }
            }
            require(count == receipt.count("records")) { "Неверное число записей $name" }
        }
        for ((kind, table) in listOf("gps" to "events", "sweep" to "sweeps", "observation" to "observations")) {
            checkpoint()
            val gps = if (kind == "gps") " AND t.kind='gps'" else ""
            val query = "SELECT 1 FROM refs r WHERE r.kind=? AND NOT EXISTS (SELECT 1 FROM $table t WHERE t.stream_id=r.stream_id AND t.id=r.id$gps) LIMIT 1"
            db.rawQuery(query, arrayOf(kind)).use { require(!it.moveToFirst()) { "Отсутствует исходная запись в потоке" } }
        }
        db.execSQL("DROP TABLE refs")
        return streams.values.flatMap { channel ->
            val hasSweeps = channel.streamId in sweeps
            val hasObservations = channel.streamId in observations
            when {
                hasSweeps && hasObservations -> listOf(channel, channel.copy(receiver = true))
                hasObservations -> listOf(channel.copy(receiver = true))
                hasSweeps -> listOf(channel)
                else -> listOf(channel.copy(receiver = channel.profile == "ardv1"))
            }
        }
    }

    private fun stream(record: JSONObject): String {
        require(record.text("session_id") == session)
        return record.text("stream_id").also { require(it in streams) }
    }

    private fun ref(stream: String, kind: String, data: JSONObject, key: String): String? {
        if (data.isNull(key)) return null
        val id = data.text(key)
        db.execSQL("INSERT INTO refs VALUES(?,?,?)", arrayOf(stream, kind, id))
        return id
    }

    private fun measurements(lines: FieldLines): Long {
        val header = FieldDocumentJson(requireNotNull(lines.next())).parse()
        require(header.text("format") == "openradioinspector-archive-ndjson" && header.count("format_version") == 1L &&
            header.text("session_id") == session)
        var sweepCount = 0L
        var observationCount = 0L
        while (true) {
            checkpoint()
            val raw = lines.next() ?: break
            val entry = FieldDocumentJson(raw).parse()
            val kind = entry.text("kind")
            val record = entry.getJSONObject("measurement")
            val stream = stream(record)
            val id = record.text("id")
            record.text("level_unit")
            val interval = record.getJSONObject("interval")
            require(fieldTimestamp(interval.text("started_at")) <= fieldTimestamp(interval.text("ended_at"))) { "Интервал измерения обращён" }
            val table: String
            val linkKey: String
            when (kind) {
                "sweep" -> {
                    val frequencies = record.getJSONArray("frequencies_hz")
                    val levels = record.getJSONArray("levels")
                    require(frequencies.length() > 0 && levels.length() == frequencies.length()) { "Неверная сетка спектра" }
                    var last = 0L
                    for (i in 0 until frequencies.length()) {
                        val hz = frequencies.get(i)
                        require(hz is Long && hz >= last && levels.get(i) is Number) { "Недопустимый отсчёт спектра" }
                        last = hz
                    }
                    table = "sweeps"
                    linkKey = "settings"
                    sweeps += stream
                    sweepCount++
                }
                "observation" -> {
                    require(record.count("frequency_hz") > 0 && (record.isNull("level") || record.get("level") is Number))
                    record.text("mode")
                    table = "observations"
                    linkKey = "metadata"
                    observations += stream
                    observationCount++
                }
                else -> throw IllegalArgumentException("Неизвестный тип измерения")
            }
            val fix = if (record.has(linkKey)) ref(stream, "gps", record.getJSONObject(linkKey), "field_fix_id") else null
            // Keep the complete source envelope, including signed zero, settings and unknown fields.
            db.execSQL("INSERT INTO $table(stream_id,id,fix_id,json) VALUES(?,?,?,?)", arrayOf(stream, id, fix, raw))
        }
        require(sweepCount == manifest.count("sweep_count") && observationCount == manifest.count("observation_count"))
        return sweepCount + observationCount
    }

    private fun events(lines: FieldLines): Long {
        var count = 0L
        while (true) {
            checkpoint()
            val raw = lines.next() ?: break
            val event = FieldDocumentJson(raw).parse()
            val stream = stream(event)
            val id = event.text("id")
            val kind = event.text("kind")
            fieldTimestamp(event.text("time_utc"))
            require(event.isNull("sweep_id") || event.isNull("observation_id"))
            ref(stream, "sweep", event, "sweep_id")
            ref(stream, "observation", event, "observation_id")
            when (kind) {
                "gps" -> {
                    fun numeric(key: String) = (event.get(key) as Number).toDouble()
                    require(numeric("latitude") in -90.0..90.0 && numeric("longitude") in -180.0..180.0)
                    if (!event.isNull("accuracy_m")) require(numeric("accuracy_m") >= 0)
                    if (!event.isNull("altitude_m")) numeric("altitude_m")
                }
                "note" -> event.text("text")
                "gap" -> event.text("reason")
                else -> throw IllegalArgumentException("Неизвестное событие")
            }
            db.execSQL("INSERT INTO events(stream_id,kind,id,json) VALUES(?,?,?,?)", arrayOf(stream, kind, id, raw))
            count++
        }
        return count
    }

    private fun track(lines: FieldLines): Long {
        require(lines.next() == "{\"type\":\"FeatureCollection\",\"features\":[")
        var count = 0L
        db.rawQuery("SELECT json FROM events WHERE kind='gps' ORDER BY seq", null).use { fixes ->
            while (fixes.moveToNext()) {
                checkpoint()
                val line = requireNotNull(lines.next())
                if (count > 0) require(line.startsWith(','))
                val actual = FieldDocumentJson(if (count == 0L) line else line.substring(1)).parse()
                val gps = FieldDocumentJson(fixes.getString(0)).parse()
                val expected = FieldExporter.feature(gps)
                val coordinates = JSONArray().put(gps.get("longitude")).put(gps.get("latitude"))
                if (!gps.isNull("altitude_m")) coordinates.put(gps.get("altitude_m"))
                expected.getJSONObject("geometry").put("coordinates", coordinates)
                require(sameFieldJson(expected, actual)) { "Трек не совпадает с записанными координатами GPS" }
                count++
            }
        }
        require(lines.next() == "]}" && lines.next() == null)
        return count
    }
}
