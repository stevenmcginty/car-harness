package com.carharness.app.ui

import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
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
import android.content.Intent
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState
import com.carharness.app.ai.AiFaultContext
import com.carharness.app.obd.Brands
import com.carharness.app.obd.JlrAdBlue
import com.carharness.app.obd.JlrSweep
import com.carharness.app.obd.UdsDtc
import com.carharness.app.obd.UdsModuleResult
import com.carharness.app.obd.VinDecode

@OptIn(ExperimentalLayoutApi::class)
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
                }) { Text("Clear module", color = Bad) }
            },
            dismissButton = {
                TextButton(onClick = { confirmAddr = null }) { Text("Cancel") }
            },
        )
    }

    val brand = state.moduleBrand
    val brandDef = Brands.byId(brand)

    LazyColumn(Modifier.fillMaxSize()) {
        item { StatusRail("Modules", "Deep scan, straight to each control unit", state) }

        item { BrandPicker(brand) { state.chooseModuleBrand(it) } }

        item {
            SectionCard {
                Text(
                    brandDef?.scanHint ?: "",
                    color = TextMuted, fontFamily = Barlow,
                    fontSize = 13.sp, lineHeight = 19.sp,
                )
                Spacer(Modifier.height(12.dp))
                FlowRow(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    PrimaryButton(
                        if (state.modScanning) "Scanning…" else "Scan all modules",
                        onClick = { state.runModuleScan() },
                        enabled = state.connState is ConnState.Connected && !state.modScanning,
                    )
                    val ready = state.connState is ConnState.Connected &&
                        !state.modScanning && !state.extrasBusy
                    brandDef?.actions?.forEach { action ->
                        GhostButton(
                            if (state.extrasRunningId == action.id) "Reading…" else action.label,
                            onClick = { state.runBrandAction(action) },
                            // A reference view touches no car, so it stays
                            // pressable with nothing connected — that is when
                            // you most want to look an address up.
                            enabled = if (action.dataOnly != null) {
                                !state.modScanning && !state.extrasBusy
                            } else {
                                ready
                            },
                        )
                    }
                }
                // JLR job settings, each sitting under the button it configures:
                // which DID range the sweep walks, and how long the drive log
                // runs for against which watch set.
                if (brand == "jlr") {
                    // DID sweep range. The desktop has three text fields; the
                    // phone gets chips over the ranges worth walking, so the
                    // SDD-named Dxxx/DExx identifiers can actually be asked for
                    // rather than only read about in the SDD data card.
                    Spacer(Modifier.height(10.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "Sweep range", color = TextFaint, fontFamily = Barlow,
                            fontSize = 12.sp,
                        )
                        Spacer(Modifier.width(10.dp))
                        JlrSweep.RANGES.forEach { range ->
                            DurationChip(
                                label = range.label,
                                selected = state.sweepRangeId == range.id,
                                onClick = { state.sweepRangeId = range.id },
                            )
                        }
                    }
                    JlrSweep.RANGES
                        .firstOrNull { it.id == state.sweepRangeId }
                        ?.let { range ->
                            Spacer(Modifier.height(6.dp))
                            Text(
                                range.note,
                                color = TextFaint, fontFamily = Barlow,
                                fontSize = 12.sp, lineHeight = 17.sp,
                            )
                        }
                    Spacer(Modifier.height(10.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "Drive log length", color = TextFaint, fontFamily = Barlow,
                            fontSize = 12.sp,
                        )
                        Spacer(Modifier.width(10.dp))
                        listOf(60L, 180L, 600L, 1500L).forEach { secs ->
                            DurationChip(
                                label = if (secs >= 60) "${secs / 60} min" else "$secs s",
                                selected = state.driveLogSeconds == secs,
                                onClick = { state.driveLogSeconds = secs },
                            )
                        }
                    }
                    // Watch set: which DIDs the drive log polls. The chips come
                    // from JlrAdBlue.PRESETS rather than being written out here,
                    // so adding a watch is a data change in one file and the
                    // screen needs no edit. Each preset's note explains what the
                    // watch is for and what it needs from the drive.
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "Watch set", color = TextFaint, fontFamily = Barlow,
                            fontSize = 12.sp,
                        )
                        Spacer(Modifier.width(10.dp))
                        JlrAdBlue.PRESETS.forEach { preset ->
                            DurationChip(
                                label = preset.label,
                                selected = state.driveLogPresetId == preset.id,
                                onClick = { state.driveLogPresetId = preset.id },
                            )
                        }
                    }
                    JlrAdBlue.PRESETS
                        .firstOrNull { it.id == state.driveLogPresetId }
                        ?.let { preset ->
                            Spacer(Modifier.height(6.dp))
                            Text(
                                preset.note,
                                color = TextFaint, fontFamily = Barlow,
                                fontSize = 12.sp, lineHeight = 17.sp,
                            )
                        }
                }
                if ((state.modScanning || state.extrasBusy) && state.modProgress.isNotEmpty()) {
                    Spacer(Modifier.height(14.dp))
                    ScanTicker(state.modProgress)
                }
                if (state.connState !is ConnState.Connected) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "Not connected yet — the button on Home links up and walks " +
                            "every module for you in one go.",
                        color = TextFaint, fontFamily = Barlow, fontSize = 12.sp,
                    )
                }
                if (state.modError.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        state.modError, color = Bad, fontFamily = Barlow,
                        fontSize = 13.sp, lineHeight = 18.sp,
                    )
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
                            color = TextMuted, fontFamily = Barlow,
                            fontSize = 13.sp, lineHeight = 18.sp,
                        )
                    } else {
                        state.extras.forEach { r ->
                            val warn = r.value.startsWith("⚠")
                            Row(Modifier.padding(vertical = 3.dp)) {
                                Text(
                                    r.name, color = if (warn) Bad else TextMuted,
                                    fontFamily = Barlow, fontSize = 13.sp,
                                    modifier = Modifier.weight(1f)
                                )
                                Text(
                                    if (r.unit.isEmpty()) r.value else "${r.value} ${r.unit}",
                                    color = if (warn) Bad else TextMain, fontSize = 12.sp,
                                    fontFamily = Mono, fontWeight = FontWeight.Medium,
                                )
                            }
                        }
                        Spacer(Modifier.height(4.dp))
                        Text(
                            extrasAction.refreshHint,
                            color = TextFaint, fontFamily = Barlow, fontSize = 11.sp,
                        )
                    }
                    // The drive log's capture: where the CSV landed + a share
                    // hand-off. The file survives the card (and a crash) —
                    // this is only the handle onto it.
                    if (state.extrasSource == "drivelog" && state.driveLogSavedTo != null) {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            "Saved to ${state.driveLogSavedTo}",
                            color = TextFaint, fontFamily = Mono, fontSize = 10.sp,
                            lineHeight = 14.sp,
                        )
                        Spacer(Modifier.height(8.dp))
                        val ctx = LocalContext.current
                        GhostButton("Share CSV", onClick = {
                            state.shareDriveLogCsvIntent()?.let { intent ->
                                ctx.startActivity(
                                    Intent.createChooser(intent, "Share drive log"),
                                )
                            }
                        })
                    }
                }
            }
        }

        // Only show a report produced by the currently selected brand — a BMW
        // report under a Mazda heading (or vice versa) would mislead.
        val report = state.modReport?.takeIf { it.brand == state.moduleBrand }

        // Until a scan runs, headline the connect-time VIN + make/year here too
        // (same decode as the Car tab). A scan's consensus VIN replaces it below.
        if (report == null && state.connState is ConnState.Connected) {
            item { VehicleIdentityCard(state.vehicle, state.identifying) }
        }

        report?.let {
            val present = it.modules.filter { m -> m.present }
            val faults = present.sumOf { m -> m.dtcs.size }
            val noData = present.count { m -> m.error != null }
            val faulted = present.count { m -> m.error == null && m.dtcs.isNotEmpty() }
            val clean = present.size - noData - faulted

            item {
                SectionCard {
                    PanelTitle("Scan result")
                    if (it.simulated) {
                        StatusPill("SIMULATED — no real car connected", Warn)
                        Spacer(Modifier.height(10.dp))
                    }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        HealthRing(
                            ok = clean, faulted = faulted, noData = noData,
                            faultCount = faults,
                        )
                        Spacer(Modifier.width(16.dp))
                        Column(Modifier.weight(1f)) {
                            val ident = VinDecode.decode(it.vin)
                            Text(
                                ident.make ?: (if (it.vin != null) "Identified by VIN" else "Vehicle"),
                                color = TextMain, fontFamily = Barlow,
                                fontSize = 20.sp, fontWeight = FontWeight.SemiBold,
                            )
                            ident.year?.let { y ->
                                Text(
                                    "Likely $y", color = Info, fontFamily = Barlow,
                                    fontSize = 13.sp, fontWeight = FontWeight.Medium,
                                )
                            }
                            ident.marketNote?.let { n ->
                                Spacer(Modifier.height(2.dp))
                                Text(
                                    n, color = TextFaint,
                                    fontFamily = Barlow, fontSize = 10.sp, lineHeight = 14.sp,
                                )
                            }
                            Spacer(Modifier.height(8.dp))
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                StatusDot(Ok)
                                Spacer(Modifier.width(6.dp))
                                Text(
                                    "$clean clean", color = TextMuted,
                                    fontFamily = Barlow, fontSize = 11.sp,
                                )
                                if (faulted > 0) {
                                    Spacer(Modifier.width(10.dp))
                                    StatusDot(Bad)
                                    Spacer(Modifier.width(6.dp))
                                    Text(
                                        "$faulted with faults", color = TextMuted,
                                        fontFamily = Barlow, fontSize = 11.sp,
                                    )
                                }
                                if (noData > 0) {
                                    Spacer(Modifier.width(10.dp))
                                    StatusDot(Warn)
                                    Spacer(Modifier.width(6.dp))
                                    Text(
                                        "$noData no data", color = TextMuted,
                                        fontFamily = Barlow, fontSize = 11.sp,
                                    )
                                }
                            }
                        }
                    }
                    it.vin?.let { vin ->
                        Spacer(Modifier.height(12.dp))
                        Row { InfoChip("VIN", vin, Info) }
                    }
                    Spacer(Modifier.height(10.dp))
                    Text(
                        it.message, color = TextMuted, fontFamily = Barlow,
                        fontSize = 13.sp, lineHeight = 19.sp,
                    )
                }
            }

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
                    ai = AiFaultContext(
                        vehicle = it.vin?.let { v -> VinDecode.decode(v) },
                        brand = Brands.byId(it.brand)?.label,
                        module = "${module.code} — ${module.name}",
                        siblings = module.dtcs.map { d -> d.code },
                    ),
                )
            }

            val absent = it.modules.filter { m -> !m.present }
            if (absent.isNotEmpty()) {
                item {
                    AbsentCard(absent, it.absentHint)
                }
            }
        }

        item { Spacer(Modifier.height(12.dp)) }
    }
}

