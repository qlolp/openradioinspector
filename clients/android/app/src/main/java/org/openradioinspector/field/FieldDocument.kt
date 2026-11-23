package org.openradioinspector.field

import android.database.sqlite.SQLiteDatabase
import org.json.JSONObject
import java.io.Closeable
import java.io.File
import java.io.InputStream
import java.io.RandomAccessFile
import java.security.MessageDigest
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
                        require(total <= FieldExporter.maxArchive) { "Размер файла превышает предел ZIP V1" }
                        output.write(block, 0, n)
                    }
                }
                val zip = StoredFieldZip(copy)
                require(zip.size(fieldManifest) <= 256 * 1024) { "Манифест превышает 256 КиБ" }
                val manifest = zip.read(fieldManifest) { FieldRowObject.root(FieldBytes.Memory(it.readBytes())) }
                val dbFile = File(work, "review.sqlite")
                val indexed = SQLiteDatabase.openOrCreateDatabase(dbFile, null).use { db ->
                    db.beginTransaction()
                    try {
                        val index = DocumentIndex(db, zip, manifest, checkpoint)
                        val channels = RandomAccessFile(copy, "r").use(index::build)
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

    fun build(source: RandomAccessFile): List<DocumentChannel> {
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
            db.execSQL("CREATE TABLE $table(seq INTEGER PRIMARY KEY,stream_id TEXT NOT NULL,id TEXT NOT NULL,fix_id TEXT,source_offset INTEGER NOT NULL,source_length INTEGER NOT NULL,source_hash BLOB NOT NULL,UNIQUE(stream_id,id))")
            db.execSQL("CREATE INDEX ${table}_fix ON $table(stream_id,fix_id,seq)")
            db.execSQL("CREATE INDEX ${table}_page ON $table(stream_id,seq)")
        }
        db.execSQL("CREATE TABLE events(seq INTEGER PRIMARY KEY,stream_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,source_offset INTEGER NOT NULL,source_length INTEGER NOT NULL,source_hash BLOB NOT NULL,feature_hash BLOB,UNIQUE(stream_id,kind,id))")
        db.execSQL("CREATE INDEX events_kind ON events(stream_id,kind,seq)")
        db.execSQL("CREATE INDEX events_track ON events(kind,seq)")
        db.execSQL("CREATE TABLE refs(stream_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL)")
        for (name in fieldPayloads) {
            checkpoint()
            val receipt = files.getJSONObject(name)
            val digest = receipt.text("sha256")
            require(digest.matches(Regex("[0-9a-f]{64}")) && receipt.count("bytes") == zip.size(name))
            val count = zip.read(name, digest) {
                val lines = FieldLines(it, zip.dataStart(name), checkpoint)
                when (name) {
                    "measurements.ndjson" -> measurements(lines, source)
                    "events.ndjson" -> events(lines, source)
                    else -> track(lines, source)
                }
            }
            require(count == receipt.count("records")) { "Неверное число записей $name" }
        }
        val lastRef = db.rawQuery("SELECT COALESCE(MAX(rowid),0) FROM refs", null).use { it.moveToFirst(); it.getLong(0) }
        for ((kind, table) in listOf("gps" to "events", "sweep" to "sweeps", "observation" to "observations")) {
            val gps = if (kind == "gps") " AND t.kind='gps'" else ""
            val query = "SELECT 1 FROM refs r WHERE r.rowid BETWEEN ? AND ? AND r.kind=? AND NOT EXISTS " +
                "(SELECT 1 FROM $table t WHERE t.stream_id=r.stream_id AND t.id=r.id$gps) LIMIT 1"
            for (first in 1L..lastRef step 4096L) {
                checkpoint()
                db.rawQuery(query, arrayOf(first.toString(), (first + 4095).toString(), kind)).use {
                    require(!it.moveToFirst()) { "Отсутствует исходная запись в потоке" }
                }
            }
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

    private fun ref(stream: String, kind: String, data: FieldRowObject, key: String): String? {
        if (data.isNull(key)) return null
        val id = data.indexKey(key)
        db.execSQL("INSERT INTO refs VALUES(?,?,?)", arrayOf(stream, kind, id))
        return id
    }

    private fun contents(source: RandomAccessFile, row: FieldRow) = FieldBytes.Window(source, row, checkpoint)

    private fun measurements(lines: FieldLines, source: RandomAccessFile): Long {
        val header = FieldRowObject.root(contents(source, requireNotNull(lines.next())))
        require(header.text("format") == "openradioinspector-archive-ndjson" && header.count("format_version") == 1L &&
            header.text("session_id") == session)
        var sweepCount = 0L
        var observationCount = 0L
        while (true) {
            checkpoint()
            val row = lines.next() ?: break
            val entry = FieldRowObject.root(contents(source, row))
            val kind = entry.text("kind")
            val record = entry.getJSONObject("measurement") as FieldRowObject
            val stream = stream(record)
            val id = record.indexKey("id")
            require(record.validText("level_unit"))
            val interval = record.getJSONObject("interval") as FieldRowObject
            require(interval.timestamp("started_at") <= interval.timestamp("ended_at")) { "Интервал измерения обращён" }
            val table: String
            val linkKey: String
            when (kind) {
                "sweep" -> {
                    val points = record.numericArray("frequencies_hz", frequencies = true)
                    require(points > 0 && record.numericArray("levels", frequencies = false) == points) { "Неверная сетка спектра" }
                    table = "sweeps"
                    linkKey = "settings"
                    sweeps += stream
                    sweepCount++
                }
                "observation" -> {
                    require(record.count("frequency_hz") > 0 && (record.isNull("level") || record.get("level") is Number))
                    require(record.validText("mode"))
                    table = "observations"
                    linkKey = "metadata"
                    observations += stream
                    observationCount++
                }
                else -> throw IllegalArgumentException("Неизвестный тип измерения")
            }
            val fix = if (record.has(linkKey)) ref(stream, "gps", record.getJSONObject(linkKey) as FieldRowObject, "field_fix_id") else null
            db.execSQL("INSERT INTO $table(stream_id,id,fix_id,source_offset,source_length,source_hash) VALUES(?,?,?,?,?,?)",
                arrayOf(stream, id, fix, row.position, row.length, row.hash))
        }
        require(sweepCount == manifest.count("sweep_count") && observationCount == manifest.count("observation_count"))
        return sweepCount + observationCount
    }

    private fun events(lines: FieldLines, source: RandomAccessFile): Long {
        var count = 0L
        while (true) {
            checkpoint()
            val row = lines.next() ?: break
            val event = FieldRowObject.root(contents(source, row), semantic = true)
            val stream = stream(event)
            val id = event.indexKey("id")
            val kind = event.text("kind")
            event.timestamp("time_utc")
            require(event.isNull("sweep_id") || event.isNull("observation_id"))
            ref(stream, "sweep", event, "sweep_id")
            ref(stream, "observation", event, "observation_id")
            val feature = when (kind) {
                "gps" -> {
                    fun numeric(key: String) = (event.get(key) as Number).toDouble()
                    require(numeric("latitude") in -90.0..90.0 && numeric("longitude") in -180.0..180.0)
                    if (!event.isNull("accuracy_m")) require(numeric("accuracy_m") >= 0)
                    if (!event.isNull("altitude_m")) numeric("altitude_m")
                    event.gpsFeatureHash()
                }
                "note", "gap" -> {
                    require(event.validText(if (kind == "note") "text" else "reason"))
                    null
                }
                else -> throw IllegalArgumentException("Неизвестное событие")
            }
            db.execSQL("INSERT INTO events(stream_id,kind,id,source_offset,source_length,source_hash,feature_hash) VALUES(?,?,?,?,?,?,?)",
                arrayOf(stream, kind, id, row.position, row.length, row.hash, feature))
            count++
        }
        return count
    }

    private fun track(lines: FieldLines, source: RandomAccessFile): Long {
        require(contents(source, requireNotNull(lines.next())).matches("{\"type\":\"FeatureCollection\",\"features\":["))
        var count = 0L
        db.rawQuery("SELECT feature_hash FROM events WHERE kind='gps' ORDER BY seq", null).use { fixes ->
            while (fixes.moveToNext()) {
                checkpoint()
                val row = requireNotNull(lines.next())
                val feature = FieldRowObject.root(contents(source, row), semantic = true, comma = count > 0)
                require(MessageDigest.isEqual(fixes.getBlob(0), feature.canonicalHash())) {
                    "Трек не совпадает с записанными координатами GPS"
                }
                count++
            }
        }
        require(contents(source, requireNotNull(lines.next())).matches("]}") && lines.next() == null)
        return count
    }
}
