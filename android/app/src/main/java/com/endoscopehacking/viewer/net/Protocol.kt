package com.endoscopehacking.viewer.net

/**
 * Reverse-engineered constants for the endoscope's two UDP protocols:
 *  - port [VIDEO_PORT]: fragmented MJPEG video stream + packed accelerometer sample
 *  - port [CONTROL_PORT]: request/response queries (battery, device info, ...)
 *
 * Mirrors the Python relay's `camera.py`; see the repo README for the full
 * reverse-engineering write-up.
 */
object Protocol {
    const val CAMERA_IP = "192.168.10.123"
    const val VIDEO_PORT = 8031
    const val CONTROL_PORT = 50000

    // Keep-alive heartbeat the vendor app sends on the video socket every ~0.5s.
    val HEARTBEAT_PAYLOAD: ByteArray = hexToBytes(
        "999901000000000000000000000000000000000000000000"
    )
    const val HEARTBEAT_INTERVAL_MS = 500L

    // 24-byte video fragment header (all integers little-endian):
    //   0-1   packet type: 0x0166 first, 0x0366 middle, 0x0266 last
    //   2-3   frame counter (8-bit effective)
    //   4-7   declared total frame size in bytes
    //   8-11  unknown (always 0)
    //   12-13 fragment sequence number (0-based, contiguous)
    //   14-15 payload length of this packet
    //   16-19 packed accelerometer sample (constant within a frame)
    //   20-23 unknown (always 0)
    const val HEADER_SIZE = 24
    const val PKT_FIRST = 0x0166
    const val PKT_MIDDLE = 0x0366
    const val PKT_LAST = 0x0266
    const val MAX_FRAME_SIZE = 2 * 1024 * 1024
    const val FRAME_TIMEOUT_MS = 2000L

    val JPEG_SOI: ByteArray = byteArrayOf(0xFF.toByte(), 0xD8.toByte())
    val JPEG_EOI: ByteArray = byteArrayOf(0xFF.toByte(), 0xD9.toByte())

    const val CONTROL_MAGIC = 0x9999
    const val CONTROL_HEADER_SIZE = 24 // <HHIIIQ>: 2+2+4+4+4+8
    const val CMD_GET_BATTERY = 0x1017
    const val CMD_GET_DEVICE_INFO = 0x1060

    private fun hexToBytes(hex: String): ByteArray =
        ByteArray(hex.length / 2) { i -> hex.substring(i * 2, i * 2 + 2).toInt(16).toByte() }
}
