package org.openradioinspector.field

import android.graphics.Bitmap
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

private val plotBackground = Color(0xff0c141c)
private val plotGrid = Color(0xff283744)
private val traceColor = Color(0xff7dddc6)

@Composable
internal fun SeriesPlot(x: List<Long>, y: List<Double>, description: String) {
    if (x.isEmpty() || x.size != y.size || y.any { !it.isFinite() }) return
    val low = y.min()
    val high = y.max()
    val left = x.min()
    val right = x.max()
    Canvas(Modifier.fillMaxWidth().height(145.dp).background(plotBackground)
        .semantics { contentDescription = description }) {
        for (i in 1..3) {
            val row = size.height * i / 4
            drawLine(plotGrid, Offset(0f, row), Offset(size.width, row))
            val col = size.width * i / 4
            drawLine(plotGrid, Offset(col, 0f), Offset(col, size.height))
        }
        val path = Path()
        x.indices.forEach { i ->
            val px = if (right == left) size.width / 2 else size.width * ((x[i] - left).toDouble() / (right - left)).toFloat()
            val py = if (high == low) size.height / 2 else 2f + (size.height - 4f) * ((high - y[i]) / (high - low)).toFloat()
            if (i == 0) path.moveTo(px, py) else path.lineTo(px, py)
            if (x.size == 1) drawCircle(traceColor, 3.dp.toPx(), Offset(px, py))
        }
        drawPath(path, traceColor, style = Stroke(2.dp.toPx()))
    }
}

internal fun unitLabel(unit: String) = when (unit.lowercase()) {
    "dbm" -> "дБм"
    "s_meter_count" -> "отсч."
    else -> unit
}

internal fun archivedLevel(value: Double) = value.toString().removeSuffix(".0").replace('.', ',')

@Composable
internal fun SpectrumPanel(x: List<Long>, y: List<Double>, unit: String, history: List<List<Double>>) {
    if (x.isEmpty() || x.size != y.size) return
    val peak = y.indices.maxBy { y[it] }
    Text("Спектр", style = MaterialTheme.typography.titleMedium)
    Text("Пик ${level(y[peak])} ${unitLabel(unit)}  ·  ${showMHz(x[peak])} МГц")
    SeriesPlot(x, y, "Спектр по частоте, ${unitLabel(unit)}")
    Text("${showMHz(x.first())} – ${showMHz(x.last())} МГц  ·  ${y.size} точек  ·  " +
        "${level(y.min())}…${level(y.max())} ${unitLabel(unit)}", style = MaterialTheme.typography.bodySmall)
    if (history.isEmpty()) return
    val rows = remember(history, x.size) { history.filter { it.size == x.size } }
    if (rows.isEmpty()) return
    val min = rows.minOf { it.min() }
    val max = rows.maxOf { it.max() }
    val bitmap = remember(rows) {
        val colors = IntArray(x.size * rows.size)
        rows.asReversed().forEachIndexed { j, row ->
            row.forEachIndexed { i, value ->
                val t = if (max == min) 0.5 else ((value - min) / (max - min)).coerceIn(0.0, 1.0)
                colors[j * x.size + i] = android.graphics.Color.rgb(
                    (18 + 222 * t * t).roundToInt(), (32 + 190 * t).roundToInt(),
                    (56 + 75 * (1 - kotlin.math.abs(2 * t - 1))).roundToInt())
            }
        }
        Bitmap.createBitmap(colors, x.size, rows.size, Bitmap.Config.ARGB_8888).asImageBitmap()
    }
    Text("Водопад", style = MaterialTheme.typography.titleMedium)
    Canvas(Modifier.fillMaxWidth().height(95.dp).background(plotBackground)
        .semantics { contentDescription = "Водопад спектра" }) {
        drawImage(bitmap, dstSize = IntSize(size.width.roundToInt(), size.height.roundToInt()),
            filterQuality = FilterQuality.None)
    }
    Text("Сверху — последний спектр  ·  ${rows.size} строк  ·  ${level(min)}…${level(max)} ${unitLabel(unit)}",
        style = MaterialTheme.typography.bodySmall)
}

