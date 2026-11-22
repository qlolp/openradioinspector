package org.openradioinspector.field

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

class MainActivity : ComponentActivity() {
    private var service by mutableStateOf<FieldService?>(null)
    private var bound = false
    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            service = (binder as FieldService.LocalBinder).service
        }
        override fun onServiceDisconnected(name: ComponentName) { service = null }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { OperatorConsole(service) }
    }
    override fun onStart() {
        super.onStart()
        bound = bindService(Intent(this, FieldService::class.java), connection, Context.BIND_AUTO_CREATE)
    }
    override fun onStop() {
        if (bound) unbindService(connection)
        bound = false
        service = null
        super.onStop()
    }
}

internal fun exportDocument() =
    ActivityResultContracts.CreateDocument("application/vnd.openradioinspector.field+zip")

private val consoleColors = darkColorScheme(
    primary = Color(0xff82dac5), secondary = Color(0xffabc8dc),
    background = Color(0xff101820), surface = Color(0xff17212a),
    surfaceVariant = Color(0xff25323c), onSurface = Color(0xffe6edf1),
)

@Composable
private fun OperatorConsole(service: FieldService?) {
    MaterialTheme(colorScheme = consoleColors) {
        Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
            var tab by rememberSaveable { mutableIntStateOf(0) }
            var privacy by rememberSaveable { mutableStateOf(false) }
            var exportId by rememberSaveable { mutableStateOf<String?>(null) }
            var exportUri by rememberSaveable { mutableStateOf<String?>(null) }
            val export = rememberLauncherForActivityResult(exportDocument()) {
                exportUri = it?.toString()
                if (it == null) exportId = null
            }
            LaunchedEffect(service, exportId, exportUri) {
                val id = exportId
                val uri = exportUri
                if (id != null && uri != null && service != null) {
                    service.exportSession(id, Uri.parse(uri))
                    exportId = null
                    exportUri = null
                }
            }
            Column(Modifier.safeDrawingPadding().imePadding()) {
                Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("Полевой приём", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                    TextButton(onClick = { privacy = true }) { Text("Конфиденциальность") }
                }
                val holder = androidx.compose.runtime.saveable.rememberSaveableStateHolder()
                val consoleState = service?.state?.collectAsStateWithLifecycle()?.value
                if (service == null || consoleState == null) {
                    Text("Подключение к службе записи…", Modifier.padding(16.dp))
                } else {
                    val tabs = listOf("Прибор", "Сигнал", "Архив")
                    TabRow(selectedTabIndex = tab) {
                        tabs.forEachIndexed { index, text ->
                            Tab(selected = tab == index, onClick = { tab = index }, text = { Text(text) })
                        }
                    }
                    // The activity can return after rotation or a system permission dialog.
                    // Save form state independently of the currently displayed tab.
                    holder.SaveableStateProvider(tab) {
                        when (tab) {
                            0 -> SetupPanel(service, consoleState) { tab = 1 }
                            1 -> LivePanel(service, consoleState)
                            else -> ArchivePanel(consoleState) { id ->
                                exportId = id
                                export.launch("$id.orifield")
                            }
                        }
                    }
                }
            }
            if (privacy) FieldPrivacyPolicy { privacy = false }
        }
    }
}

@Composable
private fun FieldPrivacyPolicy(close: () -> Unit) {
    val context = LocalContext.current
    val paragraphs = remember(context) {
        val link = Regex("""\[([^\]]+)]\([^)]+\)""")
        context.assets.open("privacy.md").bufferedReader().use { it.readText() }
            .trim().split(Regex("""\r?\n\s*\r?\n""")).drop(1)
            .map { paragraph -> link.replace(paragraph) { it.groupValues[1] } }
    }
    AlertDialog(onDismissRequest = close, title = { Text("Политика конфиденциальности") }, text = {
        Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            for (paragraph in paragraphs) {
                if (paragraph.startsWith("#")) {
                    Text(paragraph.trimStart('#', ' '), style = MaterialTheme.typography.titleMedium)
                } else {
                    Text(paragraph)
                }
            }
        }
    }, confirmButton = { TextButton(onClick = close) { Text("Закрыть") } })
}

