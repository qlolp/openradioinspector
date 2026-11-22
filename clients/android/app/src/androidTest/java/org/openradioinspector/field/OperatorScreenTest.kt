package org.openradioinspector.field

import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.res.Configuration
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class OperatorScreenTest {
    private val instrument = InstrumentationRegistry.getInstrumentation()

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

    private fun awaitNode(text: String): AccessibilityNodeInfo {
        val until = SystemClock.uptimeMillis() + 15000
        val automation = instrument.uiAutomation
        do {
            val nodes = automation.rootInActiveWindow?.findAccessibilityNodeInfosByText(text).orEmpty()
            nodes.firstOrNull { it.isVisibleToUser && it.text?.toString() == text }?.let { return it }
            SystemClock.sleep(100)
        } while (SystemClock.uptimeMillis() < until)
        throw AssertionError("На экране не найден текст «$text»")
    }
}
