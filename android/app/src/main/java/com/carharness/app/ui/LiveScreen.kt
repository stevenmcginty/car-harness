package com.carharness.app.ui

import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
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
import com.carharness.app.obd.Pids

/**
 * The live dashboard: big mono numbers with min/max holds and trend lines,
 * plus a grouped picker of every standard value the car supports — the same
 * sections as the desktop Live Data page (Engine, Fuel & air, Temperatures…),
 * so the two screens read as one tool.
 */
@Composable
fun LiveScreen(state: AppState) {
    val connected = state.connState is ConnState.Connected

    // Ask the car what it supports as soon as there is a car to ask.
    LaunchedEffect(connected) {
        if (connected) state.loadLiveCatalog()
    }

    // Hoisted so a group stays open when scrolled away and back.
    val expanded = remember { mutableStateMapOf<String, Boolean>() }

    LazyColumn(Modifier.fillMaxSize()) {
        item { StatusRail("Live", "Values straight off the ECU, min/max held", state) }

        item { StreamCard(state, connected) }

        item { Dashboard(state, connected) }

        item { PickerHeader(state, connected) }

        items(Pids.groupOrder.size) { i ->
            PickerGroup(state, Pids.groupOrder[i], expanded)
        }

        item { Spacer(Modifier.height(24.dp)) }
    }
}

// ------------------------------------------------------------------ stream --

@Composable
private fun StreamCard(state: AppState, connected: Boolean) {
    SectionCard(accent = state.liveOn) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column {
                Text(
                    "STREAM", color = TextFaint, fontFamily = Barlow,
                    fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp,
                )
                Text(
                    when {
                        !connected -> "Connect first"
                        state.liveOn -> "Watching ${state.liveSelected.size} values"
                        else -> "Off — ${state.liveSelected.size} values chosen"
                    },
                    color = if (state.liveOn) Ok else TextMuted,
                    fontFamily = Barlow, fontSize = 15.sp, fontWeight = FontWeight.Medium,
                )
            }
            Switch(
                checked = state.liveOn,
                onCheckedChange = { state.setLive(it) },
                enabled = connected,
                colors = harnessSwitchColors(),
            )
        }
        if (state.liveError.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text(
                state.liveError, color = Bad, fontFamily = Barlow,
                fontSize = 13.sp, lineHeight = 18.sp,
            )
        }
    }
}

// --------------------------------------------------------------- dashboard --

@Composable
private fun Dashboard(state: AppState, connected: Boolean) {
    // UI-side sample history and min/max holds, keyed by channel key —
    // cleared each time live streaming is switched on.
    val history = remember { mutableStateMapOf<String, List<Float>>() }
    val holds = remember { mutableStateMapOf<String, Pair<Double, Double>>() }
    LaunchedEffect(state.liveOn) {
        if (state.liveOn) { history.clear(); holds.clear() }
    }
    LaunchedEffect(state.readings) {
        state.readings.forEach { r ->
            val prev = history[r.key] ?: emptyList()
            history[r.key] = (prev + r.value.toFloat()).takeLast(40)
            val h = holds[r.key]
            holds[r.key] = if (h == null) r.value to r.value
            else minOf(h.first, r.value) to maxOf(h.second, r.value)
        }
    }

    if (state.readings.isNotEmpty()) {
        LazyVerticalGrid(
            columns = GridCells.Fixed(2),
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 14.dp)
                .height(((state.readings.size + 1) / 2 * 142).dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
            userScrollEnabled = false,
        ) {
            items(state.readings) { r ->
                Column(
                    Modifier
                        .clip(RoundedCornerShape(12.dp))
                        .background(Panel)
                        .border(1.dp, Line, RoundedCornerShape(12.dp))
                        .padding(13.dp)
                ) {
                    Text(
                        r.label.uppercase(),
                        color = TextFaint, fontFamily = Barlow,
                        fontSize = 9.sp, fontWeight = FontWeight.Bold,
                        letterSpacing = 1.4.sp, maxLines = 1,
                    )
                    Spacer(Modifier.height(5.dp))
                    Row(verticalAlignment = Alignment.Bottom) {
                        Text(
                            fmt(r.value), color = TextMain, fontSize = 26.sp,
                            fontFamily = Mono, fontWeight = FontWeight.Medium,
                            maxLines = 1,
                        )
                        Spacer(Modifier.width(4.dp))
                        Text(
                            r.unit, color = TextFaint,
                            fontFamily = Barlow, fontSize = 11.sp,
                            modifier = Modifier.padding(bottom = 4.dp),
                        )
                    }
                    Spacer(Modifier.height(6.dp))
                    Sparkline(
                        samples = history[r.key] ?: emptyList(),
                        modifier = Modifier.fillMaxWidth().height(24.dp),
                    )
                    Spacer(Modifier.height(5.dp))
                    val h = holds[r.key]
                    Text(
                        if (h != null) "min ${fmt(h.first)}   max ${fmt(h.second)}" else " ",
                        color = TextFaint, fontFamily = Mono, fontSize = 10.sp,
                        maxLines = 1,
                    )
                }
            }
        }
    } else {
        SectionCard {
            Text(
                when {
                    !connected ->
                        "Not connected. Go back to Home and hit the button — the " +
                            "dashboard fills itself once the link is up."
                    state.liveOn -> "Waiting for the first values…"
                    else -> "Choose values below, then flip the stream on."
                },
                color = TextMuted, fontFamily = Barlow,
                fontSize = 14.sp, lineHeight = 20.sp,
            )
        }
    }
}

