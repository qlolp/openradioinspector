package org.openradioinspector.field

import android.database.sqlite.SQLiteDatabase
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.RandomAccessFile
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.CancellationException
import java.util.UUID
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

@RunWith(AndroidJUnit4::class)
class FieldDocumentTest {
    private fun fixture(name: String = "field-archive-v1.orifield") =
        checkNotNull(javaClass.getResourceAsStream("/$name")).use { it.readBytes() }

    private fun inCache(test: (File) -> Unit) {
        val cache = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        val root = Files.createTempDirectory(cache.toPath(), "field-document-test").toFile()
        try { test(root) } finally { root.deleteRecursively() }
    }

    @Test fun canonicalMultistreamDocumentOpensTheSameRouteAndExactOriginals() = inCache { root ->
        val original = fixture()
        FieldDocument.open(root, original.inputStream()).use { doc ->
            assertEquals("fixture-сессия", doc.sessionId)
            val spectrum = doc.channels.single { it.streamId == "spectrum/one" }
            val receiver = doc.channels.single { it.streamId == "receiver+one" }
            val reader = doc.reader(spectrum)
            val page = reader.page(spectrum.profile, ReviewKind.TRACK, PageCursor(), null, 100_000_002L)
            assertEquals(listOf("fix-1"), page.items.map { it.json.getString("id") })
            val mark = page.routeLevels.getValue("fix-1") as RouteReading.Measured
            assertEquals("fixture-sweep-α", mark.sourceId)
            assertEquals(100_000_002L, mark.frequencyHz)
            assertEquals((-0.0).toRawBits(), mark.level.toRawBits())
            val raw = reader.source(spectrum.profile, mark.sourceId)!!
            assertEquals(-1.0000000000000002, raw.getJSONArray("levels").getDouble(2), 0.0)
            val ar = doc.reader(receiver)
            assertTrue(ar.info().receiver)
            assertTrue(ar.page(receiver.profile, ReviewKind.TRACK, PageCursor(), null, 100_000_003L).items.isEmpty())
            assertEquals(137.0, ar.source(receiver.profile, "fixture-observation-α")!!.getDouble("level"), 0.0)
            assertNull(ar.source(receiver.profile, mark.sourceId))
        }
        assertTrue(root.listFiles()!!.isEmpty())
        assertArrayEquals(original, fixture())
    }

    @Test fun mixedStreamKeepsOverlappingMeasurementIdsScopedByKind() = inCache { root ->
        val bytes = repack {
            for (name in listOf("measurements.ndjson", "events.ndjson")) {
                it.replaceText(name, "receiver+one", "spectrum/one")
                it.replaceText(name, "fixture-observation-α", "fixture-sweep-α")
            }
            it.replaceText("measurements.ndjson", "\"metadata\":{", "\"metadata\":{\"field_fix_id\":\"fix-1\",")
        }
        FieldDocument.open(root, bytes.inputStream()).use { doc ->
            val selected = doc.channels.filter { it.streamId == "spectrum/one" }
            assertEquals(2, selected.size)
            for (channel in selected) {
                val reader = doc.reader(channel)
                val page = reader.page(channel.profile, ReviewKind.TRACK, PageCursor(), null, 100_000_003L)
                val point = page.routeLevels.getValue("fix-1") as RouteReading.Measured
                assertEquals("fixture-sweep-α", point.sourceId)
                assertEquals(if (channel.receiver) 137.0 else -1.0000000000000002, point.level, 0.0)
                assertEquals(137.0, reader.source(channel.profile, point.sourceId, observation = true)!!.getDouble("level"), 0.0)
                assertEquals(4, reader.source(channel.profile, point.sourceId, observation = false)!!.getJSONArray("levels").length())
            }
        }
    }

    @Test fun originalAndGpsFieldsLargerThanSqliteCursorWindowKeepTheirContentsAndLinks() = inCache { root ->
        val text = "x".repeat(2 * 1024 * 1024 + 31) + " край"
        val bytes = repack { members ->
            members.replaceText("measurements.ndjson", "\"settings\":{", "\"settings\":{\"instrument_log\":\"$text\",")
            for (name in listOf("events.ndjson", "track.geojson")) {
                members.replaceText(name, "\"accuracy_m\":5.5", "\"accuracy_m\":5.5,\"provider_log\":\"$text\"")
            }
        }
        FieldDocument.open(root, bytes.inputStream()).use { doc ->
            val channel = doc.channels.single { !it.receiver }
            val reader = doc.reader(channel)
            val route = reader.page(channel.profile, ReviewKind.TRACK, PageCursor(), null, 100_000_002L)
            assertEquals(text, route.items.single().json.getString("provider_log"))
            val link = route.routeLevels.getValue("fix-1") as RouteReading.Measured
            val original = reader.source(channel.profile, link.sourceId)!!
            assertEquals(text, original.getJSONObject("settings").getString("instrument_log"))
            assertEquals((-0.0).toRawBits(), original.getJSONArray("levels").getDouble(1).toRawBits())
        }
        assertTrue(root.listFiles()!!.isEmpty())
    }

