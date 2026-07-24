package com.carharness.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
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

/**
 * The live dashboard, on its own screen now rather than buried under the
 * connect form. Big mono numbers, a trend line under each, and an honest empty
 * state that says what to do instead of showing dead tiles.
 */
@Composable
fun LiveScreen(state: AppState) {
    val connected = state.connState is ConnState.Connected
    LazyColumn(Modifier.fillMaxSize()) {
        item { StatusRail("Live", "Values straight off the engine ECU", state) }

        item {
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
                                state.liveOn -> "Polling every 400ms"
                                else -> "Off"
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

        item {
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
                        .padding(horizontal = 14.dp)
                        .height(((state.readings.size + 1) / 2 * 126).dp),
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
                                letterSpacing = 1.4.sp,
                            )
                            Spacer(Modifier.height(5.dp))
                            Row(verticalAlignment = Alignment.Bottom) {
                                val shown = if (r.value % 1.0 == 0.0) {
                                    r.value.toInt().toString()
                                } else {
                                    "%.1f".format(r.value)
                                }
                                Text(
                                    shown, color = TextMain, fontSize = 27.sp,
                                    fontFamily = Mono, fontWeight = FontWeight.Medium,
                                )
                                Spacer(Modifier.width(4.dp))
                                Text(
                                    r.unit, color = TextFaint,
                                    fontFamily = Barlow, fontSize = 11.sp,
                                    modifier = Modifier.padding(bottom = 4.dp),
                                )
                            }
                            Spacer(Modifier.height(7.dp))
                            Sparkline(
                                samples = history[r.label] ?: emptyList(),
                                modifier = Modifier.fillMaxWidth().height(26.dp),
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
                            else -> "Flip the stream on to watch the engine live."
                        },
                        color = TextMuted, fontFamily = Barlow,
                        fontSize = 14.sp, lineHeight = 20.sp,
                    )
                }
            }
        }

        item { Spacer(Modifier.height(24.dp)) }
    }
}