private fun fmt(v: Double): String =
    if (v % 1.0 == 0.0) v.toInt().toString() else "%.1f".format(v)

// ------------------------------------------------------------------ picker --

@Composable
private fun PickerHeader(state: AppState, connected: Boolean) {
    Column(Modifier.padding(horizontal = 14.dp, vertical = 6.dp)) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "CHOOSE VALUES", color = TextFaint, fontFamily = Barlow,
                fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp,
            )
            SelectPill("Basics", selected = false) {
                state.setLiveKeys(Pids.all.map { it.key }, on = false)
                state.setLiveKeys(Pids.dashboardKeys, on = true)
            }
        }
        Spacer(Modifier.height(4.dp))
        Text(
            when {
                state.liveCatalogBusy -> "Asking the car which values it supports…"
                state.liveSupported != null ->
                    "This car reports ${state.liveSupported?.size ?: 0} standard values. " +
                        "Every extra value slows the refresh over a Bluetooth dongle — " +
                        "a focused handful updates fastest."
                connected -> "Showing the full standard set."
                else -> "Showing the full standard set — connect and the list trims " +
                    "itself to what this exact car answers."
            },
            color = TextMuted, fontFamily = Barlow, fontSize = 12.sp, lineHeight = 17.sp,
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun PickerGroup(
    state: AppState,
    group: String,
    expanded: MutableMap<String, Boolean>,
) {
    val sup = state.liveSupported
    val defs = Pids.all.filter { it.group == group && (sup == null || it.key in sup) }
    if (defs.isEmpty()) return

    val open = expanded[group] ?: false
    val chosen = defs.count { it.key in state.liveSelected }

    SectionCard {
        Row(
            Modifier
                .fillMaxWidth()
                .clickable { expanded[group] = !open },
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column {
                Text(
                    group, color = TextMain, fontFamily = Barlow,
                    fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
                )
                Text(
                    if (chosen > 0) "$chosen of ${defs.size} watched" else "${defs.size} values",
                    color = TextFaint, fontFamily = Barlow, fontSize = 12.sp,
                )
            }
            Text(
                if (open) "▴" else "▾",
                color = TextFaint, fontFamily = Barlow, fontSize = 14.sp,
            )
        }
        if (open) {
            Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                SelectPill("All", selected = false) {
                    state.setLiveKeys(defs.map { it.key }, on = true)
                }
                SelectPill("None", selected = false) {
                    state.setLiveKeys(defs.map { it.key }, on = false)
                }
            }
            Spacer(Modifier.height(10.dp))
            FlowRow(
                Modifier.fillMaxWidth().animateContentSize(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                defs.forEach { def ->
                    val label = if (def.unit.isNotEmpty()) "${def.label} · ${def.unit}" else def.label
                    SelectPill(label, selected = def.key in state.liveSelected) {
                        state.toggleLiveKey(def.key)
                    }
                }
            }
        }
    }
}
