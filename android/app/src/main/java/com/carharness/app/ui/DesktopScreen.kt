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
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState

@Composable
fun DesktopScreen(state: AppState) {
    LaunchedEffect(state.desktopPaired) {
        if (state.desktopPaired) state.refreshDesktop()
    }

    LazyColumn(Modifier.fillMaxSize()) {
        item { ScreenHeader("Desktop", "Pairing & app updates") }
        if (!state.desktopPaired) {
            item {
                SectionCard {
                    PanelTitle("PAIR WITH THE DESKTOP APP")
                    Text(
                        "On the PC, open Car Harness and click “Pair phone…”. Then find " +
                            "the PC here and type the 6-digit code.",
                        color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp
                    )
                    Spacer(Modifier.height(10.dp))
                    OutlinedButton(onClick = { state.discoverDesktops() }) {
                        Text(if (state.discovering) "Searching…" else "Find my PC")
                    }
                    state.servers.forEach { server ->
                        Card(
                            modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
                            colors = CardDefaults.cardColors(containerColor = PanelRaised),
                            shape = RoundedCornerShape(10.dp),
                        ) {
                            Column(Modifier.padding(10.dp)) {
                                Text(server.name, color = TextMain, fontSize = 14.sp)
                                Text(
                                    "${server.host}:${server.port}",
                                    color = TextFaint, fontSize = 11.sp,
                                    fontFamily = FontFamily.Monospace
                                )
                                Spacer(Modifier.height(6.dp))
                                Button(
                                    onClick = { state.pairWith(server.host, server.port) },
                                    enabled = !state.pairBusy,
                                ) { Text(if (state.pairBusy) "Pairing…" else "Pair with this PC") }
                            }
                        }
                    }
                    Spacer(Modifier.height(10.dp))
                    OutlinedTextField(
                        value = state.pairCode,
                        onValueChange = { state.pairCode = it.filter { c -> c.isDigit() }.take(6) },
                        label = { Text("6-digit code from the desktop") },
                        singleLine = true,
                    )
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedTextField(
                            value = state.manualHost,
                            onValueChange = { state.manualHost = it },
                            label = { Text("PC address (if not found)") },
                            modifier = Modifier.width(220.dp),
                            singleLine = true,
                        )
                        Button(
                            onClick = { state.pairWith(state.manualHost.trim(), 8765) },
                            enabled = state.manualHost.isNotBlank() && !state.pairBusy,
                        ) { Text("Pair") }
                    }
                    if (state.desktopError.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(state.desktopError, color = Red, fontSize = 12.sp, lineHeight = 17.sp)
                    }
                }
            }
        } else {
            item {
                SectionCard {
                    PanelTitle("DESKTOP")
                    Text(
                        "Paired with ${state.harness.serverName()} (${state.harness.host()})",
                        color = Green, fontSize = 13.sp
                    )
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = { state.refreshDesktop() }) { Text("Refresh") }
                        OutlinedButton(onClick = { state.unpair() }) { Text("Un-pair") }
                    }
                    if (state.desktopError.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(state.desktopError, color = Red, fontSize = 12.sp, lineHeight = 17.sp)
                    }
                }
            }

            item {
                SectionCard {
                    PanelTitle("SCAN VIA THE DESKTOP'S ADAPTERS")
                    Text(
                        "Runs the scan on the PC, using whatever cables/dongles the PC has " +
                            "— for the deeper jobs the phone can't reach.",
                        color = TextMuted, fontSize = 12.sp, lineHeight = 17.sp
                    )
                    Spacer(Modifier.height(8.dp))
                    state.remoteAdapters.forEach { adapter ->
                        FilterChip(
                            selected = state.remoteAdapterId == adapter.id,
                            onClick = { state.remoteAdapterId = adapter.id },
                            label = { Text(adapter.name) },
                            modifier = Modifier.padding(end = 6.dp),
                        )
                    }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Simulate", color = TextMuted, fontSize = 12.sp)
                        Spacer(Modifier.width(8.dp))
                        Switch(checked = state.remoteSim, onCheckedChange = { state.remoteSim = it })
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(
                            onClick = { state.remoteObdScan() },
                            enabled = !state.remoteBusy && (state.remoteSim || state.remoteAdapterId != null),
                        ) { Text(if (state.remoteBusy) "Working…" else "Read fault codes") }
                        OutlinedButton(
                            onClick = { state.remoteObdClear() },
                            enabled = !state.remoteBusy && state.remoteScan?.ok == true,
                        ) { Text("Clear") }
                    }
                    if (state.remoteMessage.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(state.remoteMessage, color = Amber, fontSize = 12.sp, lineHeight = 17.sp)
                    }
                }
            }

            state.remoteScan?.let { result ->
                item {
                    SectionCard { ScanResultView(result) }
                }
            }

            if (state.remoteModules.isNotEmpty()) {
                item {
                    SectionCard {
                        PanelTitle("JOBS ON THE DESKTOP")
                        state.remoteModules.forEach { module ->
                            Card(
                                modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                                colors = CardDefaults.cardColors(containerColor = PanelRaised),
                                shape = RoundedCornerShape(10.dp),
                            ) {
                                Column(Modifier.padding(10.dp)) {
                                    Text(module.name, color = TextMain, fontSize = 14.sp)
                                    Text(
                                        module.profile.uppercase() +
                                            if (module.builtin) " · BUILT-IN" else "",
                                        color = TextFaint, fontSize = 10.sp
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }

        item { UpdateCard() }
    }
}
