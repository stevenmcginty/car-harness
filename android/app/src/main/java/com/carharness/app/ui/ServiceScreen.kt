package com.carharness.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState
import com.carharness.app.obd.Brands
import com.carharness.app.obd.ServiceReset

@Composable
fun ServiceScreen(state: AppState) {
    var confirm by remember { mutableStateOf<ServiceReset?>(null) }
    val brand = state.moduleBrand

    confirm?.let { reset ->
        AlertDialog(
            onDismissRequest = { confirm = null },
            containerColor = Panel,
            title = { Text("Run '${reset.name}'?", color = TextMain) },
            text = {
                Column {
                    Text(
                        "This sends the reset to the ${reset.targetName.ifEmpty { "car" }} over the " +
                            "dongle and changes it. Make sure the service work is actually done first.",
                        color = TextMuted
                    )
                    if (reset.cautions.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        reset.cautions.forEach {
                            Text("• $it", color = Amber, fontSize = 12.sp, lineHeight = 17.sp)
                        }
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    confirm = null
                    state.runReset(reset)
                }) { Text("Run reset", color = Green) }
            },
            dismissButton = { TextButton(onClick = { confirm = null }) { Text("Cancel") } },
        )
    }

    LazyColumn(Modifier.fillMaxSize()) {
        item { ScreenHeader("Service", "Resets & maintenance procedures") }
        item {
            SectionCard {
                PanelTitle("SERVICE RESETS")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Brands.all.forEach { b ->
                        ResetBrandChip(b.label, brand == b.id) { state.chooseModuleBrand(b.id) }
                    }
                }
                Spacer(Modifier.height(10.dp))
                Text(
                    "Reset the service indicators after you've done the work — oil/inspection " +
                        "counters and brake reminders. Green 'Run reset' items go over the OBD " +
                        "dongle; the rest are manual dash steps to follow.",
                    color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp
                )
                if (state.connState !is ConnState.Connected) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Connect to the car on the Car tab to run dongle resets (you can still " +
                            "read the manual steps without a connection).",
                        color = TextFaint, fontSize = 12.sp, lineHeight = 17.sp
                    )
                }
                if (state.resetProgress.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(state.resetProgress, color = Amber, fontSize = 12.sp)
                }
                if (state.resetMessage.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(state.resetMessage, color = TextMain, fontSize = 13.sp, lineHeight = 18.sp)
                }
                if (state.resetLog.isNotEmpty()) {
                    Spacer(Modifier.height(6.dp))
                    state.resetLog.forEach {
                        Text("✓ $it", color = TextFaint, fontSize = 11.sp, fontFamily = FontFamily.Monospace)
                    }
                }
            }
        }

        val resets = state.resetsForBrand()
        if (resets.isEmpty()) {
            item {
                SectionCard {
                    Text(
                        "No service resets catalogued for this brand yet.",
                        color = TextMuted, fontSize = 13.sp
                    )
                }
            }
        } else {
            items(count = resets.size) { i ->
                ResetCard(
                    reset = resets[i],
                    connected = state.connState is ConnState.Connected,
                    running = state.resetRunningId == resets[i].id,
                    busy = state.resetRunningId != null,
                    onRun = { confirm = resets[i] },
                )
            }
        }
    }
}

@Composable
private fun ResetBrandChip(label: String, selected: Boolean, onClick: () -> Unit) {
    Text(
        label,
        color = if (selected) TextMain else TextFaint,
        fontSize = 13.sp,
        modifier = Modifier
            .clip(RoundedCornerShape(20.dp))
            .background(if (selected) PanelRaised else Panel)
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 7.dp)
    )
}

@Composable
private fun ConfidenceBadge(confidence: String) {
    val color = when (confidence) {
        "verified" -> Green
        "community" -> Amber
        else -> TextFaint
    }
    val label = when (confidence) {
        "verified" -> "VERIFIED"
        "community" -> "COMMUNITY"
        else -> "EXPERIMENTAL"
    }
    Text(label, color = color, fontSize = 10.sp)
}

@Composable
private fun ResetCard(
    reset: ServiceReset,
    connected: Boolean,
    running: Boolean,
    busy: Boolean,
    onRun: () -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 4.dp),
        colors = CardDefaults.cardColors(containerColor = Panel),
        shape = RoundedCornerShape(14.dp),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(reset.name, color = TextMain, fontSize = 16.sp, modifier = Modifier.weight(1f))
                ConfidenceBadge(reset.confidence)
            }
            Spacer(Modifier.height(6.dp))
            Text(reset.description, color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp)

            if (reset.models.isNotEmpty()) {
                Spacer(Modifier.height(6.dp))
                Text(reset.models, color = TextFaint, fontSize = 11.sp)
            }

            if (reset.uds) {
                if (reset.steps.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text("Sends:", color = TextFaint, fontSize = 10.sp)
                    reset.steps.forEach {
                        Text(
                            it.describe(), color = TextMuted, fontSize = 11.sp,
                            fontFamily = FontFamily.Monospace
                        )
                    }
                }
                Spacer(Modifier.height(10.dp))
                Button(onClick = onRun, enabled = connected && !busy) {
                    Text(if (running) "Running…" else "Run reset")
                }
                if (!connected) {
                    Spacer(Modifier.height(4.dp))
                    Text("Connect to the car first.", color = TextFaint, fontSize = 11.sp)
                }
            } else {
                // Manual dash procedure — show numbered steps, no send button.
                Spacer(Modifier.height(8.dp))
                Text("Steps (manual):", color = TextFaint, fontSize = 10.sp)
                Spacer(Modifier.height(4.dp))
                reset.procedure.forEachIndexed { idx, step ->
                    Row(Modifier.padding(vertical = 2.dp)) {
                        Text(
                            "${idx + 1}.", color = TextFaint, fontSize = 12.sp,
                            modifier = Modifier.width(22.dp)
                        )
                        Text(step, color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                    }
                }
            }

            if (reset.cautions.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                reset.cautions.forEach {
                    Text("⚠ $it", color = Amber, fontSize = 11.sp, lineHeight = 16.sp)
                }
            }
        }
    }
}
