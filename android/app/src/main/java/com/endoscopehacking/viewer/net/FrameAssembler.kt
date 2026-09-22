package com.endoscopehacking.viewer.net

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ConcurrentHashMap

/** A fully reassembled JPEG frame plus the orientation sample carried in its header. */
data class AssembledFrame(val jpeg: ByteArray, val orientationDegrees: Float?)

private class PendingFrame(val declaredSize: Int) {
    val fragments = HashMap<Int, ByteArray>()
    var received = 0
    var lastSeenMs = System.currentTimeMillis()
    var accelerometerRaw: Long = 0
}

/**
 * Reassembles frames from UDP fragments using the header's declared size and
 * fragment sequence number, so lost or reordered fragments never reach the
 * UI as glitches. Port of `FrameAssembler` in the Python relay.
 */
class FrameAssembler {
    // Keyed by (frameCounter, accelerometerRaw) like the Python version, since the
    // camera reuses small frame counters across in-flight frames.
    private val pending = ConcurrentHashMap<Long, PendingFrame>()
    var completeCount = 0
        private set
    var droppedCount = 0
        private set

    fun add(data: ByteArray): AssembledFrame? {
        if (data.size < Protocol.HEADER_SIZE) return null
        val buf = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN)

        val ptype = buf.getShort(0).toInt() and 0xffff
        val frameCounter = buf.getShort(2).toInt() and 0xffff
        val declared = buf.getInt(4)
        val seq = buf.getShort(12).toInt() and 0xffff
        val dlen = buf.getShort(14).toInt() and 0xffff
        val accelerometerRaw = buf.getInt(16).toLong() and 0xffffffffL

        if (ptype != Protocol.PKT_FIRST && ptype != Protocol.PKT_MIDDLE && ptype != Protocol.PKT_LAST) {
            return null
        }

        val key = (frameCounter.toLong() shl 32) or accelerometerRaw
        if (declared <= 0 || declared > Protocol.MAX_FRAME_SIZE) {
            pending.remove(key)
            return null
        }

        val end = minOf(Protocol.HEADER_SIZE + dlen, data.size)
        val payload = data.copyOfRange(Protocol.HEADER_SIZE, end)

        val entry = pending.getOrPut(key) { PendingFrame(declared).also { it.accelerometerRaw = accelerometerRaw } }
        entry.lastSeenMs = System.currentTimeMillis()
        if (!entry.fragments.containsKey(seq)) {
            entry.fragments[seq] = payload
            entry.received += payload.size
        }

        if (entry.received >= entry.declaredSize) {
            pending.remove(key)
            return finish(entry)
        }
        return null
    }

    /** Drops frames that have been incomplete for too long, to bound memory use. */
    fun expire() {
        val now = System.currentTimeMillis()
        val stale = pending.entries.filter { now - it.value.lastSeenMs > Protocol.FRAME_TIMEOUT_MS }
        for (entry in stale) pending.remove(entry.key)
    }

    private fun finish(entry: PendingFrame): AssembledFrame? {
        val frame = ByteArray(entry.received)
        var offset = 0
        for (seq in entry.fragments.keys.sorted()) {
            val chunk = entry.fragments.getValue(seq)
            chunk.copyInto(frame, offset)
            offset += chunk.size
        }
        if (frame.size != entry.declaredSize) {
            droppedCount++
            return null
        }
        val eoi = indexOf(frame, Protocol.JPEG_EOI)
        if (!startsWith(frame, Protocol.JPEG_SOI) || eoi == -1) {
            droppedCount++
            return null
        }
        completeCount++
        val jpeg = frame.copyOfRange(0, eoi + 2)
        return AssembledFrame(jpeg, Accelerometer.degrees(entry.accelerometerRaw))
    }

    private fun startsWith(data: ByteArray, prefix: ByteArray): Boolean {
        if (data.size < prefix.size) return false
        for (i in prefix.indices) if (data[i] != prefix[i]) return false
        return true
    }

    private fun indexOf(data: ByteArray, needle: ByteArray): Int {
        outer@ for (i in 0..data.size - needle.size) {
            for (j in needle.indices) if (data[i + j] != needle[j]) continue@outer
            return i
        }
        return -1
    }
}