    @Test fun recordAboveTheV1LimitIsRejectedBeforeDecoding() {
        val input = ByteArray(FieldExporter.maxLine) { 32 }.inputStream()
        val error = assertThrows(IllegalArgumentException::class.java) { FieldLines(input, 0L) {}.next() }
        assertTrue(error.message.orEmpty().contains("V1"))
    }

    @Test fun verifiedOffsetsDetectChangedSourceBeforeShowingTheOriginal() = inCache { root ->
        val bytes = fixture()
        FieldDocument.open(root, bytes.inputStream()).use { doc ->
            val copy = File(root, "field-document-review").listFiles()!!.single().resolve("source.orifield")
            val offset = StoredFieldZip(copy).dataStart("measurements.ndjson")
            RandomAccessFile(copy, "rw").use { file ->
                file.seek(offset)
                file.readLine() // the measurement header
                val position = file.filePointer + 2
                file.seek(position)
                file.write(32)
            }
            val channel = doc.channels.single { !it.receiver }
            assertThrows(IllegalArgumentException::class.java) { doc.reader(channel).source(channel.profile, "fixture-sweep-α") }
        }
        assertArrayEquals(fixture(), bytes)
        assertTrue(root.listFiles()!!.isEmpty())
    }

    @Test fun exportedTrackOver128MiBReopensAndResolvesTheLastOriginal() = inCache { root ->
        val captureRoot = File(root, "captures")
        val id = UUID.randomUUID().toString()
        val config = FieldConfig(1, "tinysa", 100_000_000, 101_000_000, 10_000)
        FieldArchive.create(captureRoot, id, FieldDevice(1, "synthetic", listOf("tinysa")), config).use { it.finish(null) }
        val database = FieldArchive.database(captureRoot, id)
        val fixes = 1030
        val detail = "x".repeat(64 * 1024)
        val utc = "2026-11-11T12:00:00Z"
        SQLiteDatabase.openDatabase(database.path, null, SQLiteDatabase.OPEN_READWRITE).use { db ->
            val stream = db.rawQuery("SELECT stream_id FROM sessions", null).use { it.moveToFirst(); it.getString(0) }
            db.beginTransaction()
            try {
                val gps = JSONObject().put("kind", "gps").put("session_id", id).put("stream_id", stream)
                    .put("time_utc", utc).put("latitude", 1.5).put("longitude", 2.5).put("provider_log", detail)
                repeat(fixes) { n ->
                    val fix = "point-$n"
                    db.execSQL("INSERT INTO events(id,kind,json) VALUES(?,'gps',?)", arrayOf(fix, gps.put("id", fix).toString()))
                }
                val fix = "point-${fixes - 1}"
                val sweep = """{"id":"last","session_id":"$id","stream_id":"$stream","interval":{"started_at":"$utc","ended_at":"$utc"},"frequencies_hz":[100000000],"levels":[-73.125],"level_unit":"dBm","settings":{"field_fix_id":"$fix"}}"""
                db.execSQL("INSERT INTO sweeps(id,fix_id,json) VALUES('last',?,?)", arrayOf(fix, sweep))
                db.setTransactionSuccessful()
            } finally { db.endTransaction() }
        }
        val exported = FieldExporter.build(database, File(root, "export").apply { mkdir() })
        assertTrue(exported.length() > 128L * 1024 * 1024)
        exported.inputStream().use { input -> FieldDocument.open(root, input).use { doc ->
            val channel = doc.channels.single()
            val reader = doc.reader(channel)
            val page = reader.page(channel.profile, ReviewKind.TRACK, PageCursor(fixes - 1L), null, 100_000_000L)
            val last = page.items.single().json
            assertEquals("point-${fixes - 1}", last.getString("id"))
            assertEquals(detail, last.getString("provider_log"))
            val reading = page.routeLevels.getValue(last.getString("id")) as RouteReading.Measured
            assertEquals(-73.125, reading.level, 0.0)
            assertEquals("last", reader.source(channel.profile, reading.sourceId)!!.getString("id"))
            val first = reader.page(channel.profile, ReviewKind.TRACK, PageCursor(), null)
            assertTrue(first.newer)
            assertTrue(first.items.size in 1 until 128)
            val stage = File(root, "field-document-review").listFiles()!!.single()
            assertTrue(File(stage, "review.sqlite").length() < exported.length() / 10)
        } }
        assertTrue(exported.isFile)
        assertFalse(File(root, "field-document-review").exists())
    }

