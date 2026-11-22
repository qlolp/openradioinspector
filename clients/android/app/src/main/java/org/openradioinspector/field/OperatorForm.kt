package org.openradioinspector.field

import java.math.BigDecimal
import java.util.Locale

internal val scanLabels = mapOf(
    "tinysa_rx" to "Спектр",
    "rf_explorer_sweep" to "Спектр",
    "rf_explorer_survey" to "Обзор диапазона",
    "ardv1_single" to "Одна частота",
    "ardv1_list" to "Список частот",
    "ardv1_range" to "Диапазон с шагом",
)

internal fun kindsFor(profile: String) = when (profile) {
    "tinysa" -> listOf("tinysa_rx")
    "rfexplorer" -> listOf("rf_explorer_sweep", "rf_explorer_survey")
    "ardv1" -> listOf("ardv1_single", "ardv1_list", "ardv1_range")
    else -> emptyList()
}

internal fun profileName(profile: String) = when (profile) {
    "tinysa" -> "tinySA"
    "rfexplorer" -> "RF Explorer"
    "ardv1" -> "AR-DV1"
    else -> profile
}

// Decimal input is converted directly to integer Hz, including a Russian decimal comma.
internal fun parseMHz(text: String): Long? {
    val normalized = text.trim().replace(',', '.')
    if (normalized.length > 20 || !Regex("""\d+(\.\d+)?""").matches(normalized)) return null
    return try {
        BigDecimal(normalized).movePointRight(6).longValueExact().takeIf { it > 0 }
    } catch (_: ArithmeticException) {
        null
    }
}

internal fun showMHz(hz: Long): String = BigDecimal.valueOf(hz, 6).stripTrailingZeros()
    .toPlainString().replace('.', ',')

internal fun level(value: Double) = String.format(Locale.forLanguageTag("ru"), "%.1f", value)

internal data class ScanForm(
    val deviceId: Int?, val profile: String, val kind: String,
    val start: String, val end: String, val step: String, val list: String,
    val points: String, val rbw: String, val settle: String, val mode: String,
    val continuous: Boolean, val gps: Boolean, val followWindow: Boolean = true,
) {
    fun config(): FieldConfig {
        require(deviceId != null && kind in kindsFor(profile)) { "Выберите USB-прибор и профиль" }
        if (kind == "rf_explorer_sweep" && followWindow) {
            // The native RF Explorer adapter reads both endpoints from the active board.
            return FieldConfig(deviceId, profile, 0, 0, 10_000, gps = gps, kind = kind, continuous = continuous)
        }
        val listScan = kind == "ardv1_list"
        val single = kind == "ardv1_single"
        val frequencies = if (listScan) {
            val tokens = list.trim().split(Regex("""[;\s]+"""))
            tokens.map { requireNotNull(parseMHz(it)) { "Проверьте список частот в МГц" } }
        } else emptyList()
        val first = frequencies.firstOrNull() ?: requireNotNull(parseMHz(start)) { "Укажите частоту в МГц" }
        val last = when {
            listScan -> frequencies.last()
            single -> first
            else -> requireNotNull(parseMHz(end)?.takeIf { it > first }) { "Конец диапазона должен превышать начало" }
        }
        val spacing = if (kind == "ardv1_range") {
            requireNotNull(step.toLongOrNull()?.takeIf { it > 0 }) { "Укажите положительный шаг в Гц" }
        } else 10000L
        val count = if (profile == "tinysa") {
            requireNotNull(points.toIntOrNull()?.takeIf { it in 2..290 }) { "Допустимо от 2 до 290 точек спектра" }
        } else 101
        val bandwidth = if (profile == "tinysa") {
            requireNotNull(rbw.toIntOrNull()?.takeIf { it > 0 }) { "Укажите полосу разрешения в Гц" }
        } else 10000
        val settling = if (profile == "ardv1") {
            requireNotNull(settle.toIntOrNull()?.takeIf { it in 0..1000 }) { "Время установления — от 0 до 1000 мс" }
        } else 200
        return FieldConfig(deviceId = deviceId, profile = profile, startHz = first, stopHz = last,
            stepHz = spacing, gps = gps, points = count, kind = kind, frequenciesHz = frequencies,
            mode = if (kind == "ardv1_range") "FM" else mode, settleMs = settling, rbwHz = bandwidth, continuous = continuous)
    }
}

// The Compose click handler captures the frame used to draw the graph. New samples can
// already be committed by the time the operator taps, or the executor writes the note.
internal fun FieldState.submitDisplayedNote(text: String, enqueue: (String, String, String?, String?) -> Boolean): Boolean {
    val origin = sessionId ?: return false
    if (!running || text.isBlank() || text.length > 4096) return false
    return enqueue(text.trim(), origin, measurementKind, measurementId)
}
