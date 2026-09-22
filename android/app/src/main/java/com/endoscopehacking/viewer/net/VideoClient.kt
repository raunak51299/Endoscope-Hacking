package com.endoscopehacking.viewer.net

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException

/** Connection state surfaced to the UI. */
enum class VideoState { CONNECTING, STREAMING, STALLED }

/**
 * Talks to the camera's video/heartbeat UDP socket, reassembles frames, decodes
 * them to bitmaps, and retains the last known orientation the way the vendor
 * app does (a momentary indeterminate sample does not snap the view back to 0).
 */
class VideoClient(
    private val cameraIp: String = Protocol.CAMERA_IP,
    private val cameraPort: Int = Protocol.VIDEO_PORT,
) {
    private val assembler = FrameAssembler()

    private val _bitmap = MutableStateFlow<Bitmap?>(null)
    val bitmap: StateFlow<Bitmap?> = _bitmap.asStateFlow()

    private val _orientationDegrees = MutableStateFlow(0f)
    val orientationDegrees: StateFlow<Float> = _orientationDegrees.asStateFlow()

    private val _state = MutableStateFlow(VideoState.CONNECTING)
    val state: StateFlow<VideoState> = _state.asStateFlow()

    private val _stats = MutableStateFlow(Pair(0, 0)) // (complete, dropped)
    val stats: StateFlow<Pair<Int, Int>> = _stats.asStateFlow()

    private var socket: DatagramSocket? = null
    private var running = false

    fun start(scope: CoroutineScope) {
        if (running) return
        running = true
        // One socket shared by both loops: the camera streams video back to
        // whichever local port sent it the heartbeat, so both must use it.
        val sock = DatagramSocket(null).apply {
            reuseAddress = true
            soTimeout = 1000
            bind(InetSocketAddress(0))
        }
        socket = sock
        scope.launch(Dispatchers.IO) { receiveLoop(sock) }
        scope.launch(Dispatchers.IO) { heartbeatLoop(sock) }
    }

    fun stop() {
        running = false
        socket?.close()
    }

    private suspend fun heartbeatLoop(sock: DatagramSocket) {
        val address = InetAddress.getByName(cameraIp)
        val packet = DatagramPacket(
            Protocol.HEARTBEAT_PAYLOAD, Protocol.HEARTBEAT_PAYLOAD.size,
            InetSocketAddress(address, cameraPort),
        )
        try {
            while (running) {
                sock.send(packet)
                delay(Protocol.HEARTBEAT_INTERVAL_MS)
            }
        } catch (_: Exception) {
            // Socket closed on stop(); nothing to do.
        }
    }

    private suspend fun receiveLoop(sock: DatagramSocket) {
        var lastFrameAtMs = 0L
        try {
            val buf = ByteArray(65535)
            while (running && currentCoroutineContext().isActive) {
                val packet = DatagramPacket(buf, buf.size)
                try {
                    sock.receive(packet)
                } catch (_: SocketTimeoutException) {
                    assembler.expire()
                    if (lastFrameAtMs != 0L && System.currentTimeMillis() - lastFrameAtMs > 3000) {
                        _state.value = VideoState.STALLED
                    }
                    continue
                }
                if (packet.address?.hostAddress != cameraIp) continue
                val data = buf.copyOfRange(0, packet.length)
                val assembled = assembler.add(data) ?: continue
                lastFrameAtMs = System.currentTimeMillis()
                _state.value = VideoState.STREAMING
                _stats.value = Pair(assembler.completeCount, assembler.droppedCount)

                assembled.orientationDegrees?.let { _orientationDegrees.value = it }
                val decoded = BitmapFactory.decodeByteArray(assembled.jpeg, 0, assembled.jpeg.size)
                if (decoded != null) _bitmap.value = decoded
            }
        } catch (_: Exception) {
            // Socket closed on stop(); nothing to do.
        } finally {
            sock.close()
        }
    }
}
