package com.endoscopehacking.viewer.ui

import android.app.Application
import android.graphics.Bitmap
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.endoscopehacking.viewer.media.PhotoSaver
import com.endoscopehacking.viewer.media.VideoRecorder
import com.endoscopehacking.viewer.net.ControlClient
import com.endoscopehacking.viewer.net.VideoClient
import com.endoscopehacking.viewer.net.VideoState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

enum class ViewerMode { OTOSCOPE, DENTAL }

data class UiState(
    val mode: ViewerMode = ViewerMode.OTOSCOPE,
    val videoState: VideoState = VideoState.CONNECTING,
    val orientationDegrees: Float = 0f,
    val batteryPercent: Int? = null,
    val deviceInfo: Map<String, Any?> = emptyMap(),
    val isRecording: Boolean = false,
    val recordingDurationMs: Long = 0L,
    val message: String? = null,
)

class CameraViewModel(app: Application) : AndroidViewModel(app) {
    private val videoClient = VideoClient()
    private val controlClient = ControlClient()
    private val videoRecorder = VideoRecorder()
    private var recordingTimerJob: Job? = null
    private var recordingStartedAtMs = 0L

    val bitmap: StateFlow<Bitmap?> = videoClient.bitmap

    private val _uiState = MutableStateFlow(UiState())
    val uiState: StateFlow<UiState> = _uiState.asStateFlow()

    init {
        videoClient.start(viewModelScope)
        viewModelScope.launch {
            videoClient.state.collect { state -> _uiState.update { it.copy(videoState = state) } }
        }
        viewModelScope.launch {
            videoClient.orientationDegrees.collect { deg -> _uiState.update { it.copy(orientationDegrees = deg) } }
        }
        refreshStatus()
    }

    fun setMode(mode: ViewerMode) {
        _uiState.update { it.copy(mode = mode) }
    }

    fun refreshStatus() {
        viewModelScope.launch {
            try {
                val battery = controlClient.getBatteryPercent()
                _uiState.update { it.copy(batteryPercent = battery) }
            } catch (e: Exception) {
                _uiState.update { it.copy(message = "Battery query failed: ${e.message}") }
            }
            try {
                val info = controlClient.getDeviceInfo()
                _uiState.update { it.copy(deviceInfo = info) }
            } catch (e: Exception) {
                _uiState.update { it.copy(message = "Device info query failed: ${e.message}") }
            }
        }
    }

    fun takePhoto() {
        val frame = bitmap.value ?: return
        val rotated = rotatedForCurrentMode(frame)
        viewModelScope.launch {
            val ok = PhotoSaver.save(getApplication(), rotated)
            _uiState.update { it.copy(message = if (ok) "Photo saved" else "Photo save failed") }
        }
    }

    fun toggleRecording() {
        if (videoRecorder.isRecording) {
            viewModelScope.launch {
                val ok = videoRecorder.stop(getApplication())
                stopRecordingTimer()
                _uiState.update {
                    it.copy(
                        isRecording = false,
                        recordingDurationMs = 0L,
                        message = if (ok) "Video saved" else "Video save failed",
                    )
                }
            }
        } else {
            val started = videoRecorder.start(getApplication())
            if (started) startRecordingTimer()
            _uiState.update {
                it.copy(
                    isRecording = started,
                    recordingDurationMs = if (started) 0L else it.recordingDurationMs,
                    message = if (started) null else "Could not start recording",
                )
            }
        }
    }

    private fun startRecordingTimer() {
        recordingTimerJob?.cancel()
        recordingStartedAtMs = SystemClock.elapsedRealtime()
        recordingTimerJob = viewModelScope.launch {
            while (true) {
                _uiState.update {
                    it.copy(recordingDurationMs = SystemClock.elapsedRealtime() - recordingStartedAtMs)
                }
                delay(1_000L)
            }
        }
    }

    private fun stopRecordingTimer() {
        recordingTimerJob?.cancel()
        recordingTimerJob = null
    }

    /** Feeds the currently displayed (already rotated) frame into an active recording. */
    fun onFrameForRecording(bitmap: Bitmap) {
        if (!videoRecorder.isRecording) return
        val degrees = if (_uiState.value.mode == ViewerMode.OTOSCOPE) _uiState.value.orientationDegrees else 0f
        videoRecorder.submitFrame(bitmap, degrees)
    }

    fun rotatedForCurrentMode(frame: Bitmap): Bitmap {
        if (_uiState.value.mode != ViewerMode.OTOSCOPE) return frame
        val degrees = _uiState.value.orientationDegrees
        if (degrees == 0f) return frame
        val matrix = android.graphics.Matrix().apply { postRotate(degrees) }
        return Bitmap.createBitmap(frame, 0, 0, frame.width, frame.height, matrix, true)
    }

    fun consumeMessage() {
        _uiState.update { it.copy(message = null) }
    }

    override fun onCleared() {
        super.onCleared()
        stopRecordingTimer()
        videoClient.stop()
        if (videoRecorder.isRecording) videoRecorder.stop(getApplication())
    }
}
