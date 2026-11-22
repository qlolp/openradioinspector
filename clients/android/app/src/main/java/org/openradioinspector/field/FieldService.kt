package org.openradioinspector.field

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.net.Uri
import android.os.Binder
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import java.io.File
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlin.concurrent.thread

class FieldService : Service() {
    inner class LocalBinder : Binder() { val service get() = this@FieldService }

    /** All lifecycle changes run on main. Only bridge/cancellation cross into the USB worker. */
    private data class Stop(val message: String, val operator: Boolean)

    private class Capture(val config: FieldConfig, val device: UsbDevice) {
        val id = UUID.randomUUID().toString()
        val stopReason = AtomicReference<Stop?>()
        @Volatile var bridge: UsbBridge? = null
        var archive: FieldArchive? = null // archive executor only
        var wake: PowerManager.WakeLock? = null
        var listener: LocationListener? = null
    }

    companion object {
        private const val USB_PERMISSION = "org.openradioinspector.field.USB_PERMISSION"
        private const val CHANNEL = "field-capture"
        private const val NOTIFICATION = 41
        private const val STOP = "org.openradioinspector.field.STOP"
        // Shared across service instances so recovery can never interleave two SQLite writers.
        private val writer = Executors.newSingleThreadExecutor()
        private val deviceOwner = AtomicReference<Capture?>()
        private val openSessions = mutableSetOf<String>() // writer thread only
    }

