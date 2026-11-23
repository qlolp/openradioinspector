package org.openradioinspector.field

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

@Composable
internal fun ArchivePanel(state: FieldState, export: (String) -> Unit) {
    var selection by rememberSaveable { mutableStateOf<String?>(null) }
    val root = LocalContext.current.filesDir
    var documentUri by rememberSaveable { mutableStateOf<String?>(null) }
    var revision by rememberSaveable { mutableIntStateOf(0) }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) { documentUri = uri.toString(); revision++ }
    }
    val sessions = state.sessions.filter { !state.running || it != state.sessionId }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(if (documentUri == null) "Сохранённые сеансы" else "Файл выезда", style = MaterialTheme.typography.titleMedium)
        OutlinedButton(onClick = { picker.launch(arrayOf("*/*")) }, modifier = Modifier.fillMaxWidth()) { Text("Открыть .orifield…") }
        val uri = documentUri
        if (uri != null) {
            key(uri, revision) { OpenFieldDocument(uri) { documentUri = null } }
        } else if (sessions.isEmpty()) {
            Text("Завершённые сеансы появятся здесь после остановки записи.")
        } else {
            val selected = selection?.takeIf { it in sessions } ?: sessions.first()
            Choice("Сеанс", selected, sessions.map { it to it }, true) { selection = it }
            key(selected) {
                val reader = remember(root, selected) { SessionReview(root, selected) }
                ArchiveDetail(reader) { export(selected) }
            }
        }
        if (documentUri == null) Text(state.status, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
internal fun ArchiveDetail(reader: SessionReview, export: (() -> Unit)? = null) {
    var kind by rememberSaveable { mutableStateOf(ReviewKind.TRACK) }
    var seq by rememberSaveable { mutableStateOf<Long?>(null) }
    var backwards by rememberSaveable { mutableStateOf(false) }
    var fixId by rememberSaveable { mutableStateOf<String?>(null) }
    var directSource by rememberSaveable { mutableStateOf<String?>(null) }
    var directObservation by rememberSaveable { mutableStateOf<Boolean?>(null) }
    var selectedId by rememberSaveable { mutableStateOf<String?>(null) }
    var frequencyHz by rememberSaveable { mutableStateOf<Long?>(null) }
    // Keep the track cursor and selected fix while the operator inspects its original.
    val showingTrackSource = kind == ReviewKind.TRACK && directSource != null
    BackHandler(enabled = showingTrackSource) { directSource = null }
    fun open(section: ReviewKind, filter: String? = null, source: String? = null, observation: Boolean? = null) {
        kind = section
        seq = null
        backwards = false
        selectedId = null
        fixId = filter
        directSource = source
        directObservation = observation
    }
    var info by remember { mutableStateOf<SessionInfo?>(null) }
    var page by remember(kind, seq, backwards, fixId, directSource, directObservation, frequencyHz) { mutableStateOf<ReviewPage?>(null) }
    var error by remember(kind, seq, backwards, fixId, directSource, directObservation, frequencyHz) { mutableStateOf<String?>(null) }
    LaunchedEffect(reader, kind, seq, backwards, fixId, directSource, directObservation, frequencyHz) {
        try {
            val session = info ?: withContext(Dispatchers.IO) { reader.info() }
            info = session
            val sourceId = directSource
            page = withContext(Dispatchers.IO) {
                if (sourceId != null) {
                    val source = reader.source(session.profile, sourceId, directObservation)
                    ReviewPage(listOfNotNull(source?.let { ReviewEntry(0, it) }), false, false)
                } else reader.page(session.profile, kind, PageCursor(seq, backwards), fixId, frequencyHz)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            error = "Не удалось прочитать сеанс. ${e.localizedMessage.orEmpty()}"
        }
    }
    info?.let { Text(listOf(profileName(it.profile), it.started).filter { part -> part.isNotEmpty() }.joinToString("  ·  "), style = MaterialTheme.typography.bodySmall) }
    if (export != null) OutlinedButton(onClick = export, modifier = Modifier.fillMaxWidth()) { Text("Экспортировать .orifield…") }
    Choice("Просмотр", kind.title, ReviewKind.values().map { it to it.title }, true) { open(it) }
    val current = page
    if (showingTrackSource) TextButton(onClick = { directSource = null }) { Text("К треку") }
    else if (fixId != null || directSource != null) TextButton(onClick = { open(ReviewKind.DATA) }) { Text("Все измерения") }
    else if (kind == ReviewKind.TRACK && current?.items?.isEmpty() != true) {
        RouteFrequency(frequencyHz) { frequencyHz = it }
    }
    if (error != null) Text(error.orEmpty(), color = MaterialTheme.colorScheme.error)
    else if (current == null) Text("Чтение…")
    else if (current.items.isEmpty()) Text(when {
        fixId != null -> "К этой точке GPS измерения не привязаны."
        directSource != null -> "Исходное измерение недоступно."
        kind == ReviewKind.TRACK -> "В этом сеансе нет точек GPS."
        else -> "Записей нет."
    })
    else {
        val entries = current.items
        val index = entries.indexOfFirst { it.json.getString("id") == selectedId }.let {
            if (it >= 0) it else if (backwards) entries.lastIndex else 0
        }
        val entry = entries[index]
        val json = entry.json
        val entryId = json.getString("id")
        val section = if (showingTrackSource) ReviewKind.DATA else kind
        if (section == ReviewKind.TRACK) {
            val points = remember(entries) { entries.map(::reviewTrack) }
            val readings = frequencyHz?.let { hz ->
                val units = if (info?.receiver == true) "исходные отсчёты S-метра" else "ближайший бин, дБм"
                Text("${showMHz(hz)} МГц · $units", style = MaterialTheme.typography.titleMedium)
                current.routeLevels
            }
            TrackPlot(points, entryId, readings) { pointId ->
                selectedId = pointId
                directSource = readings?.get(pointId)?.sourceId
            }
        }
        if (directSource == null) Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            TextButton(enabled = index > 0 || current.older, onClick = {
                if (index > 0) selectedId = entries[index - 1].json.getString("id")
                else {
                    seq = entries.first().seq
                    backwards = true
                    selectedId = null
                }
            }) { Text("← Назад") }
            Text("№ ${entry.seq}", modifier = Modifier.padding(top = 12.dp))
            TextButton(enabled = index < entries.lastIndex || current.newer, onClick = {
                if (index < entries.lastIndex) selectedId = entries[index + 1].json.getString("id")
                else {
                    seq = entries.last().seq
                    backwards = false
                    selectedId = null
                }
            }) { Text("Далее →") }
        }
        when (section) {
            ReviewKind.TRACK -> {
                Text("${json.getDouble("latitude")}, ${json.getDouble("longitude")}", fontFamily = FontFamily.Monospace)
                if (!json.isNull("accuracy_m")) Text("Точность ±${level(json.getDouble("accuracy_m"))} м")
                Text(json.getString("time_utc"))
                if (frequencyHz != null) {
                    val reading = current.routeLevels[entryId]
                    RouteReadingDetails(reading, info?.receiver == true)
                    if (reading != null) OutlinedButton(onClick = { directSource = reading.sourceId }) { Text("Исходное измерение") }
                }
                Button(onClick = { open(ReviewKind.DATA, filter = json.getString("id")) }) { Text("Измерения в точке") }
            }
            ReviewKind.DATA -> {
                if (showingTrackSource) frequencyHz?.let { hz ->
                    Text("Частота на треке ${showMHz(hz)} МГц")
                    val receiver = info?.receiver == true
                    RouteReadingDetails(routeReading(json, receiver, hz), receiver)
                }
                SourceDetails(json)
            }
            ReviewKind.NOTES, ReviewKind.GAPS -> {
                ArchiveText(json, if (kind == ReviewKind.NOTES) "text" else "reason", entryId)
                Text(json.getString("time_utc"))
                val sourceKey = listOf("sweep_id", "observation_id").firstOrNull { !json.isNull(it) }
                if (sourceKey != null) OutlinedButton(onClick = {
                    open(ReviewKind.DATA, source = json.getString(sourceKey), observation = sourceKey == "observation_id")
                }) { Text("К исходному измерению") }
            }
        }
    }
}

@Composable
private fun RouteFrequency(activeHz: Long?, apply: (Long?) -> Unit) {
    var draft by rememberSaveable { mutableStateOf(activeHz?.let(::showMHz).orEmpty()) }
    val hz = remember(draft) { parseMHz(draft) }
    val invalid = draft.isNotBlank() && hz == null
    val focus = LocalFocusManager.current
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(draft, { draft = it }, modifier = Modifier.weight(1f),
            label = { Text("Частота на треке, МГц") }, singleLine = true, isError = invalid,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal))
        TextButton(enabled = !invalid && hz != activeHz, onClick = {
            focus.clearFocus()
            apply(hz)
        }) { Text("Применить") }
    }
    if (invalid) Text("Введите положительную частоту с точностью до 1 Гц.", color = MaterialTheme.colorScheme.error)
}

