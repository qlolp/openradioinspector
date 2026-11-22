package org.openradioinspector.field

/** Each handle owns a receive-only adapter on a duplicate of the local USB bridge socket. */
internal object NativeCapture {
    init { System.loadLibrary("ori_android_field") }

    external fun open(fd: Int, planJson: String): Long
    external fun next(handle: Long): String?
    external fun close(handle: Long)
}