@Composable
private fun SetupPanel(service: FieldService, state: FieldState, onStarted: () -> Unit) {
    var devices by remember { mutableStateOf(emptyList<FieldDevice>()) }
    fun refresh() { devices = service.devices() }
    LaunchedEffect(service) { refresh() }
    var deviceId by rememberSaveable { mutableStateOf<Int?>(null) }
    var profile by rememberSaveable { mutableStateOf("") }
    var kind by rememberSaveable { mutableStateOf("") }
    var start by rememberSaveable { mutableStateOf("100") }
    var followWindow by rememberSaveable { mutableStateOf(true) }
    var rfStart by rememberSaveable { mutableStateOf("433") }
    var rfEnd by rememberSaveable { mutableStateOf("434,11") }
    var end by rememberSaveable { mutableStateOf("102") }
    var step by rememberSaveable { mutableStateOf("10000") }
    var frequencies by rememberSaveable { mutableStateOf("") }
    var points by rememberSaveable { mutableStateOf("101") }
    var baud by rememberSaveable { mutableIntStateOf(115200) }
    var rbw by rememberSaveable { mutableStateOf("10000") }
    var settle by rememberSaveable { mutableStateOf("200") }
    var mode by rememberSaveable { mutableStateOf("FM") }
    var gps by rememberSaveable { mutableStateOf(false) }
    var continuous by rememberSaveable { mutableStateOf(false) }
    var error by rememberSaveable { mutableStateOf("") }
    val context = LocalContext.current
    val location = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        gps = it[Manifest.permission.ACCESS_FINE_LOCATION] == true
        if (!gps) error = "Для GPS-трека разрешите точное местоположение"
    }
    val notification = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    val device = devices.firstOrNull { it.id == deviceId }
    val enabled = !state.running
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween) {
            Text("USB-прибор", style = MaterialTheme.typography.titleMedium)
            TextButton(onClick = { refresh() }) { Text("Обновить") }
        }
        Choice("Подключённое устройство", device?.label ?: "Выбрать устройство",
            devices.map { it.id to it.label }, enabled) { id ->
            deviceId = id
            val profiles = devices.first { it.id == id }.profiles
            profile = profiles.singleOrNull().orEmpty()
            kind = kindsFor(profile).firstOrNull().orEmpty()
        }
        if (devices.isEmpty()) Text("Подключите прибор через USB OTG и обновите список.")
        if (device != null) Choice("Профиль", profileName(profile).ifEmpty { "Выбрать профиль" },
            device.profiles.map { it to profileName(it) }, enabled) {
            profile = it
            kind = kindsFor(it).firstOrNull().orEmpty()
        }
        if (profile.isNotEmpty() && device != null) {
            Choice("Режим измерения", scanLabels[kind].orEmpty(),
                kindsFor(profile).map { it to scanLabels.getValue(it) }, enabled) { kind = it }
            if (kind == "rf_explorer_sweep") {
                Toggle("Использовать текущее окно прибора", followWindow, enabled) { followWindow = it }
                if (!followWindow) Text(
                    "Границы задаются с шагом 1 кГц в пределах активной платы. Ширина окна в Гц " +
                    "должна делиться без остатка на число интервалов (число точек прибора минус один). " +
                    "Для 112 точек подходит окно 433–434,11 МГц.",
                    style = MaterialTheme.typography.bodySmall)
            }
            if (kind == "ardv1_list") {
                OutlinedTextField(frequencies, { frequencies = it }, enabled = enabled,
                    label = { Text("Частоты, МГц") }, supportingText = { Text("Разделитель — пробел, строка или ;") },
                    modifier = Modifier.fillMaxWidth())
            } else if (kind != "rf_explorer_sweep" || !followWindow) {
                val rf = profile == "rfexplorer"
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    NumberField(if (kind == "ardv1_single") "Частота, МГц" else "От, МГц", if (rf) rfStart else start,
                        { if (rf) rfStart = it else start = it }, enabled, Modifier.weight(1f))
                    if (kind != "ardv1_single") NumberField("До, МГц", if (rf) rfEnd else end,
                        { if (rf) rfEnd = it else end = it }, enabled, Modifier.weight(1f))
                }
            }
            if (kind == "rf_explorer_survey") Text("Сетка 10 кГц · окно 112 точек")
            if (kind == "ardv1_range") NumberField("Шаг, Гц", step, { step = it }, enabled)
            if (profile == "tinysa") {
                NumberField("Точки спектра (2–290)", points, { points = it }, enabled)
                Choice("Полоса разрешения", "${rbw.toInt() / 1000} кГц",
                    listOf("10000" to "10 кГц", "30000" to "30 кГц"), enabled) { rbw = it }
            }
            if (profile == "ardv1") {
                val modes = listOf("FM", "AM", "AUTO", "DSTAR", "YSF", "ALINCO", "NXDN", "P25", "DPMR", "DMR")
                val rates = listOf(9600, 19200, 38400, 57600, 115200)
                Choice("Скорость порта", "$baud бод", rates.map { it to "$it бод" }, enabled) { baud = it }
                if (kind == "ardv1_range") Text("Демодуляция FM")
                else Choice("Демодуляция", mode, modes.map { it to it }, enabled) { mode = it }
                NumberField("Установление частоты, мс", settle, { settle = it }, enabled)
            }
            Toggle("Повторять проходы", continuous, enabled) { continuous = it }
        }
        Toggle("Записывать GPS-трек", gps, enabled) {
            if (!it) gps = false
            else if (context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) gps = true
            else location.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION))
        }
        if (Build.VERSION.SDK_INT >= 33 && context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            TextButton(onClick = { notification.launch(Manifest.permission.POST_NOTIFICATIONS) }) { Text("Разрешить уведомления о записи") }
        }
        if (error.isNotEmpty()) Text(error, color = MaterialTheme.colorScheme.error)
        Button(enabled = enabled && device != null && profile in device.profiles && kind.isNotEmpty(),
            modifier = Modifier.fillMaxWidth(), onClick = {
                try {
                    val rf = profile == "rfexplorer"
                    val config = ScanForm(deviceId, profile, kind, if (rf) rfStart else start,
                        if (rf) rfEnd else end, step, frequencies, points, rbw, settle, mode,
                        continuous, gps, followWindow).config()
                    service.start(if (profile == "ardv1") config.copy(baud = baud) else config)
                    error = ""
                    onStarted()
                } catch (e: IllegalArgumentException) {
                    error = e.message ?: "Проверьте параметры"
                }
            }) { Text(if (state.running) "Идёт запись" else "Начать приём") }
        Text("Система запросит доступ к выбранному USB-прибору при запуске.",
            style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
internal fun <T> Choice(label: String, value: String, options: List<Pair<T, String>>,
    enabled: Boolean, select: (T) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        OutlinedButton({ open = true }, enabled = enabled && options.isNotEmpty(),
            modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.weight(1f)) {
                Text(label, style = MaterialTheme.typography.labelSmall)
                Text(value.ifEmpty { "Выбрать" }, style = MaterialTheme.typography.bodyLarge)
            }
            Text("▾")
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            options.forEach { (key, title) ->
                DropdownMenuItem(text = { Text(title) }, onClick = { open = false; select(key) })
            }
        }
    }
}

