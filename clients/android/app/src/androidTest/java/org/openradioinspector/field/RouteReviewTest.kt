package org.openradioinspector.field

import android.database.sqlite.SQLiteDatabase
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class RouteReviewTest {
    @Test fun sourceLookupRequiresTheOriginalSessionStreamAndId() = withCapture("tinysa") {
        record("original", sweep("fix", -42.5))
        record("another-session", sweep("fix", -12.0).put("session_id", "other"))
        record("another-stream", sweep("fix", -13.0).put("stream_id", "other"))
        record("different-id", sweep("fix", -14.0).put("id", "unrelated"))

        assertEquals("original", reader.source(profile, "original")!!.getString("id"))
        for (id in listOf("another-session", "another-stream", "different-id")) {
            assertNull(id, reader.source(profile, id))
        }
    }

    @Test fun routeUsesLatestCoveringSweepAndItsActualBinOnTheOriginalFix() = withCapture("rfexplorer") {
        for (id in listOf("fix", "outside", "empty", "unlinked", "mismatched", "foreign", "unit")) gps(id)
        gps("other-stream-fix", fixStream = "other")
        gps("other-session-fix", fixSession = "other")
        record("older", sweep("fix", -17.0))
        record("chosen", sweep("fix", -42.5))
        val otherBand = JSONArray(listOf(200L, 220L, 250L))
        record("later-tile", sweep("fix", -11.0).put("frequencies_hz", otherBand))
        record("outside-source", sweep("outside", -88.0).put("frequencies_hz", otherBand), "outside")
        record("no-link", sweep(null, -21.0), "unlinked")
        record("wrong-fix", sweep("empty", -22.0), "mismatched")
        record("wrong-session", sweep("foreign", -23.0).put("session_id", "other"), "foreign")
        record("wrong-stream", sweep("foreign", -24.0).put("stream_id", "other"), "foreign")
        record("wrong-unit", sweep("unit", 127.0).put("level_unit", "s_meter_count"), "unit")
        record("wrong-gps-stream", sweep("other-stream-fix", -31.0), "other-stream-fix")
        record("wrong-gps-session", sweep("other-session-fix", -32.0), "other-session-fix")

        val points = route(135)
        assertEquals(setOf("fix", "outside"), points.keys)
        // The nonuniform grid is equidistant here. The lower original bin wins.
        assertEquals(RouteReading.Measured("chosen", 120L, -42.5, "dBm"), points["fix"])
        assertEquals(RouteReading.OutsideRange("outside-source"), points["outside"])
        assertEquals(-42.5, reader.source(profile, points.getValue("fix").sourceId)!!
            .getJSONArray("levels").getDouble(1), 0.0)
        assertEquals(RouteReading.Measured("chosen", 150L, -75.0, "dBm"), route(149)["fix"])
        assertEquals(RouteReading.Measured("chosen", 100L, -91.0, "dBm"), route(100)["fix"])
        assertEquals(RouteReading.OutsideRange("later-tile"), route(99)["fix"])
    }

    @Test fun receiverRequiresExactFrequencyAndKeepsTheRawCountIncludingZero() = withCapture("ardv1") {
        gps("fix")
        gps("off-channel")
        gps("empty")
        record("older", observation("fix", 120, 146))
        record("zero", observation("fix", 120, 0))
        record("one-hz-away", observation("fix", 121, 180))
        record("off-channel-source", observation("off-channel", 119, 170), "off-channel")
        record("unlinked", observation(null, 120, 200), null)
        val points = route(120)
        assertEquals(setOf("fix", "off-channel"), points.keys)
        assertEquals(RouteReading.Measured("zero", 120, 0.0, "s_meter_count"), points["fix"])
        assertEquals(RouteReading.OutsideRange("off-channel-source"), points["off-channel"])
        assertEquals(0, reader.source(profile, points.getValue("fix").sourceId)!!.getInt("level"))
    }

    @Test fun fixBrowserSkipsMismatchedLinksBeforePaginatingOriginalMeasurements() = withCapture("tinysa") {
        gps("fix")
        repeat(19) { record("stale-index-$it", sweep("different", -20.0)) }
        record("wrong-stream", sweep("fix", -21.0).put("stream_id", "other"))
        record("original", sweep("fix", -24.0))
        val linked = reader.page(profile, ReviewKind.DATA, PageCursor(), "fix")
        assertEquals(listOf("original"), linked.items.map { it.json.getString("id") })
        assertFalse(linked.newer)
    }

    @Test fun missingReceiverCountRetainsItsSourceAndNeverBecomesZeroOrMasksAnEarlierCount() = withCapture("ardv1") {
        gps("fix")
        gps("with-count")
        record("no-count", observation("fix", 120, 1).put("level", JSONObject.NULL))
        record("off-channel", observation("fix", 121, 200))
        record("measured", observation("with-count", 120, 137), "with-count")
        record("later-without-count", observation("with-count", 120, 1).put("level", JSONObject.NULL), "with-count")
        val points = route(120)
        val missing = points.getValue("fix")
        assertEquals("no-count", missing.sourceId)
        assertFalse(missing is RouteReading.Measured || missing is RouteReading.OutsideRange)
        assertTrue(reader.source(profile, missing.sourceId)!!.isNull("level"))
        assertEquals(RouteReading.Measured("measured", 120, 137.0, "s_meter_count"), points["with-count"])
    }

    @Test fun routeLevelsFollowTheGpsPageInBothDirections() = withCapture("tinysa") {
        for (i in 1..129) gps("p$i")
        record("first-page", sweep("p128", -28.0), "p128")
        record("second-page", sweep("p129", -29.0), "p129")
        val first = reader.page(profile, ReviewKind.TRACK, PageCursor(), null, frequencyHz = 120L)
        assertTrue(first.newer)
        assertEquals(setOf("p128"), first.routeLevels.keys)
        val second = reader.page(profile, ReviewKind.TRACK, PageCursor(first.items.last().seq), null, frequencyHz = 120L)
        assertFalse(second.newer)
        assertEquals(setOf("p129"), second.routeLevels.keys)
        val back = reader.page(profile, ReviewKind.TRACK, PageCursor(second.items.first().seq, true), null, frequencyHz = 120L)
        assertEquals(first.routeLevels, back.routeLevels)
    }

    private class Capture(val reader: SessionReview, val profile: String, val session: String,
        val stream: String, val db: SQLiteDatabase) {
        private val utc = "2026-06-01T10:00:00Z"

        fun route(hz: Long) = reader.page(profile, ReviewKind.TRACK, PageCursor(), null, frequencyHz = hz).routeLevels

        fun gps(id: String, fixStream: String = stream, fixSession: String = session) {
            // Identical UTCs make a timestamp-based match impossible to distinguish by proximity.
            val gps = JSONObject().put("id", id).put("kind", "gps").put("time_utc", utc)
                .put("session_id", fixSession).put("stream_id", fixStream).put("latitude", 45.0).put("longitude", 15.0)
            db.execSQL("INSERT INTO events (id,kind,json) VALUES (?,'gps',?)", arrayOf(id, gps.toString()))
        }

        private fun base() = JSONObject().put("session_id", session).put("stream_id", stream)
            .put("interval", JSONObject().put("started_at", utc).put("ended_at", utc))

        fun sweep(fix: String?, middleLevel: Double) = base()
            .put("frequencies_hz", JSONArray(listOf(100L, 120L, 150L)))
            .put("levels", JSONArray(listOf(-91.0, middleLevel, -75.0))).put("level_unit", "dBm")
            .put("settings", JSONObject().put("field_fix_id", fix))

        fun observation(fix: String?, hz: Long, count: Int) = base()
            .put("frequency_hz", hz).put("level", count).put("level_unit", "s_meter_count").put("mode", "FM")
            .put("metadata", JSONObject().put("field_fix_id", fix))

        fun record(id: String, source: JSONObject, indexFix: String? = "fix") {
            if (!source.has("id")) source.put("id", id)
            val table = if (profile == "ardv1") "observations" else "sweeps"
            db.execSQL("INSERT INTO $table (id,fix_id,json) VALUES (?,?,?)", arrayOf(id, indexFix, source.toString()))
        }
    }

    private fun withCapture(profile: String, test: Capture.() -> Unit) {
        val cache = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        val session = UUID.randomUUID().toString()
        val root = File(cache, session)
        val field = File(root, "field")
        try {
            val config = FieldConfig(7, profile, 100, 150, 10)
            FieldArchive.create(field, session, FieldDevice(7, "synthetic", listOf(profile)), config).use { it.finish(null) }
            SQLiteDatabase.openDatabase(FieldArchive.database(field, session).path, null, SQLiteDatabase.OPEN_READWRITE).use { db ->
                val stream = db.rawQuery("SELECT stream_id FROM sessions WHERE id=?", arrayOf(session)).use {
                    check(it.moveToFirst())
                    it.getString(0)
                }
                Capture(SessionReview(root, session), profile, session, stream, db).test()
            }
        } finally {
            root.deleteRecursively()
        }
    }
}
