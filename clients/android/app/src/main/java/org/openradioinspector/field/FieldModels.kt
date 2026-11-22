package org.openradioinspector.field

data class FieldDevice(val id: Int, val label: String, val profiles: List<String>)

data class FieldConfig(
    val deviceId: Int,
    val profile: String,
    val startHz: Long,
    val stopHz: Long,
    val stepHz: Long,
    val gps: Boolean = false,
    val points: Int = 101,
    val kind: String = when (profile) {
        "rfexplorer" -> "rf_explorer_sweep"
        "tinysa" -> "tinysa_rx"
        else -> "ardv1_range"
    },
    val continuous: Boolean = false,
    val frequenciesHz: List<Long> = emptyList(),
    val mode: String = "FM",
    val settleMs: Int = 200,
    val rbwHz: Int = 10_000,
    val baud: Int = 0,
) {
    internal fun checkedBaud(): Int {
        val allowedKinds = when (profile) {
            "rfexplorer" -> setOf("rf_explorer_sweep", "rf_explorer_survey")
            "tinysa" -> setOf("tinysa_rx")
            "ardv1" -> setOf("ardv1_single", "ardv1_list", "ardv1_range")
            else -> error("Unknown instrument profile")
        }
        require(kind in allowedKinds) { "Scan mode does not match the instrument" }
        val rate = if (baud != 0) baud else when (profile) {
            "rfexplorer" -> 500_000
            "tinysa" -> 9_600
            else -> 115_200
        }
        require(if (profile == "rfexplorer") rate == 500_000 else rate in setOf(9600, 19200, 38400, 57600, 115200)) {
            "Unsupported baud rate for this instrument"
        }
        return rate
    }

    internal fun passComplete(progress: Long, total: Long): Boolean =
        total > 0 && progress == total
}

data class FieldFix(
    val latitude: Double, val longitude: Double, val accuracyM: Double?,
    val utc: String, val id: String, val segmentId: String = "", val fresh: Boolean = true,
)

data class FieldSample(
    val frequencyHz: Long, val value: Double, val utc: String,
    val measurementId: String, val fixId: String?,
)

data class FieldState(
    val running: Boolean = false,
    val gpsRequested: Boolean = false,
    val status: String = "Готово",
    val sessionId: String? = null,
    val frequenciesHz: List<Long> = emptyList(),
    val values: List<Double> = emptyList(),
    val unit: String = "",
    val progress: Long = 0,
    val total: Long = 0,
    val latitude: Double? = null,
    val longitude: Double? = null,
    val sessions: List<String> = emptyList(),
    val track: List<FieldFix> = emptyList(),
    val waterfall: List<List<Double>> = emptyList(),
    val sequence: Long = 0,
    val receiverSamples: List<FieldSample> = emptyList(),
)

internal fun fixApplies(fixUtcMs: Long, sampleUtcMs: Long, fixElapsedMs: Long, nowElapsedMs: Long): Boolean =
    sampleUtcMs - fixUtcMs in 0..15_000 && nowElapsedMs - fixElapsedMs in 0..15_000

/** Tracks GPS coverage against elapsed time even when wall time or the provider changes. */
internal class GpsCoverage(startMs: Long) {
    private var lastFreshMs = startMs
    private var lostAtMs: Long? = null
    var segment = 1L
        private set

    fun check(nowMs: Long): Boolean = nowMs - lastFreshMs > 15_000 && lose(lastFreshMs + 15_000)

    fun lose(atMs: Long): Boolean {
        if (lostAtMs != null) return false
        lostAtMs = atMs
        return true
    }

    fun receive(fixMs: Long, nowMs: Long): Boolean {
        if (nowMs - fixMs !in 0L..15_000L) return false
        val lost = lostAtMs
        if (lost != null && fixMs < lost) return false
        if (lost != null) segment++
        lostAtMs = null
        lastFreshMs = fixMs
        return true
    }
}
