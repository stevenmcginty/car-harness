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
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState

@Composable
fun CarScreen(state: AppState, requestBt: ((() -> Unit)) -> Unit) {
    LazyColumn(Modifier.fillMaxSize()) {
        item { StatusRail("Car", "Connection & live data", state) }
        // The moment we're connected, headline what the car is (VIN + make/year).
        if (state.connState is ConnState.Connected) {
            item { VehicleIdentityCard(state.vehicle, state.identifying) }
        }
        item {
            SectionCard {
                PanelTitle("Connect to the car")
                SegmentedControl(
                    options = listOf(
                        "demo" to "Demo",
                        "ble" to "BLE",
                        "bt" to "Bluetooth",
                        "wifi" to "WiFi",
                    ),
                    selected = state.transportChoice,
                    onSelect = { id ->
                        state.transportChoice = id
                        if (id == "bt") requestBt { state.refreshBonded() }
                    },
                )
                Spacer(Modifier.height(12.dp))

                when (state.transportChoice) {
                    "demo" -> {
                        Text(
                            "A simulated car for trying the app — clearly labelled, no hardware needed.",
                            color = TextMuted, fontFamily = Barlow,
                            fontSize = 12.sp, lineHeight = 17.sp,
                        )
                        Spacer(Modifier.height(10.dp))
                        PrimaryButton("Connect (demo)", onClick = { state.connect("demo") })
                    }

                    "ble" -> {
                        Text(
                            "For BLE dongles like the Veepeak OBDCheck BLE. Dongle must be " +
                                "plugged into the car. No Android pairing needed.",
                            color = TextMuted, fontFamily = Barlow,
                            fontSize = 12.sp, lineHeight = 17.sp,
                        )
                        Spacer(Modifier.height(10.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            GhostButton(
                                if (state.bleScanning) "Scanning…" else "Scan for dongles",
                                onClick = { requestBt { state.startBleScan() } },
                            )
                            if (state.bleScanning) {
                                Spacer(Modifier.width(10.dp))
                                CircularProgressIndicator(
                                    Modifier.size(18.dp), strokeWidth = 2.dp, color = Green
                                )
                            }
                        }
                        state.bleDevices.forEach { (name, address) ->
                            DeviceRow(name, address) {
                                requestBt { state.connect("ble", address) }
                            }
                        }
                    }

                    "bt" -> {
                        Text(
                            "For Classic-Bluetooth dongles. Pair the dongle in Android " +
                                "Bluetooth settings first (PIN usually 1234), then pick it here.",
                            color = TextMuted, fontFamily = Barlow,
                            fontSize = 12.sp, lineHeight = 17.sp,
                        )
                        Spacer(Modifier.height(10.dp))
                        GhostButton(
                            "Show paired devices",
                            onClick = { requestBt { state.refreshBonded() } },
                        )
                        state.bondedDevices.forEach { (name, address) ->
                            DeviceRow(name, address) {
                                requestBt { state.connect("bt", address) }
                            }
                        }
                    }

                    "wifi" -> {
                        Text(
                            "For WiFi dongles: join the phone to the dongle's WiFi network " +
                                "(usually 'WiFi_OBDII') first.",
                            color = TextMuted, fontFamily = Barlow,
                            fontSize = 12.sp, lineHeight = 17.sp,
                        )
                        Spacer(Modifier.height(10.dp))
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
                        Spacer(Modifier.height(10.dp))
                        PrimaryButton("Connect", onClick = { state.connect("wifi") })
                    }
                }

                Spacer(Modifier.height(12.dp))
                when (val cs = state.connState) {
                    is ConnState.Idle -> Text(
                        "Not connected.", color = TextFaint,
                        fontFamily = Barlow, fontSize = 13.sp,
                    )
                    is ConnState.Connecting -> Row(verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(
                            Modifier.size(16.dp), strokeWidth = 2.dp, color = Amber
                        )
                        Spacer(Modifier.width(8.dp))
                        Text(
                            "Connecting…", color = TextMuted,
                            fontFamily = Barlow, fontSize = 13.sp,
                        )
                    }
                    is ConnState.Connected -> Column {
                        HairlineDivider()
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            PulseDot(Green)
                            Spacer(Modifier.width(8.dp))
                            Text(
                                "Connected — ${cs.label}",
                                color = Green, fontFamily = Barlow,
                                fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                            )
                        }
                        Spacer(Modifier.height(3.dp))
                        Text(
                            "${cs.firmware} · battery ${cs.voltage}",
                            color = TextMuted, fontFamily = Mono, fontSize = 11.sp,
                        )
                        Spacer(Modifier.height(10.dp))
                        GhostButton("Disconnect", onClick = { state.disconnect() })
                    }
                    is ConnState.Error -> Text(
                        cs.message, color = Red, fontFamily = Barlow,
                        fontSize = 13.sp, lineHeight = 18.sp,
                    )
                }
            }
        }

        item {
            SectionCard {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    PanelTitle("Live data")
                    Switch(
                        checked = state.liveOn,
                        onCheckedChange = { state.setLive(it) },
                        colors = harnessSwitchColors(),
                    )
                }
                if (state.liveError.isNotEmpty()) {
                    Text(
                        state.liveError, color = Red,
                        fontFamily = Barlow, fontSize = 12.sp,
                    )
                }
                // UI-side sample history so every tile carries a trend line —
                // cleared each time live streaming is switched on.
                val history = remember { mutableStateMapOf<String, List<Float>>() }
                LaunchedEffect(state.liveOn) {
                    if (state.liveOn) history.clear()
                }
                LaunchedEffect(state.readings) {
                    state.readings.forEach { r ->
                        val prev = history[r.label] ?: emptyList()
                        history[r.label] = (prev + r.value.toFloat()).takeLast(40)
                    }
                }
                if (state.readings.isNotEmpty()) {
                    LazyVerticalGrid(
                        columns = GridCells.Fixed(2),
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(((state.readings.size + 1) / 2 * 118).dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                        userScrollEnabled = false,
                    ) {
                        items(state.readings) { r ->
                            Card(
                                colors = CardDefaults.cardColors(containerColor = PanelRaised),
                                shape = RoundedCornerShape(12.dp),
                            ) {
                                Column(Modifier.padding(12.dp)) {
                                    Text(
                                        r.label.uppercase(),
                                        color = TextFaint, fontFamily = Barlow,
                                        fontSize = 9.sp, fontWeight = FontWeight.SemiBold,
                                        letterSpacing = 1.2.sp,
                                    )
                                    Spacer(Modifier.height(4.dp))
                                    Row(verticalAlignment = Alignment.Bottom) {
                                        val shown = if (r.value % 1.0 == 0.0) {
                                            r.value.toInt().toString()
                                        } else {
                                            "%.1f".format(r.value)
                                        }
                                        Text(
                                            shown, color = TextMain, fontSize = 24.sp,
                                            fontFamily = Mono, fontWeight = FontWeight.Medium,
                                        )
                                        Spacer(Modifier.width(4.dp))
                                        Text(
                                            r.unit, color = TextFaint,
                                            fontFamily = Barlow, fontSize = 11.sp,
                                            modifier = Modifier.padding(bottom = 3.dp),
                                        )
                                    }
                                    Spacer(Modifier.height(6.dp))
                                    Sparkline(
                                        samples = history[r.label] ?: emptyList(),
                                        modifier = Modifier.fillMaxWidth().height(24.dp),
                                    )
                                }
                            }
                        }
                    }
                } else if (state.liveOn) {
                    Text(
                        "Waiting for data…", color = TextMuted,
                        fontFamily = Barlow, fontSize = 12.sp,
                    )
                } else {
                    Text(
                        "Connect, then flip the switch for the live dashboard.",
                        color = TextFaint, fontFamily = Barlow, fontSize = 12.sp,
                    )
                }
            }
        }
    }
}

@Composable
private fun DeviceRow(name: String, address: String, onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(top = 6.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(PanelRaised)
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                name, color = TextMain, fontFamily = Barlow,
                fontSize = 14.sp, fontWeight = FontWeight.Medium,
            )
            Text(
                address, color = TextFaint, fontFamily = Mono, fontSize = 10.sp,
            )
        }
        Text("›", color = TextFaint, fontSize = 16.sp)
    }
}
