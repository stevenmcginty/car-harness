package com.carharness.app.ui

import androidx.compose.foundation.background
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
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState

@Composable
fun DesktopScreen(state: AppState) {
    LaunchedEffect(state.desktopPaired) {
        if (state.desktopPaired) state.refreshDesktop()
    }

    LazyColumn(Modifier.fillMaxSize()) {
        item { StatusRail("Desktop", "Pairing & app updates", state) }
        if (!state.desktopPaired) {
            item {
                SectionCard {
                    PanelTitle("Pair with the desktop app")
                    Text(
                        "On the PC, open Car Harness and click “Pair phone…”. Then find " +
                            "the PC here and type the 6-digit code.",
                        color = TextMuted, fontFamily = Barlow,
                        fontSize = 13.sp, lineHeight = 18.sp,
                    )
                    Spacer(Modifier.height(10.dp))
                    GhostButton(
                        if (state.discovering) "Searching…" else "Find my PC",
                        onClick = { state.discoverDesktops() },
                    )
                    state.servers.forEach { server ->
                        Column(
                            Modifier
                                .fillMaxWidth()
                                .padding(top = 6.dp)
                                .clip(RoundedCornerShape(10.dp))
                                .background(PanelHi)
                                .padding(12.dp)
                        ) {
                            Text(
                                server.name, color = TextMain, fontFamily = Barlow,
                                fontSize = 14.sp, fontWeight = FontWeight.Medium,
                            )
                            Text(
                                "${server.host}:${server.port}",
                                color = TextFaint, fontSize = 10.sp,
                                fontFamily = Mono,
                            )
                            Spacer(Modifier.height(8.dp))
                            PrimaryButton(
                                if (state.pairBusy) "Pairing…" else "Pair with this PC",
                                onClick = { state.pairWith(server.host, server.port) },
                                enabled = !state.pairBusy,
                            )
                        }
                    }
                    Spacer(Modifier.height(10.dp))
                    OutlinedTextField(
                        value = state.pairCode,
                        onValueChange = { state.pairCode = it.filter { c -> c.isDigit() }.take(6) },
                        label = { Text("6-digit code from the desktop") },
                        singleLine = true,
                        colors = harnessFieldColors(),
                    )
                    Spacer(Modifier.height(8.dp))
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        OutlinedTextField(
                            value = state.manualHost,
                            onValueChange = { state.manualHost = it },
                            label = { Text("PC address (if not found)") },
                            modifier = Modifier.width(220.dp),
                            singleLine = true,
                            colors = harnessFieldColors(),
                        )
                        PrimaryButton(
                            "Pair",
                            onClick = { state.pairWith(state.manualHost.trim(), 8765) },
                            enabled = state.manualHost.isNotBlank() && !state.pairBusy,
                        )
                    }
                    if (state.desktopError.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            state.desktopError, color = Bad, fontFamily = Barlow,
                            fontSize = 12.sp, lineHeight = 17.sp,
                        )
                    }
                }
            }
        } else {
            item {
                SectionCard {
                    PanelTitle("Desktop")
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        PulseDot(Ok)
                        Spacer(Modifier.width(8.dp))
                        Text(
                            "Paired with ${state.harness.serverName()}",
                            color = Ok, fontFamily = Barlow,
                            fontSize = 13.sp, fontWeight = FontWeight.Medium,
                        )
                    }
                    Spacer(Modifier.height(2.dp))
                    Text(
                        state.harness.host(), color = TextFaint,
                        fontFamily = Mono, fontSize = 10.sp,
                    )
                    Spacer(Modifier.height(10.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        GhostButton("Refresh", onClick = { state.refreshDesktop() })
                        GhostButton("Un-pair", onClick = { state.unpair() })
                    }
                    if (state.desktopError.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            state.desktopError, color = Bad, fontFamily = Barlow,
                            fontSize = 12.sp, lineHeight = 17.sp,
                        )
                    }
                }
            }

            item {
                SectionCard {
                    PanelTitle("Scan via the desktop's adapters")
                    Text(
                        "Runs the scan on the PC, using whatever cables/dongles the PC has " +
                            "— for the deeper jobs the phone can't reach.",
                        color = TextMuted, fontFamily = Barlow,
                        fontSize = 12.sp, lineHeight = 17.sp,
                    )
                    Spacer(Modifier.height(10.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        state.remoteAdapters.forEach { adapter ->
                            SelectPill(
                                adapter.name,
                                selected = state.remoteAdapterId == adapter.id,
                                onClick = { state.remoteAdapterId = adapter.id },
                            )
                        }
                    }
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "Simulate", color = TextMuted,
                            fontFamily = Barlow, fontSize = 12.sp,
                        )
                        Spacer(Modifier.width(8.dp))
                        Switch(
                            checked = state.remoteSim,
                            onCheckedChange = { state.remoteSim = it },
                            colors = harnessSwitchColors(),
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        PrimaryButton(
                            if (state.remoteBusy) "Working…" else "Read fault codes",
                            onClick = { state.remoteObdScan() },
                            enabled = !state.remoteBusy &&
                                (state.remoteSim || state.remoteAdapterId != null),
                        )
                        GhostButton(
                            "Clear",
                            onClick = { state.remoteObdClear() },
                            enabled = !state.remoteBusy && state.remoteScan?.ok == true,
                        )
                    }
                    if (state.remoteMessage.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            state.remoteMessage, color = Warn, fontFamily = Barlow,
                            fontSize = 12.sp, lineHeight = 17.sp,
                        )
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
                        PanelTitle("Jobs on the desktop")
                        state.remoteModules.forEach { module ->
                            Column(
                                Modifier
                                    .fillMaxWidth()
                                    .padding(vertical = 4.dp)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(PanelHi)
                                    .padding(12.dp)
                            ) {
                                Text(
                                    module.name, color = TextMain, fontFamily = Barlow,
                                    fontSize = 14.sp, fontWeight = FontWeight.Medium,
                                )
                                Text(
                                    module.profile.uppercase() +
                                        if (module.builtin) " · BUILT-IN" else "",
                                    color = TextFaint, fontFamily = Barlow,
                                    fontSize = 9.sp, letterSpacing = 1.2.sp,
                                    fontWeight = FontWeight.SemiBold,
                                )
                            }
                        }
                    }
                }
            }
        }

        item { UpdateCard() }
        item { Spacer(Modifier.height(12.dp)) }
    }
}