    private val events = MutableStateFlow(FieldState())
    val state: StateFlow<FieldState> get() = events
    private val main = Handler(Looper.getMainLooper())
    private val root get() = File(filesDir, "field")
    private val usb get() = requireNotNull(getSystemService(UsbManager::class.java)) { "USB Host недоступен" }
    private val location get() = requireNotNull(getSystemService(LocationManager::class.java)) { "GPS недоступен" }
    private var active: Capture? = null
    private var pending: Capture? = null
    @Volatile private var destroyed = false

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == USB_PERMISSION) {
                val request = pending ?: return
                if (intent.getStringExtra("request") != request.id) return
                pending = null
                if (usb.hasPermission(request.device)) begin(request)
                else idle("Доступ к USB не предоставлен")
            } else if (intent.action == UsbManager.ACTION_USB_DEVICE_DETACHED) {
                @Suppress("DEPRECATION") val device = intent.getParcelableExtra<UsbDevice>(UsbManager.EXTRA_DEVICE) ?: return
                val request = pending
                if (request?.device?.deviceId == device.deviceId) {
                    pending = null
                    idle("USB-прибор отключён")
                }
                active?.takeIf { it.device.deviceId == device.deviceId }?.let {
                    cancel(it, "USB-прибор отключён", immediate = true)
                }
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        requireNotNull(getSystemService(NotificationManager::class.java)).createNotificationChannel(
            NotificationChannel(CHANNEL, "Полевые измерения", NotificationManager.IMPORTANCE_LOW))
        val filter = IntentFilter(USB_PERMISSION).apply { addAction(UsbManager.ACTION_USB_DEVICE_DETACHED) }
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(receiver, filter, RECEIVER_NOT_EXPORTED)
        else registerReceiver(receiver, filter)
        writer.execute {
            val sessions = FieldArchive.list(root)
            val errors = sessions.filter { it !in openSessions }.mapNotNull {
                runCatching { FieldArchive.recover(FieldArchive.database(root, it)) }.exceptionOrNull()
            }
            events.update { it.copy(sessions = sessions, status =
                if (errors.isEmpty()) it.status else "Ошибка восстановления архива: ${errors.first().message}") }
        }
    }

    override fun onBind(intent: Intent): IBinder = LocalBinder()
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP && intent.getStringExtra("session") == active?.id) stop()
        return START_NOT_STICKY
    }

    fun devices(): List<FieldDevice> = getSystemService(UsbManager::class.java)?.deviceList?.values.orEmpty()
        .mapNotNull { it.fieldDevice() }.sortedBy { it.id }

    fun start(config: FieldConfig) {
        if (active != null || pending != null) return
        try {
            config.checkedBaud()
            val device = usb.deviceList.values.singleOrNull { it.deviceId == config.deviceId }
                ?: error("USB-прибор отключён")
            require(device.fieldDevice()?.profiles?.contains(config.profile) == true) { "Недоступный профиль USB" }
            if (config.gps) require(checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED) { "Для GPS разрешите точное местоположение" }
            // Keep ownership while Android displays its USB grant dialog and the activity unbinds.
            startService(Intent(this, FieldService::class.java))
            val request = Capture(config, device)
            if (usb.hasPermission(device)) begin(request) else {
                pending = request
                events.update { FieldState(running = true, gpsRequested = config.gps, sessions = it.sessions, status = "Ожидание разрешения USB") }
                val intent = Intent(USB_PERMISSION).setPackage(packageName).putExtra("request", request.id)
                val grant = PendingIntent.getBroadcast(this, request.id.hashCode(), intent,
                    PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_IMMUTABLE)
                usb.requestPermission(device, grant)
            }
        } catch (e: Exception) {
            pending = null
            idle("Ошибка запуска: ${e.message}")
        }
    }

    private fun idle(message: String) {
        events.update { it.copy(running = false, status = message) }
        stopSelf()
    }

    private fun notification(job: Capture): Notification {
        val show = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, job.id.hashCode(),
            Intent(this, FieldService::class.java).setAction(STOP).putExtra("session", job.id),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_sync).setContentTitle("Полевые измерения")
            .setContentText(job.config.profile).setOngoing(true).setContentIntent(show)
            .addAction(Notification.Action.Builder(null, "Остановить", stop).build()).build()
    }

    @SuppressLint("WakelockTimeout")
    private fun begin(job: Capture) {
        if (!deviceOwner.compareAndSet(null, job)) {
            idle("Ожидание закрытия предыдущего сеанса")
            return
        }
        active = job
        try {
            val types = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE or
                (if (job.config.gps) ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION else 0)
            startForeground(NOTIFICATION, notification(job), types)
            job.wake = requireNotNull(getSystemService(PowerManager::class.java))
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ori:field-capture").apply { acquire() }
            events.update { FieldState(running = true, gpsRequested = job.config.gps, status = "Открытие прибора", sessions = it.sessions) }
            thread(name = "field-capture") { capture(job) }
        } catch (e: Exception) {
            deviceOwner.compareAndSet(job, null)
            cleanup(job, "Ошибка запуска: ${e.message}")
        }
    }

    private fun <T> write(action: () -> T): T = try {
        writer.submit(Callable { action() }).get()
    } catch (e: ExecutionException) { throw (e.cause ?: e) }

    private fun capture(job: Capture) {
        var problem: Throwable? = null
        try {
            write {
                job.archive = FieldArchive.create(root, job.id, requireNotNull(job.device.fieldDevice()), job.config)
                openSessions.add(job.id)
                events.update { it.copy(sessionId = job.id, sessions = listOf(job.id) + it.sessions) }
            }
            main.post {
                if (active === job && job.stopReason.get() == null && job.config.gps) startGps(job)
            }
            if (job.stopReason.get() == null) UsbBridge.open(usb, job.device, job.config).use { bridge ->
                job.bridge = bridge
                if (job.stopReason.get() != null) return@use
                val handle = NativeCapture.open(bridge.nativeFd, job.config.nativePlan().toString())
                try {
                    while (job.stopReason.get() == null) {
                        val raw = NativeCapture.next(handle) ?: break
                        val sample = write { requireNotNull(job.archive).measurement(raw, job.config) }
                        events.update { current ->
                            val previous = if (sample.frequencies == current.frequenciesHz) current.waterfall else emptyList()
                            current.copy(status = "Идёт запись", frequenciesHz = sample.frequencies,
                                values = sample.values, unit = sample.unit, sequence = current.sequence + 1,
                                waterfall = if (sample.receiver == null) (previous + listOf(sample.values)).takeLast(64) else emptyList(),
                                receiverSamples = sample.receiver?.let { (current.receiverSamples + it).takeLast(256) }
                                    ?: current.receiverSamples,
                                progress = sample.progress, total = sample.total)
                        }
                        if (!job.config.continuous && job.config.passComplete(sample.progress, sample.total)) break
                    }
                } finally {
                    try {
                        NativeCapture.close(handle)
                    } finally {
                        bridge.checkTransport()
                    }
                }
            }
        } catch (e: Exception) {
            problem = e
        } catch (e: LinkageError) {
            problem = e
        } finally {
            job.bridge = null
            val requested = job.stopReason.get()
            var reason = when {
                problem == null -> requested?.message
                requested == null -> "Ошибка записи: ${problem.message}"
                else -> "${requested.message}. Ошибка обмена: ${problem.message}"
            }
            try {
                write {
                    try {
                        job.archive?.use { it.finish(if (requested?.operator == true && problem == null) null else reason) }
                    } finally {
                        job.archive = null
                        openSessions.remove(job.id)
                    }
                }
            } catch (e: Exception) {
                reason = "Ошибка закрытия архива: ${e.message}"
            } finally {
                deviceOwner.compareAndSet(job, null)
            }
            val message = reason ?: "Запись завершена"
            main.post { cleanup(job, message) }
        }
    }

    /** Record an operator or location event in the same commit order as measurements. */
    private fun record(job: Capture, action: (FieldArchive) -> Unit) {
        writer.execute {
            val archive = job.archive ?: return@execute
            try {
                action(archive)
            } catch (e: Exception) {
                main.post { if (active === job) cancel(job, "Ошибка архива: ${e.message}", immediate = true) }
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun startGps(job: Capture) {
        val listener = object : LocationListener {
            override fun onLocationChanged(fix: Location) {
                if (active !== job || job.stopReason.get() != null) return
                record(job) { store ->
                    val point = store.gps(fix)
                    events.update { it.copy(latitude = point.latitude.takeIf { point.fresh },
                        longitude = point.longitude.takeIf { point.fresh },
                        track = (it.track + point).takeLast(512)) }
                }
            }
            override fun onProviderDisabled(provider: String) {
                if (active === job) record(job) { archive ->
                    archive.gpsDisabled()
                    events.update { it.copy(latitude = null, longitude = null) }
                }
            }
            override fun onProviderEnabled(provider: String) {}
            @Deprecated("Legacy callback") override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
        }
        try {
            job.listener = listener
            val checkFreshness = object : Runnable {
                override fun run() {
                    if (active !== job || job.stopReason.get() != null) return
                    record(job) { archive ->
                        if (archive.gpsTimeout()) events.update { it.copy(latitude = null, longitude = null) }
                    }
                    main.postDelayed(this, 1000)
                }
            }
            main.postDelayed(checkFreshness, 1000)
            location.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1_000L, 0f, listener, Looper.getMainLooper())
            if (!location.isProviderEnabled(LocationManager.GPS_PROVIDER)) listener.onProviderDisabled(LocationManager.GPS_PROVIDER)
        } catch (e: Exception) {
            cancel(job, "Не удалось включить GPS: ${e.message}", immediate = true)
        }
    }

    fun addNote(text: String) {
        val job = active ?: return
        if (state.value.sessionId != job.id || job.stopReason.get() != null) return
        if (text.isBlank() || text.length > 4096) {
            events.update { it.copy(status = "Заметка должна содержать от 1 до 4096 символов") }
            return
        }
        record(job) { it.note(text.trim()) }
    }

    fun stop() {
        if (pending != null) {
            pending = null
            idle("Запрос USB отменён")
        }
        active?.let { cancel(it, "Запись остановлена оператором", immediate = false, operator = true) }
    }

    private fun cancel(job: Capture, reason: String, immediate: Boolean, operator: Boolean = false) {
        job.stopReason.updateAndGet { current ->
            if (current == null || (current.operator && !operator)) Stop(reason, operator) else current
        }
        events.update { it.copy(status = reason) }
        if (immediate) job.bridge?.interruptIo()
        else main.postDelayed({ if (active === job) job.bridge?.interruptIo() }, 3000)
    }

    private fun cleanup(job: Capture, message: String) {
        if (active !== job) return
        job.listener?.let { runCatching { location.removeUpdates(it) } }
        job.listener = null
        if (job.wake?.isHeld == true) job.wake?.release()
        active = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        events.update { it.copy(running = false, status = message) }
        if (!destroyed) stopSelf()
    }

    fun exportSession(sessionId: String, destination: Uri) {
        if (sessionId == active?.id) {
            events.update { it.copy(status = "Завершите запись перед экспортом") }
            return
        }
        thread(name = "field-export") {
            val result = runCatching {
                FieldExporter.export(this, FieldArchive.database(root, sessionId), destination)
            }
            events.update { it.copy(status = result.fold({ "Архив экспортирован" }, { e -> "Ошибка экспорта: ${e.message}" })) }
        }
    }

    override fun onDestroy() {
        destroyed = true
        pending = null
        unregisterReceiver(receiver)
        active?.let {
            cancel(it, "Служба записи остановлена системой", immediate = true)
            cleanup(it, "Служба записи остановлена системой")
        }
        super.onDestroy()
    }
}
