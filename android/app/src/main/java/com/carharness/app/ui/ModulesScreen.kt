package com.carharness.app.ui

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
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState
import com.carharness.app.obd.BmwDtc
import com.carharness.app.obd.BmwModuleResult

@Composable
fun ModulesScreen(state: AppState) {
    var expanded by remember { mutableStateOf<Int?>(null) }
    var confirmAddr by remember { mutableStateOf<Int?>(null) }

    confirmAddr?.let { addr ->
        val module = state.bmwReport?.modules?.firstOrNull { it.address == addr }
        AlertDialog(
            onDismissRequest = { confirmAddr = null },
            containerColor = Panel,
            title = { Text("Clear ${module?.code ?: "module"} fault memory?", color = TextMain) },
            text = {
                Text(
                    "This erases the stored faults in ${module?.name ?: "this module"} only. " +
                        "Faults come back if the problem is still there. Freeze-frame data " +
                        "a specialist might want is lost.",
                    color = TextMuted
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmAddr = null
                    state.runBmwClear(addr)
                }) { Text("Clear module", color = Red) }
            },
            dismissButton = {
                TextButton(onClick = { confirmAddr = null }) { Text("Cancel") }
            },
        )
    }

    LazyColumn(Modifier.fillMaxSize().padding(top = 10.dp)) {
        item {
            SectionCard {
                PanelTitle("BMW MODULE SCAN")
                Text(
                    "Walks every control unit on the car — engine, gearbox, ABS, airbags, " +
                        "cluster, head unit and more — and reads each one's own fault memory. " +
                        "This is the deep scan generic OBD apps can't do.",
                    color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp
                )
                Spacer(Modifier.height(10.dp))
                Button(
                    onClick = { state.runBmwScan() },
                    enabled = state.connState is ConnState.Connected && !state.bmwScanning,
                ) {
                    Text(if (state.bmwScanning) "Scanning…" else "Scan all modules")
                }
                if (state.bmwScanning && state.bmwProgress.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(state.bmwProgress, color = Amber, fontSize = 12.sp)
                }
                if (state.connState !is ConnState.Connected) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Connect to the car on the Car tab first (or use Demo).",
                        color = TextFaint, fontSize = 12.sp
                    )
                }
                if (state.bmwError.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(state.bmwError, color = Red, fontSize = 13.sp, lineHeight = 18.sp)
                }
            }
        }

        state.bmwReport?.let { report ->
            item {
                SectionCard {
                    if (report.simulated) {
                        Text("SIMULATED — no real car connected", color = Amber, fontSize = 12.sp)
                        Spacer(Modifier.height(6.dp))
                    }
                    Text(report.message, color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp)
                }
            }

            val present = report.modules.filter { it.present }
            val absent = report.modules.filter { !it.present }

            items(count = present.size) { i ->
                val module = present[i]
                ModuleCard(
                    module = module,
                    expanded = expanded == module.address,
                    clearing = state.bmwClearingAddr == module.address,
                    onToggle = {
                        expanded = if (expanded == module.address) null else module.address
                    },
                    onClear = { confirmAddr = module.address },
                )
            }

            if (absent.isNotEmpty()) {
                item {
                    SectionCard {
                        PanelTitle("NOT FITTED / NO ANSWER")
                        Text(
                            absent.joinToString("   ") { "${it.code} (${it.address.hex()})" },
                            color = TextFaint, fontSize = 12.sp, lineHeight = 18.sp
                        )
                    }
                }
            }
        }
    }
}

private fun Int.hex(): String = "0x%02X".format(this)

@Composable
private fun ModuleCard(
    module: BmwModuleResult,
    expanded: Boolean,
    clearing: Boolean,
    onToggle: () -> Unit,
    onClear: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 14.dp, vertical = 4.dp)
            .clickable(onClick = onToggle),
        colors = CardDefaults.cardColors(containerColor = Panel),
        shape = RoundedCornerShape(14.dp),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(module.code, color = TextMain, fontSize = 16.sp)
                    Text(module.name, color = TextMuted, fontSize = 12.sp)
                }
                val faults = module.dtcs.size
                val label = when {
                    module.error != null -> "read error"
                    faults == 0 -> "OK"
                    else -> "$faults fault" + if (faults == 1) "" else "s"
                }
                val color = when {
                    module.error != null -> Amber
                    faults == 0 -> Green
                    else -> Red
                }
                Text(label, color = color, fontSize = 13.sp)
                Spacer(Modifier.width(8.dp))
                Text(if (expanded) "▾" else "▸", color = TextFaint, fontSize = 13.sp)
            }

            if (expanded) {
                Spacer(Modifier.height(10.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    InfoChip("Address", module.address.hex())
                }
                if (module.ident.isNotEmpty()) {
                    Spacer(Modifier.height(6.dp))
                    module.ident.forEach { (label, value) ->
                        Row(Modifier.padding(vertical = 2.dp)) {
                            Text(
                                label.uppercase(), color = TextFaint, fontSize = 10.sp,
                                modifier = Modifier.width(90.dp)
                            )
                            Text(value, color = TextMain, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
                        }
                    }
                }
                module.error?.let {
                    Spacer(Modifier.height(6.dp))
                    Text(it, color = Amber, fontSize = 12.sp, lineHeight = 17.sp)
                }
                Spacer(Modifier.height(6.dp))
                if (module.dtcs.isEmpty() && module.error == null) {
                    Text("✓ No faults stored in this module.", color = Green, fontSize = 13.sp)
                } else {
                    module.dtcs.forEach { BmwDtcCard(it) }
                }
                if (module.dtcs.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    OutlinedButton(onClick = onClear, enabled = !clearing) {
                        Text(if (clearing) "Clearing…" else "Clear this module")
                    }
                }
            }
        }
    }
}

@Composable
private fun BmwDtcCard(dtc: BmwDtc) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.cardColors(containerColor = PanelRaised),
        shape = RoundedCornerShape(10.dp),
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(dtc.code, color = TextMain, fontSize = 16.sp, fontFamily = FontFamily.Monospace)
                Spacer(Modifier.width(10.dp))
                Text(dtc.hex, color = Amber, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
            }
            Spacer(Modifier.height(4.dp))
            Text(dtc.description, color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp)
            if (dtc.flags.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                Text(dtc.flags.joinToString(" · "), color = TextFaint, fontSize = 11.sp)
            }
        }
    }
}
