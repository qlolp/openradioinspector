package org.openradioinspector.field

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.net.Uri
import java.io.BufferedOutputStream
import java.io.File
import java.io.IOException
import java.io.InterruptedIOException
import java.nio.file.Files
import java.security.MessageDigest
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import org.json.JSONArray
import org.json.JSONObject

/** Call on a worker thread with a fresh ACTION_CREATE_DOCUMENT URI. */
object FieldExporter {
    private const val maxArchive = 0xffff_fffeL
    private const val maxLine = 16 * 1024 * 1024 + 1024
    private const val trackStart = "{\"type\":\"FeatureCollection\",\"features\":["

    fun export(context: Context, database: File, destination: Uri) {
        val work = Files.createTempDirectory(context.cacheDir.toPath(), "orifield-").toFile()
        try {
            val archive = build(database, work)
            val target = context.contentResolver.openOutputStream(destination, "wt")
                ?: throw IOException("Document provider could not open the destination")
            target.use { output -> archive.inputStream().buffered().use { it.copyTo(output) } }
        } catch (failure: Exception) {
            // The picker creates a new document. Some providers cannot remove a failed copy.
            runCatching { context.contentResolver.delete(destination, null, null) }
            throw failure
        } finally {
            work.deleteRecursively()
        }
    }

    private class Payload(val file: File) {
        val crc = CRC32()
        private val digest = MessageDigest.getInstance("SHA-256")
        var bytes = 0L
            private set
        var records = 0L
            private set

        fun line(output: BufferedOutputStream, text: String, record: Boolean = false) {
            if (Thread.currentThread().isInterrupted) throw InterruptedIOException()
            val data = (text + "\n").toByteArray(Charsets.UTF_8)
            require(data.size <= maxLine) { "Field record exceeds the archive line limit" }
            bytes += data.size
            require(bytes < maxArchive) { "Payload exceeds the V1 archive limit" }
            output.write(data)
            digest.update(data)
            crc.update(data)
            if (record) records++
        }

        fun receipt() = JSONObject()
            .put("bytes", bytes)
            .put("records", records)
            .put("sha256", digest.digest().joinToString("") { "%02x".format(it) })
    }

    /** Build in a private, empty staging directory. The caller owns cleanup. */
    internal fun build(database: File, work: File): File {
        val measurements = Payload(File(work, "measurements.ndjson"))
        val events = Payload(File(work, "events.ndjson"))
        val track = Payload(File(work, "track.geojson"))
        val manifest = Payload(File(work, "manifest.json"))
        val members = listOf(measurements, events, track)
        SQLiteDatabase.openDatabase(database.path, null, SQLiteDatabase.OPEN_READONLY).use { db ->
            val header = db.rawQuery(
                "SELECT id,stream_id,closed_at,profile,started_at,plan_json,device FROM sessions",
                null
            ).use { cursor ->
                require(cursor.moveToFirst() && !cursor.isNull(2)) { "Finalize the session before export" }
                val stream = JSONObject()
                    .put("id", cursor.getString(1))
                    .put("device", cursor.getString(6))
                    .put("profile", cursor.getString(3))
                    .put("settings", JSONObject(cursor.getString(5)))
                val info = JSONObject().put("format", "openradioinspector-field")
                    .put("format_version", 1).put("session_id", cursor.getString(0))
                    .put("stream_ids", JSONArray().put(cursor.getString(1)))
                    .put("streams", JSONArray().put(stream))
                    .put("started_at", cursor.getString(4)).put("closed_at", cursor.getString(2))
                require(!cursor.moveToNext()) { "Expected one field session" }
                info
            }
            measurements.file.outputStream().buffered().use { output ->
                measurements.line(output, JSONObject().put("format", "openradioinspector-archive-ndjson")
                    .put("format_version", 1).put("session_id", header.getString("session_id")).toString())
                for ((table, kind) in listOf("sweeps" to "sweep", "observations" to "observation")) {
                    val previous = measurements.records
                    db.rawQuery("SELECT json FROM $table ORDER BY seq", null).use { rows ->
                        while (rows.moveToNext()) {
                            val raw = rows.getString(0)
                            require('\n' !in raw && '\r' !in raw) { "Expected compact measurement JSON" }
                            measurements.line(output, "{\"kind\":\"$kind\",\"measurement\":$raw}", true)
                        }
                    }
                    header.put("${kind}_count", measurements.records - previous)
                }
            }
            events.file.outputStream().buffered().use { eventOut ->
                track.file.outputStream().buffered().use { trackOut ->
                    track.line(trackOut, trackStart)
                    db.rawQuery("SELECT json FROM events ORDER BY seq", null).use { rows ->
                        while (rows.moveToNext()) {
                            val raw = rows.getString(0)
                            require('\n' !in raw && '\r' !in raw) { "Expected compact event JSON" }
                            events.line(eventOut, raw, true)
                            val event = JSONObject(raw)
                            if (event.getString("kind") == "gps") {
                                val prefix = if (track.records == 0L) "" else ","
                                track.line(trackOut, prefix + feature(event), true)
                            }
                        }
                    }
                    track.line(trackOut, "]}")
                }
            }
            val files = JSONObject()
            members.forEach { files.put(it.file.name, it.receipt()) }
            manifest.file.outputStream().buffered().use { manifest.line(it, header.put("files", files).toString()) }
            require(manifest.bytes <= 65536) { "Manifest exceeds the V1 size limit" }
        }

        val archive = File(work, "session.orifield")
        val entries = members + manifest
        // Four ASCII names, fixed local/central headers and EOCD fit in the 1 KiB reserve.
        require(entries.sumOf { it.bytes } <= maxArchive - 1024) { "Session exceeds the V1 size limit" }
        ZipOutputStream(archive.outputStream().buffered()).use { zip ->
            for (member in entries) {
                zip.putNextEntry(ZipEntry(member.file.name).apply {
                    method = ZipEntry.STORED
                    size = member.bytes
                    compressedSize = member.bytes
                    crc = member.crc.value
                })
                member.file.inputStream().buffered().use { it.copyTo(zip) }
                zip.closeEntry()
                check(member.file.delete()) { "Could not release staging space" }
            }
        }
        require(archive.length() <= maxArchive) { "Session exceeds the V1 size limit" }
        return archive
    }

    private fun feature(event: JSONObject): JSONObject {
        val coordinates = JSONArray().put(event.getDouble("longitude")).put(event.getDouble("latitude"))
        if (!event.isNull("altitude_m")) coordinates.put(event.getDouble("altitude_m"))
        val properties = JSONObject()
        val geometryFields = setOf("kind", "id", "latitude", "longitude", "altitude_m")
        event.keys().forEach { if (it !in geometryFields) properties.put(it, event.get(it)) }
        return JSONObject().put("type", "Feature").put("id", event.getString("id"))
            .put("geometry", JSONObject().put("type", "Point").put("coordinates", coordinates))
            .put("properties", properties)
    }
}
