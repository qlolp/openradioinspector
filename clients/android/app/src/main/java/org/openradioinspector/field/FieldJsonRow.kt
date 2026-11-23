package org.openradioinspector.field

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import java.io.OutputStream
import java.io.RandomAccessFile
import java.math.BigDecimal
import java.security.MessageDigest

// Offsets always address the immutable, verified source. Indexing uses a small file window.
// A review page owns at most its byte budget or one selected verified record.
internal abstract class FieldBytes(val size: Int) {
    abstract operator fun get(index: Int): Int

    class Window(private val file: RandomAccessFile, private val row: FieldRow,
        private val checkpoint: () -> Unit) : FieldBytes(row.length) {
        private val buffer = ByteArray(minOf(row.length, 32 * 1024))
        private var start = -1
        private var end = -1
        override fun get(index: Int): Int {
            if (index !in start until end) {
                checkpoint()
                start = index
                end = minOf(size, index + buffer.size)
                file.seek(row.position + index)
                file.readFully(buffer, 0, end - start)
            }
            return buffer[index - start].toInt() and 255
        }
    }

    class Memory(private val bytes: ByteArray) : FieldBytes(bytes.size) {
        override fun get(index: Int) = bytes[index].toInt() and 255
        override fun utf8(from: Int, end: Int) = String(bytes, from, end - from, Charsets.UTF_8)
    }

    open fun utf8(from: Int, end: Int): String =
        ByteArray(end - from) { this[from + it].toByte() }.toString(Charsets.UTF_8)

    fun copyTo(output: OutputStream, from: Int, end: Int) {
        val block = ByteArray(minOf(end - from, 8192))
        var at = from
        while (at < end) {
            val n = minOf(end - at, block.size)
            for (i in 0 until n) block[i] = this[at + i].toByte()
            output.write(block, 0, n)
            at += n
        }
    }

    fun matches(text: String) = size == text.length && text.indices.all { this[it] == text[it].code }
}

private class Stamp(val bytes: ByteArray) : Comparable<Stamp> {
    override fun equals(other: Any?) = other is Stamp && bytes.contentEquals(other.bytes)
    override fun hashCode() = bytes.contentHashCode()
    override fun compareTo(other: Stamp): Int {
        for (i in bytes.indices) {
            val difference = (bytes[i].toInt() and 255) - (other.bytes[i].toInt() and 255)
            if (difference != 0) return difference
        }
        return 0
    }
}

private fun sha(tag: Char) = MessageDigest.getInstance("SHA-256").apply { update(tag.code.toByte()) }
private fun stringStamp(value: String) = Stamp(sha('"').digest(value.toByteArray(Charsets.UTF_8)))
private fun Stamp.hex(): String {
    val digits = "0123456789abcdef"
    val result = CharArray(bytes.size * 2)
    for ((i, byte) in bytes.withIndex()) {
        val n = byte.toInt() and 255
        result[i * 2] = digits[n shr 4]
        result[i * 2 + 1] = digits[n and 15]
    }
    return String(result)
}

internal fun fieldIndexKey(id: String) = stringStamp(id).hex()

private fun objectStamp(fields: Map<Stamp, Stamp>) = Stamp(sha('{').apply {
    for (key in fields.keys.sorted()) { update(key.bytes); update(fields.getValue(key).bytes) }
}.digest())
private fun arrayStamp(values: List<Stamp>) = Stamp(sha('[').apply { values.forEach { update(it.bytes) } }.digest())

private data class TextMark(val offset: Int, val chars: Int)
private data class Token(val from: Int, val end: Int, val kind: Char, val chars: Int = 0,
    val nonblank: Boolean = false, val number: Number? = null, val stamp: Stamp? = null,
    val marks: List<TextMark>? = null)
private data class Member(val key: Token, val value: Token)

