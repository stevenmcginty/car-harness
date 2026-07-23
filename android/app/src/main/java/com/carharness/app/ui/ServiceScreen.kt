package com.carharness.app.ui

import androidx.compose.foundation.BorderStroke
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState
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
        item { StatusRail("Service", "Resets & maintenance procedures", state) }

        item { BrandRail(brand) { state.chooseModuleBrand(it) } }

        item {
            SectionCard {
                PanelTitle("Service resets")
                Text(
                    "Reset the service indicators after you've done the work — oil/inspection " +
                        "counters and brake reminders. Green 'Run reset' items go over the OBD " +
                        "dongle; the rest are manual dash steps to follow.",
                    color = TextMuted, fontFamily = Barlow,
                    fontSize = 13.sp, lineHeight = 18.sp,
                )
                if (state.connState !is ConnState.Connected) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Connect to the car on the Car tab to run dongle resets (you can still " +
                            "read the manual steps without a connection).",
                        color = TextFaint, fontFamily = Barlow,
                        fontSize = 12.sp, lineHeight = 17.sp,
                    )
                }
                if (state.resetProgress.isNotEmpty()) {
                    Spacer(Modifier.height(10.dp))
                    SweepBar()
                    Spacer(Modifier.height(6.dp))
                    Text(
                        state.resetProgress, color = Amber,
                        fontFamily = Mono, fontSize = 11.sp,
                    )
                }
                if (state.resetMessage.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        state.resetMessage, color = TextMain, fontFamily = Barlow,
                        fontSize = 13.sp, lineHeight = 18.sp,
                    )
                }
                if (state.resetLog.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    ConsoleBlock("Reset log", state.resetLog.map { "✓ $it" })
                }
            }
        }

        val resets = state.resetsForBrand()
        if (resets.isEmpty()) {
            item {
                SectionCard {
                    Text(
                        "No service resets catalogued for this brand yet.",
                        color = TextMuted, fontFamily = Barlow, fontSize = 13.sp,
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

        item { Spacer(Modifier.height(12.dp)) }
    }
}

@Composable
private fun ConfidenceBadge(confidence: String) {
    val (color, label) = when (confidence) {
        "verified" -> Green to "VERIFIED"
        "community" -> Amber to "COMMUNITY"
        else -> TextFaint to "EXPERIMENTAL"
    }
    StatusPill(label, color)
}

/** "DONGLE" (sends frames) vs "DASH STEPS" (manual procedure) tag. */
@Composable
private fun MethodTag(uds: Boolean) {
    Text(
        if (uds) "OVER THE DONGLE" else "DASH PROCEDURE",
        color = if (uds) Green else TextFaint,
        fontFamily = Barlow, fontSize = 9.sp,
        fontWeight = FontWeight.SemiBold, letterSpacing = 1.4.sp,
    )
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
        border = BorderStroke(1.dp, BorderSoft),
        shape = RoundedCornerShape(14.dp),
    ) {
        Column(Modifier.padding(14.dp)) {
            MethodTag(reset.uds)
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    reset.name, color = TextMain, fontFamily = Barlow,
                    fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                ConfidenceBadge(reset.confidence)
            }
            Spacer(Modifier.height(6.dp))
            Text(
                reset.description, color = TextMuted, fontFamily = Barlow,
                fontSize = 13.sp, lineHeight = 18.sp,
            )

            if (reset.models.isNotEmpty()) {
                Spacer(Modifier.height(6.dp))
                Text(
                    reset.models, color = TextFaint,
                    fontFamily = Barlow, fontSize = 11.sp,
                )
            }

            if (reset.uds) {
                if (reset.steps.isNotEmpty()) {
                    Spacer(Modifier.height(10.dp))
                    ConsoleBlock("Sends", reset.steps.map { it.describe() })
                }
                Spacer(Modifier.height(12.dp))
                PrimaryButton(
                    if (running) "Running…" else "Run reset",
                    onClick = onRun,
                    enabled = connected && !busy,
                )
                if (!connected) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Connect to the car first.", color = TextFaint,
                        fontFamily = Barlow, fontSize = 11.sp,
                    )
                }
            } else {
                // Manual dash procedure — numbered steps, no send button.
                Spacer(Modifier.height(10.dp))
                reset.procedure.forEachIndexed { idx, step ->
                    StepRow(idx + 1, step)
                }
            }

            if (reset.cautions.isNotEmpty()) {
                Spacer(Modifier.height(10.dp))
                CautionBox(reset.cautions)
            }
        }
    }
}
