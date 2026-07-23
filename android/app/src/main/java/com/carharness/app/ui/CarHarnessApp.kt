package com.carharness.app.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.R
import com.carharness.app.obd.DtcRecord
import com.carharness.app.obd.ScanResult
import com.carharness.app.obd.VehicleIdentity

// ---------------------------------------------------------------- design tokens
// A quiet graphite-blue workshop theme: deep neutral surfaces, one signal
// green for "good/primary", amber for attention, red for faults, blue for
// identity/info. Text runs a strict 3-step hierarchy (main/muted/faint).

val Bg = Color(0xFF0A0D12)
val Panel = Color(0xFF11161E)
val PanelRaised = Color(0xFF18202B)
val BorderSoft = Color(0xFF212C3A)
val TextMain = Color(0xFFEAF0F6)
val TextMuted = Color(0xFF9AA8B6)
val TextFaint = Color(0xFF5F6D7C)
val Green = Color(0xFF46D586)
val GreenInk = Color(0xFF052E17)   // text on a green fill
val Amber = Color(0xFFEDB04C)
val Red = Color(0xFFEF6F63)
val Blue = Color(0xFF64A8F5)

private val scheme = darkColorScheme(
    primary = Green,
    onPrimary = GreenInk,
    background = Bg,
    surface = Panel,
    surfaceVariant = PanelRaised,
    onBackground = TextMain,
    onSurface = TextMain,
    secondary = Blue,
    outline = BorderSoft,
    error = Red,
)

private data class TabDef(val label: String, val icon: Int)

private val TABS = listOf(
    TabDef("Car", R.drawable.ic_car),
    TabDef("Faults", R.drawable.ic_faults),
    TabDef("Modules", R.drawable.ic_modules),
    TabDef("Service", R.drawable.ic_service),
    TabDef("Desktop", R.drawable.ic_desktop),
)

