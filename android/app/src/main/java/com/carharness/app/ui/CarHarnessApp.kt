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
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.obd.DtcRecord
import com.carharness.app.obd.ScanResult

val Bg = Color(0xFF0B0F14)
val Panel = Color(0xFF131A22)
val PanelRaised = Color(0xFF17202B)
val BorderSoft = Color(0xFF1B2530)
val TextMain = Color(0xFFE6EDF3)
val TextMuted = Color(0xFF93A3B3)
val TextFaint = Color(0xFF5C6B7A)
val Green = Color(0xFF35C766)
val Amber = Color(0xFFE8A53D)
val Red = Color(0xFFD5695F)

private val scheme = darkColorScheme(
    primary = Green,
    onPrimary = Color(0xFF07220F),
    background = Bg,
    surface = Panel,
    surfaceVariant = PanelRaised,
    onBackground = TextMain,
    onSurface = TextMain,
    secondary = Amber,
    error = Red,
)

@Composable
fun CarHarnessApp(state: AppState, requestBt: ((() -> Unit)) -> Unit) {
    MaterialTheme(colorScheme = scheme) {
        var tab by remember { mutableIntStateOf(0) }
        Scaffold(
            containerColor = Bg,
            bottomBar = {
                NavigationBar(containerColor = Panel) {
                    NavigationBarItem(
                        selected = tab == 0, onClick = { tab = 0 },
                        icon = { Text("🚗", fontSize = 20.sp) }, label = { Text("Car") }
                    )
                    NavigationBarItem(
                        selected = tab == 1, onClick = { tab = 1 },
                        icon = { Text("⚠️", fontSize = 20.sp) }, label = { Text("Faults") }
                    )
                    NavigationBarItem(
                        selected = tab == 2, onClick = { tab = 2 },
                        icon = { Text("🖥️", fontSize = 20.sp) }, label = { Text("Desktop") }
                    )
                }
            }
        ) { padding ->
            Surface(
                modifier = Modifier.fillMaxSize().padding(padding).background(Bg),
                color = Bg
            ) {
                when (tab) {
                    0 -> CarScreen(state, requestBt)
                    1 -> FaultsScreen(state)
                    else -> DesktopScreen(state)
                }
            }
        }
    }
}

// ---------------------------------------------------------------- shared bits

@Composable
fun SectionCard(content: @Composable () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 6.dp),
        colors = CardDefaults.cardColors(containerColor = Panel),
        shape = RoundedCornerShape(14.dp),
    ) {
        Column(Modifier.padding(14.dp)) { content() }
    }
}

@Composable
fun PanelTitle(text: String) {
    Text(
        text,
        color = TextMuted,
        fontSize = 12.sp,
        letterSpacing = 1.2.sp,
        modifier = Modifier.padding(bottom = 8.dp)
    )
}

@Composable
fun InfoChip(label: String, value: String, valueColor: Color = TextMain) {
    Card(
        colors = CardDefaults.cardColors(containerColor = PanelRaised),
        shape = RoundedCornerShape(999.dp),
    ) {
        Row(Modifier.padding(horizontal = 10.dp, vertical = 6.dp)) {
            Text(label.uppercase(), color = TextFaint, fontSize = 10.sp)
            Spacer(Modifier.width(6.dp))
            Text(value, color = valueColor, fontSize = 12.sp)
        }
    }
}

@Composable
fun DtcCard(record: DtcRecord) {
    val tagColor = when (record.status) {
        "stored" -> Red
        "pending" -> Amber
        else -> TextFaint
    }
    Card(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        colors = CardDefaults.cardColors(containerColor = PanelRaised),
        shape = RoundedCornerShape(10.dp),
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                Text(
                    record.code,
                    color = TextMain,
                    fontSize = 17.sp,
                    fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                )
                Spacer(Modifier.width(10.dp))
                Text(record.status.uppercase(), color = tagColor, fontSize = 10.sp)
            }
            Spacer(Modifier.height(4.dp))
            Text(record.description, color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp)
        }
    }
}

@Composable
fun ScanResultView(result: ScanResult) {
    if (result.simulated) {
        Text("SIMULATED — no real car connected", color = Amber, fontSize = 12.sp)
        Spacer(Modifier.height(6.dp))
    }
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        result.vin?.let { InfoChip("VIN", it) }
    }
    Spacer(Modifier.height(6.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        if (result.voltage.isNotEmpty()) InfoChip("Battery", result.voltage)
        InfoChip(
            "Engine light",
            if (result.milOn) "ON" else "off",
            if (result.milOn) Red else Green
        )
    }
    if (result.protocol.isNotEmpty()) {
        Spacer(Modifier.height(6.dp))
        Text(result.protocol, color = TextFaint, fontSize = 11.sp)
    }
    Spacer(Modifier.height(8.dp))
    Text(result.message, color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp)
    Spacer(Modifier.height(8.dp))
    val all = result.stored + result.pending + result.permanent
    if (all.isEmpty()) {
        Text("✓ No fault codes.", color = Green, fontSize = 14.sp)
    } else {
        all.forEach { DtcCard(it) }
    }
}
