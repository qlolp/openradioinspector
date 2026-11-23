package org.openradioinspector.field

import org.junit.Assert.*
import org.junit.Test

class OperatorFormTest {
    private val form = ScanForm(7, "ardv1", "ardv1_range", "100", "102", "10000", "",
        "101", "10000", "200", "AM", false, true)

    @Test fun noteTargetsTheDisplayedFrameEvenAfterAnotherMeasurementArrives() {
        val visible = FieldState(running = true, sessionId = "session-1",
            measurementKind = "sweep", measurementId = "displayed")
        val nowRecording = visible.copy(measurementId = "newer")
        val queued = mutableListOf<List<String?>>()
        val enqueue = { text: String, id: String, kind: String?, source: String? ->
            queued += listOf(text, id, kind, source)
            true
        }
        assertTrue(visible.submitDisplayedNote("  ориентир  ", enqueue))
        assertEquals(listOf("ориентир", "session-1", "sweep", "displayed"), queued.single())
        assertNotEquals(nowRecording.measurementId, queued.single().last())
        assertFalse(visible.copy(running = false).submitDisplayedNote("поздно", enqueue))
        assertFalse(visible.submitDisplayedNote("ещё", { _, _, _, _ -> false }))
    }

    @Test fun decimalMHzIsExact() {
        assertEquals(100_000_001L, parseMHz("100,000001"))
        assertEquals(100_000_010L, parseMHz("100.000010"))
        assertEquals("100,000001", showMHz(100_000_001L))
        listOf("1e6", "0", "-1", "100.0000001", "18446744073709551615", "1,0,2").forEach {
            assertNull(it, parseMHz(it))
        }
    }

    @Test fun listSupportsRussianDecimalsAndKeepsOrder() {
        val config = form.copy(kind = "ardv1_list", start = "", end = "", list = "107,9; 99.1\n105").config()
        assertEquals(listOf(107_900_000L, 99_100_000L, 105_000_000L), config.frequenciesHz)
        assertEquals("AM", config.mode)
        assertThrows(IllegalArgumentException::class.java) { form.copy(kind = "ardv1_list", list = "101 x").config() }
    }

    @Test fun adapterPlanKeepsFixedSurveyGeometryAndScopeMode() {
        assertEquals(10_000L, form.copy(profile = "rfexplorer", kind = "rf_explorer_survey", step = "5").config().stepHz)
        assertEquals("FM", form.config().mode)
        assertThrows(IllegalArgumentException::class.java) { form.copy(end = "90").config() }
        assertThrows(IllegalArgumentException::class.java) { form.copy(deviceId = null).config() }
        assertThrows(IllegalArgumentException::class.java) { form.copy(kind = "tinysa_rx").config() }
    }

    @Test fun rfSweepFollowsReadbackByDefaultAndPreservesExplicitBounds() {
        val defaults = form.copy(profile = "rfexplorer", kind = "rf_explorer_sweep", start = "", end = "")
        val follow = defaults.config()
        assertEquals(0L, follow.startHz)
        assertEquals(0L, follow.stopHz)
        assertThrows(IllegalArgumentException::class.java) { defaults.copy(followWindow = false).config() }
        val fixed = defaults.copy(start = "433", end = "434,11", followWindow = false).config()
        assertEquals(433_000_000L, fixed.startHz)
        assertEquals(434_110_000L, fixed.stopHz)
        assertEquals(0L, (fixed.stopHz - fixed.startHz) % 111)
    }

    @Test fun singleFrequencyDoesNotRequireAnEndOrStep() {
        val config = form.copy(kind = "ardv1_single", end = "-", step = "bad").config()
        assertEquals(100_000_000L, config.startHz)
        assertEquals(config.startHz, config.stopHz)
    }

    @Test fun trackNeverConnectsAcrossAnOutageStaleFixOrClockJump() {
        val base = TrackPoint("a", 50.0, 30.0, "2026-01-01T12:00:00Z")
        val next = base.copy(id = "b", utc = "2026-01-01T12:00:15Z")
        assertEquals(listOf(false, true), trackLinks(listOf(base, next)))
        val disconnected = listOf(next.copy(segmentId = "new"), next.copy(priorGapSeq = 3),
            next.copy(utc = "2026-01-01T12:00:15.001Z"), next.copy(utc = "2025-12-31T23:59:59Z"),
            next.copy(utc = ""), next.copy(fresh = false))
        disconnected.forEach { assertEquals(listOf(false, false), trackLinks(listOf(base, it))) }
        assertEquals(listOf(false, false), trackLinks(listOf(base.copy(fresh = false), next)))
    }

    @Test fun chartHandlesStationaryTrackAndSelectableDatelineCrossing() {
        val first = TrackPoint("a", 5.0, 179.99, "")
        val center = MapPoint(100.0, 50.0)
        assertEquals(listOf(center, center), projectTrack(listOf(first, first), 200.0, 100.0))
        val track = projectTrack(listOf(first, first.copy(id = "b", longitude = -179.99),
            first.copy(id = "c", longitude = -179.98, latitude = 5.01)), 200.0, 100.0)
        assertTrue(track[0].x < track[1].x && track[1].x < track[2].x)
        assertTrue(track[2].y < track[0].y)
        assertEquals(1, closestPoint(track, track[1].x, track[1].y))
        assertNull(closestPoint(emptyList(), 0.0, 0.0))
    }

    @Test fun tappingOverlappingFixesSelectsTheMarkerDrawnOnTop() {
        val stationary = listOf(MapPoint(50.0, 20.0), MapPoint(50.0, 20.0))
        assertEquals(1, closestPoint(stationary, 49.0, 21.0))
        assertNull(closestPoint(stationary, 100.0, 100.0, radius = 18.0))
    }
}
