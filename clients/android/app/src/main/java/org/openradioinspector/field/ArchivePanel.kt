package org.openradioinspector.field

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

@Composable
internal fun ArchivePanel(state: FieldState, export: (String) -> Unit) {
    var selection by rememberSaveable { mutableStateOf<String?>(null) }
    val sessions = state.sessions.filter { !state.running || it != state.sessionId }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("Сохранённые сеансы", style = MaterialTheme.typography.titleMedium)
        if (sessions.isEmpty()) {
            Text("Завершённые сеансы появятся здесь после остановки записи.")
        } else {
            val selected = selection?.takeIf { it in sessions } ?: sessions.first()
            Choice("Сеанс", selected, sessions.map { it to it }, true) { selection = it }
            key(selected) { ArchiveDetail(selected, export) }
        }
        Text(state.status, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun ArchiveDetail(id: String, export: (String) -> Unit) {
    val root = LocalContext.current.filesDir
    val reader = remember(root, id) { SessionReview(root, id) }
    var kind by rememberSaveable { mutableStateOf(ReviewKind.TRACK) }
    var seq by rememberSaveable { mutableStateOf<Long?>(null) }
    var backwards by rememberSaveable { mutableStateOf(false) }
    var fixId by rememberSaveable { mutableStateOf<String?>(null) }
    var directSource by rememberSaveable { mutableStateOf<String?>(null) }
    var selectedId by rememberSaveable { mutableStateOf<String?>(null) }
    fun open(section: ReviewKind, filter: String? = null, source: String? = null) {
        kind = section
        seq = null
        backwards = false
        selectedId = null
        fixId = filter
        directSource = source
    }
    var info by remember { mutableStateOf<SessionInfo?>(null) }
    var page by remember(kind, seq, backwards, fixId, directSource) { mutableStateOf<ReviewPage?>(null) }
    var error by remember(kind, seq, backwards, fixId, directSource) { mutableStateOf<String?>(null) }
    LaunchedEffect(reader, kind, seq, backwards, fixId, directSource) {
        try {
            val session = info ?: withContext(Dispatchers.IO) { reader.info() }
            info = session
            val sourceId = directSource
            page = withContext(Dispatchers.IO) {
                if (sourceId != null) {
                    val source = reader.source(session.profile, sourceId)
                    ReviewPage(listOfNotNull(source?.let { ReviewEntry(0, it) }), false, false)
                } else reader.page(session.profile, kind, PageCursor(seq, backwards), fixId)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            error = "Не удалось прочитать сеанс. ${e.localizedMessage.orEmpty()}"
        }
    }
    info?.let { Text("${profileName(it.profile)}  ·  ${it.started}", style = MaterialTheme.typography.bodySmall) }
    OutlinedButton(onClick = { export(id) }, modifier = Modifier.fillMaxWidth()) { Text("Экспортировать .orifield…") }
    Choice("Просмотр", kind.title, ReviewKind.values().map { it to it.title }, true) { open(it) }
    if (fixId != null || directSource != null) TextButton(onClick = { open(ReviewKind.DATA) }) { Text("Все измерения") }
    val current = page
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
        if (kind == ReviewKind.TRACK) {
            val points = remember(entries) { entries.map(::reviewTrack) }
            TrackPlot(points, json.getString("id")) { selectedId = it }
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
        when (kind) {
            ReviewKind.TRACK -> {
                Text("${json.getDouble("latitude")}, ${json.getDouble("longitude")}", fontFamily = FontFamily.Monospace)
                if (!json.isNull("accuracy_m")) Text("Точность ±${level(json.getDouble("accuracy_m"))} м")
                Text(json.getString("time_utc"))
                Button(onClick = { open(ReviewKind.DATA, filter = json.getString("id")) }) { Text("Измерения в точке") }
            }
            ReviewKind.DATA -> SourceDetails(json)
            ReviewKind.NOTES, ReviewKind.GAPS -> {
                Text(json.optString(if (kind == ReviewKind.NOTES) "text" else "reason"))
                Text(json.getString("time_utc"))
                val sourceKey = listOf("sweep_id", "observation_id").firstOrNull { !json.isNull(it) }
                if (sourceKey != null) OutlinedButton(onClick = {
                    open(ReviewKind.DATA, source = json.getString(sourceKey))
                }) { Text("К исходному измерению") }
            }
        }
    }
}

@Composable
private fun SourceDetails(json: JSONObject) {
    val interval = json.getJSONObject("interval")
    Text(interval.getString("started_at") + " — " + interval.getString("ended_at"),
        style = MaterialTheme.typography.bodySmall)
    if (json.has("frequencies_hz")) {
        val frequencies = json.getJSONArray("frequencies_hz")
        val levels = json.getJSONArray("levels")
        SpectrumPanel(List(frequencies.length()) { frequencies.getLong(it) },
            List(levels.length()) { levels.getDouble(it) }, json.getString("level_unit"), emptyList())
    } else {
        Text("${showMHz(json.getLong("frequency_hz"))} МГц  ·  ${json.getString("mode")}")
        Text("S-метр  ${level(json.getDouble("level"))} отсч.", style = MaterialTheme.typography.titleMedium)
    }
    Text(json.getString("id"), fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
}
