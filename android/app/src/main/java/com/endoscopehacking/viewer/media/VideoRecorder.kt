package com.endoscopehacking.viewer.media

import android.content.ContentValues
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Paint
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.os.Build
import android.provider.MediaStore
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.locks.ReentrantLock

/**
 * Encodes the stabilized stream to an H.264 MP4 using a MediaCodec Surface
 * input, mirroring the "Start/stop recording" feature the browser relay
 * offers via `MediaRecorder`. Frames are pushed in as already-rotated bitmaps
 * so the recorded file matches what the viewer shows on screen.
 */
class VideoRecorder(
    private val width: Int = 720,
    private val height: Int = 720,
    private val frameRate: Int = 15,
    private val bitRate: Int = 4_000_000,
) {
    private var encoder: MediaCodec? = null
    private var muxer: MediaMuxer? = null
    private var inputSurface: android.view.Surface? = null
    private var trackIndex = -1
    private var muxerStarted = false
    private var tempFile: File? = null
    private val lock = ReentrantLock()
    private val recording = AtomicBoolean(false)
    private var drainThread: Thread? = null

    val isRecording: Boolean get() = recording.get()

    fun start(context: Context): Boolean {
        if (recording.get()) return false
        return try {
            val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
                setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
                setInteger(MediaFormat.KEY_BIT_RATE, bitRate)
                setInteger(MediaFormat.KEY_FRAME_RATE, frameRate)
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
            }
            val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            val surface = codec.createInputSurface()
            codec.start()

            val file = File.createTempFile("endoscope_", ".mp4", context.cacheDir)
            val mux = MediaMuxer(file.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)

            encoder = codec
            inputSurface = surface
            muxer = mux
            tempFile = file
            trackIndex = -1
            muxerStarted = false
            recording.set(true)

            drainThread = Thread(::drainLoop, "endoscope-video-drain").apply { start() }
            true
        } catch (e: Exception) {
            release()
            false
        }
    }

    /**
     * Draws one bitmap onto the encoder's input surface, rotated by [rotationDegrees] so the
     * recording matches the on-screen Otoscope stabilization. Safe to call from any thread.
     */
    fun submitFrame(bitmap: Bitmap, rotationDegrees: Float = 0f) {
        if (!recording.get()) return
        val surface = inputSurface ?: return
        lock.lock()
        try {
            val canvas: Canvas = surface.lockCanvas(null) ?: return
            try {
                canvas.drawColor(Color.BLACK)
                val scale = minOf(width.toFloat() / bitmap.width, height.toFloat() / bitmap.height)
                val matrix = Matrix().apply {
                    postScale(scale, scale)
                    postTranslate(
                        (width - bitmap.width * scale) / 2f,
                        (height - bitmap.height * scale) / 2f,
                    )
                    postRotate(rotationDegrees, width / 2f, height / 2f)
                }
                canvas.drawBitmap(bitmap, matrix, Paint(Paint.ANTI_ALIAS_FLAG or Paint.FILTER_BITMAP_FLAG))
            } finally {
                surface.unlockCanvasAndPost(canvas)
            }
        } finally {
            lock.unlock()
        }
    }

    /** Stops encoding, finalizes the MP4, and copies it into the public Movies collection. */
    fun stop(context: Context): Boolean {
        if (!recording.get()) return false
        recording.set(false)
        try {
            encoder?.signalEndOfInputStream()
        } catch (_: Exception) {
        }
        drainThread?.join(3000)
        drainThread = null

        val file = tempFile
        release()
        if (file == null || !file.exists() || file.length() == 0L) return false
        val saved = saveToMovies(context, file)
        file.delete()
        return saved
    }

    private fun drainLoop() {
        val codec = encoder ?: return
        val bufferInfo = MediaCodec.BufferInfo()
        // Runs until end-of-stream is observed (signaled by stop()) or the codec errors out.
        while (true) {
            val outputIndex = try {
                codec.dequeueOutputBuffer(bufferInfo, 10_000)
            } catch (_: Exception) {
                break
            }
            when {
                outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    trackIndex = muxer?.addTrack(codec.outputFormat) ?: -1
                    muxer?.start()
                    muxerStarted = true
                }
                outputIndex >= 0 -> {
                    val buffer: ByteBuffer? = codec.getOutputBuffer(outputIndex)
                    if (buffer != null && muxerStarted && bufferInfo.size > 0) {
                        buffer.position(bufferInfo.offset)
                        buffer.limit(bufferInfo.offset + bufferInfo.size)
                        muxer?.writeSampleData(trackIndex, buffer, bufferInfo)
                    }
                    codec.releaseOutputBuffer(outputIndex, false)
                    if (bufferInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
                }
            }
        }
    }

    private fun release() {
        try { muxer?.stop() } catch (_: Exception) { }
        try { muxer?.release() } catch (_: Exception) { }
        try { encoder?.stop() } catch (_: Exception) { }
        try { encoder?.release() } catch (_: Exception) { }
        try { inputSurface?.release() } catch (_: Exception) { }
        muxer = null
        encoder = null
        inputSurface = null
        muxerStarted = false
    }

    private fun saveToMovies(context: Context, file: File): Boolean {
        val name = "endoscope_" + PhotoSaver.timestamp() + ".mp4"
        val values = ContentValues().apply {
            put(MediaStore.Video.Media.DISPLAY_NAME, name)
            put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                put(MediaStore.Video.Media.RELATIVE_PATH, "Movies/EndoscopeViewer")
                put(MediaStore.Video.Media.IS_PENDING, 1)
            }
        }
        val resolver = context.contentResolver
        val uri = resolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values) ?: return false
        val out = resolver.openOutputStream(uri) ?: return false
        out.use { stream -> file.inputStream().use { it.copyTo(stream) } }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            values.clear()
            values.put(MediaStore.Video.Media.IS_PENDING, 0)
            resolver.update(uri, values, null, null)
        }
        return true
    }
}
