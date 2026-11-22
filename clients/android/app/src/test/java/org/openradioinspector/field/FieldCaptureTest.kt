package org.openradioinspector.field

import org.junit.Assert.*
import org.junit.Test

class FieldCaptureTest {
    private val range = FieldConfig(7, "ardv1", 145_000_000, 146_000_000, 10_000)

    @Test fun nativePlansKeepReadbackOptionsAndInstrumentBoundaries() {
        val plan = range.copy(continuous = true, settleMs = 150).nativePlan()
        assertEquals("ardv1_range", plan.getString("kind"))
        assertTrue(plan.getBoolean("continuous"))
        assertEquals(10_000L, plan.getLong("step_hz"))
        assertEquals(150, plan.getInt("settle_ms"))
        assertFalse(plan.has("baud"))
        val list = range.copy(kind = "ardv1_list", frequenciesHz = listOf(145_000_000L, 145_500_000L)).nativePlan()
        assertEquals(145_500_000L, list.getJSONArray("frequencies_hz").getLong(1))
        assertFalse(list.has("start_hz"))
        val sweep = range.copy(profile = "rfexplorer", kind = "rf_explorer_sweep").nativePlan()
        assertFalse(sweep.has("continuous"))
        assertFalse(sweep.has("step_hz"))
        assertFalse(sweep.has("mode"))
        assertEquals(500_000, range.copy(profile = "rfexplorer", kind = "rf_explorer_survey").checkedBaud())
        assertThrows(IllegalArgumentException::class.java) { range.copy(kind = "tinysa_rx").checkedBaud() }
    }

    @Test fun lastCommittedTileEndsOnePass() {
        assertFalse(range.passComplete(1, 1001))
        assertFalse(range.passComplete(1000, 1001))
        assertTrue(range.passComplete(1001, 1001))
        assertFalse(range.passComplete(0, 0))
    }

    @Test fun positionMustPrecedeMeasurementAndRemainFreshAcrossClockChanges() {
        assertTrue(fixApplies(100, 100, 51, 51))
        assertTrue(fixApplies(100, 15_100, 51, 15_051))
        assertFalse(fixApplies(100, 99, 50, 51))
        assertFalse(fixApplies(100, 15_101, 50, 51))
        assertFalse(fixApplies(100, 101, 50, 15_051))
        assertFalse(fixApplies(100, 101, 52, 51))
    }
}
