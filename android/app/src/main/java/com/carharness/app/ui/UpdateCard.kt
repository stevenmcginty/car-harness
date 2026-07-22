package com.carharness.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.update.AppUpdater
import com.carharness.app.update.ReleaseInfo
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

private enum class UpdateStage { Idle, Checking, UpToDate, Available, Downloading, Downloaded }

/** "App updates" card: check GitHub for a newer release, download, install. */
@Composable
fun UpdateCard() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var stage by remember { mutableStateOf(UpdateStage.Idle) }
    var release by remember { mutableStateOf<ReleaseInfo?>(null) }
    var apkFile by remember { mutableStateOf<File?>(null) }
    var progress by remember { mutableIntStateOf(0) }
    var error by remember { mutableStateOf("") }
    var installHint by remember { mutableStateOf(false) }
    val installed = remember { AppUpdater.installedVersion(context) }

    fun check() {
        stage = UpdateStage.Checking
        error = ""
        scope.launch {
            try {
                val info = withContext(Dispatchers.IO) { AppUpdater.checkLatest(context) }
                release = info
                stage = if (info.newer) UpdateStage.Available else UpdateStage.UpToDate
            } catch (e: Exception) {
                error = e.message ?: "Couldn't reach GitHub."
                stage = UpdateStage.Idle
            }
        }
    }

    fun downloadAndInstall() {
        val info = release ?: return
        stage = UpdateStage.Downloading
        progress = 0
        error = ""
        scope.launch {
            try {
                val file = withContext(Dispatchers.IO) {
                    AppUpdater.download(context, info) { progress = it }
                }
                apkFile = file
                stage = UpdateStage.Downloaded
                installHint = !AppUpdater.install(context, file)
            } catch (e: Exception) {
                error = e.message ?: "Download failed."
                stage = UpdateStage.Available
            }
        }
    }

    SectionCard {
        PanelTitle("APP UPDATES")
        Text("Installed version: $installed", color = TextMuted, fontSize = 13.sp)
        Spacer(Modifier.height(8.dp))

        when (stage) {
            UpdateStage.Idle, UpdateStage.Checking -> {
                OutlinedButton(onClick = { check() }, enabled = stage == UpdateStage.Idle) {
                    Text(if (stage == UpdateStage.Checking) "Checking…" else "Check for updates")
                }
            }

            UpdateStage.UpToDate -> {
                Text(
                    "✓ You're on the latest version (${release?.tag}).",
                    color = Green, fontSize = 13.sp
                )
                Spacer(Modifier.height(6.dp))
                OutlinedButton(onClick = { check() }) { Text("Check again") }
            }

            UpdateStage.Available -> {
                val info = release
                if (info != null) {
                    Text("Update available: ${info.tag}", color = Amber, fontSize = 14.sp)
                    if (info.title.isNotEmpty() && info.title != info.tag) {
                        Spacer(Modifier.height(2.dp))
                        Text(info.title, color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                    }
                    Spacer(Modifier.height(8.dp))
                    val mb = "%.1f".format(info.apkBytes / 1024.0 / 1024.0)
                    Button(onClick = { downloadAndInstall() }) {
                        Text("Download & install ($mb MB)")
                    }
                }
            }

            UpdateStage.Downloading -> {
                Text("Downloading ${release?.tag}…", color = TextMuted, fontSize = 13.sp)
                Spacer(Modifier.height(6.dp))
                if (progress >= 0) {
                    LinearProgressIndicator(
                        progress = { progress / 100f },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(4.dp))
                    Text("$progress%", color = TextFaint, fontSize = 11.sp)
                } else {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                }
            }

            UpdateStage.Downloaded -> {
                if (installHint) {
                    Text(
                        "Android needs your one-time OK first: flip on “Allow from this " +
                            "source” in the screen that just opened, come back, and tap Install.",
                        color = Amber, fontSize = 12.sp, lineHeight = 17.sp
                    )
                } else {
                    Text(
                        "Downloaded — follow the install prompt. If it didn't appear, " +
                            "tap Install.",
                        color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp
                    )
                }
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = {
                        apkFile?.let { installHint = !AppUpdater.install(context, it) }
                    }) { Text("Install") }
                    OutlinedButton(onClick = { check() }) { Text("Re-check") }
                }
            }
        }

        if (error.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text(error, color = Red, fontSize = 12.sp, lineHeight = 17.sp)
        }
    }
}
