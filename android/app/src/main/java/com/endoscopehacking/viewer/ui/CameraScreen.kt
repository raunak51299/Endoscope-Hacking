package com.endoscopehacking.viewer.ui

import android.widget.Toast
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CameraAlt
import androidx.compose.material.icons.filled.FiberManualRecord
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.endoscopehacking.viewer.net.VideoState

@Composable
fun CameraScreen(viewModel: CameraViewModel) {
    val uiState by viewModel.uiState.collectAsState()
    val bitmap by viewModel.bitmap.collectAsState()
    val context = LocalContext.current

    LaunchedEffect(bitmap) {
        bitmap?.let { viewModel.onFrameForRecording(it) }
    }
    LaunchedEffect(uiState.message) {
        uiState.message?.let {
            Toast.makeText(context, it, Toast.LENGTH_SHORT).show()
            viewModel.consumeMessage()
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f),
            contentAlignment = Alignment.Center,
        ) {
            val frame = bitmap
            if (frame == null) {
                CircularProgressIndicator(color = Color(0xFFEF9BA4))
            } else {
                val rotation = if (uiState.mode == ViewerMode.OTOSCOPE) uiState.orientationDegrees else 0f
                val shapeModifier = if (uiState.mode == ViewerMode.OTOSCOPE) {
                    Modifier.size(320.dp).clip(CircleShape)
                } else {
                    Modifier.fillMaxWidth()
                }
                Image(
                    bitmap = frame.asImageBitmap(),
                    contentDescription = "Endoscope live view",
                    modifier = shapeModifier.graphicsLayer(rotationZ = rotation),
                )
            }
        }

        StatusBar(uiState)

        ModeRow(uiState.mode, onModeChange = viewModel::setMode)

        CaptureRow(
            isRecording = uiState.isRecording,
            onPhoto = viewModel::takePhoto,
            onToggleRecord = viewModel::toggleRecording,
        )
    }
}

@Composable
private fun StatusBar(uiState: UiState) {
    val connectionText = when (uiState.videoState) {
        VideoState.CONNECTING -> "Connecting..."
        VideoState.STREAMING -> "Streaming"
        VideoState.STALLED -> "No signal - check WiFi"
    }
    val battery = uiState.batteryPercent?.let { "Battery: $it%" } ?: "Battery: --"
    val model = uiState.deviceInfo["model"]?.toString()
    Column(modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp)) {
        Text(connectionText, color = Color(0xFFB7C7D8), style = MaterialTheme.typography.bodySmall)
        Text(
            battery + (model?.let { " * $it" } ?: ""),
            color = Color(0xFFB7C7D8),
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

@Composable
private fun ModeRow(mode: ViewerMode, onModeChange: (ViewerMode) -> Unit) {
    Box(modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp), contentAlignment = Alignment.Center) {
        androidx.compose.foundation.layout.Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = { onModeChange(ViewerMode.OTOSCOPE) },
                colors = modeColors(mode == ViewerMode.OTOSCOPE),
            ) { Text("Otoscope mode") }
            Button(
                onClick = { onModeChange(ViewerMode.DENTAL) },
                colors = modeColors(mode == ViewerMode.DENTAL),
            ) { Text("Dental mirror mode") }
        }
    }
}

@Composable
private fun modeColors(selected: Boolean) = if (selected) {
    ButtonDefaults.buttonColors(containerColor = Color(0xFFEF9BA4), contentColor = Color.Black)
} else {
    ButtonDefaults.buttonColors(containerColor = Color(0xFF111111), contentColor = Color.White)
}

@Composable
private fun CaptureRow(isRecording: Boolean, onPhoto: () -> Unit, onToggleRecord: () -> Unit) {
    Box(
        modifier = Modifier.fillMaxWidth().padding(vertical = 12.dp),
        contentAlignment = Alignment.Center,
    ) {
        CaptureButtons(onPhoto, isRecording, onToggleRecord)
    }
}

@Composable
private fun CaptureButtons(onPhoto: () -> Unit, isRecording: Boolean, onToggleRecord: () -> Unit) {
    androidx.compose.foundation.layout.Row(
        horizontalArrangement = Arrangement.spacedBy(24.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        IconButton(onClick = onPhoto) {
            Icon(Icons.Filled.CameraAlt, contentDescription = "Take photo", tint = Color.White)
        }
        IconButton(onClick = onToggleRecord) {
            Icon(
                if (isRecording) Icons.Filled.Stop else Icons.Filled.FiberManualRecord,
                contentDescription = if (isRecording) "Stop recording" else "Start recording",
                tint = if (isRecording) Color(0xFFB42318) else Color.White,
            )
        }
    }
}