@Composable
fun CarHarnessApp(state: AppState, requestBt: ((() -> Unit)) -> Unit) {
    MaterialTheme(colorScheme = scheme) {
        var tab by remember { mutableIntStateOf(0) }
        Scaffold(
            containerColor = Bg,
            bottomBar = {
                Column {
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .height(1.dp)
                            .background(BorderSoft)
                    )
                    NavigationBar(containerColor = Panel, tonalElevation = 0.dp) {
                        TABS.forEachIndexed { i, def ->
                            NavigationBarItem(
                                selected = tab == i,
                                onClick = { tab = i },
                                icon = {
                                    Icon(
                                        painterResource(def.icon),
                                        contentDescription = def.label,
                                        modifier = Modifier.size(22.dp),
                                    )
                                },
                                label = { Text(def.label, fontSize = 11.sp) },
                                colors = NavigationBarItemDefaults.colors(
                                    selectedIconColor = Green,
                                    selectedTextColor = TextMain,
                                    indicatorColor = Green.copy(alpha = 0.14f),
                                    unselectedIconColor = TextFaint,
                                    unselectedTextColor = TextFaint,
                                ),
                            )
                        }
                    }
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
                    2 -> ModulesScreen(state)
                    3 -> ServiceScreen(state)
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
        modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 5.dp),
        colors = CardDefaults.cardColors(containerColor = Panel),
        border = BorderStroke(1.dp, BorderSoft),
        shape = RoundedCornerShape(18.dp),
    ) {
        Column(Modifier.padding(16.dp)) { content() }
    }
}

@Composable
fun PanelTitle(text: String) {
    Text(
        text,
        color = TextFaint,
        fontSize = 11.sp,
        fontWeight = FontWeight.SemiBold,
        letterSpacing = 1.6.sp,
        modifier = Modifier.padding(bottom = 10.dp)
    )
}

@Composable
fun InfoChip(label: String, value: String, valueColor: Color = TextMain) {
    Card(
        colors = CardDefaults.cardColors(containerColor = PanelRaised),
        border = BorderStroke(1.dp, BorderSoft),
        shape = RoundedCornerShape(999.dp),
    ) {
        Row(
            Modifier.padding(horizontal = 11.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                label.uppercase(),
                color = TextFaint, fontSize = 9.sp,
                fontWeight = FontWeight.SemiBold, letterSpacing = 1.sp
            )
            Spacer(Modifier.width(7.dp))
            Text(
                value, color = valueColor, fontSize = 12.sp,
                fontFamily = FontFamily.Monospace
            )
        }
    }
}

/** Small tinted status pill: "OK", "2 faults", "no data", … */
@Composable
fun StatusPill(text: String, color: Color) {
    Box(
        Modifier
            .background(color.copy(alpha = 0.14f), RoundedCornerShape(999.dp))
            .padding(horizontal = 10.dp, vertical = 4.dp)
    ) {
        Text(text, color = color, fontSize = 12.sp, fontWeight = FontWeight.Medium)
    }
}

/** A round status dot used at the front of list rows. */
@Composable
fun StatusDot(color: Color) {
    Box(
        Modifier
            .size(9.dp)
            .background(color, CircleShape)
    )
}

/** Big page header: one per tab, above the first card. */
@Composable
fun ScreenHeader(title: String, subtitle: String? = null) {
    Column(Modifier.padding(horizontal = 18.dp, vertical = 8.dp)) {
        Text(
            title,
            color = TextMain,
            fontSize = 24.sp,
            fontWeight = FontWeight.SemiBold,
            letterSpacing = (-0.3).sp,
        )
        if (subtitle != null) {
            Spacer(Modifier.height(2.dp))
            Text(subtitle, color = TextFaint, fontSize = 13.sp)
        }
    }
}

/** "Mazda · likely 2016" — make and/or likely year, or null if neither decoded. */
fun vehicleDescription(v: VehicleIdentity?): String? {
    if (v == null) return null
    val parts = buildList {
        v.make?.let { add(it) }
        v.year?.let { add("likely $it") }
    }
    return if (parts.isEmpty()) null else parts.joinToString(" · ")
}

/**
 * The car's identity, headlined: big VIN + "Make · likely Year". While the
 * connect-time read is in flight it shows "Reading VIN…"; if the car never
 * answered it says so honestly — the card still shows so the user sees the
 * app tried.
 */
@Composable
fun VehicleIdentityCard(vehicle: VehicleIdentity?, identifying: Boolean) {
    SectionCard {
        PanelTitle("VEHICLE")
        val vin = vehicle?.vin
        when {
            vin != null -> {
                Text(
                    vin,
                    color = TextMain,
                    fontSize = 21.sp,
                    fontWeight = FontWeight.SemiBold,
                    fontFamily = FontFamily.Monospace,
                    letterSpacing = 1.sp,
                )
                vehicleDescription(vehicle)?.let { line ->
                    Spacer(Modifier.height(5.dp))
                    Text(line, color = Blue, fontSize = 15.sp, fontWeight = FontWeight.Medium)
                }
                vehicle.marketNote?.let {
                    Spacer(Modifier.height(2.dp))
                    Text(it, color = TextFaint, fontSize = 11.sp)
                }
                if (vehicle.make == null) {
                    Spacer(Modifier.height(4.dp))
                    Text("Make not recognised from the VIN.", color = TextFaint, fontSize = 11.sp)
                }
            }
            identifying -> Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp, color = Blue)
                Spacer(Modifier.width(10.dp))
                Text("Reading VIN…", color = TextMuted, fontSize = 13.sp)
            }
            else -> Text(
                "VIN not answered — is the ignition on? The app asked the car for its VIN " +
                    "over OBD Mode 09 the moment it connected.",
                color = Amber, fontSize = 13.sp, lineHeight = 18.sp,
            )
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
        border = BorderStroke(1.dp, BorderSoft),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    record.code,
                    color = TextMain,
                    fontSize = 17.sp,
                    fontWeight = FontWeight.SemiBold,
                    fontFamily = FontFamily.Monospace,
                )
                Spacer(Modifier.width(10.dp))
                StatusPill(record.status, tagColor)
            }
            Spacer(Modifier.height(5.dp))
            Text(record.description, color = TextMuted, fontSize = 13.sp, lineHeight = 18.sp)
        }
    }
}

@Composable
fun ScanResultView(result: ScanResult) {
    if (result.simulated) {
        StatusPill("SIMULATED — no real car connected", Amber)
        Spacer(Modifier.height(8.dp))
    }
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        result.vin?.let { InfoChip("VIN", it, Blue) }
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
        Row(verticalAlignment = Alignment.CenterVertically) {
            StatusDot(Green)
            Spacer(Modifier.width(8.dp))
            Text("No fault codes.", color = Green, fontSize = 14.sp)
        }
    } else {
        all.forEach { DtcCard(it) }
    }
}