    @Test fun actualAndroidExportCanOpenTwiceWithoutReplacingAnExistingSession() = inCache { root ->
        val capture = File(root, "saved-capture").apply { writeText("original") }
        val bytes = fixture("field-android-export-v1.orifield")
        FieldDocument.open(root, bytes.inputStream()).use { first ->
            FieldDocument.open(root, bytes.inputStream()).use { second ->
                for (document in listOf(first, second)) {
                    val channel = document.channels.single()
                    val reader = document.reader(channel)
                    val gps = reader.page(channel.profile, ReviewKind.TRACK, PageCursor(), null).items.single().json
                    val data = reader.page(channel.profile, ReviewKind.DATA, PageCursor(), gps.getString("id")).items.single().json
                    val hz = data.getJSONArray("frequencies_hz").getLong(0)
                    val levels = reader.page(channel.profile, ReviewKind.TRACK, PageCursor(), null, hz).routeLevels
                    assertEquals(data.getString("id"), levels.getValue(gps.getString("id")).sourceId)
                }
            }
        }
        assertEquals(listOf(capture), root.listFiles()!!.toList())
        assertEquals("original", capture.readText())
    }

    @Test fun invalidContainerReceiptsRecordsAndTrackLeaveNoVisibleDocument() = inCache { root ->
        val bad = listOf(
            fixture().dropLast(12).toByteArray(),
            fixture().also { it[30] = 'x'.code.toByte() }, // local filename disagrees with the central directory

            repack { it["other"] = byteArrayOf(1) },
            repack(updateHashes = false) { it.replaceText("events.ndjson", "Synthetic marker", "Changed marker") },
            repack { it.replaceText("track.geojson", "[0.125,-0.25]", "[-0.25,0.125]") },
            repack { it.replaceText("measurements.ndjson", "\"field_fix_id\":\"fix-1\"", "\"field_fix_id\":\"unknown\"") },
            repack { it.replaceText("events.ndjson", "\"id\":\"note-1\"", "\"id\":\"note-1\",\"id\":\"other\"") },
            repack { it.replaceText("manifest.json", "\"sweep_count\":1", "\"sweep_count\":2") },
        )
        bad.forEachIndexed { i, bytes ->
            assertTrue("bad fixture $i", runCatching { FieldDocument.open(root, bytes.inputStream()).close() }.isFailure)
            assertTrue("staging $i", root.listFiles()!!.isEmpty())
        }
    }

    @Test fun reopeningAfterProcessLossReclaimsOnlyAbandonedPrivateDocumentStaging() = inCache { root ->
        val abandoned = File(root, "field-document-review/open-12345").apply { mkdirs() }
        File(abandoned, "source.orifield").writeText("interrupted copy")
        val unrelated = File(root, "saved-capture").apply { writeText("original") }
        FieldDocument.open(root, fixture().inputStream()).use {
            assertFalse(abandoned.exists())
            assertTrue(unrelated.exists())
        }
        assertEquals(listOf(unrelated), root.listFiles()!!.toList())
    }

    @Test fun cancelledImportReleasesItsPrivateStagingDirectory() = inCache { root ->
        var checks = 0
        assertThrows(CancellationException::class.java) {
            FieldDocument.open(root, fixture().inputStream()) {
                if (++checks == 3) throw CancellationException("stopped")
            }
        }
        assertTrue(root.listFiles()!!.isEmpty())
    }

    private fun MutableMap<String, ByteArray>.replaceText(name: String, before: String, after: String) {
        val text = getValue(name).toString(Charsets.UTF_8)
        check(before in text)
        put(name, text.replace(before, after).toByteArray())
    }

    private fun repack(updateHashes: Boolean = true, change: (MutableMap<String, ByteArray>) -> Unit): ByteArray {
        val members = linkedMapOf<String, ByteArray>()
        ZipInputStream(fixture().inputStream()).use { zip ->
            while (true) {
                val entry = zip.nextEntry ?: break
                members[entry.name] = zip.readBytes()
            }
        }
        change(members)
        if (updateHashes) {
            val manifest = JSONObject(members.getValue("manifest.json").toString(Charsets.UTF_8))
            val files = manifest.getJSONObject("files")
            for (name in files.keys()) {
                val bytes = members.getValue(name)
                files.getJSONObject(name).put("bytes", bytes.size).put("sha256",
                    MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) })
            }
            members["manifest.json"] = manifest.toString().toByteArray()
        }
        val bytes = ByteArrayOutputStream()
        ZipOutputStream(bytes).use { zip ->
            for ((name, value) in members) {
                zip.putNextEntry(ZipEntry(name).apply {
                    method = ZipEntry.STORED
                    size = value.size.toLong()
                    compressedSize = size
                    crc = CRC32().apply { update(value) }.value
                })
                zip.write(value)
                zip.closeEntry()
            }
        }
        return bytes.toByteArray()
    }
}
