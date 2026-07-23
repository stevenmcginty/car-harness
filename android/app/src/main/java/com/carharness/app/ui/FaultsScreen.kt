package com.carharness.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState

@Composable
fun FaultsScreen(state: AppState) {
    var confirmClear by remember { mutableStateOf(false) }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            containerColor = Panel,
            title = { Text("Clear fault codes?", color = TextMain) },
            text = {
                Text(
                    "This resets the engine light. Codes return if the fault is still " +
                        "present, and readiness monitors reset (matters for an MOT " +
                        "emissions check).",
                    color = TextMuted
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmClear = false
                    state.runClear()
                }) { Text("Clear codes", color = Red) }
            },
            dismissButton = {
                TextButton(onClick = { confirmClear = false }) { Text("Cancel") }
            },
        )
    }

    LazyColumn(Modifier.fillMaxSize()) {
        item { ScreenHeader("Faults", "Engine fault codes over OBD-II") }
        item {
            SectionCard {
                PanelTitle("FAULT CODES")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = { state.runScan() },
                        enabled = state.connState is ConnState.Connected && !state.scanning,
                    ) {
                        Text(if (state.scanning) "Scanning…" else "Read fault codes")
                    }
                    OutlinedButton(
                        onClick = { confirmClear = true },
                        enabled = state.scanResult != null && !state.clearing,
                    ) {
                        Text(if (state.clearing) "Clearing…" else "Clear")
                    }
                }
                if (state.connState !is ConnState.Connected) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Connect to the car on the Car tab first (or use Demo).",
                        color = TextFaint, fontSize = 12.sp
                    )
                }
                if (state.faultError.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(state.faultError, color = Red, fontSize = 13.sp, lineHeight = 18.sp)
                }
            }
        }
        state.scanResult?.let { result ->
            item {
                SectionCard {
                    ScanResultView(result, vehicle = state.vehicle)
                }
            }
        }
    }
}
