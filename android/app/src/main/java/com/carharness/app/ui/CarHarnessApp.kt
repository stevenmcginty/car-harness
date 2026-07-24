package com.carharness.app.ui

import androidx.compose.animation.Crossfade
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.R
import com.carharness.app.ai.AiFaultContext
import com.carharness.app.obd.DtcRecord
import com.carharness.app.obd.ScanResult
import com.carharness.app.obd.VehicleIdentity
import com.carharness.app.obd.VinDecode

private val scheme = darkColorScheme(
    primary = Accent,
    onPrimary = AccentInk,
    background = Ink,
    surface = Panel,
    surfaceVariant = PanelHi,
    onBackground = TextMain,
    onSurface = TextMain,
    secondary = Info,
    outline = Line,
    error = Bad,
)

private data class TabDef(val label: String, val icon: Int)

/**
 * Five tabs, and the first one is the whole job. Faults / Modules / Service
 * are where Home sends you when you tap a result; Live is the dashboard.
 * Desktop pairing is a once-a-year errand, so it sits behind the icon in the
 * Home header rather than eating a fifth of the bottom bar forever.
 */
private val TABS = listOf(
    TabDef("Home", R.drawable.ic_car),
    TabDef("Faults", R.drawable.ic_faults),
    TabDef("Modules", R.drawable.ic_modules),
    TabDef("Service", R.drawable.ic_service),
    TabDef("Live", R.drawable.ic_live),
)

private const val TAB_DESKTOP = 5

@Composable
fun CarHarnessApp(state: AppState, requestBt: ((() -> Unit)) -> Unit) {
    MaterialTheme(colorScheme = scheme, typography = AppTypography) {
        var tab by remember { mutableIntStateOf(0) }
        Scaffold(
            containerColor = Ink,
            bottomBar = {
                Column(Modifier.background(Panel)) {
                    Box(Modifier.fillMaxWidth().height(1.dp).background(Line))
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .navigationBarsPadding()
                            .height(64.dp)
                    ) {
                        TABS.forEachIndexed { i, def ->
                            val active = tab == i
                            val interaction = remember { MutableInteractionSource() }
                            Column(
                                Modifier
                                    .weight(1f)
                                    .fillMaxHeight()
                                    .clickable(
                                        interactionSource = interaction,
                                        indication = null,
                                    ) { tab = i },
                                horizontalAlignment = Alignment.CenterHorizontally,
                                verticalArrangement = Arrangement.Center,
                            ) {
                                Box(
                                    Modifier
                                        .width(26.dp)
                                        .height(3.dp)
                                        .background(
                                            if (active) Accent else Color.Transparent,
                                            RoundedCornerShape(99.dp),
                                        )
                                )
                                Spacer(Modifier.height(7.dp))
                                Icon(
                                    painterResource(def.icon),
                                    contentDescription = def.label,
                                    modifier = Modifier.size(22.dp),
                                    tint = if (active) Accent else TextFaint,
                                )
                                Spacer(Modifier.height(3.dp))
                                Text(
                                    def.label,
                                    color = if (active) TextMain else TextFaint,
                                    fontFamily = Barlow,
                                    fontSize = 10.sp,
                                    fontWeight = if (active) FontWeight.Bold
                                    else FontWeight.Medium,
                                )
                            }
                        }
                    }
                }
            }
        ) { padding ->
            Surface(
                modifier = Modifier.fillMaxSize().padding(padding).background(Ink),
                color = Ink
            ) {
                Crossfade(
                    targetState = tab,
                    animationSpec = tween(180),
                    label = "tab",
                ) { t ->
                    when (t) {
                        0 -> HomeScreen(
                            state,
                            requestBt,
                            onOpenTab = { tab = it },
                            onOpenDesktop = { tab = TAB_DESKTOP },
                        )
                        1 -> FaultsScreen(state)
                        2 -> ModulesScreen(state)
                        3 -> ServiceScreen(state)
                        4 -> LiveScreen(state)
                        else -> Column {
                            BackRow("Desktop pairing") { tab = 0 }
                            DesktopScreen(state)
                        }
                    }
                }
            }
        }
    }
}

/** Header for the screens that aren't in the bottom bar. */
@Composable
private fun BackRow(title: String, onBack: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .background(Panel)
            .clickable(onClick = onBack)
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text("‹", color = Accent, fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.width(12.dp))
        Text(
            title.uppercase(), color = TextMuted, fontFamily = Barlow,
            fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.sp,
        )
    }
}

// ---------------------------------------------------------------- shared bits

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
 * The car's identity, headlined: make big, VIN in mono underneath. While the
 * connect-time read is in flight it shows "Reading VIN…"; if the car never
 * answered it says so honestly — the card still shows so the user sees the
 * app tried.
 */
