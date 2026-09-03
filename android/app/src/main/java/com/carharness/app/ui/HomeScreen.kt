package com.carharness.app.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState
import com.carharness.app.JobStep
import com.carharness.app.R
import com.carharness.app.StepState

/**
 * The screen the app opens on, and the only one a normal job needs.
 *
 * The shape is deliberate: what car is this → one big button → what it found.
 * Everything that used to be a five-tab errand (pick transport, connect, pick
 * manufacturer, read codes, scan modules) happens behind that one button, and
 * the tabs are left as the place you go to drill in, not the place you go to
 * get work done.
 */
@Composable
fun HomeScreen(
    state: AppState,
    requestBt: ((() -> Unit)) -> Unit,
    onOpenTab: (Int) -> Unit,
    onOpenDesktop: () -> Unit,
) {
    val connected = state.connState is ConnState.Connected
    // The connect panel opens itself when there's nothing to connect with, and
    // stays out of the way once the link is up.
    var showConnect by remember { mutableStateOf(false) }
    LaunchedEffect(state.jobNeedsSetup) {
        if (state.jobNeedsSetup) showConnect = true
    }
    LaunchedEffect(connected) {
        if (connected) showConnect = false
    }

    LazyColumn(Modifier.fillMaxSize()) {

        item { HomeHeader(state, onOpenDesktop) }

        item { VehicleHero(state) }

        // ---- the one button ------------------------------------------------
        item {
            val ready = connected || state.canAutoConnect()
            Column(Modifier.padding(horizontal = 14.dp, vertical = 8.dp)) {
                BigActionButton(
                    label = when {
                        state.jobRunning -> "Scanning…"
                        !ready -> "Set up my dongle"
                        connected -> "Scan this car"
                        else -> "Connect & scan"
                    },
                    sublabel = when {
                        state.jobRunning -> "Leave the ignition on"
                        !ready -> "One-off — pick your dongle, then it's one tap forever"
                        else -> "Link, VIN, fault codes and every module — one go"
                    },
                    busy = state.jobRunning,
                    onClick = {
                        if (!ready) {
                            showConnect = true
                        } else {
                            requestBt { state.runFullScan() }
                        }
                    },
                )
                if (state.jobRunning) {
                    Spacer(Modifier.height(8.dp))
                    GhostButton(
                        "Stop",
                        onClick = { state.cancelFullScan() },
                        modifier = Modifier.fillMaxWidth(),
                        tint = TextMuted,
                    )
                }
            }
        }

        // ---- what the job is doing / did ------------------------------------
        if (state.jobSteps.isNotEmpty()) {
            item { JobPanel(state) }
        }

        if (state.jobDone && !state.jobRunning) {
            item { ResultSummary(state, onOpenTab) }
        }

        // ---- setup, tucked underneath ---------------------------------------
        item {
            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 14.dp, vertical = 6.dp)
                    .clip(RoundedCornerShape(12.dp))
                    .background(Panel)
                    .border(1.dp, Line, RoundedCornerShape(12.dp))
                    .clickable { showConnect = !showConnect }
                    .padding(horizontal = 14.dp, vertical = 14.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    "CONNECTION", color = TextFaint, fontFamily = Barlow,
                    fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp,
                )
                Spacer(Modifier.weight(1f))
                Text(
                    connectionSummary(state),
                    color = if (connected) Ok else TextMuted,
                    fontFamily = Barlow, fontSize = 13.sp, fontWeight = FontWeight.Medium,
                )
                Spacer(Modifier.width(8.dp))
                Text(if (showConnect) "▾" else "▸", color = Accent, fontSize = 13.sp)
            }
        }

        item {
            AnimatedVisibility(visible = showConnect) {
                Column { ConnectPanel(state, requestBt) }
            }
        }

        item { BrandPicker(state.moduleBrand, state.moduleBrandFromVin) { state.chooseModuleBrand(it) } }

        item { UpdateCard() }

        item { Spacer(Modifier.height(24.dp)) }
    }
}

private fun connectionSummary(state: AppState): String = when (val cs = state.connState) {
    is ConnState.Connected -> cs.label
    is ConnState.Connecting -> "Connecting…"
    is ConnState.Error -> "Link down"
    else -> when {
        state.lastAddressName.isNotEmpty() -> "${state.lastAddressName} (saved)"
        state.canAutoConnect() -> "Ready"
        else -> "Not set up"
    }
}

// ---------------------------------------------------------------- header