@Composable
private fun RouteReadingDetails(reading: RouteReading?, receiver: Boolean) {
    Text(when (reading) {
        is RouteReading.Measured -> "Измерено ${showMHz(reading.frequencyHz)} МГц · ${archivedLevel(reading.level)} ${unitLabel(reading.unit)}"
        is RouteReading.MissingLevel -> "На ${showMHz(reading.frequencyHz)} МГц показание отсутствует."
        is RouteReading.OutsideRange -> if (receiver) "S-метр измерял другие частоты в этой точке."
            else "Частота вне диапазона привязанных спектров."
        null -> "В точке нет привязанного отсчёта."
    })
}

@Composable
private fun ArchivePages(page: Int, pages: Int, change: (Int) -> Unit) {
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
        TextButton(onClick = { change(page - 1) }, enabled = page > 0) { Text("Предыдущая часть") }
        Text("${page + 1} / $pages")
        TextButton(onClick = { change(page + 1) }, enabled = page + 1 < pages) { Text("Следующая часть") }
    }
}

@Composable
private fun ArchiveText(json: JSONObject, field: String, id: String) {
    val source = json as? FieldRowObject
    val local = if (source == null) json.optString(field) else ""
    val length = source?.textLength(field) ?: local.length
    val chars = 2048
    val pages = maxOf(1, (length - 1) / chars + 1)
    var page by rememberSaveable(id, pages) { mutableIntStateOf(0) }
    val first = page * chars
    val end = minOf(first + chars, length)
    if (pages > 1) ArchivePages(page, pages) { page = it }
    val visible = remember(json, field, page) {
        if (source != null) source.textPage(field, first, end)
        else {
            fun boundary(at: Int) = if (at in 1 until length && local[at].isLowSurrogate() && local[at-1].isHighSurrogate()) at + 1 else at
            local.substring(boundary(first), boundary(end))
        }
    }
    Text(visible)
}

