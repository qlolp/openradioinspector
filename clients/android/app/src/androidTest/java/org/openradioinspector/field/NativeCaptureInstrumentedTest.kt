package org.openradioinspector.field

import android.os.ParcelFileDescriptor
import android.system.Os
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class NativeCaptureInstrumentedTest {
    @Test(timeout = 20_000)
    fun packagedNativeAdapterDecodesFixtureAcrossAndroidSocket() {
        val (client, peer) = ParcelFileDescriptor.createSocketPair()
        client.use {
            peer.use {
                val handle = NativeCapture.open(client.fd, """{"kind":"rf_explorer_sweep"}""")
                try {
                    val header = "RF Explorer 01.34 27-Apr-22 01.04.05\r\n" +
                        "#C2-M:003,005,01.34\r\n" +
                        "#C2-F:0100000,0010000,0000,-118,0112,1,000,0015000,2700000,0600000,00015,0000,004\r\n\$Sp"
                    val frame = header.toByteArray() + ByteArray(112) { 149.toByte() } + "\r\n".toByteArray()
                    var offset = 0
                    while (offset < frame.size) {
                        val count = Os.write(peer.fileDescriptor, frame, offset, frame.size - offset)
                        check(count > 0)
                        offset += count
                    }
                    val sweep = JSONObject(requireNotNull(NativeCapture.next(handle)))
                    val frequencies = sweep.getJSONArray("frequencies_hz")
                    assertEquals(112, frequencies.length())
                    assertEquals(100_000_000L, frequencies.getLong(0))
                    assertEquals(101_110_000L, frequencies.getLong(111))
                    assertEquals(-74.5, sweep.getJSONArray("levels").getDouble(111), 0.0)
                } finally {
                    NativeCapture.close(handle)
                }
            }
        }
    }
}