@Composable
private fun HomeHeader(state: AppState, onOpenDesktop: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(start = 18.dp, end = 12.dp, top = 14.dp, bottom = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                "CAR HARNESS", color = TextMain, fontFamily = Barlow,
                fontSize = 15.sp, fontWeight = FontWeight.Bold, letterSpacing = 3.sp,
            )
            Text(
                "Diagnostics", color = TextFaint, fontFamily = Barlow,
                fontSize = 11.sp, letterSpacing = 0.5.sp,
            )
        }
        LinkChip(state)
        Spacer(Modifier.width(8.dp))
        Box(
            Modifier
                .size(44.dp)
                .clip(RoundedCornerShape(10.dp))
                .background(PanelHi)
                .border(1.dp, Line, RoundedCornerShape(10.dp))
                .clickable(onClick = onOpenDesktop),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                painterResource(R.drawable.ic_desktop),
                contentDescription = "Desktop pairing",
                modifier = Modifier.size(19.dp),
                tint = TextMuted,
            )
        }
    }
}

// ---------------------------------------------------------------- hero

/**
 * What car is this — the first question on any forecourt. Big make, likely
 * year, VIN in mono. Before a scan it says so plainly rather than showing an
 * empty frame.
 */
@Composable
private fun VehicleHero(state: AppState) {
    val v = state.vehicle
    // Captured locally: `vin` lives in the :core module now, and Kotlin won't
    // smart-cast a property across a module boundary.
    val vin = v?.vin
    SectionCard(accent = vin != null) {
        PanelTitle("Vehicle")
        when {
            v != null && vin != null -> {
                Text(
                    v.make ?: "Unknown make",
                    color = TextMain, fontFamily = Barlow,
                    fontSize = 32.sp, fontWeight = FontWeight.Bold,
                    letterSpacing = (-1.0).sp,
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    v.year?.let { y ->
                        Text(
                            "Likely $y", color = Info, fontFamily = Barlow,
                            fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
                        )
                        Spacer(Modifier.width(10.dp))
                    }
                    v.marketNote?.let {
                        Text(it, color = TextFaint, fontFamily = Barlow, fontSize = 11.sp)
                    }
                }
                HairlineDivider()
                Text(
                    vin, color = TextMain, fontFamily = Mono,
                    fontSize = 16.sp, fontWeight = FontWeight.Medium,
                    letterSpacing = 1.2.sp,
                )
                Text(
                    "VIN", color = TextFaint, fontFamily = Barlow,
                    fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.6.sp,
                )
            }

            state.identifying -> Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp, color = Accent)
                Spacer(Modifier.width(10.dp))
                Text("Reading the VIN…", color = TextMuted, fontFamily = Barlow, fontSize = 14.sp)
            }

            state.connState is ConnState.Connected -> Text(
                "Connected, but the car didn't answer the VIN request. Turn the " +
                    "ignition on (engine off is fine) and scan again.",
                color = Warn, fontFamily = Barlow, fontSize = 14.sp, lineHeight = 20.sp,
            )

            else -> Text(
                "No car yet. Plug the dongle in, ignition on, then hit the button.",
                color = TextMuted, fontFamily = Barlow, fontSize = 15.sp, lineHeight = 21.sp,
            )
        }
    }
}

// ---------------------------------------------------------------- job panel

@Composable
private fun JobPanel(state: AppState) {
    SectionCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            PanelTitle(if (state.jobRunning) "Working" else "Last scan")
            Spacer(Modifier.weight(1f))
            if (state.jobRunning) {
                Text(
                    "${state.jobSteps.count { it.state == StepState.Done }}/${state.jobSteps.size}",
                    color = TextFaint, fontFamily = Mono, fontSize = 11.sp,
                    modifier = Modifier.padding(bottom = 10.dp),
                )
            }
        }
        state.jobSteps.forEach { step -> JobStepRow(step) }
        if (state.jobRunning && state.modProgress.isNotEmpty()) {
            Spacer(Modifier.height(12.dp))
            ScanTicker(state.modProgress)
        }
    }
}

@Composable
private fun JobStepRow(step: JobStep) {
    val tint = when (step.state) {
        StepState.Done -> Ok
        StepState.Running -> Accent
        StepState.Failed -> Bad
        StepState.Skipped -> Warn
        StepState.Pending -> TextFaint
    }
    Row(
        Modifier.fillMaxWidth().padding(vertical = 7.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Box(Modifier.size(20.dp), contentAlignment = Alignment.Center) {
            when (step.state) {
                StepState.Running -> CircularProgressIndicator(
                    Modifier.size(15.dp), strokeWidth = 2.dp, color = Accent
                )
                StepState.Pending -> Box(
                    Modifier
                        .size(11.dp)
                        .border(1.5.dp, TextFaint.copy(alpha = 0.6f), CircleShape)
                )
                else -> Box(
                    Modifier.size(18.dp).background(tint.copy(alpha = 0.18f), CircleShape),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        when (step.state) {
                            StepState.Done -> "✓"
                            StepState.Failed -> "✕"
                            else -> "–"
                        },
                        color = tint, fontSize = 11.sp, fontWeight = FontWeight.Bold,
                    )
                }
            }
        }
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                step.label,
                color = if (step.state == StepState.Pending) TextFaint else TextMain,
                fontFamily = Barlow, fontSize = 15.sp,
                fontWeight = if (step.state == StepState.Running) FontWeight.Bold
                else FontWeight.Medium,
            )
            if (step.note.isNotEmpty()) {
                Spacer(Modifier.height(2.dp))
                Text(
                    step.note,
                    color = when (step.state) {
                        StepState.Failed -> Bad
                        StepState.Skipped -> Warn
                        else -> TextMuted
                    },
                    fontFamily = Barlow, fontSize = 12.sp, lineHeight = 17.sp,
                )
            }
        }
    }
}

