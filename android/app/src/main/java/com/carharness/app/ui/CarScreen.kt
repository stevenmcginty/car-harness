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
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState

@Composable
fun CarScreen(state: AppState, requestBt: ((() -> Unit)) -> Unit) {
    LazyColumn(Modifier.fillMaxSize().padding(top = 10.dp)) {
        item {
            SectionCard {
                PanelTitle("CONNECT TO THE CAR")
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf(
                        "demo" to "Demo",
                        "ble" to "BLE",
                        "bt" to "Bluetooth",
                        "wifi" to "WiFi",
                    ).forEach { (id, label) ->
                        FilterChip(
                            selected = state.transportChoice == id,
                            onClick = {
                                state.transportChoice = id
                                if (id == "bt") requestBt { state.refreshBonded() }
                            },
                            label = { Text(label) },
                        )
                    }
                }
                Spacer(Modifier.height(10.dp))

                when (state.transportChoice) {
                    "demo" -> {
                        Text(
                            "A simulated car for trying the app — clearly labelled, no hardware needed.",
                            color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp
                        )
                        Spacer(Modifier.height(8.dp))
                        Button(onClick = { state.connect("demo") }) { Text("Connect (demo)") }
                    }

                    "ble" -> {
                        Text(
                            "For BLE dongles like the Veepeak OBDCheck BLE. Dongle must be " +
                                "plugged into the car. No Android pairing needed.",
                            color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp
                        )
                        Spacer(Modifier.height(8.dp))
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            OutlinedButton(onClick = { requestBt { state.startBleScan() } }) {
                                Text(if (state.bleScanning) "Scanning…" else "Scan for dongles")
                            }
                            if (state.bleScanning) {
                                Spacer(Modifier.width(10.dp))
                                CircularProgressIndicator(Modifier.width(18.dp).height(18.dp), strokeWidth = 2.dp)
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
                            color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp
                        )
                        Spacer(Modifier.height(8.dp))
                        OutlinedButton(onClick = { requestBt { state.refreshBonded() } }) {
                            Text("Show paired devices")
                        }
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
                            color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp
                        )
                        Spacer(Modifier.height(8.dp))
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            OutlinedTextField(
                                value = state.wifiHost,
                                onValueChange = { state.wifiHost = it },
                                label = { Text("IP") },
                                modifier = Modifier.width(180.dp),
                                singleLine = true,
                            )
                            OutlinedTextField(
                                value = state.wifiPort,
                                onValueChange = { state.wifiPort = it },
                                label = { Text("Port") },
                                modifier = Modifier.width(110.dp),
                                singleLine = true,
                            )
                        }
                        Spacer(Modifier.height(8.dp))
                        Button(onClick = { state.connect("wifi") }) { Text("Connect") }
                    }
                }

                Spacer(Modifier.height(12.dp))
                when (val cs = state.connState) {
                    is ConnState.Idle -> Text("Not connected.", color = TextFaint, fontSize = 13.sp)
                    is ConnState.Connecting -> Row(verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(Modifier.width(16.dp).height(16.dp), strokeWidth = 2.dp)
                        Spacer(Modifier.width(8.dp))
                        Text("Connecting…", color = TextMuted, fontSize = 13.sp)
                    }
                    is ConnState.Connected -> Column {
                        Text("● Connected — ${cs.label}", color = Green, fontSize = 13.sp)
                        Text(
                            "${cs.firmware} · battery ${cs.voltage}",
                            color = TextMuted, fontSize = 12.sp
                        )
                        Spacer(Modifier.height(6.dp))
                        OutlinedButton(onClick = { state.disconnect() }) { Text("Disconnect") }
                    }
                    is ConnState.Error -> Column {
                        Text(cs.message, color = Red, fontSize = 13.sp, lineHeight = 18.sp)
                    }
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
                    PanelTitle("LIVE DATA")
                    Switch(
                        checked = state.liveOn,
                        onCheckedChange = { state.setLive(it) },
                    )
                }
                if (state.liveError.isNotEmpty()) {
                    Text(state.liveError, color = Red, fontSize = 12.sp)
                }
                if (state.readings.isNotEmpty()) {
                    LazyVerticalGrid(
                        columns = GridCells.Fixed(2),
                        modifier = Modifier.fillMaxWidth().height(((state.readings.size + 1) / 2 * 86).dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                        userScrollEnabled = false,
                    ) {
                        items(state.readings) { r ->
                            Card(
                                colors = CardDefaults.cardColors(containerColor = PanelRaised),
                                shape = RoundedCornerShape(10.dp),
                            ) {
                                Column(Modifier.padding(12.dp)) {
                                    Row(verticalAlignment = Alignment.Bottom) {
                                        val shown = if (r.value % 1.0 == 0.0) {
                                            r.value.toInt().toString()
                                        } else {
                                            "%.1f".format(r.value)
                                        }
                                        Text(
                                            shown, color = TextMain, fontSize = 22.sp,
                                            fontFamily = FontFamily.Monospace
                                        )
                                        Spacer(Modifier.width(4.dp))
                                        Text(r.unit, color = TextFaint, fontSize = 11.sp)
                                    }
                                    Text(r.label.uppercase(), color = TextMuted, fontSize = 10.sp)
                                }
                            }
                        }
                    }
                } else if (state.liveOn) {
                    Text("Waiting for data…", color = TextMuted, fontSize = 12.sp)
                } else {
                    Text(
                        "Connect, then flip the switch for a Torque-style dashboard.",
                        color = TextFaint, fontSize = 12.sp
                    )
                }
            }
        }
    }
}

@Composable
private fun DeviceRow(name: String, address: String, onClick: () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(top = 6.dp).clickable(onClick = onClick),
        colors = CardDefaults.cardColors(containerColor = PanelRaised),
        shape = RoundedCornerShape(10.dp),
    ) {
        Column(Modifier.padding(10.dp)) {
            Text(name, color = TextMain, fontSize = 14.sp)
            Text(address, color = TextFaint, fontSize = 11.sp, fontFamily = FontFamily.Monospace)
        }
    }
}
