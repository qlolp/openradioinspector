package org.openradioinspector.field

import org.json.JSONArray
import org.json.JSONObject
import java.math.BigDecimal
import java.time.LocalDateTime
import java.time.ZoneOffset

// Android's JSONObject accepts non-JSON syntax and silently replaces duplicate keys.
internal class FieldDocumentJson(private val text: String, private val checkpoint: () -> Unit = {}) {
    private var at = 0
    private var tokens = 0
    private val number = numberPattern.matcher(text)

    fun parse(): JSONObject {
        val result = value(0)
        spaces()
        require(result is JSONObject && at == text.length) { "Ожидался объект JSON" }
        return result
    }

    private fun spaces() { while (at < text.length && text[at] in " \n\r\t") at++ }
    private fun take(c: Char): Boolean {
        if (at >= text.length || text[at] != c) return false
        at++
        return true
    }

    private fun value(depth: Int): Any {
        require(depth <= 64 && ++tokens <= 100_000) { "Слишком сложная запись JSON" }
        if (tokens % 256 == 0) checkpoint()
        spaces()
        require(at < text.length)
        return when (text[at]) {
            '{' -> {
                at++
                val obj = JSONObject()
                spaces()
                if (!take('}')) {
                    do {
                        spaces()
                        val key = string()
                        require(!obj.has(key)) { "Повтор ключа JSON" }
                        spaces()
                        require(take(':'))
                        obj.put(key, value(depth + 1))
                        spaces()
                    } while (take(','))
                    require(take('}'))
                }
                obj
            }
            '[' -> {
                at++
                val array = JSONArray()
                spaces()
                if (!take(']')) {
                    do {
                        array.put(value(depth + 1))
                        spaces()
                    } while (take(','))
                    require(take(']'))
                }
                array
            }
            '"' -> string()
            'n' -> literal("null", JSONObject.NULL)
            't' -> literal("true", true)
            'f' -> literal("false", false)
            else -> {
                require(number.region(at, text.length).lookingAt()) { "Некорректное число JSON" }
                val end = number.end()
                require(end - at <= 128)
                val raw = text.substring(at, end)
                at = end
                raw.toLongOrNull() ?: BigDecimal(raw).also { require(it.toDouble().isFinite()) }
            }
        }
    }

    private fun literal(word: String, value: Any): Any {
        require(text.startsWith(word, at))
        at += word.length
        return value
    }

    private fun string(): String {
        require(take('"'))
        var out: StringBuilder? = null
        var start = at
        while (at < text.length) {
            if (at % 16384 == 0) checkpoint()
            val c = text[at++]
            when (c) {
                '"' -> {
                    val decoded = out?.append(text, start, at - 1)?.toString() ?: text.substring(start, at - 1)
                    var index = 0
                    while (index < decoded.length) {
                        val ch = decoded[index++]
                        if (ch.isHighSurrogate()) require(index < decoded.length && decoded[index++].isLowSurrogate())
                        else require(!ch.isLowSurrogate())
                    }
                    return decoded
                }
                '\\' -> {
                    require(at < text.length)
                    val escaped = out ?: StringBuilder().also { out = it }
                    escaped.append(text, start, at - 1)
                    escaped.append(when (val escape = text[at++]) {
                        '"', '\\', '/' -> escape
                        'b' -> '\b'
                        'f' -> '\u000c'
                        'n' -> '\n'
                        'r' -> '\r'
                        't' -> '\t'
                        'u' -> {
                            require(at + 4 <= text.length)
                            val digits = text.substring(at, at + 4)
                            require(digits.all { it in "0123456789abcdefABCDEF" })
                            at += 4
                            digits.toInt(16).toChar()
                        }
                        else -> throw IllegalArgumentException("Некорректная escape-последовательность JSON")
                    })
                    start = at
                }
                else -> require(c >= ' ')
            }
        }
        throw IllegalArgumentException("Незавершённая строка JSON")
    }

    companion object {
        private val numberPattern = java.util.regex.Pattern.compile("-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
    }
}

internal fun JSONObject.text(key: String): String =
    (opt(key) as? String)?.takeIf { it.isNotBlank() } ?: throw IllegalArgumentException("Недопустимое поле $key")

internal fun JSONObject.count(key: String): Long =
    (opt(key) as? Long)?.takeIf { it >= 0 } ?: throw IllegalArgumentException("Недопустимое целое $key")

internal fun sameFieldJson(a: Any?, b: Any?): Boolean = when {
    a is Number && b is Number -> BigDecimal(a.toString()).compareTo(BigDecimal(b.toString())) == 0
    a is JSONObject && b is JSONObject -> a.length() == b.length() && a.keys().asSequence().all { b.has(it) && sameFieldJson(a.get(it), b.get(it)) }
    a is JSONArray && b is JSONArray -> a.length() == b.length() && (0 until a.length()).all { sameFieldJson(a.get(it), b.get(it)) }
    else -> a == b
}

private val timestamp = Regex("""([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]+))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])""")

internal data class FieldTimestamp(val seconds: Long, val fraction: String) : Comparable<FieldTimestamp> {
    override fun compareTo(other: FieldTimestamp): Int = compareValuesBy(this, other, { it.seconds }, { it.fraction })
}

internal fun fieldTimestamp(value: String): FieldTimestamp {
    val match = requireNotNull(timestamp.matchEntire(value)) { "Недопустимое время UTC" }
    val (local, fraction, zone) = match.destructured
    require(!local.startsWith("0000-"))
    val offset = if (zone == "Z") 0 else {
        val seconds = (zone.substring(1, 3).toInt() * 60 + zone.substring(4).toInt()) * 60
        if (zone.first() == '-') -seconds else seconds
    }
    return FieldTimestamp(LocalDateTime.parse(local).toEpochSecond(ZoneOffset.UTC) - offset, fraction.trimEnd('0'))
}