@Composable
fun VehicleIdentityCard(vehicle: VehicleIdentity?, identifying: Boolean) {
    SectionCard {
        PanelTitle("Vehicle")
        val vin = vehicle?.vin
        when {
            vin != null -> {
                if (vehicle.make != null) {
                    Text(
                        vehicle.make,
                        color = TextMain, fontFamily = Barlow,
                        fontSize = 26.sp, fontWeight = FontWeight.Bold,
                        letterSpacing = (-0.6).sp,
                    )
                    vehicle.year?.let { y ->
                        Spacer(Modifier.height(2.dp))
                        Text(
                            "Likely $y", color = Info, fontFamily = Barlow,
                            fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                        )
                    }
                    vehicle.marketNote?.let {
                        Spacer(Modifier.height(3.dp))
                        Text(it, color = TextFaint, fontFamily = Barlow, fontSize = 11.sp)
                    }
                    HairlineDivider()
                    Text(
                        vin,
                        color = TextMain, fontFamily = Mono,
                        fontSize = 15.sp, fontWeight = FontWeight.Medium,
                        letterSpacing = 1.sp,
                    )
                } else {
                    Text(
                        vin,
                        color = TextMain, fontFamily = Mono,
                        fontSize = 17.sp, fontWeight = FontWeight.Medium,
                        letterSpacing = 1.sp,
                    )
                    vehicle.marketNote?.let {
                        Spacer(Modifier.height(3.dp))
                        Text(it, color = TextFaint, fontFamily = Barlow, fontSize = 11.sp)
                    }
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Make not recognised from the VIN.",
                        color = TextFaint, fontFamily = Barlow, fontSize = 11.sp,
                    )
                }
            }
            identifying -> Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp, color = Accent)
                Spacer(Modifier.width(10.dp))
                Text(
                    "Reading VIN…", color = TextMuted,
                    fontFamily = Barlow, fontSize = 14.sp,
                )
            }
            else -> Text(
                "VIN not answered — is the ignition on? The app asked the car for its VIN " +
                    "over OBD Mode 09 the moment it connected.",
                color = Warn, fontFamily = Barlow, fontSize = 14.sp, lineHeight = 19.sp,
            )
        }
    }
}

/** Severity-striped generic OBD fault card: plain English first, code in mono. */
@Composable
fun DtcCard(record: DtcRecord, ai: AiFaultContext? = null) {
    val tagColor = when (record.status) {
        "stored" -> Bad
        "pending" -> Warn
        else -> TextFaint
    }
    Row(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 5.dp)
            .clip(RoundedCornerShape(11.dp))
            .background(PanelHi)
            .border(1.dp, Line, RoundedCornerShape(11.dp))
            .height(IntrinsicSize.Min)
    ) {
        Box(
            Modifier
                .width(4.dp)
                .fillMaxHeight()
                .background(tagColor)
        )
        Column(Modifier.padding(13.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    record.code,
                    color = TextMain,
                    fontSize = 17.sp,
                    fontWeight = FontWeight.SemiBold,
                    fontFamily = Mono,
                )
                Spacer(Modifier.width(10.dp))
                StatusPill(record.status, tagColor)
            }
            Spacer(Modifier.height(6.dp))
            Text(
                record.description, color = TextMuted, fontFamily = Barlow,
                fontSize = 14.sp, lineHeight = 19.sp,
            )
            if (ai != null) AiExplainBlock(record.code, null, ai)
        }
    }
}

@Composable
fun ScanResultView(result: ScanResult, vehicle: VehicleIdentity? = null) {
    if (result.simulated) {
        StatusPill("SIMULATED — no real car connected", Warn)
        Spacer(Modifier.height(10.dp))
    }
    val all = result.stored + result.pending + result.permanent
    Row(
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        StatusPill(
            if (result.milOn) "ENGINE LIGHT ON" else "ENGINE LIGHT OFF",
            if (result.milOn) Bad else Ok,
        )
        if (result.voltage.isNotEmpty()) InfoChip("Battery", result.voltage)
    }
    result.vin?.let {
        Spacer(Modifier.height(6.dp))
        Row { InfoChip("VIN", it, Info) }
    }
    if (result.protocol.isNotEmpty()) {
        Spacer(Modifier.height(8.dp))
        Text(result.protocol, color = TextFaint, fontFamily = Mono, fontSize = 10.sp)
    }
    Spacer(Modifier.height(8.dp))
    Text(
        result.message, color = TextMuted, fontFamily = Barlow,
        fontSize = 14.sp, lineHeight = 19.sp,
    )
    Spacer(Modifier.height(8.dp))
    // Give the AI the same picture the dealer has: who the car is (connect-time
    // identity, else decoded from the scan's own VIN) + every code read together.
    val ai = AiFaultContext(
        vehicle = vehicle ?: result.vin?.let { VinDecode.decode(it) },
        siblings = all.map { it.code },
    )
    if (all.isEmpty()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            StatusDot(Ok)
            Spacer(Modifier.width(8.dp))
            Text(
                "No fault codes.", color = Ok,
                fontFamily = Barlow, fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
            )
        }
    } else {
        all.forEach { DtcCard(it, ai) }
    }
}