// ---------------------------------------------------------------- results

/**
 * The answer, three tiles wide: what's wrong, what answered, what's due. Each
 * one is the door to the tab that holds the detail — so the tabs are somewhere
 * you're sent, never somewhere you have to remember to go.
 */
@Composable
private fun ResultSummary(state: AppState, onOpenTab: (Int) -> Unit) {
    val faults = state.jobFaultTotal()
    val report = state.modReport
    val answered = report?.modules?.count { it.present } ?: 0
    val resets = state.resetsForBrand().size

    SectionCard(accent = faults > 0) {
        PanelTitle("Result")
        Row(verticalAlignment = Alignment.CenterVertically) {
            HealthRing(
                ok = report?.modules?.count { it.present && it.dtcs.isEmpty() && it.error == null } ?: 0,
                faulted = report?.modules?.count { it.dtcs.isNotEmpty() } ?: 0,
                noData = report?.modules?.count { it.error != null } ?: 0,
                faultCount = faults,
                ringSize = 88.dp,
            )
            Spacer(Modifier.width(16.dp))
            Column(Modifier.weight(1f)) {
                Text(
                    if (faults == 0) "Nothing stored" else
                        if (faults == 1) "1 fault found" else "$faults faults found",
                    color = if (faults == 0) Ok else Bad,
                    fontFamily = Barlow, fontSize = 20.sp, fontWeight = FontWeight.Bold,
                    letterSpacing = (-0.4).sp,
                )
                Spacer(Modifier.height(3.dp))
                Text(
                    if (faults == 0)
                        "No codes in the engine ECU or any module that answered."
                    else "Tap through for the plain-English detail on each one.",
                    color = TextMuted, fontFamily = Barlow,
                    fontSize = 13.sp, lineHeight = 18.sp,
                )
            }
        }
        Spacer(Modifier.height(14.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            SummaryTile("Faults", "$faults", if (faults == 0) Ok else Bad, Modifier.weight(1f)) {
                onOpenTab(1)
            }
            SummaryTile("Modules", "$answered", Accent, Modifier.weight(1f)) { onOpenTab(2) }
            SummaryTile("Resets", "$resets", Accent, Modifier.weight(1f)) { onOpenTab(3) }
        }
    }
}

@Composable
private fun SummaryTile(
    label: String,
    value: String,
    tint: Color,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Column(
        modifier
            .clip(RoundedCornerShape(10.dp))
            .background(PanelHi)
            .border(1.dp, Line, RoundedCornerShape(10.dp))
            .clickable(onClick = onClick)
            .padding(vertical = 13.dp, horizontal = 12.dp),
    ) {
        Text(
            value, color = tint, fontFamily = Mono,
            fontSize = 24.sp, fontWeight = FontWeight.Medium,
        )
        Spacer(Modifier.height(2.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                label.uppercase(), color = TextFaint, fontFamily = Barlow,
                fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp,
            )
            Spacer(Modifier.weight(1f))
            Text("›", color = TextFaint, fontSize = 14.sp)
        }
    }
}

// ---------------------------------------------------------------- connect

/**
 * The setup you do once. Lives collapsed under the button because after the
 * first successful link the app remembers the dongle and never needs it again.
 */
@Composable
fun ConnectPanel(state: AppState, requestBt: ((() -> Unit)) -> Unit) {
    SectionCard {
        PanelTitle("How the phone reaches the car")
        SegmentedControl(
            options = listOf(
                "ble" to "BLE",
                "bt" to "Bluetooth",
                "wifi" to "WiFi",
                "demo" to "Demo",
            ),
            selected = state.transportChoice,
            onSelect = { id ->
                state.transportChoice = id
                if (id == "bt") requestBt { state.refreshBonded() }
            },
        )
        Spacer(Modifier.height(14.dp))

        when (state.transportChoice) {
            "demo" -> {
                Text(
                    "A simulated car for trying the app — clearly labelled, no hardware needed.",
                    color = TextMuted, fontFamily = Barlow, fontSize = 13.sp, lineHeight = 18.sp,
                )
                Spacer(Modifier.height(12.dp))
                PrimaryButton("Connect (demo)", onClick = { state.connect("demo") })
            }

            "ble" -> {
                Text(
                    "For BLE dongles like the Veepeak OBDCheck BLE. Plug it into the car " +
                        "first — no Android pairing needed.",
                    color = TextMuted, fontFamily = Barlow, fontSize = 13.sp, lineHeight = 18.sp,
                )
                Spacer(Modifier.height(12.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    GhostButton(
                        if (state.bleScanning) "Scanning…" else "Find my dongle",
                        onClick = { requestBt { state.startBleScan() } },
                    )
                    if (state.bleScanning) {
                        Spacer(Modifier.width(12.dp))
                        CircularProgressIndicator(
                            Modifier.size(18.dp), strokeWidth = 2.dp, color = Accent
                        )
                    }
                }
                state.bleDevices.forEach { (name, address) ->
                    DeviceRow(name, address, address == state.lastAddress) {
                        requestBt { state.connect("ble", address) }
                    }
                }
            }

            "bt" -> {
                Text(
                    "For Classic-Bluetooth dongles. Pair the dongle in Android Bluetooth " +
                        "settings first (cheap clones use PIN 1234; the OBDLink MX+ pairs " +
                        "via its button, no PIN), then pick it here.",
                    color = TextMuted, fontFamily = Barlow, fontSize = 13.sp, lineHeight = 18.sp,
                )
                Spacer(Modifier.height(12.dp))
                GhostButton(
                    "Show paired devices",
                    onClick = { requestBt { state.refreshBonded() } },
                )
                state.bondedDevices.forEach { (name, address) ->
                    DeviceRow(name, address, address == state.lastAddress) {
                        requestBt { state.connect("bt", address) }
                    }
                }
            }

            "wifi" -> {
                Text(
                    "For WiFi dongles: join the phone to the dongle's WiFi network " +
                        "(usually 'WiFi_OBDII') first.",
                    color = TextMuted, fontFamily = Barlow, fontSize = 13.sp, lineHeight = 18.sp,
                )
                Spacer(Modifier.height(12.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(
                        value = state.wifiHost,
                        onValueChange = { state.wifiHost = it },
                        label = { Text("IP") },
                        modifier = Modifier.width(180.dp),
                        singleLine = true,
                        colors = harnessFieldColors(),
                    )
                    OutlinedTextField(
                        value = state.wifiPort,
                        onValueChange = { state.wifiPort = it },
                        label = { Text("Port") },
                        modifier = Modifier.width(110.dp),
                        singleLine = true,
                        colors = harnessFieldColors(),
                    )
                }
                Spacer(Modifier.height(12.dp))
                PrimaryButton("Connect", onClick = { state.connect("wifi") })
            }
        }

        Spacer(Modifier.height(12.dp))
        when (val cs = state.connState) {
            is ConnState.Connected -> {
                HairlineDivider()
                Row(verticalAlignment = Alignment.CenterVertically) {
                    PulseDot(Ok)
                    Spacer(Modifier.width(9.dp))
                    Text(
                        "Connected — ${cs.label}", color = Ok, fontFamily = Barlow,
                        fontSize = 15.sp, fontWeight = FontWeight.Bold,
                    )
                }
                Spacer(Modifier.height(3.dp))
                Text(
                    "${cs.firmware} · battery ${cs.voltage}",
                    color = TextMuted, fontFamily = Mono, fontSize = 11.sp,
                )
                Spacer(Modifier.height(12.dp))
                GhostButton("Disconnect", onClick = { state.disconnect() })
            }
            is ConnState.Error -> Text(
                cs.message, color = Bad, fontFamily = Barlow,
                fontSize = 13.sp, lineHeight = 18.sp,
            )
            is ConnState.Connecting -> Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp, color = Accent)
                Spacer(Modifier.width(9.dp))
                Text("Connecting…", color = TextMuted, fontFamily = Barlow, fontSize = 14.sp)
            }
            else -> {}
        }
    }
}

@Composable
private fun DeviceRow(name: String, address: String, remembered: Boolean, onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(top = 8.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(PanelHi)
            .border(
                1.dp,
                if (remembered) Accent.copy(alpha = 0.5f) else Line,
                RoundedCornerShape(10.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 13.dp, vertical = 13.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    name, color = TextMain, fontFamily = Barlow,
                    fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
                )
                if (remembered) {
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "SAVED", color = Accent, fontFamily = Barlow, fontSize = 9.sp,
                        fontWeight = FontWeight.Bold, letterSpacing = 1.2.sp,
                    )
                }
            }
            Text(address, color = TextFaint, fontFamily = Mono, fontSize = 10.sp)
        }
        Text("›", color = Accent, fontSize = 18.sp)
    }
}