@Composable
private fun NumberField(title: String, value: String, change: (String) -> Unit,
    enabled: Boolean, modifier: Modifier = Modifier.fillMaxWidth()) {
    OutlinedTextField(value, change, modifier = modifier, label = { Text(title) },
        enabled = enabled, singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal))
}

@Composable
private fun Toggle(label: String, checked: Boolean, enabled: Boolean, change: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label)
        Switch(checked = checked, onCheckedChange = change, enabled = enabled)
    }
}

@Composable
private fun LivePanel(service: FieldService, state: FieldState) {
    var note by rememberSaveable(state.sessionId) { mutableStateOf("") }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(state.status, style = MaterialTheme.typography.titleMedium)
                if (state.total > 0) Text("${state.progress} / ${state.total}", fontFamily = FontFamily.Monospace)
            }
            if (state.running) Button(onClick = service::stop) { Text("Стоп") }
        }
        if (state.total > 0) LinearProgressIndicator(
            progress = { (state.progress.toDouble() / state.total).coerceIn(0.0, 1.0).toFloat() },
            modifier = Modifier.fillMaxWidth())
        if (state.receiverSamples.isNotEmpty()) {
            val samples = state.receiverSamples
            val last = samples.last()
            Text("S-метр", style = MaterialTheme.typography.titleMedium)
            Text("${showMHz(last.frequencyHz)} МГц  ·  ${level(last.value)} отсч.", fontFamily = FontFamily.Monospace)
            SeriesPlot(samples.indices.map { it.toLong() }, samples.map { it.value }, "Отсчёты S-метра по порядку поступления")
            Text("По порядку измерения  ·  ${samples.size} отсч.", style = MaterialTheme.typography.bodySmall)
            Text(last.utc, style = MaterialTheme.typography.bodySmall)
        } else if (state.values.isNotEmpty()) {
            SpectrumPanel(state.frequenciesHz, state.values, state.unit, state.waterfall)
        } else {
            Text("Ожидание измерений", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (state.gpsRequested || state.track.isNotEmpty()) {
            Text("Трек GPS", style = MaterialTheme.typography.titleMedium)
            if (state.latitude != null && state.longitude != null) Text("${state.latitude}, ${state.longitude}")
            else if (state.running) Text("Ожидание свежих координат")
            TrackPlot(state.track.map { TrackPoint(it.id, it.latitude, it.longitude, it.utc, it.segmentId, fresh = it.fresh) })
            state.track.lastOrNull()?.let { Text(it.utc, style = MaterialTheme.typography.bodySmall) }
        }
        if (state.running && state.sessionId != null) {
            OutlinedTextField(note, { note = it }, label = { Text("Заметка оператора") },
                isError = note.length > 4096, supportingText = {
                    when {
                        note.length > 4096 -> Text("Максимум 4096 символов")
                        state.measurementId == null -> Text("Привязка к сеансу")
                        else -> Text("Исходное измерение ${state.measurementId}", style = MaterialTheme.typography.bodySmall)
                    }
                },
                modifier = Modifier.fillMaxWidth())
            OutlinedButton(enabled = note.isNotBlank() && note.length <= 4096, onClick = {
                if (state.submitDisplayedNote(note, service::addNote)) note = ""
            }) { Text("Добавить заметку") }
        }
    }
}
