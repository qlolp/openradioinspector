package org.openradioinspector.field

import android.database.sqlite.SQLiteDatabase
import android.location.Location
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.nio.file.Files
import java.time.Instant
import java.util.UUID
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class FieldArchiveTest {
    @Test fun providerLossPersistsOneGapAndSplitsTheNextGpsSegment() {
        val cache = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        val root = Files.createTempDirectory(cache.toPath(), "field-gps-test").toFile()
        val id = UUID.randomUUID().toString()
        val config = FieldConfig(9, "tinysa", 100_000_000, 101_000_000, 0, gps = true)
        fun fix() = Location("gps").apply {
            latitude = 12.0
            longitude = 13.0
            time = System.currentTimeMillis()
            elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos()
        }
        try {
            FieldArchive.create(root, id, FieldDevice(9, "Synthetic GPS", listOf("tinysa")), config).use { store ->
                val before = store.gps(fix())
                store.gpsDisabled()
                store.gpsDisabled()
                val after = store.gps(fix())
                assertTrue(after.fresh)
                assertNotEquals(before.segmentId, after.segmentId)
                store.finish(null)
            }
            SQLiteDatabase.openDatabase(FieldArchive.database(root, id).path, null, SQLiteDatabase.OPEN_READONLY).use { db ->
                db.rawQuery("SELECT kind,json FROM events ORDER BY seq", null).use { cursor ->
                    val kinds = mutableListOf<String>()
                    val segments = mutableListOf<String>()
                    while (cursor.moveToNext()) {
                        kinds += cursor.getString(0)
                        if (cursor.getString(0) == "gps") segments += JSONObject(cursor.getString(1)).getString("segment_id")
                    }
                    assertEquals(listOf("gps", "gap", "gps"), kinds)
                    assertEquals(2, segments.distinct().size)
                }
            }
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun sizeLimitLeavesLastCommittedRowAndTerminalGapExportable() {
        val cache = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        val root = Files.createTempDirectory(cache.toPath(), "field-limit-test").toFile()
        val id = UUID.randomUUID().toString()
        val config = FieldConfig(9, "tinysa", 100_000_000, 101_000_000, 0)
        try {
            FieldArchive.create(root, id, FieldDevice(9, "Synthetic analyzer", listOf("tinysa")), config, dataLimit = 650).use { store ->
                val text = "я".repeat(100)
                assertTrue(store.note(text))
                val failure = assertThrows(IllegalStateException::class.java) { store.note(text) }
                store.finish(failure.message)
            }
            val work = java.io.File(root, "staging").apply { mkdir() }
            val file = FieldExporter.build(FieldArchive.database(root, id), work)
            java.util.zip.ZipFile(file).use { archive ->
                val lines = archive.getInputStream(archive.getEntry("events.ndjson")).bufferedReader().use { it.readLines() }
                assertEquals(listOf("note", "gap"), lines.map { JSONObject(it).getString("kind") })
                assertFalse(JSONObject(lines.first()).has("sweep_id"))
            }
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun surveyTilesRetainCommonPassIdentifierAtDesktopContractPath() {
        val cache = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        val root = Files.createTempDirectory(cache.toPath(), "field-survey-test").toFile()
        val id = UUID.randomUUID().toString()
        val config = FieldConfig(9, "rfexplorer", 100_000_000, 101_000_000, 10_000, kind = "rf_explorer_survey")
        try {
            FieldArchive.create(root, id, FieldDevice(9, "Synthetic survey", listOf("rfexplorer")), config).use { store ->
                for ((pass, tile) in listOf(1 to 1, 1 to 2, 2 to 1)) {
                    val first = 100_000_000 + (tile - 1) * 10_000
                    val raw = """{"kind":"raw_spectrum","frequencies_hz":[$first],"levels":[-42.5],"level_unit":"dBm","interval":{"started_at":"2026-01-01T00:00:00.000000Z","ended_at":"2026-01-01T00:00:00.100000Z"},"settings":{"survey":{"pass":$pass,"tile":$tile,"tiles":2}}}"""
                    assertEquals(tile.toLong(), store.measurement(raw, config).progress)
                }
                store.finish(null)
            }
            SQLiteDatabase.openDatabase(FieldArchive.database(root, id).path, null, SQLiteDatabase.OPEN_READONLY).use { db ->
                val passIds = mutableListOf<String>()
                db.rawQuery("SELECT json FROM sweeps ORDER BY seq", null).use { cursor ->
                    while (cursor.moveToNext()) {
                        val settings = JSONObject(cursor.getString(0)).getJSONObject("settings")
                        passIds += settings.getJSONObject("survey").getString("pass_id")
                    }
                }
                assertEquals(3, passIds.size)
                assertEquals(passIds[0], passIds[1])
                assertNotEquals(passIds[1], passIds[2])
            }
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun interruptedCaptureRetainsRawObservationGpsLinkNoteAndGap() {
        val cache = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        val root = Files.createTempDirectory(cache.toPath(), "field-store-test").toFile()
        val id = UUID.randomUUID().toString()
        val config = FieldConfig(12, "ardv1", 145_500_000, 145_500_000, 0, gps = true, kind = "ardv1_single")
        try {
            val now = System.currentTimeMillis()
            val utc = Instant.ofEpochMilli(now).toString()
            val file = FieldArchive.database(root, id)
            FieldArchive.create(root, id, FieldDevice(12, "Synthetic serial fixture", listOf("ardv1")), config).use { store ->
                val fix = store.gps(Location("gps").apply {
                    latitude = 45.0
                    longitude = 15.0
                    accuracy = 2.5f
                    time = now
                    elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos()
                })
                val raw = """{"frequency_hz":145500000,"mode":"FM","level":41,"level_unit":"s_meter_count","squelch_open":true,"raw_squelch_status":1,"interval":{"started_at":"$utc","ended_at":"$utc"},"metadata":{"scan_total":1,"scan_index":0,"scan_pass":1}}"""
                val sample = store.measurement(raw, config)
                assertEquals(fix.id, sample.receiver?.fixId)
                assertEquals(listOf(41.0), sample.values)
                val newer = store.measurement(raw, config)
                assertNotEquals(sample.id, newer.id)
                assertFalse(store.note("Неверный тип", "sweep", sample.id))
                assertFalse(store.note("Нет источника", "observation", UUID.randomUUID().toString()))
                assertTrue(store.note("Проверка", "observation", sample.id))
                // Simulate process loss after committed rows, before finish().
            }
            FieldArchive.recover(file)
            FieldArchive.recover(file)
            SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READONLY).use { db ->
                fun one(query: String) = db.rawQuery(query, null).use { cursor ->
                    assertTrue(cursor.moveToFirst())
                    cursor.getString(0)
                }
                val row = JSONObject(one("SELECT json FROM observations ORDER BY seq LIMIT 1"))
                val gps = JSONObject(one("SELECT json FROM events WHERE kind='gps'"))
                val note = JSONObject(one("SELECT json FROM events WHERE kind='note'"))
                assertEquals(row.getString("id"), note.getString("observation_id"))
                assertEquals(gps.getString("id"), row.getJSONObject("metadata").getString("field_fix_id"))
                assertEquals(1, row.getJSONObject("metadata").getInt("raw_squelch_status"))
                assertEquals("s_meter_count", row.getString("level_unit"))
                assertEquals("1", one("SELECT COUNT(*) FROM events WHERE kind='gap'"))
                assertNotNull(one("SELECT closed_at FROM sessions"))
            }
        } finally {
            root.deleteRecursively()
        }
    }
}