@Composable
private fun SourceDetails(json: JSONObject) {
    val interval = json.getJSONObject("interval")
    Text(interval.getString("started_at") + " — " + interval.getString("ended_at"),
        style = MaterialTheme.typography.bodySmall)
    if (json.has("frequencies_hz")) {
        val frequencies = json.getJSONArray("frequencies_hz")
        val levels = json.getJSONArray("levels")
        val points = frequencies.length()
        val pageSize = 512
        val pages = (points - 1) / pageSize + 1
        var page by rememberSaveable(json.getString("id"), pages) { mutableIntStateOf(0) }
        val first = page * pageSize
        val count = minOf(pageSize, points - first)
        if (pages > 1) {
            Text("Исходные точки ${first + 1}–${first + count} из $points")
            ArchivePages(page, pages) { page = it }
        }
        val x = remember(json, first) { List(count) { frequencies.getLong(first + it) } }
        val y = remember(json, first) { List(count) { levels.getDouble(first + it) } }
        SpectrumPanel(x, y, json.getString("level_unit"), emptyList())
    } else {
        Text("${showMHz(json.getLong("frequency_hz"))} МГц  ·  ${json.getString("mode")}")
        val unit = json.getString("level_unit")
        val value = json.opt("level") as? Number
        val name = if (unit == "s_meter_count") "S-метр" else "Уровень"
        val reading = value?.let { "${archivedLevel(it.toDouble())} ${unitLabel(unit)}" } ?: "показание отсутствует"
        Text("$name  $reading", style = MaterialTheme.typography.titleMedium)
    }
    Text(json.getString("id"), fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
}
