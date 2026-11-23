package org.openradioinspector.field

import android.database.sqlite.SQLiteFullException
import android.net.Uri
import android.os.CancellationSignal
import android.os.ParcelFileDescriptor
import android.system.ErrnoException
import android.system.OsConstants
import androidx.activity.compose.BackHandler
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.platform.LocalContext
import kotlinx.coroutines.*
import java.io.InputStream
import java.util.concurrent.atomic.AtomicReference

@Composable
internal fun OpenFieldDocument(uri: String, close: () -> Unit) {
    val context = LocalContext.current
    var document by remember { mutableStateOf<FieldDocument?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    BackHandler(onBack = close)
    LaunchedEffect(uri) {
        var owned: FieldDocument? = null
        try {
            coroutineScope {
                val signal = CancellationSignal()
                val stream = AtomicReference<InputStream?>()
                val loading = async(Dispatchers.IO) {
                    val fd = requireNotNull(context.contentResolver.openFileDescriptor(Uri.parse(uri), "r", signal)) {
                        "Хранилище не предоставило файл"
                    }
                    ParcelFileDescriptor.AutoCloseInputStream(fd).use { input ->
                        stream.set(input)
                        ensureActive()
                        owned = FieldDocument.open(context.cacheDir, input) { ensureActive() }
                    }
                }
                try { loading.await() } finally {
                    signal.cancel()
                    runCatching { stream.getAndSet(null)?.close() }
                }
            }
            document = owned
            awaitCancellation()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            val noSpace = e is SQLiteFullException || generateSequence<Throwable>(e) { it.cause }
                .any { it is ErrnoException && it.errno == OsConstants.ENOSPC }
            val detail = when {
                noSpace -> "Недостаточно места для временной копии и индекса выезда."
                e is SecurityException -> "Хранилище не разрешило чтение."
                e is IllegalArgumentException -> e.message?.takeUnless { it == "Failed requirement." }
                else -> null
            }
            error = "Не удалось открыть файл выезда. " + (detail ?: "Проверьте доступ к файлу и его целостность в формате V1.")
        } finally {
            withContext(NonCancellable + Dispatchers.IO) { owned?.close() }
        }
    }
    val opened = document
    TextButton(onClick = close) { Text(if (opened == null && error == null) "Отменить открытие" else "Закрыть файл") }
    if (error != null) Text(error.orEmpty(), color = MaterialTheme.colorScheme.error)
    else if (opened == null) Text("Проверка файла выезда…")
    else {
        Text(opened.sessionId, style = MaterialTheme.typography.titleMedium)
        var index by rememberSaveable { mutableIntStateOf(0) }
        val channel = opened.channels.getOrElse(index) { opened.channels.first() }
        if (opened.channels.size > 1) Choice("Поток измерений", channel.title,
            opened.channels.mapIndexed { i, source -> i to source.title }, true) { index = it }
        else Text(channel.title, style = MaterialTheme.typography.bodySmall)
        key(channel) { ArchiveDetail(remember(opened, channel) { opened.reader(channel) }) }
    }
}
