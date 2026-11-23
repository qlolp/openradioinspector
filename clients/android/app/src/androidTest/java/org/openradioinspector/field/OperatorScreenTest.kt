package org.openradioinspector.field

import android.app.Activity
import android.app.Instrumentation
import android.content.IntentFilter
import android.net.Uri
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.res.Configuration
import android.location.Location
import android.os.Bundle
import android.os.SystemClock
import android.view.InputDevice
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.ViewGroup
import android.view.accessibility.AccessibilityNodeInfo
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.platform.ViewRootForTest
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.getAllSemanticsNodes
import androidx.compose.ui.semantics.getOrNull
import androidx.core.view.descendants
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.time.Instant
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class OperatorScreenTest {
    private val instrument = InstrumentationRegistry.getInstrumentation()

    @Test fun privacyPolicyOpensOfflineFromLaunch() {
        ActivityScenario.launch(MainActivity::class.java).use {
            var button: AccessibilityNodeInfo? = awaitNode("Конфиденциальность")
            while (button != null && !button.isClickable) button = button.parent
            assertTrue(button?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true)
            awaitNode("Политика конфиденциальности")
            val policy = instrument.targetContext.assets.open("privacy.md").bufferedReader().use { it.readText() }
            assertTrue("9218907903@mail.ru" in policy)
            assertTrue("Android" in policy)
        }
    }

    @Test fun archiveTabSurvivesActivityRecreationAndLandscape() {
        ActivityScenario.launch(MainActivity::class.java).use { activity ->
            awaitNode("USB-прибор")
            var archive: AccessibilityNodeInfo? = awaitNode("Архив")
            while (archive != null && !archive.isClickable) archive = archive.parent
            assertTrue("Вкладка архива доступна для выбора", archive?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true)
            awaitNode("Сохранённые сеансы")
            activity.recreate()
            awaitNode("Сохранённые сеансы")
            activity.onActivity { it.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE }
            val until = SystemClock.uptimeMillis() + 10000
            while (instrument.targetContext.resources.configuration.orientation != Configuration.ORIENTATION_LANDSCAPE &&
                SystemClock.uptimeMillis() < until) SystemClock.sleep(100)
            assertEquals(Configuration.ORIENTATION_LANDSCAPE, instrument.targetContext.resources.configuration.orientation)
            awaitNode("Сохранённые сеансы")
        }
    }

    @Test fun documentPickerKeepsFieldExtensionAndMimeType() {
        val filename = "example.orifield"
        val intent = exportDocument().createIntent(instrument.targetContext, filename)
        assertEquals(Intent.ACTION_CREATE_DOCUMENT, intent.action)
        assertEquals("application/vnd.openradioinspector.field+zip", intent.type)
        assertEquals(filename, intent.getStringExtra(Intent.EXTRA_TITLE))
    }

    @Test fun tappingTheFrequencyMapOpensTheDisplayedOriginalAndBackRestoresTheFix() {
        val field = File(instrument.targetContext.filesDir, "field")
        val session = UUID.randomUUID().toString()
        val config = FieldConfig(7, "ardv1", 100_000_000, 100_010_000, 10_000)
        try {
            val original = FieldArchive.create(field, session, FieldDevice(7, "synthetic", listOf("ardv1")), config).use { store ->
                val gps = Location("gps").apply {
                    latitude = 45.0
                    longitude = 15.0
                    time = System.currentTimeMillis()
                    elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos()
                }
                store.gps(gps)
                val utc = Instant.ofEpochMilli(gps.time).toString()
                val observation = JSONObject().put("interval", JSONObject().put("started_at", utc).put("ended_at", utc))
                    .put("mode", "FM").put("metadata", JSONObject()).put("level_unit", "s_meter_count")
                    .put("frequency_hz", 100_000_000L).put("level", 137)
                val chosen = store.measurement(observation.toString(), config).id
                store.measurement(observation.put("frequency_hz", 100_010_000L).put("level", 210).toString(), config)
                store.finish(null)
                chosen
            }
            ActivityScenario.launch(MainActivity::class.java).use { activity ->
                click("Архив")
                awaitNode(session)
                tapFrequency("100", activity)
                awaitNode(original, scroll = true)
                instrument.sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
                awaitNode(singleFixChart, scroll = true)
            }
        } finally {
            File(field, session).deleteRecursively()
        }
    }

    private val singleFixChart = "Уровни по треку GPS, север сверху. Точек на экране 1"

    private fun tapFrequency(frequency: String, activity: ActivityScenario<MainActivity>) {
        val edit = awaitNode("Частота на треке", scroll = true) { it.isEditable }
        val value = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, frequency) }
        assertTrue(edit.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, value))
        awaitNode(frequency) { it.isEditable && it.text?.contains(frequency) == true }
        click("Применить")
        val chart = awaitNode(singleFixChart, scroll = true)
        chart.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_SHOW_ON_SCREEN.id)
        val point = renderedFixCenter(activity)
        val down = SystemClock.uptimeMillis()
        for (action in listOf(MotionEvent.ACTION_DOWN, MotionEvent.ACTION_UP)) {
            val event = MotionEvent.obtain(down, SystemClock.uptimeMillis(), action, point.x, point.y, 0)
            event.source = InputDevice.SOURCE_TOUCHSCREEN
            try { instrument.sendPointerSync(event) } finally { event.recycle() }
        }
    }

    private fun renderedFixCenter(activity: ActivityScenario<MainActivity>): Offset {
        // Android reports clipped accessibility bounds while scrolling. The single fix is drawn
        // at the center of the full Canvas. Wait for that point to be visible and stationary.
        var previous: Offset? = null
        val until = SystemClock.uptimeMillis() + 5_000
        do {
            var screen: Offset? = null
            activity.onActivity { host ->
                val root = (host.window.decorView as ViewGroup).descendants.filterIsInstance<ViewRootForTest>().single()
                val chart = root.semanticsOwner.getAllSemanticsNodes(mergingEnabled = false).single {
                    singleFixChart in it.config.getOrNull(SemanticsProperties.ContentDescription).orEmpty()
                }
                val coordinates = chart.layoutInfo.coordinates
                val center = Offset(chart.size.width / 2f, chart.size.height / 2f)
                if (chart.boundsInWindow.contains(coordinates.localToWindow(center))) {
                    screen = coordinates.localToScreen(center)
                }
            }
            screen?.let { if (it == previous) return it }
            previous = screen
            SystemClock.sleep(50)
        } while (SystemClock.uptimeMillis() < until)
        throw AssertionError("Маркер трека не появился в видимой области")
    }

    private fun withPickedFile(bytes: ByteArray, inspect: (ActivityScenario<MainActivity>) -> Unit) {
        val file = File.createTempFile("portable-route-", ".orifield", instrument.targetContext.cacheDir)
        try {
            file.writeBytes(bytes)
            val filter = IntentFilter(Intent.ACTION_OPEN_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE)
                addDataType("*/*")
            }
            val result = Instrumentation.ActivityResult(Activity.RESULT_OK, Intent().setData(Uri.fromFile(file)))
            val monitor = instrument.addMonitor(filter, result, true)
            try {
                ActivityScenario.launch(MainActivity::class.java).use { activity ->
                    click("Архив")
                    click("Открыть .orifield…")
                    inspect(activity)
                }
                assertArrayEquals(bytes, file.readBytes())
            } finally {
                instrument.removeMonitor(monitor)
            }
        } finally {
            file.delete()
        }
    }

    @Test fun filePickerOpensPortableFixtureAndTappingItsMapResolvesTheOriginal() {
        val bytes = checkNotNull(javaClass.getResourceAsStream("/field-archive-v1.orifield")).use { it.readBytes() }
        withPickedFile(bytes) { activity ->
            awaitNode("fixture-сессия", scroll = true)
            tapFrequency("100,000002", activity)
            awaitNode("fixture-sweep-α", scroll = true)
        }
    }

    @Test fun filePickerRejectsAnIncompleteDocumentAndKeepsTheSource() {
        withPickedFile(byteArrayOf(80, 75, 3, 4)) {
            awaitNode("Ошибка открытия") { it.text?.startsWith("Не удалось открыть файл выезда.") == true }
            click("Закрыть файл")
        }
    }

    private fun click(text: String) {
        var node: AccessibilityNodeInfo? = awaitNode(text)
        while (node != null && !node.isClickable) node = node.parent
        assertTrue(text, node?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true)
    }

    private fun awaitNode(text: String, scroll: Boolean = false,
        matches: (AccessibilityNodeInfo) -> Boolean = { false }): AccessibilityNodeInfo {
        val until = SystemClock.uptimeMillis() + 15000
        val automation = instrument.uiAutomation
        var window = ""
        var nextScroll = SystemClock.uptimeMillis() + 500
        val visibleLabels = mutableListOf<String>()
        do {
            val nodes = ArrayDeque<AccessibilityNodeInfo>()
            var scrollView: AccessibilityNodeInfo? = null
            visibleLabels.clear()
            automation.rootInActiveWindow?.let {
                nodes.addLast(it)
                window = it.packageName.toString()
            }
            // Compose exposes a virtual provider whose children must be walked explicitly.
            // Android's provider-side findAccessibilityNodeInfosByText does not search them.
            while (nodes.isNotEmpty()) {
                val node = nodes.removeFirst()
                if (node.isVisibleToUser) {
                    val labels = listOfNotNull(node.text?.toString(), node.contentDescription?.toString())
                    if (text in labels || matches(node)) return node
                    if (node.isScrollable) scrollView = node
                    if (visibleLabels.size < 40) visibleLabels.addAll(labels)
                }
                for (i in 0 until node.childCount) node.getChild(i)?.let(nodes::addLast)
            }
            if (scroll && SystemClock.uptimeMillis() >= nextScroll) {
                scrollView?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
                nextScroll = SystemClock.uptimeMillis() + 500
            }
            SystemClock.sleep(100)
        } while (SystemClock.uptimeMillis() < until)
        throw AssertionError("Не найден «$text». Окно $window, видимые подписи $visibleLabels")
    }
}
