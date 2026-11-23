package org.openradioinspector.field

import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FilterInputStream
import java.io.InputStream
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.zip.CRC32
import java.util.zip.CheckedInputStream

internal val fieldPayloads = listOf("measurements.ndjson", "events.ndjson", "track.geojson")
internal const val fieldManifest = "manifest.json"
internal const val mobileLineLimit = 1024 * 1024
internal const val mobileDocumentLimit = 128L * 1024 * 1024

private fun ByteBuffer.u16(offset: Int) = getShort(offset).toInt() and 0xffff
private fun ByteBuffer.u32(offset: Int) = getInt(offset).toLong() and 0xffffffffL
private fun RandomAccessFile.bytes(length: Int) = ByteArray(length).also(::readFully)
private fun RandomAccessFile.header(length: Int) = ByteBuffer.wrap(bytes(length)).order(ByteOrder.LITTLE_ENDIAN)

private fun RandomAccessFile.extra(length: Int) {
    val bytes = header(length)
    while (bytes.hasRemaining()) {
        require(bytes.remaining() >= 4)
        val tag = bytes.short.toInt() and 0xffff
        val n = bytes.short.toInt() and 0xffff
        require(tag != 1 && n <= bytes.remaining()) { "Недопустимое расширение ZIP" }
        bytes.position(bytes.position() + n)
    }
}

internal class StoredFieldZip(private val file: File) {
    private data class Member(val name: String, val flags: Int, val length: Long, val crc: Long,
        val local: Long, var data: Long = 0)
    private val members = linkedMapOf<String, Member>()

    init {
        RandomAccessFile(file, "r").use { zip ->
            val size = zip.length()
            require(size in 22..mobileDocumentLimit) { "Недопустимый размер ZIP" }
            zip.seek(size - 22)
            val end = zip.header(22)
            val start = end.u32(16)
            require(end.u32(0) == 0x06054b50L && end.u16(4) == 0 && end.u16(6) == 0 &&
                end.u16(8) == 4 && end.u16(10) == 4 && end.u16(20) == 0 &&
                end.u32(12) <= 65536 && start + end.u32(12) + 22 == size) { "Ожидался архив V1 из четырёх файлов" }
            zip.seek(start)
            repeat(4) {
                val h = zip.header(46)
                val flags = h.u16(8)
                val attr = h.u32(38)
                require(h.u32(0) == 0x02014b50L && flags and 0xf7ff == 0 && h.u16(10) == 0 &&
                    h.u32(20) == h.u32(24) && h.u16(34) == 0 && attr and 16 == 0L &&
                    (attr ushr 16 and 0xf000) in listOf(0L, 0x8000L)) { "Недопустимая запись ZIP" }
                val name = zip.bytes(h.u16(28)).toString(Charsets.US_ASCII)
                require((name in fieldPayloads || name == fieldManifest) && name !in members) { "Недопустимое имя файла ZIP" }
                members[name] = Member(name, flags, h.u32(24), h.u32(16), h.u32(42))
                zip.extra(h.u16(30))
                zip.seek(zip.filePointer + h.u16(32))
                require(zip.filePointer <= size - 22)
            }
            require(zip.filePointer == size - 22 && members.keys.last() == fieldManifest)
            val ordered = members.values.sortedBy { it.local }
            require(ordered.last().name == fieldManifest)
            var next = 0L
            for (member in ordered) {
                require(member.local == next) { "Нарушены границы ZIP" }
                zip.seek(next)
                val h = zip.header(30)
                require(h.u32(0) == 0x04034b50L && h.u16(6) == member.flags && h.u16(8) == 0 &&
                    h.u32(14) == member.crc && h.u32(18) == member.length && h.u32(22) == member.length &&
                    zip.bytes(h.u16(26)).contentEquals(member.name.toByteArray(Charsets.US_ASCII))) { "Заголовки ZIP расходятся" }
                zip.extra(h.u16(28))
                member.data = zip.filePointer
                next = member.data + member.length
                require(next <= start)
            }
            require(next == start)
        }
    }

    fun size(name: String) = members.getValue(name).length

    fun <T> read(name: String, sha256: String? = null, consume: (InputStream) -> T): T {
        val member = members.getValue(name)
        val crc = CRC32()
        val digest = MessageDigest.getInstance("SHA-256")
        return file.inputStream().use { source ->
            source.channel.position(member.data)
            val slice = object : FilterInputStream(source) {
                var remaining = member.length
                override fun read(): Int {
                    if (remaining == 0L) return -1
                    val byte = super.read()
                    require(byte >= 0)
                    remaining--
                    return byte
                }
                override fun read(bytes: ByteArray, offset: Int, length: Int): Int {
                    if (remaining == 0L) return -1
                    val n = super.read(bytes, offset, minOf(length.toLong(), remaining).toInt())
                    require(n > 0)
                    remaining -= n
                    return n
                }
            }
            val data = DigestInputStream(CheckedInputStream(slice, crc), digest).buffered()
            val result = consume(data)
            require(data.read() == -1 && crc.value == member.crc) { "Нарушена целостность ZIP" }
            if (sha256 != null) require(digest.digest().joinToString("") { "%02x".format(it) } == sha256) {
                "Контрольная сумма $name расходится с манифестом"
            }
            result
        }
    }
}

internal fun strictUtf8(bytes: ByteArray): String = Charsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes)).toString()

internal class FieldLines(private val input: InputStream) {
    private val buffer = ByteArray(16 * 1024)
    private var offset = 0
    private var end = 0

    fun next(): String? {
        if (end < 0) return null
        val line = ByteArrayOutputStream()
        while (true) {
            if (offset == end) {
                offset = 0
                end = input.read(buffer)
                if (end < 0) {
                    require(line.size() == 0) { "У записи отсутствует завершающий LF" }
                    return null
                }
            }
            val begin = offset
            while (offset < end && buffer[offset] != 10.toByte()) offset++
            val n = offset - begin
            require(line.size() + n < mobileLineLimit) { "Запись превышает предел 1 МиБ" }
            line.write(buffer, begin, n)
            if (offset < end) {
                offset++
                return strictUtf8(line.toByteArray())
            }
        }
    }
}
