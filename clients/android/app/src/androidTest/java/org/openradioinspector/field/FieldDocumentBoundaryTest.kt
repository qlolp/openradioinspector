package org.openradioinspector.field

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.io.OutputStream
import java.nio.file.Files
import java.security.DigestOutputStream
import java.security.MessageDigest
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

@RunWith(AndroidJUnit4::class)
class FieldDocumentBoundaryTest {
    private val marker = "@padding@"
    private val suffix = "𝄞конец"
    private val block = ByteArray(32 * 1024) { 'a'.code.toByte() }

    private fun synthetic() = checkNotNull(javaClass.getResourceAsStream("/field-archive-v1.orifield"))
    private fun cache(block: (File) -> Unit) {
        val parent = InstrumentationRegistry.getInstrumentation().targetContext.cacheDir
        val root = Files.createTempDirectory(parent.toPath(), "field-boundary-test").toFile()
        try { block(root) } finally { root.deleteRecursively() }
    }

    private fun padded(output: OutputStream, template: String, count: Int, tail: String = suffix) {
        require(template.contains(marker))
        output.write(template.substringBefore(marker).toByteArray())
        var left = count
        while (left > 0) {
            val n = minOf(left, block.size)
            output.write(block, 0, n)
            left -= n
        }
        output.write(tail.toByteArray())
        output.write(template.substringAfter(marker).toByteArray())
    }

    private fun paddingFor(template: String) =
        FieldExporter.maxLine - 1 - template.replace(marker, suffix).toByteArray().size

    private data class BoundaryFile(val archive: File, val measurementHash: ByteArray, val gpsChars: Int)

    // Write the limit-size lines and ZIP straight to disk. The test itself needs no large strings,
    // byte arrays or in-memory copies of the archive and can run under a constrained heap.
    private fun prepare(root: File, differentTrack: Boolean): BoundaryFile {
        val small = linkedMapOf<String, String>()
        ZipInputStream(synthetic()).use { zip ->
            while (true) {
                val entry = zip.nextEntry ?: break
                small[entry.name] = zip.readBytes().toString(Charsets.UTF_8)
            }
        }
        val source = small.getValue("measurements.ndjson").lines()
        val sweep = source[1].replace("\"settings\":{", "\"settings\":{\"diagnostic\":\"$marker\",")
        val events = small.getValue("events.ndjson").lines()
        val gps = events[0].replace("\"accuracy_m\":5.5", "\"accuracy_m\":5.5,\"detail\":\"$marker\"")
        val note = events[1].replace("Synthetic marker", marker)
        val track = small.getValue("track.geojson").lines()
        val feature = track[1].replace("\"accuracy_m\":5.5", "\"accuracy_m\":5.5,\"detail\":\"$marker\"")
        val n = paddingFor(feature)

        File(root, "measurements.ndjson").outputStream().buffered().use {
            it.write((source[0] + "\n").toByteArray())
            padded(it, sweep, paddingFor(sweep))
            it.write(("\n" + source[2] + "\n").toByteArray())
        }
        File(root, "events.ndjson").outputStream().buffered().use {
            padded(it, gps, n)
            it.write(10)
            padded(it, note, paddingFor(note))
            it.write(("\n" + events[2] + "\n").toByteArray())
        }
        File(root, "track.geojson").outputStream().buffered().use {
            it.write((track[0] + "\n").toByteArray())
            padded(it, feature, n, if (differentTrack) "𝄞конех" else suffix)
            it.write(("\n" + track[2] + "\n").toByteArray())
        }
        val sink = object : OutputStream() {
            override fun write(b: Int) {}
            override fun write(bytes: ByteArray, off: Int, len: Int) {}
        }
        val measured = MessageDigest.getInstance("SHA-256")
        val measurement = sweep.substringAfter("\"measurement\":").dropLast(1)
        padded(DigestOutputStream(sink, measured), measurement, paddingFor(sweep))
        val manifest = JSONObject(small.getValue(fieldManifest))
        val zipFile = File(root, "boundary.orifield")
        ZipOutputStream(zipFile.outputStream().buffered()).use { zip ->
            for (name in fieldPayloads + fieldManifest) {
                val file = File(root, name)
                if (name == fieldManifest) file.writeText(manifest.toString())
                val crc = CRC32()
                val sha = MessageDigest.getInstance("SHA-256")
                file.inputStream().buffered().use { input ->
                    val chunk = ByteArray(8192)
                    while (true) {
                        val length = input.read(chunk)
                        if (length < 0) break
                        sha.update(chunk, 0, length)
                        crc.update(chunk, 0, length)
                    }
                }
                if (name != fieldManifest) manifest.getJSONObject("files").getJSONObject(name)
                    .put("bytes", file.length()).put("sha256", sha.digest().joinToString("") { "%02x".format(it) })
                zip.putNextEntry(ZipEntry(name).apply {
                    method = ZipEntry.STORED
                    size = file.length()
                    compressedSize = size
                    this.crc = crc.value
                })
                file.inputStream().buffered().use { it.copyTo(zip) }
                zip.closeEntry()
                check(file.delete())
            }
        }
        return BoundaryFile(zipFile, measured.digest(), n + suffix.length)
    }

