package com.endoscopehacking.viewer.net

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicLong

class CameraControlException(message: String) : RuntimeException(message)

/** Non-sensitive fields the vendor app itself surfaces in its settings screen. */
private val SAFE_DEVICE_INFO_FIELDS = setOf(
    "brand", "model", "hardware", "firmware", "fw_date",
    "manufacturer", "cam_brightness", "stream_type", "wifi_channel",
)

/**
 * Read-only client for the endoscope's UDP/50000 command protocol. Port of
 * `CameraController` in the Python relay: builds/parses the 24-byte
 * `<HHIIIQ>` control header and decodes battery/device-info responses.
 */
class ControlClient(
    private val cameraIp: String = Protocol.CAMERA_IP,
    private val commandPort: Int = Protocol.CONTROL_PORT,
    private val timeoutMs: Int = 1500,
) {
    private val sequence = AtomicLong(0)

    private fun buildPacket(command: Int, seq: Long): ByteArray {
        val buf = ByteBuffer.allocate(Protocol.CONTROL_HEADER_SIZE).order(ByteOrder.LITTLE_ENDIAN)
        buf.putShort(Protocol.CONTROL_MAGIC.toShort())
        buf.putShort(command.toShort())
        buf.putInt(seq.toInt())
        buf.putInt(0) // arg1
        buf.putInt(0) // payload length
        buf.putLong(0) // unknown
        return buf.array()
    }

    private data class ParsedResponse(val command: Int, val arg1: Int, val payload: ByteArray)

    private fun parsePacket(data: ByteArray, length: Int): ParsedResponse {
        if (length < Protocol.CONTROL_HEADER_SIZE) {
            throw CameraControlException("control response is shorter than its header")
        }
        val buf = ByteBuffer.wrap(data, 0, length).order(ByteOrder.LITTLE_ENDIAN)
        val magic = buf.getShort(0).toInt() and 0xffff
        val command = buf.getShort(2).toInt() and 0xffff
        val arg1 = buf.getInt(8)
        val payloadLength = buf.getInt(12)
        if (magic != Protocol.CONTROL_MAGIC) {
            throw CameraControlException("control response has an invalid magic value")
        }
        val end = Protocol.CONTROL_HEADER_SIZE + payloadLength
        if (length < end) throw CameraControlException("control response payload is truncated")
        return ParsedResponse(command, arg1, data.copyOfRange(Protocol.CONTROL_HEADER_SIZE, end))
    }

    private suspend fun query(command: Int): ParsedResponse = withContext(Dispatchers.IO) {
        val seq = sequence.incrementAndGet()
        val request = buildPacket(command, seq)
        DatagramSocket().use { sock ->
            sock.soTimeout = timeoutMs
            val address = InetAddress.getByName(cameraIp)
            sock.send(DatagramPacket(request, request.size, address, commandPort))
            val buf = ByteArray(65535)
            val response = DatagramPacket(buf, buf.size)
            try {
                sock.receive(response)
            } catch (e: Exception) {
                throw CameraControlException("camera control request timed out")
            }
            if (response.address.hostAddress != cameraIp) {
                throw CameraControlException("camera control response came from an unexpected address")
            }
            val parsed = parsePacket(buf, response.length)
            if (parsed.command != command) {
                throw CameraControlException("camera control response has an unexpected command")
            }
            parsed
        }
    }

    /** Converts the camera's battery-voltage field to the vendor's percentage scale. */
    private fun batteryPercentage(encoded: Int): Int {
        val millivolts = encoded and 0xffff
        if (millivolts >= 4080) return 100
        val modifiers = listOf(
            Modifier(3750, 3750, 0.15, 50.0),
            Modifier(3520, 3530, 0.135, 20.0),
            Modifier(3450, 3450, 0.14, 10.0),
            Modifier(3390, 3390, 0.15, 1.0),
        )
        for (m in modifiers) {
            if (millivolts >= m.threshold) {
                return minOf(100, ((millivolts - m.baseline) * m.multiplier + m.offset).toInt())
            }
        }
        return 1
    }

    private data class Modifier(val threshold: Int, val baseline: Int, val multiplier: Double, val offset: Double)

    suspend fun getBatteryPercent(): Int {
        val response = query(Protocol.CMD_GET_BATTERY)
        return batteryPercentage(response.arg1)
    }

    suspend fun getDeviceInfo(): Map<String, Any?> {
        val response = query(Protocol.CMD_GET_DEVICE_INFO)
        val json = try {
            JSONObject(String(response.payload, Charsets.UTF_8))
        } catch (e: Exception) {
            throw CameraControlException("camera device-info response is not valid JSON")
        }
        val result = LinkedHashMap<String, Any?>()
        for (field in SAFE_DEVICE_INFO_FIELDS) {
            if (json.has(field)) result[field] = json.get(field)
        }
        return result
    }
}
