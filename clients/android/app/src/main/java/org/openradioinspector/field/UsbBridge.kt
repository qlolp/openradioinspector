package org.openradioinspector.field

import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.ParcelFileDescriptor
import android.system.Os
import android.system.OsConstants
import com.hoho.android.usbserial.driver.CdcAcmSerialDriver
import com.hoho.android.usbserial.driver.Cp21xxSerialDriver
import com.hoho.android.usbserial.driver.UsbSerialPort
import java.io.Closeable
import java.io.EOFException
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

internal fun UsbDevice.fieldDevice(): FieldDevice? {
    val profiles = when (vendorId to productId) {
        0x0483 to 0x5740 -> listOf("tinysa")
        0x10c4 to 0xea60 -> listOf("rfexplorer", "ardv1")
        else -> return null
    }
    return FieldDevice(deviceId, "%04X:%04X · %s".format(vendorId, productId, deviceName), profiles)
}

/** One duplex socket carries the byte stream between the Android USB driver and the Rust adapter. */
internal class UsbBridge private constructor(
    private val port: UsbSerialPort,
    private val sockets: Array<ParcelFileDescriptor>,
) : Closeable {
    val nativeFd get() = sockets[0].fd
    private val peer get() = sockets[1].fileDescriptor
    private val stopped = AtomicBoolean(false)
    private val failure = AtomicReference<Throwable?>()
    private val pumps = mutableListOf<Thread>()

    private fun pump(name: String, copy: () -> Unit) = thread(name = name) {
        try {
            while (!stopped.get()) copy()
        } catch (e: Exception) {
            if (!stopped.get()) failure.compareAndSet(null, e)
            interruptIo()
        }
    }

    private fun start() {
        val rx = ByteArray(8192)
        pumps += pump("field-usb-read") {
            val count = port.read(rx, 250)
            var offset = 0
            while (offset < count) {
                val written = Os.write(peer, rx, offset, count - offset)
                if (written <= 0) throw EOFException("Native input closed")
                offset += written
            }
        }
        val tx = ByteArray(8192)
        pumps += pump("field-usb-write") {
            val count = Os.read(peer, tx, 0, tx.size)
            if (count <= 0) throw EOFException("Native output closed")
            port.write(tx, count, 2000)
        }
    }

    fun interruptIo() {
        stopped.set(true)
        runCatching { Os.shutdown(peer, OsConstants.SHUT_RDWR) }
    }

    fun checkTransport() {
        failure.get()?.let { throw IOException("USB transport failed: ${it.message}", it) }
    }

    override fun close() {
        interruptIo()
        try {
            port.close()
        } finally {
            pumps.forEach { it.join() }
            sockets.forEach { it.close() }
        }
    }

    companion object {
        fun open(manager: UsbManager, device: UsbDevice, config: FieldConfig): UsbBridge {
            require(device.fieldDevice()?.profiles?.contains(config.profile) == true) { "Unsupported USB device" }
            val baud = config.checkedBaud()
            val driver = if (config.profile == "tinysa") CdcAcmSerialDriver(device) else Cp21xxSerialDriver(device)
            val port = driver.ports.singleOrNull() ?: throw IOException("Expected one USB serial port")
            val connection = manager.openDevice(device) ?: throw IOException("USB device unavailable")
            try {
                port.open(connection)
                port.setParameters(baud, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE)
                if (config.profile == "tinysa") port.setDTR(true)
                val bridge = UsbBridge(port, ParcelFileDescriptor.createSocketPair())
                try {
                    bridge.start()
                    return bridge
                } catch (e: Throwable) {
                    bridge.close()
                    throw e
                }
            } catch (e: Throwable) {
                if (port.isOpen) runCatching { port.close() }
                connection.close()
                throw e
            }
        }
    }
}
