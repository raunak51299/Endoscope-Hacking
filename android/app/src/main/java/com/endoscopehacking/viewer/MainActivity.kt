package com.endoscopehacking.viewer

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.core.content.ContextCompat
import com.endoscopehacking.viewer.ui.CameraScreen
import com.endoscopehacking.viewer.ui.CameraViewModel

class MainActivity : ComponentActivity() {
    private val viewModel: CameraViewModel by viewModels()
    private val requestStoragePermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestLegacyStoragePermissionIfNeeded()
        setContent {
            EndoscopeViewerTheme {
                CameraScreen(viewModel)
            }
        }
    }

    /** Photo/video saves need this on API 24-28; API 29+ uses scoped storage instead. */
    private fun requestLegacyStoragePermissionIfNeeded() {
        if (Build.VERSION.SDK_INT > Build.VERSION_CODES.P) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.WRITE_EXTERNAL_STORAGE,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) requestStoragePermission.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
    }
}

@Composable
fun EndoscopeViewerTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = darkColorScheme(primary = androidx.compose.ui.graphics.Color(0xFFEF9BA4)),
        content = content,
    )
}
