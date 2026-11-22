package org.openradioinspector.field

import android.content.ContentValues
import android.location.Location
import android.provider.MediaStore
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import java.time.Instant
import java.util.UUID
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class FieldExportInstrumentedTest {
    @Test
    fun finalizedCapturePreservesMeasurementsAndLocationLinks() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val root = Files.createTempDirectory(context.cacheDir.toPath(), "export-test-").toFile()
        try {
            val id = UUID.randomUUID().toString()
            val database = FieldArchive.database(root, id)
            val plan = FieldConfig(1, "tinysa", 100_000_001, 100_000_003, 1, gps = true)
            var fixId: String
            FieldArchive.create(root, id, FieldDevice(1, "Synthetic analyzer", listOf("tinysa")), plan).use { capture ->
                val stillOpen = File(root, "open").apply { mkdir() }
                try {
                    FieldExporter.build(database, stillOpen)
                    fail("Unfinalized capture was exported")
                } catch (expected: IllegalArgumentException) {
                    assertTrue(expected.message.orEmpty().contains("Finalize"))
                }
                val fix = Location("synthetic").apply {
                    latitude = -0.25
                    longitude = 0.125
                    accuracy = 0.5f
                    altitude = 42.5
                    time = System.currentTimeMillis()
                    elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos()
                }
                fixId = capture.gps(fix).id
                val utc = Instant.now().toString()
                val source = JSONObject()
                    .put("interval", JSONObject().put("started_at", utc).put("ended_at", utc))
                    .put("frequencies_hz", JSONArray(listOf(100_000_001L, 100_000_003L)))
                    .put("levels", JSONArray(listOf(-100.125, -32.75))).put("level_unit", "dBm")
                    .put("settings", JSONObject().put("simulated", true))
                val sample = capture.measurement(source.toString(), plan)
                capture.note("Synthetic field marker", "sweep", sample.id)
                capture.finish("End of synthetic capture")
            }
            val output = FieldExporter.build(database, File(root, "closed").apply { mkdir() })
            ZipFile(output).use { zip ->
                fun read(name: String) = zip.getInputStream(zip.getEntry(name)).use { it.readBytes() }
                val m = JSONObject(read("manifest.json").toString(Charsets.UTF_8))
                assertEquals(1, m.getInt("format_version"))
                assertEquals(1, m.getInt("sweep_count"))
                assertEquals(0, m.getInt("observation_count"))
                assertEquals(id, m.getString("session_id"))
                assertEquals(plan.startHz, m.getJSONArray("streams").getJSONObject(0)
                    .getJSONObject("settings").getLong("start_hz"))
                for ((name, count) in mapOf("measurements.ndjson" to 1, "events.ndjson" to 3, "track.geojson" to 1)) {
                    assertEquals(ZipEntry.STORED, zip.getEntry(name).method)
                    val bytes = read(name)
                    val receipt = m.getJSONObject("files").getJSONObject(name)
                    val hash = MessageDigest.getInstance("SHA-256").digest(bytes)
                    assertEquals(hash.joinToString("") { "%02x".format(it) }, receipt.getString("sha256"))
                    assertEquals(bytes.size, receipt.getInt("bytes"))
                    assertEquals(count, receipt.getInt("records"))
                }
                val data = read("measurements.ndjson").toString(Charsets.UTF_8).lineSequence().toList()
                assertEquals(id, JSONObject(data[0]).getString("session_id"))
                val stored = JSONObject(data[1]).getJSONObject("measurement")
                assertEquals(fixId, stored.getJSONObject("settings").getString("field_fix_id"))
                assertEquals(-100.125, stored.getJSONArray("levels").getDouble(0), 0.0)
                val events = read("events.ndjson").toString(Charsets.UTF_8).lineSequence().toList()
                assertEquals(stored.getString("id"), JSONObject(events[1]).getString("sweep_id"))
                val point = JSONObject(read("track.geojson").toString(Charsets.UTF_8))
                    .getJSONArray("features").getJSONObject(0)
                assertEquals(fixId, point.getString("id"))
                assertEquals("[0.125,-0.25,42.5]", point.getJSONObject("geometry").getJSONArray("coordinates").toString())
            }
            if (InstrumentationRegistry.getArguments().getString("publishFieldInterop") == "true") {
                // Only the CI emulator exports this synthetic capture to its shared Downloads.
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, "synthetic.orifield")
                    put(MediaStore.MediaColumns.MIME_TYPE, "application/octet-stream")
                    put(MediaStore.MediaColumns.RELATIVE_PATH, "Download/ORIFieldInterop/")
                    put(MediaStore.MediaColumns.IS_PENDING, 1)
                }
                val resolver = context.contentResolver
                val uri = checkNotNull(resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values))
                FieldExporter.export(context, database, uri)
                val published = ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }
                assertEquals(1, resolver.update(uri, published, null, null))
            }
        } finally {
            root.deleteRecursively()
        }
    }
}
