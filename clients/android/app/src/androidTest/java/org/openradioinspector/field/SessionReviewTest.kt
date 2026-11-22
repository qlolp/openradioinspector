package org.openradioinspector.field

import android.location.Location
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.time.Instant
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class SessionReviewTest {
    @Test fun recordedFixAndNoteResolveOnlyTheirActualSourcesAcrossPages() {
        val cache = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        val root = File(cache, UUID.randomUUID().toString())
        val session = UUID.randomUUID().toString()
        val config = FieldConfig(7, "ardv1", 100_000_000, 100_000_000, 10_000)
        try {
            val archive = FieldArchive.create(File(root, "field"), session, FieldDevice(7, "test", listOf("ardv1")), config)
            val (fix, lastId) = archive.use {
                val gps = Location("gps").apply {
                    latitude = 55.0
                    longitude = 30.0
                    time = System.currentTimeMillis()
                    elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos()
                }
                val recorded = it.gps(gps)
                var last = ""
                repeat(18) { index ->
                    val ts = Instant.now().toString()
                    val obs = JSONObject().put("interval", JSONObject().put("started_at", ts).put("ended_at", ts))
                        .put("frequency_hz", 100_000_000L + index * 10).put("mode", "FM").put("level", 20 + index)
                        .put("level_unit", "s_meter_count").put("metadata", JSONObject())
                    last = it.measurement(obs.toString(), config).receiver!!.measurementId
                }
                it.note("Контроль")
                it.gap("GPS отключён")
                it.gps(gps)
                it.finish(null)
                recorded to last
            }
            val reader = SessionReview(root, session)
            assertEquals("ardv1", reader.info().profile)
            val track = reader.page("ardv1", ReviewKind.TRACK, PageCursor(), null).items.map(::reviewTrack)
            assertEquals(fix.id, track.first().id)
            assertEquals(listOf(false, false), trackLinks(track))
            assertTrue(track.last().priorGapSeq > track.first().priorGapSeq)
            val p1 = reader.page("ardv1", ReviewKind.DATA, PageCursor(), fix.id)
            val p2 = reader.page("ardv1", ReviewKind.DATA, PageCursor(p1.items.last().seq), fix.id)
            assertEquals(16, p1.items.size)
            assertTrue(p1.newer)
            assertEquals(2, p2.items.size)
            assertFalse(p2.newer)
            assertEquals(lastId, p2.items.last().json.getString("id"))
            val back = reader.page("ardv1", ReviewKind.DATA, PageCursor(p2.items.first().seq, true), fix.id)
            assertEquals(p1.items.map { it.seq }, back.items.map { it.seq })
            assertTrue(reader.page("ardv1", ReviewKind.DATA, PageCursor(), "unlinked").items.isEmpty())
            val note = reader.page("ardv1", ReviewKind.NOTES, PageCursor(), null).items.single().json
            assertEquals(lastId, note.getString("observation_id"))
            assertEquals(37.0, reader.source("ardv1", lastId)!!.getDouble("level"), 0.0)
        } finally {
            root.deleteRecursively()
        }
    }
}