// ---------------------------------------------------------------- components

private fun Int.hex(): String = if (this > 0xFF) "0x%03X".format(this) else "0x%02X".format(this)

/** One selectable length for the drive log — same pill language as the
 *  absent-module chips, highlighted when it is the picked one. */
@Composable
private fun DurationChip(label: String, selected: Boolean, onClick: () -> Unit) {
    Box(
        Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(if (selected) Accent else PanelHi)
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 5.dp)
    ) {
        Text(
            label,
            color = if (selected) PanelHi else TextFaint,
            fontFamily = Mono, fontSize = 11.sp,
        )
    }
}

@Composable
private fun ModuleCard(
    module: UdsModuleResult,
    expanded: Boolean,
    clearing: Boolean,
    onToggle: () -> Unit,
    onClear: () -> Unit,
    ai: AiFaultContext? = null,
) {
    val faults = module.dtcs.size
    val statusColor = when {
        module.error != null -> Warn
        faults == 0 -> Ok
        else -> Bad
    }
    val statusLabel = when {
        module.error != null -> "no data"
        faults == 0 -> "OK"
        else -> "$faults fault" + if (faults == 1) "" else "s"
    }
    val chevron by animateFloatAsState(if (expanded) 0f else -90f, label = "chevron")
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 14.dp, vertical = 4.dp)
            .clickable(onClick = onToggle),
        colors = CardDefaults.cardColors(containerColor = Panel),
        border = BorderStroke(1.dp, Line),
        shape = RoundedCornerShape(14.dp),
    ) {
        Column(Modifier.padding(14.dp).animateContentSize()) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusDot(statusColor)
                Spacer(Modifier.width(11.dp))
                Column(Modifier.weight(1f)) {
                    Text(
                        module.code, color = TextMain, fontFamily = Barlow,
                        fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
                    )
                    Text(
                        module.name, color = TextFaint,
                        fontFamily = Barlow, fontSize = 12.sp,
                    )
                }
                StatusPill(statusLabel, statusColor)
                Spacer(Modifier.width(8.dp))
                Text(
                    "▾", color = TextFaint, fontSize = 13.sp,
                    modifier = Modifier.rotate(chevron),
                )
            }

            if (expanded) {
                Spacer(Modifier.height(12.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    InfoChip("Address", module.address.hex())
                }
                if (module.ident.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    module.ident.forEach { (label, value) ->
                        Row(Modifier.padding(vertical = 2.dp)) {
                            Text(
                                label.uppercase(), color = TextFaint,
                                fontFamily = Barlow, fontSize = 10.sp,
                                fontWeight = FontWeight.SemiBold,
                                modifier = Modifier.width(92.dp),
                                letterSpacing = 0.8.sp,
                            )
                            Text(
                                value,
                                color = if (label == "VIN") Info else TextMain,
                                fontSize = 11.sp, fontFamily = Mono,
                            )
                        }
                    }
                }
                module.error?.let {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        it, color = Warn, fontFamily = Barlow,
                        fontSize = 12.sp, lineHeight = 17.sp,
                    )
                }
                Spacer(Modifier.height(8.dp))
                if (module.dtcs.isEmpty() && module.error == null) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        StatusDot(Ok)
                        Spacer(Modifier.width(8.dp))
                        Text(
                            "No faults stored in this module.", color = Ok,
                            fontFamily = Barlow, fontSize = 13.sp,
                        )
                    }
                } else {
                    module.dtcs.forEach { UdsDtcCard(it, ai) }
                }
                if (module.trace.isNotEmpty()) {
                    Spacer(Modifier.height(8.dp))
                    ConsoleBlock("Diagnostic trail", module.trace)
                }
                if (module.dtcs.isNotEmpty()) {
                    Spacer(Modifier.height(10.dp))
                    GhostButton(
                        if (clearing) "Clearing…" else "Clear this module",
                        onClick = onClear,
                        enabled = !clearing,
                        tint = Bad,
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun AbsentCard(absent: List<UdsModuleResult>, hint: String?) {
    SectionCard {
        PanelTitle(if (hint != null) "Not reachable over this dongle" else "Not fitted / no answer")
        if (hint != null) {
            Text(
                hint, color = TextMuted, fontFamily = Barlow,
                fontSize = 12.sp, lineHeight = 18.sp,
            )
            Spacer(Modifier.height(10.dp))
        }
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            absent.forEach { m ->
                Box(
                    Modifier
                        .background(PanelHi, RoundedCornerShape(999.dp))
                        .padding(horizontal = 10.dp, vertical = 5.dp)
                ) {
                    Text(
                        "${m.code} · ${m.address.hex()}",
                        color = TextFaint, fontSize = 10.sp,
                        fontFamily = Mono,
                    )
                }
            }
        }
    }
}

@Composable
private fun UdsDtcCard(dtc: UdsDtc, ai: AiFaultContext? = null) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(PanelHi)
            .height(IntrinsicSize.Min)
    ) {
        Box(
            Modifier
                .width(3.dp)
                .fillMaxHeight()
                .background(Bad)
        )
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    dtc.code, color = TextMain, fontSize = 15.sp,
                    fontWeight = FontWeight.SemiBold, fontFamily = Mono,
                )
                Spacer(Modifier.width(10.dp))
                if (dtc.hex.isNotEmpty()) {
                    Text(
                        dtc.hex, color = Accent,
                        fontSize = 11.sp, fontFamily = Mono,
                    )
                }
            }
            Spacer(Modifier.height(4.dp))
            Text(
                dtc.description, color = TextMuted, fontFamily = Barlow,
                fontSize = 13.sp, lineHeight = 18.sp,
            )
            if (dtc.flags.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    dtc.flags.joinToString(" · "), color = TextFaint,
                    fontFamily = Barlow, fontSize = 11.sp,
                )
            }
            if (ai != null) AiExplainBlock(dtc.code, dtc.hex, ai)
        }
    }
}
