package com.endoscopehacking.viewer.net

import kotlin.math.PI
import kotlin.math.atan2

/**
 * Decodes the packed accelerometer sample embedded in each video-frame header
 * (bytes 16-19). Ported from `accelerometer_degrees()` in the Python relay,
 * which itself mirrors `StreamSelf` / `NativeLibs.getAccelerometer()` in the
 * vendor APK.
 *
 * Returns null when the sample is indeterminate (near-vertical hold or a tiny
 * angle) so callers can retain the last known orientation, matching the
 * app's own deadband/retention behavior.
 */
object Accelerometer {
    fun degrees(packed: Long): Float? {
        if ((packed shr 31) and 1L == 1L) {
            return (packed and 0x3fffffffL) / 1000.0f
        }

        val x = (packed shr 20) and 0x3ff
        val y = (packed shr 10) and 0x3ff
        val z = packed and 0x3ff
        val xMag = if (x >= 512) 1024 - x else x
        val yMag = if (y >= 512) 1024 - y else y
        val zMag = if (z >= 512) 1024 - z else z
        if (yMag < 64 && zMag < 64) return null

        var angle = atan2(yMag.toDouble(), zMag.toDouble())
        if (z > 512) angle = PI - angle
        if (y > 512) angle = (2 * PI) - angle

        // StreamSelf ignores tiny angles rather than resetting MySurfaceView1.
        val deadband = if (xMag < 36) 0.09 else 0.005
        if (kotlin.math.abs(angle) <= deadband) return null
        return Math.toDegrees(angle).toFloat()
    }
}
