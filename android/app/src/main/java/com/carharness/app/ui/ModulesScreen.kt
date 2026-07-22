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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState
import com.carharness.app.obd.Brands
import com.carharness.app.obd.UdsDtc
import com.carharness.app.obd.UdsModuleResult

@Composable
fun ModulesScreen(state: AppState) {
    var expanded by remember { mutableStateOf<Int?>(null) }
    var confirmAddr by remember { mutableStateOf<Int?>(null) }

    confirmAddr?.let { addr ->
        val module = state.modReport?.modules?.firstOrNull { it.address == addr }
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
                    state.runModuleClear(addr)
                }) { Text("Clear module", color = Red) }
            },
            dismissButton = {
                TextButton(onClick = { confirmAddr = null }) { Text("Cancel") }
            },
        )
    }

    val brand = state.moduleBrand
    val brandDef = Brands.byId(brand)

    LazyColumn(Modifier.fillMaxSize().padding(top = 10.dp)) {
        item {
            SectionCard {
                PanelTitle("MODULE SCAN")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Brands.all.forEach { b ->
                        BrandChip(b.label, brand == b.id) { state.chooseModuleBrand(b.id) }
                    }
                }
                Spacer(Modifier.height(10.dp))
                Text(
                    brandDef?.scanHint ?: "",
                    color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp
                )
                Spacer(Modifier.height(10.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = { state.runModuleScan() },
                        enabled = state.connState is ConnState.Connected && !state.modScanning,
                    ) {
                        Text(if (state.modScanning) "Scanning…" else "Scan all modules")
                    }
                    val ready = state.connState is ConnState.Connected &&
                        !state.modScanning && !state.extrasBusy
                    brandDef?.actions?.forEach { action ->
                        OutlinedButton(
                            onClick = { state.runBrandAction(action) },
                            enabled = ready,
                        ) {
                            Text(
                                if (state.extrasRunningId == action.id) "Reading…"
                                else action.label
                            )
                        }
                    }
                }
                if ((state.modScanning || state.extrasBusy) && state.modProgress.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(state.modProgress, color = Amber, fontSize = 12.sp)
                }
                if (state.connState !is ConnState.Connected) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Connect to the car on the Car tab first (or use Demo).",
                        color = TextFaint, fontSize = 12.sp
                    )
                }
                if (state.modError.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(state.modError, color = Red, fontSize = 13.sp, lineHeight = 18.sp)
                }
            }
        }

        // Extras card: only shown for the brand that produced it, same rule as
        // the module report below. All its text comes from the BrandAction that
        // ran (see obd/Brands.kt) — no brand names hard-coded here.
        val extrasAction = brandDef?.actions?.firstOrNull { it.id == state.extrasSource }
        if (state.extrasRead && state.extrasBrand == brand && extrasAction != null) {
            item {
                SectionCard {
                    PanelTitle(extrasAction.cardTitle)
                    if (state.extras.isEmpty()) {
                        Text(
                            extrasAction.emptyHint,
                            color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp
                        )
                    } else {
                        state.extras.forEach { r ->
                            val warn = r.value.startsWith("⚠")
                            Row(Modifier.padding(vertical = 3.dp)) {
                                Text(
                                    r.name, color = if (warn) Red else TextMuted, fontSize = 13.sp,
                                    modifier = Modifier.weight(1f)
                                )
                                Text(
                                    if (r.unit.isEmpty()) r.value else "${r.value} ${r.unit}",
                                    color = if (warn) Red else TextMain, fontSize = 13.sp,
                                    fontFamily = FontFamily.Monospace
                                )
                            }
                        }
                        Spacer(Modifier.height(4.dp))
                        Text(
                            extrasAction.refreshHint,
                            color = TextFaint, fontSize = 11.sp
                        )
                    }
                }
            }
        }

        // Only show a report produced by the currently selected brand — a BMW
        // report under a Mazda heading (or vice versa) would mislead.
        val report = state.modReport?.takeIf { it.brand == state.moduleBrand }
        report?.let {
            item {
                SectionCard {
                    if (it.simulated) {
                        Text("SIMULATED — no real car connected", color = Amber, fontSize = 12.sp)
                        Spacer(Modifier.height(6.dp))
                    }
                    it.vin?.let { vin ->
                        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            InfoChip("VIN", vin)
                        }
                        Spacer(Modifier.height(8.dp))
                    }
                    Text(it.message, color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp)
                }
            }

            val present = it.modules.filter { m -> m.present }
            val absent = it.modules.filter { m -> !m.present }

            items(count = present.size) { i ->
                val module = present[i]
                ModuleCard(
                    module = module,
                    expanded = expanded == module.address,
                    clearing = state.modClearingAddr == module.address,
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
                            absent.joinToString("   ") { m -> "${m.code} (${m.address.hex()})" },
                            color = TextFaint, fontSize = 12.sp, lineHeight = 18.sp
                        )
                    }
                }
            }
        }
    }
}

private fun Int.hex(): String = if (this > 0xFF) "0x%03X".format(this) else "0x%02X".format(this)

@Composable
private fun BrandChip(label: String, selected: Boolean, onClick: () -> Unit) {
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
private fun ModuleCard(
    module: UdsModuleResult,
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
                    module.dtcs.forEach { UdsDtcCard(it) }
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
private fun UdsDtcCard(dtc: UdsDtc) {
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