// Strings are decoded as Unicode scalar values before comparison. Field order and equivalent
// numeric spellings are insignificant. The type tag and fixed-length child digests delimit values.
private class StringHash {
    private val buffer = ByteArray(4096)
    private var size = 0
    private lateinit var digest: MessageDigest
    fun start() { size = 0; digest = sha('"') }
    private fun put(value: Int) {
        if (size == buffer.size) { digest.update(buffer); size = 0 }
        buffer[size++] = value.toByte()
    }
    fun scalar(c: Int) {
        when {
            c < 128 -> put(c)
            c < 2048 -> { put(0xc0 or (c shr 6)); put(0x80 or (c and 63)) }
            c < 65536 -> { put(0xe0 or (c shr 12)); put(0x80 or ((c shr 6) and 63)); put(0x80 or (c and 63)) }
            else -> {
                put(0xf0 or (c shr 18)); put(0x80 or ((c shr 12) and 63))
                put(0x80 or ((c shr 6) and 63)); put(0x80 or (c and 63))
            }
        }
    }
    fun finish(): Stamp { digest.update(buffer, 0, size); return Stamp(digest.digest()) }
}

private val numberGrammar = Regex("""-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?""")

private class Scanner(private val input: FieldBytes, var at: Int, private val semantic: Boolean) {
    private val strings by lazy(LazyThreadSafetyMode.NONE) { StringHash() }
    private var values = 0
    fun peek() = if (at < input.size) input[at] else -1
    private fun next(): Int { require(at < input.size); return input[at++] }
    private fun take(c: Char): Boolean {
        if (peek() != c.code) return false
        at++
        return true
    }
    fun space() { while (peek() == 32 || peek() == 9 || peek() == 13 || peek() == 10) at++ }
    private fun hex(): Int {
        var v = 0
        repeat(4) {
            val c = next()
            val digit = when (c) {
                in 48..57 -> c - 48
                in 65..70 -> c - 55
                in 97..102 -> c - 87
                else -> throw IllegalArgumentException("Недопустимая цифра escape-последовательности JSON")
            }
            v = v * 16 + digit
        }
        return v
    }
    private fun continuation(): Int = next().also { require(it in 0x80..0xbf) } and 63
    private fun scalar(c: Int): Int = when (c) {
        92 -> when (val escape = next()) {
            34, 47, 92 -> escape
            98 -> 8
            102 -> 12
            110 -> 10
            114 -> 13
            116 -> 9
            117 -> {
                val u = hex()
                if (u in 0xd800..0xdbff) {
                    require(take('\\') && take('u'))
                    val low = hex()
                    require(low in 0xdc00..0xdfff)
                    Character.toCodePoint(u.toChar(), low.toChar())
                } else u.also { require(it !in 0xdc00..0xdfff) }
            }
            else -> throw IllegalArgumentException("Некорректная escape-последовательность JSON")
        }
        in 32..127 -> c
        in 0xc2..0xdf -> ((c and 31) shl 6) or continuation()
        in 0xe0..0xef -> (((c and 15) shl 12) or (continuation() shl 6) or continuation()).also {
            require(it >= 0x800 && it !in 0xd800..0xdfff)
        }
        in 0xf0..0xf4 -> (((c and 7) shl 18) or (continuation() shl 12) or
            (continuation() shl 6) or continuation()).also { require(it in 0x10000..0x10ffff) }
        else -> throw IllegalArgumentException("Недопустимая строка UTF-8")
    }

    fun string(hash: Boolean, markText: Boolean = false): Token {
        val start = at
        require(take('"'))
        if (hash) strings.start()
        var chars = 0
        var nonblank = false
        var marks: MutableList<TextMark>? = null
        var nextMark = 4096
        while (true) {
            if (markText && chars >= nextMark) {
                if (marks == null) marks = ArrayList()
                marks.add(TextMark(at, chars))
                nextMark += 4096
            }
            val byte = next()
            if (byte == 34) break
            val c = if (byte in 32..127 && byte != 92) byte else scalar(byte)
            if (hash) strings.scalar(c)
            chars += if (c < 65536) 1 else 2
            if (!nonblank && (c > 65535 || !c.toChar().isWhitespace())) nonblank = true
        }
        return Token(start, at, '"', chars, nonblank, stamp = if (hash) strings.finish() else null, marks = marks)
    }