    @Test fun fullV1LinesVerifyAndOpenTheExactLinkedSourceAndLastTextPage() = cache { root ->
        val data = prepare(root, false)
        data.archive.inputStream().use { input -> FieldDocument.open(root, input).use { doc ->
            val channel = doc.channels.single { !it.receiver }
            val reader = doc.reader(channel)
            val route = reader.page(channel.profile, ReviewKind.TRACK, PageCursor(), null, 100_000_002L)
            val gps = route.items.single().json as FieldRowObject
            assertEquals(data.gpsChars, gps.textLength("detail"))
            assertEquals(suffix, gps.textPage("detail", data.gpsChars - suffix.length, data.gpsChars))
            val sample = route.routeLevels.getValue("fix-1") as RouteReading.Measured
            assertEquals((-0.0).toRawBits(), sample.level.toRawBits())
            val source = reader.source(channel.profile, sample.sourceId) as FieldRowObject
            val digest = MessageDigest.getInstance("SHA-256")
            source.writeOriginal(DigestOutputStream(object : OutputStream() {
                override fun write(b: Int) {}
                override fun write(b: ByteArray, off: Int, len: Int) {}
            }, digest))
            assertArrayEquals(data.measurementHash, digest.digest())
            val note = reader.page(channel.profile, ReviewKind.NOTES, PageCursor(), null).items.single().json as FieldRowObject
            val end = note.textLength("text")
            assertEquals(suffix, note.textPage("text", end - suffix.length, end))
            assertEquals("𝄞", note.textPage("text", end - suffix.length, end - suffix.length + 1))
            assertEquals("", note.textPage("text", end - suffix.length + 1, end - suffix.length + 2))
        } }
        assertFalse(File(root, "field-document-review").exists())
    }

    @Test fun differentFinalGpsValueWithValidMemberHashesRejectsOnlyTheNewDocument() = cache { root ->
        val changed = prepare(root, true)
        synthetic().use { existing -> FieldDocument.open(root, existing).use { doc ->
            val error = assertThrows(IllegalArgumentException::class.java) {
                changed.archive.inputStream().use { FieldDocument.open(root, it).close() }
            }
            assertTrue(error.message.orEmpty().contains("GPS"))
            assertEquals(1, File(root, "field-document-review").listFiles()!!.size)
            val channel = doc.channels.single { !it.receiver }
            assertNotNull(doc.reader(channel).source(channel.profile, "fixture-sweep-α"))
        } }
        assertFalse(File(root, "field-document-review").exists())
    }

    @Test fun semanticParityIgnoresMemberOrderAndEscapeSpellingAndValidatesSkippedValues() {
        fun hash(text: String) = FieldRowObject.root(FieldBytes.Memory(text.toByteArray()), semantic = true).canonicalHash()
        assertArrayEquals(hash("""{"z":{"c":"𝄞\n","b":[1,2]},"a":-0.0}"""),
            hash("""{"a":0,"z":{"b":[1.00,2e0],"\u0063":"\ud834\udd1e\u000a"}}"""))
        assertFalse(hash("""{"a":[1,2]}""").contentEquals(hash("""{"a":[2,1]}""")))
        for (value in listOf("""{"extra":{"a":1,"\u0061":2}}""", """{"extra":["\udc00"]}""",
            """{"extra":"\ud800a"}""", """{"extra":[1e999]}""", """{"extra":01}""")) {
            assertThrows(IllegalArgumentException::class.java) { hash(value) }
        }
        val invalidUtf8 = """{"extra":"""".toByteArray() + byteArrayOf(0xc0.toByte(), 0xaf.toByte()) + "\"}".toByteArray()
        assertThrows(IllegalArgumentException::class.java) { FieldRowObject.root(FieldBytes.Memory(invalidUtf8)) }
    }
}