@Composable
internal fun TrackPlot(track: List<TrackPoint>, selected: String? = null,
    routeLevels: Map<String, RouteReading>? = null, select: ((String) -> Unit)? = null) {
    if (track.isEmpty()) return
    val links = remember(track) { trackLinks(track) }
    val samples = remember(routeLevels) { routeLevels?.values?.filterIsInstance<RouteReading.Measured>().orEmpty() }
    val low = samples.minOfOrNull { it.level }
    val high = samples.maxOfOrNull { it.level }
    val weak = Color(0xff699ae9)
    val strong = Color(0xffffdc74)
    val onSelect by rememberUpdatedState(select)
    val mapped = routeLevels != null
    val tap = if (select == null) Modifier else Modifier.pointerInput(track, mapped) {
        detectTapGestures { offset ->
            val projected = projectTrack(track, size.width.toDouble(), size.height.toDouble())
            val radius = if (mapped) 18.dp.toPx().toDouble() else Double.POSITIVE_INFINITY
            closestPoint(projected, offset.x.toDouble(), offset.y.toDouble(), radius)?.let { onSelect?.invoke(track[it].id) }
        }
    }
    val description = if (mapped) "Уровни по треку GPS" else "Трек GPS"
    Canvas(Modifier.fillMaxWidth().height(145.dp).background(plotBackground).then(tap)
        .semantics { contentDescription = "$description, север сверху. Точек на экране ${track.size}" }) {
        val points = projectTrack(track, size.width.toDouble(), size.height.toDouble())
        val path = Path()
        points.forEachIndexed { i, point ->
            val x = point.x.toFloat()
            val y = point.y.toFloat()
            if (links[i]) path.lineTo(x, y) else path.moveTo(x, y)
        }
        drawPath(path, if (mapped) Color(0xff62717e) else traceColor, style = Stroke(1.5.dp.toPx()))
        points.forEachIndexed { i, point ->
            val center = Offset(point.x.toFloat(), point.y.toFloat())
            if (!mapped) drawCircle(traceColor, 2.dp.toPx(), center)
            else when (val reading = routeLevels?.get(track[i].id)) {
                is RouteReading.Measured -> {
                    val t = if (low == null || high == null || low == high) 0.5f else ((reading.level - low) / (high - low)).toFloat()
                    drawCircle(lerp(weak, strong, t), 4.dp.toPx(), center)
                }
                is RouteReading.OutsideRange -> {
                    val d = 3.dp.toPx()
                    drawLine(Color.LightGray, center - Offset(d, d), center + Offset(d, d), 1.5.dp.toPx())
                    drawLine(Color.LightGray, center - Offset(d, -d), center + Offset(d, -d), 1.5.dp.toPx())
                }
                is RouteReading.MissingLevel, null -> drawCircle(Color.LightGray, 3.dp.toPx(), center, style = Stroke(1.5.dp.toPx()))
            }
        }
        val index = if (selected == null) points.lastIndex else track.indexOfFirst { it.id == selected }
        points.getOrNull(index)?.let {
            val center = Offset(it.x.toFloat(), it.y.toFloat())
            if (mapped) drawCircle(Color.White, 7.dp.toPx(), center, style = Stroke(1.5.dp.toPx()))
            else drawCircle(Color(0xffffc97d), 5.dp.toPx(), center)
        }
    }
    Text("Север ↑  ·  ${track.size} точек на экране", style = MaterialTheme.typography.bodySmall)
    if (mapped) {
        if (low != null && high != null) {
            val unit = unitLabel(samples.first().unit)
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Шкала страницы", style = MaterialTheme.typography.bodySmall)
                Text("${archivedLevel(low)} $unit", style = MaterialTheme.typography.bodySmall)
                if (low == high) Box(Modifier.size(10.dp).background(lerp(weak, strong, 0.5f)))
                else {
                    Box(Modifier.weight(1f).height(8.dp).background(Brush.horizontalGradient(listOf(weak, strong))))
                    Text("${archivedLevel(high)} $unit", style = MaterialTheme.typography.bodySmall)
                }
            }
        }
        Text("○ Нет отсчёта  ·  × Вне диапазона", style = MaterialTheme.typography.bodySmall)
    }
}