    // Assign a scalar crossing a UTF-16 page boundary to the page where it started.
    fun slice(t: Token, first: Int, limit: Int): String {
        require(first in 0..limit && limit <= t.chars)
        val marks = t.marks
        val mark = if (marks == null || first < 4096) null else {
            val found = marks.binarySearchBy(first) { it.chars }
            marks.getOrNull(if (found >= 0) found else -found - 2)
        }
        at = mark?.offset ?: (t.from + 1)
        var chars = mark?.chars ?: 0
        val result = StringBuilder(limit - first)
        while (chars < limit) {
            val byte = next()
            val c = if (byte in 32..127 && byte != 92) byte else scalar(byte)
            if (chars >= first) result.appendCodePoint(c)
            chars += if (c < 65536) 1 else 2
        }
        return result.toString()
    }

    fun value(depth: Int = 0, fields: MutableMap<Stamp, Member>? = null,
        element: ((Token) -> Unit)? = null, markText: Boolean = false): Token {
        require(depth <= 64 && ++values <= 100_000) { "Слишком сложная запись JSON" }
        space()
        val start = at
        val kind = peek().toChar()
        if (kind == '"') return string(semantic, markText)
        var stamp: Stamp? = null
        var number: Number? = null
        when (kind) {
            '{' -> {
                at++
                val keys = HashSet<Stamp>()
                val children = if (semantic) HashMap<Stamp, Stamp>() else null
                space()
                if (!take('}')) {
                    do {
                        space()
                        val key = string(true)
                        val name = key.stamp!!
                        require(keys.add(name)) { "Повтор ключа JSON" }
                        space()
                        require(take(':'))
                        val child = value(depth + 1, markText = fields != null)
                        fields?.put(name, Member(key, child))
                        if (children != null) children[name] = child.stamp!!
                        space()
                    } while (take(','))
                    require(take('}'))
                }
                if (children != null) stamp = objectStamp(children)
            }
            '[' -> {
                at++
                val hash = if (semantic) sha('[') else null
                space()
                if (!take(']')) {
                    do {
                        val item = value(depth + 1)
                        element?.invoke(item)
                        hash?.update(item.stamp!!.bytes)
                        space()
                    } while (take(','))
                    require(take(']'))
                }
                if (hash != null) stamp = Stamp(hash.digest())
            }
            'n', 't', 'f' -> {
                val word = when (kind) { 'n' -> "null"; 't' -> "true"; else -> "false" }
                require(word.all { take(it) })
                if (semantic) stamp = Stamp(sha(kind).digest())
            }
            else -> {
                val raw = StringBuilder()
                while (true) {
                    val c = peek()
                    if (c !in 48..57 && c != 45 && c != 43 && c != 101 && c != 69 && c != 46) break
                    require(raw.length < 128)
                    raw.append(next().toChar())
                }
                val text = raw.toString()
                require(numberGrammar.matches(text)) { "Некорректное число JSON" }
                val integral = text.toLongOrNull()
                val decimal = if (semantic || integral == null) BigDecimal(text) else null
                number = integral ?: text.toDouble().also { require(it.isFinite()) }
                if (semantic) stamp = Stamp(sha('#').digest(decimal!!.stripTrailingZeros().toString().toByteArray(Charsets.US_ASCII)))
            }
        }
        return Token(start, at, kind, number = number, stamp = stamp)
    }
}

/** A sparse view over one JSON object. Only the fields requested by the caller are decoded. */
internal class FieldRowObject private constructor(private val bytes: FieldBytes, private val fields: Map<Stamp, Member>,
    private val node: Token) : JSONObject() {
    private fun token(name: String?) = name?.let { fields[stringStamp(it)]?.value }
    private fun text(t: Token, first: Int = 0, limit: Int = t.chars): String =
        Scanner(bytes, t.from, false).slice(t, first, limit)

    /** Exact UTF-8 object bytes, including the original numeric spelling and property order. */
    fun writeOriginal(output: OutputStream) = bytes.copyTo(output, node.from, node.end)

    private fun decoded(t: Token): Any = when (t.kind) {
        '"' -> text(t)
        '{' -> objectAt(bytes, t.from)
        '[' -> JSONArray().also { array -> Scanner(bytes, t.from, false).value(element = { array.put(decoded(it)) }) }
        'n' -> NULL
        't' -> true
        'f' -> false
        else -> t.number!!
    }
    override fun opt(name: String?): Any? = token(name)?.let(::decoded)
    override fun get(name: String): Any = opt(name) ?: throw JSONException("Missing field")
    override fun has(name: String?) = token(name) != null
    override fun isNull(name: String?) = token(name)?.kind.let { it == null || it == 'n' }
    override fun length() = fields.size
    override fun keys(): MutableIterator<String> = fields.values.asSequence().map { text(it.key) }.iterator().let { source ->
        object : MutableIterator<String> {
            override fun hasNext() = source.hasNext()
            override fun next() = source.next()
            override fun remove(): Unit = throw UnsupportedOperationException()
        }
    }

    override fun toString() = bytes.utf8(node.from, node.end)

    fun indexKey(name: String): String {
        val t = requireNotNull(token(name))
        require(t.kind == '"' && t.nonblank) { "Недопустимый идентификатор" }
        return (t.stamp ?: Scanner(bytes, t.from, false).string(true).stamp!!).hex()
    }

    fun validText(name: String) = token(name)?.let { it.kind == '"' && it.nonblank } == true
    fun textLength(name: String) = requireNotNull(token(name)).also { require(it.kind == '"') }.chars
    fun textPage(name: String, first: Int, limit: Int) = text(requireNotNull(token(name)), first, limit)

    fun numericArray(name: String, frequencies: Boolean): Int {
        val array = requireNotNull(token(name))
        require(array.kind == '[')
        var last = 0L
        var count = 0
        Scanner(bytes, array.from, false).value(element = { t ->
            require(t.number != null) { "Недопустимый отсчёт спектра" }
            if (frequencies) {
                require(t.number is Long && t.number >= last) { "Недопустимая частота спектра" }
                last = t.number
            }
            count++
        })
        return count
    }

    fun canonicalHash() = requireNotNull(node.stamp).bytes

    fun gpsFeatureHash(): ByteArray {
        fun member(name: String) = requireNotNull(token(name)?.stamp)
        val coordinateFields = mutableListOf(member("longitude"), member("latitude"))
        if (!isNull("altitude_m")) coordinateFields += member("altitude_m")
        val geometry = objectStamp(mapOf(stringStamp("type") to stringStamp("Point"),
            stringStamp("coordinates") to arrayStamp(coordinateFields)))
        val excluded = setOf("kind", "id", "latitude", "longitude", "altitude_m").map(::stringStamp).toSet()
        val properties = objectStamp(fields.filterKeys { it !in excluded }.mapValues { it.value.value.stamp!! })
        return objectStamp(mapOf(stringStamp("type") to stringStamp("Feature"), stringStamp("id") to member("id"),
            stringStamp("geometry") to geometry, stringStamp("properties") to properties)).bytes
    }

    companion object {
        fun root(bytes: FieldBytes, semantic: Boolean = false, comma: Boolean = false): FieldRowObject {
            val scanner = Scanner(bytes, 0, semantic)
            if (comma) { require(scanner.peek() == ','.code); scanner.at++ }
            val root = objectAt(bytes, scanner)
            scanner.space()
            require(scanner.at == bytes.size) { "Ожидался объект JSON" }
            return root
        }
        private fun objectAt(bytes: FieldBytes, at: Int) = objectAt(bytes, Scanner(bytes, at, false))
        private fun objectAt(bytes: FieldBytes, scanner: Scanner): FieldRowObject {
            val fields = HashMap<Stamp, Member>()
            val node = scanner.value(fields = fields)
            require(node.kind == '{') { "Ожидался объект JSON" }
            return FieldRowObject(bytes, fields, node)
        }
    }
}
