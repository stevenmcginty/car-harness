package com.carharness.app.ui

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.SwitchColors
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextFieldColors
import androidx.compose.material3.Typography
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState
import com.carharness.app.R
import com.carharness.app.obd.Brand
import com.carharness.app.obd.Brands

// ---------------------------------------------------------------- tokens
//
// "Garage Slate" — the app should feel like a tool off the van, not a phone
// app. Warm graphite ground (never cold blue-black), warm panels with a
// machined top edge, and ONE gold-amber accent that means "this is the thing
// you press". Status colours are strictly separate from the accent so amber
// never has to mean two things at once:
//
//   Accent  gold      the action / the active thing
//   Ok      green     clean, healthy, passed
//   Warn    orange    caution, answered oddly, no data
//   Bad     red       faults, errors, link down
//   Info    blue      identity facts (VIN, decoded year)
//
// Everything the car said is monospaced; everything a human wrote is Barlow.

val Ink = Color(0xFF14130E)         // screen ground — warm near-black
val Panel = Color(0xFF1F1C15)       // card surface
val PanelHi = Color(0xFF2A2619)     // inset / raised element inside a card
val PanelTop = Color(0xFF423B2C)    // machined highlight along a card's top edge
val Line = Color(0xFF332E23)        // card + control borders
val LineSoft = Color(0xFF272319)    // inner rules, console wells

val TextMain = Color(0xFFF5F0E6)    // warm white
val TextMuted = Color(0xFFA9A08D)
val TextFaint = Color(0xFF6E6759)

val Accent = Color(0xFFFFB020)      // gold-amber — the tool colour
val AccentDim = Color(0xFFB57A12)
val AccentInk = Color(0xFF1A1200)   // text/icons sitting ON accent

val Ok = Color(0xFF86C34A)
val Warn = Color(0xFFFF8A2B)
val Bad = Color(0xFFFF5449)
val Info = Color(0xFF74B4FF)

val Barlow = FontFamily(
    Font(R.font.barlow_regular, FontWeight.Normal),
    Font(R.font.barlow_medium, FontWeight.Medium),
    Font(R.font.barlow_semibold, FontWeight.SemiBold),
    Font(R.font.barlow_bold, FontWeight.Bold),
)

val Mono = FontFamily(
    Font(R.font.jetbrains_mono_regular, FontWeight.Normal),
    Font(R.font.jetbrains_mono_medium, FontWeight.Medium),
    Font(R.font.jetbrains_mono_semibold, FontWeight.SemiBold),
)

/** App-wide type so dialogs, buttons and fields inherit Barlow too. */
val AppTypography = Typography(
    titleLarge = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Bold,
        fontSize = 20.sp, letterSpacing = (-0.4).sp,
    ),
    titleMedium = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Bold,
        fontSize = 16.sp, letterSpacing = (-0.2).sp,
    ),
    bodyLarge = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Normal,
        fontSize = 15.sp, lineHeight = 21.sp,
    ),
    bodyMedium = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Normal,
        fontSize = 14.sp, lineHeight = 19.sp,
    ),
    bodySmall = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Normal,
        fontSize = 12.sp, lineHeight = 17.sp,
    ),
    labelLarge = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Bold,
        fontSize = 14.sp, letterSpacing = 0.4.sp,
    ),
    labelMedium = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.SemiBold, fontSize = 12.sp,
    ),
)

// ---------------------------------------------------------------- primitives

/** The machined highlight that runs along the top edge of every panel. */
@Composable
private fun MachinedEdge() {
    Box(
        Modifier
            .fillMaxWidth()
            .height(1.dp)
            .background(
                Brush.horizontalGradient(
                    listOf(Color.Transparent, PanelTop, PanelTop, Color.Transparent)
                )
            )
    )
}

/**
 * The one card shape in the app. `accent = true` gives it a gold border for
 * the panel that currently matters most.
 */
@Composable
fun SectionCard(
    modifier: Modifier = Modifier,
    accent: Boolean = false,
    content: @Composable ColumnScope.() -> Unit,
) {
    val shape = RoundedCornerShape(14.dp)
    Column(
        modifier
            .fillMaxWidth()
            .padding(horizontal = 14.dp, vertical = 6.dp)
            .clip(shape)
            .background(Panel)
            .border(1.dp, if (accent) Accent.copy(alpha = 0.40f) else Line, shape)
    ) {
        MachinedEdge()
        Column(Modifier.padding(16.dp), content = content)
    }
}

/** Uppercase eyebrow label that titles a panel. */
@Composable
fun PanelTitle(text: String) {
    Text(
        text.uppercase(),
        color = TextFaint,
        fontFamily = Barlow,
        fontSize = 11.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = 2.0.sp,
        modifier = Modifier.padding(bottom = 10.dp),
    )
}

/** Big screen-level heading — Barlow Bold, tight. */
@Composable
fun ScreenTitle(text: String) {
    Text(
        text,
        color = TextMain, fontFamily = Barlow,
        fontSize = 28.sp, fontWeight = FontWeight.Bold,
        letterSpacing = (-0.8).sp,
    )
}

@Composable
fun HairlineDivider(vertical: Dp = 12.dp) {
    Box(
        Modifier
            .fillMaxWidth()
            .padding(vertical = vertical)
            .height(1.dp)
            .background(LineSoft)
    )
}

/** LABEL + mono value chip — for facts the car reported. */
@Composable
fun InfoChip(label: String, value: String, valueColor: Color = TextMain) {
    Row(
        Modifier
            .clip(RoundedCornerShape(8.dp))
            .background(PanelHi)
            .border(1.dp, Line, RoundedCornerShape(8.dp))
            .padding(horizontal = 11.dp, vertical = 7.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            label.uppercase(),
            color = TextFaint, fontFamily = Barlow, fontSize = 9.sp,
            fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp,
        )
        Spacer(Modifier.width(8.dp))
        Text(
            value, color = valueColor, fontSize = 12.sp,
            fontFamily = Mono, fontWeight = FontWeight.Medium,
        )
    }
}

/** Small tinted status pill: "OK", "2 faults", "no data", … */
@Composable
fun StatusPill(text: String, color: Color) {
    Box(
        Modifier
            .clip(RoundedCornerShape(7.dp))
            .background(color.copy(alpha = 0.14f))
            .border(1.dp, color.copy(alpha = 0.34f), RoundedCornerShape(7.dp))
            .padding(horizontal = 10.dp, vertical = 5.dp)
    ) {
        Text(
            text, color = color, fontFamily = Barlow,
            fontSize = 12.sp, fontWeight = FontWeight.SemiBold,
        )
    }
}

/** A round status dot used at the front of list rows. */
@Composable
fun StatusDot(color: Color) {
    Box(Modifier.size(9.dp).background(color, CircleShape))
}

/** Status dot with a slow breathing pulse — the app's one ambient motion. */
@Composable
fun PulseDot(color: Color) {
    val pulse = rememberInfiniteTransition(label = "pulse")
    val alpha by pulse.animateFloat(
        initialValue = 1f, targetValue = 0.35f,
        animationSpec = infiniteRepeatable(
            tween(900, easing = FastOutSlowInEasing), RepeatMode.Reverse
        ),
        label = "pulseAlpha",
    )
    Box(Modifier.size(9.dp).background(color.copy(alpha = alpha), CircleShape))
}

// ---------------------------------------------------------------- status rail

/**
 * Screen header with the live connection readout on the right — every tab
 * carries the same cockpit status line so you always know the link state.
 */
@Composable
fun StatusRail(title: String, subtitle: String?, state: AppState) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            ScreenTitle(title)
            if (subtitle != null) {
                Text(subtitle, color = TextFaint, fontFamily = Barlow, fontSize = 12.sp)
            }
        }
        LinkChip(state)
    }
}

/** The live link readout — voltage when up, honest state when not. */
@Composable
fun LinkChip(state: AppState) {
    Row(
        Modifier
            .clip(RoundedCornerShape(8.dp))
            .background(PanelHi)
            .border(1.dp, Line, RoundedCornerShape(8.dp))
            .padding(horizontal = 10.dp, vertical = 7.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        when (val cs = state.connState) {
            is ConnState.Connected -> {
                PulseDot(Ok)
                Spacer(Modifier.width(7.dp))
                Text(
                    cs.voltage.ifEmpty { "LINKED" },
                    color = Ok, fontFamily = Mono,
                    fontSize = 11.sp, fontWeight = FontWeight.Medium,
                )
            }
            is ConnState.Connecting -> {
                CircularProgressIndicator(
                    Modifier.size(10.dp), strokeWidth = 1.5.dp, color = Accent
                )
                Spacer(Modifier.width(7.dp))
                Text(
                    "LINKING", color = Accent, fontFamily = Barlow,
                    fontSize = 10.sp, fontWeight = FontWeight.Bold,
                    letterSpacing = 1.4.sp,
                )
            }
            is ConnState.Error -> {
                StatusDot(Bad)
                Spacer(Modifier.width(7.dp))
                Text(
                    "LINK DOWN", color = Bad, fontFamily = Barlow,
                    fontSize = 10.sp, fontWeight = FontWeight.Bold,
                    letterSpacing = 1.4.sp,
                )
            }
            else -> {
                StatusDot(TextFaint.copy(alpha = 0.5f))
                Spacer(Modifier.width(7.dp))
                Text(
                    "OFFLINE", color = TextFaint, fontFamily = Barlow,
                    fontSize = 10.sp, fontWeight = FontWeight.Bold,
                    letterSpacing = 1.4.sp,
                )
            }
        }
    }
}

// ---------------------------------------------------------------- buttons

/**
 * The big physical slab the whole app is built around — gold, full width,
 * with a darker bottom edge so it reads as a thing you press. Used for the
 * one-tap scan and nothing else on a screen.
 */
@Composable
fun BigActionButton(
    label: String,
    sublabel: String? = null,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    busy: Boolean = false,
    onClick: () -> Unit,
) {
    val shape = RoundedCornerShape(12.dp)
    val face = if (!enabled) PanelHi else Accent
    val edge = if (!enabled) Line else AccentDim
    val ink = if (!enabled) TextFaint else AccentInk
    Column(
        modifier
            .fillMaxWidth()
            .clip(shape)
            .background(edge)
            .clickable(enabled = enabled && !busy, onClick = onClick)
    ) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(bottom = 4.dp)   // exposes the darker edge underneath
                .clip(shape)
                .background(face)
                .padding(horizontal = 18.dp, vertical = 20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (busy) {
                    CircularProgressIndicator(
                        Modifier.size(17.dp), strokeWidth = 2.dp, color = ink
                    )
                    Spacer(Modifier.width(11.dp))
                }
                Text(
                    label.uppercase(),
                    color = ink, fontFamily = Barlow,
                    fontSize = 19.sp, fontWeight = FontWeight.Bold,
                    letterSpacing = 1.0.sp,
                )
            }
            if (sublabel != null) {
                Spacer(Modifier.height(3.dp))
                Text(
                    sublabel,
                    color = ink.copy(alpha = 0.72f), fontFamily = Barlow,
                    fontSize = 12.sp, fontWeight = FontWeight.Medium,
                    textAlign = TextAlign.Center,
                )
            }
        }
    }
}

@Composable
fun PrimaryButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.heightIn(min = 48.dp),
        shape = RoundedCornerShape(10.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = Accent,
            contentColor = AccentInk,
            disabledContainerColor = PanelHi,
            disabledContentColor = TextFaint,
        ),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 12.dp),
    ) {
        Text(
            text, fontFamily = Barlow, fontSize = 14.sp,
            fontWeight = FontWeight.Bold, letterSpacing = 0.3.sp,
        )
    }
}

@Composable
fun GhostButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    tint: Color = TextMuted,
) {
    OutlinedButton(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.heightIn(min = 48.dp),
        shape = RoundedCornerShape(10.dp),
        border = BorderStroke(1.dp, Line),
        colors = ButtonDefaults.outlinedButtonColors(
            containerColor = PanelHi,
            contentColor = tint,
            disabledContentColor = TextFaint.copy(alpha = 0.6f),
        ),
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
    ) {
        Text(
            text, fontFamily = Barlow, fontSize = 14.sp,
            fontWeight = FontWeight.SemiBold,
        )
    }
}

/** Selectable pill (transport / adapter choices). */
@Composable
fun SelectPill(label: String, selected: Boolean, onClick: () -> Unit) {
    Box(
        Modifier
            .clip(RoundedCornerShape(8.dp))
            .background(if (selected) Accent.copy(alpha = 0.16f) else PanelHi)
            .border(
                1.dp,
                if (selected) Accent.copy(alpha = 0.55f) else Line,
                RoundedCornerShape(8.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 11.dp)
    ) {
        Text(
            label,
            color = if (selected) Accent else TextMuted,
            fontFamily = Barlow, fontSize = 13.sp,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
        )
    }
}

/** Inset segmented control — one choice out of a short fixed set. */
@Composable
fun SegmentedControl(
    options: List<Pair<String, String>>,
    selected: String,
    onSelect: (String) -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(Ink)
            .border(1.dp, Line, RoundedCornerShape(10.dp))
            .padding(3.dp),
        horizontalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        options.forEach { (id, label) ->
            val active = selected == id
            Box(
                Modifier
                    .weight(1f)
                    .clip(RoundedCornerShape(8.dp))
                    .background(if (active) Accent else Color.Transparent)
                    .clickable { onSelect(id) }
                    .padding(vertical = 12.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    label,
                    color = if (active) AccentInk else TextMuted,
                    fontFamily = Barlow, fontSize = 12.sp,
                    fontWeight = if (active) FontWeight.Bold else FontWeight.Medium,
                    maxLines = 1, overflow = TextOverflow.Visible, softWrap = false,
                )
            }
        }
    }
}

// ---------------------------------------------------------------- brand picker

private val MONOGRAMS = mapOf(
    "bmw" to "BMW", "mazda" to "MZD", "peugeot" to "PSA", "vw" to "VAG",
    "mercedes" to "MB", "fiat" to "FCA", "jlr" to "JLR", "porsche" to "POR",
)

/** The square monogram plate every brand is drawn as, rail and sheet alike. */
@Composable
private fun BrandMonogram(brand: Brand, active: Boolean, size: Dp, corner: Dp) {
    Box(
        Modifier
            .size(size)
            .clip(RoundedCornerShape(corner))
            .background(if (active) Accent else PanelHi)
            .border(
                1.dp,
                if (active) Color.Transparent else Line,
                RoundedCornerShape(corner),
            ),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            MONOGRAMS[brand.id] ?: brand.label.take(3).uppercase(),
            color = if (active) AccentInk else TextMuted,
            fontFamily = Barlow, fontSize = 11.sp,
            fontWeight = FontWeight.Bold, letterSpacing = 0.5.sp,
        )
    }
}

private fun brandMatches(b: Brand, query: String): Boolean {
    val needle = query.trim().lowercase()
    if (needle.isEmpty()) return true
    return b.label.lowercase().contains(needle) ||
        b.id.contains(needle) ||
        MONOGRAMS[b.id]?.lowercase()?.contains(needle) == true ||
        b.aliases.any { it.lowercase().contains(needle) }
}

/**
 * The manufacturer picker. Collapsed: one full-width card naming the chosen
 * brand. Tap it and a bottom sheet opens — search field plus an alphabetical
 * monogram grid — so every manufacturer stays two taps away, with no sideways
 * scrolling ever. When the VIN picked the brand, [auto] flags it so the user
 * knows why it changed under them.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BrandPicker(selected: String, auto: Boolean = false, onChoose: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    val brand = Brands.byId(selected) ?: Brands.all.first()

    Row(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 14.dp, vertical = 6.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(Panel)
            .border(1.dp, Line, RoundedCornerShape(12.dp))
            .clickable { open = true }
            .padding(horizontal = 12.dp, vertical = 11.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        BrandMonogram(brand, active = true, size = 42.dp, corner = 10.dp)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    "MANUFACTURER", color = TextFaint, fontFamily = Barlow,
                    fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.8.sp,
                )
                if (auto) {
                    Spacer(Modifier.width(6.dp))
                    Text(
                        "· FROM VIN", color = Ok, fontFamily = Barlow,
                        fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.2.sp,
                    )
                }
            }
            Text(
                brand.label, color = TextMain, fontFamily = Barlow,
                fontSize = 18.sp, fontWeight = FontWeight.Bold,
                letterSpacing = (-0.3).sp,
            )
        }
        Text(
            "CHANGE", color = Accent, fontFamily = Barlow,
            fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp,
        )
        Spacer(Modifier.width(5.dp))
        Text("▾", color = Accent, fontSize = 12.sp)
    }

    if (open) {
        ModalBottomSheet(
            onDismissRequest = { open = false },
            sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
            containerColor = Panel,
            dragHandle = {
                Box(
                    Modifier
                        .padding(vertical = 10.dp)
                        .width(36.dp)
                        .height(4.dp)
                        .clip(RoundedCornerShape(99.dp))
                        .background(Line)
                )
            },
        ) {
            BrandSheet(selected) { id ->
                onChoose(id)
                open = false
            }
        }
    }
}

@Composable
private fun BrandSheet(selected: String, onChoose: (String) -> Unit) {
    var query by remember { mutableStateOf("") }
    val sorted = remember { Brands.all.sortedBy { it.label.lowercase() } }
    val shown = sorted.filter { brandMatches(it, query) }

    Column(
        Modifier
            .fillMaxWidth()
            .imePadding()
            .navigationBarsPadding()
            .padding(horizontal = 18.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "SELECT MANUFACTURER", color = TextFaint, fontFamily = Barlow,
                fontSize = 11.sp, fontWeight = FontWeight.Bold, letterSpacing = 2.0.sp,
            )
            Spacer(Modifier.weight(1f))
            Text(
                "${Brands.all.size} FITTED", color = TextFaint,
                fontFamily = Mono, fontSize = 10.sp,
            )
        }
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = query,
            onValueChange = { query = it },
            modifier = Modifier.fillMaxWidth(),
            placeholder = {
                Text(
                    "Search — Audi, Land Rover, Alfa…",
                    color = TextFaint, fontFamily = Barlow, fontSize = 14.sp,
                )
            },
            leadingIcon = {
                Icon(Icons.Filled.Search, contentDescription = "Search", tint = TextFaint)
            },
            singleLine = true,
            shape = RoundedCornerShape(10.dp),
            colors = harnessFieldColors(),
            textStyle = TextStyle(fontFamily = Barlow, fontSize = 14.sp, color = TextMain),
        )
        Spacer(Modifier.height(12.dp))
        if (shown.isEmpty()) {
            Column(
                Modifier.fillMaxWidth().padding(vertical = 28.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    "No manufacturer matches \"${query.trim()}\"",
                    color = TextMuted, fontFamily = Barlow, fontSize = 14.sp,
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    "Try the group name — Audi lives under VW/Audi, Land Rover under Jaguar/LR.",
                    color = TextFaint, fontFamily = Barlow, fontSize = 11.sp,
                    lineHeight = 16.sp, textAlign = TextAlign.Center,
                )
            }
        } else {
            LazyVerticalGrid(
                columns = GridCells.Fixed(3),
                modifier = Modifier.fillMaxWidth().heightIn(max = 430.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
                contentPadding = PaddingValues(bottom = 20.dp),
            ) {
                items(shown.size) { i ->
                    val b = shown[i]
                    val active = selected == b.id
                    Column(
                        Modifier
                            .clip(RoundedCornerShape(11.dp))
                            .background(if (active) Accent.copy(alpha = 0.12f) else PanelHi)
                            .border(
                                1.dp,
                                if (active) Accent.copy(alpha = 0.55f) else Line,
                                RoundedCornerShape(11.dp),
                            )
                            .clickable { onChoose(b.id) }
                            .padding(vertical = 14.dp, horizontal = 6.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        BrandMonogram(b, active, size = 38.dp, corner = 9.dp)
                        Spacer(Modifier.height(7.dp))
                        Text(
                            b.label,
                            color = if (active) TextMain else TextMuted,
                            fontFamily = Barlow, fontSize = 11.sp,
                            fontWeight = if (active) FontWeight.Bold else FontWeight.Medium,
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
            }
        }
        Spacer(Modifier.height(6.dp))
    }
}

// ---------------------------------------------------------------- scan ticker

/** Animated gold sweep — "the tool is working". */
@Composable
fun SweepBar() {
    val anim = rememberInfiniteTransition(label = "sweep")
    val t by anim.animateFloat(
        initialValue = 0f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(1100, easing = LinearEasing)),
        label = "sweepT",
    )
    Canvas(
        Modifier
            .fillMaxWidth()
            .height(4.dp)
            .clip(RoundedCornerShape(99.dp))
    ) {
        drawRect(PanelHi)
        val barW = size.width * 0.35f
        val x = t * (size.width + barW) - barW
        drawRect(
            brush = Brush.horizontalGradient(
                colors = listOf(Color.Transparent, Accent, Color.Transparent),
                startX = x, endX = x + barW,
            ),
            topLeft = androidx.compose.ui.geometry.Offset(x, 0f),
            size = androidx.compose.ui.geometry.Size(barW, size.height),
        )
    }
}

/**
 * Live scan console: the sweep bar plus the last few progress lines in mono,
 * newest brightest — turns the deep-scan wait into visible work.
 */
@Composable
fun ScanTicker(progress: String) {
    val lines = remember { mutableStateListOf<String>() }
    LaunchedEffect(progress) {
        if (progress.isNotBlank() && (lines.isEmpty() || lines.last() != progress)) {
            lines.add(progress)
        }
    }
    Column {
        SweepBar()
        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "SCAN CONSOLE", color = TextFaint, fontFamily = Barlow,
                fontSize = 9.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.6.sp,
            )
            Spacer(Modifier.weight(1f))
            Text(
                "STEP ${lines.size}", color = TextFaint, fontFamily = Mono, fontSize = 9.sp,
            )
        }
        Spacer(Modifier.height(4.dp))
        val tail = lines.takeLast(3)
        tail.forEachIndexed { i, line ->
            Text(
                line,
                color = if (i == tail.lastIndex) TextMain else TextFaint,
                fontFamily = Mono, fontSize = 10.sp, lineHeight = 15.sp,
                maxLines = 1, overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

// ---------------------------------------------------------------- health ring

/**
 * Module-health donut: green = clean modules, red = modules holding faults,
 * orange = answered oddly / no data. Center carries the verdict. Fills once,
 * 700ms, when a report first appears.
 */
@Composable
fun HealthRing(
    ok: Int,
    faulted: Int,
    noData: Int,
    faultCount: Int,
    modifier: Modifier = Modifier,
    ringSize: Dp = 96.dp,
) {
    val fraction = remember { Animatable(0f) }
    LaunchedEffect(ok, faulted, noData) {
        fraction.snapTo(0f)
        fraction.animateTo(1f, tween(700, easing = FastOutSlowInEasing))
    }
    Box(modifier.size(ringSize), contentAlignment = Alignment.Center) {
        Canvas(Modifier.size(ringSize)) {
            val stroke = Stroke(width = 8.dp.toPx(), cap = StrokeCap.Butt)
            val inset = 8.dp.toPx() / 2
            val arcSize = androidx.compose.ui.geometry.Size(
                size.width - inset * 2, size.height - inset * 2
            )
            val topLeft = androidx.compose.ui.geometry.Offset(inset, inset)
            drawArc(PanelHi, 0f, 360f, false, topLeft, arcSize, style = stroke)
            val total = (ok + faulted + noData).coerceAtLeast(1)
            var start = -90f
            listOf(Ok to ok, Bad to faulted, Warn to noData).forEach { (color, n) ->
                if (n > 0) {
                    val sweep = (n.toFloat() / total) * 360f * fraction.value
                    drawArc(
                        color, start, (sweep - 3f).coerceAtLeast(1f), false,
                        topLeft, arcSize, style = stroke,
                    )
                    start += (n.toFloat() / total) * 360f * fraction.value
                }
            }
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            if (faultCount > 0) {
                Text(
                    "$faultCount", color = Bad, fontFamily = Mono,
                    fontSize = 26.sp, fontWeight = FontWeight.Medium,
                )
                Text(
                    if (faultCount == 1) "FAULT" else "FAULTS",
                    color = TextFaint, fontFamily = Barlow, fontSize = 8.sp,
                    fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp,
                )
            } else {
                Text(
                    "OK", color = Ok, fontFamily = Barlow,
                    fontSize = 22.sp, fontWeight = FontWeight.Bold,
                )
                Text(
                    "ALL CLEAR", color = TextFaint, fontFamily = Barlow, fontSize = 8.sp,
                    fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp,
                )
            }
        }
    }
}

// ---------------------------------------------------------------- sparkline

/** Tiny trend line under a live value — last N samples, newest at the right. */
@Composable
fun Sparkline(samples: List<Float>, modifier: Modifier = Modifier) {
    Canvas(modifier) {
        if (samples.size < 2) return@Canvas
        val min = samples.min()
        val max = samples.max()
        val range = (max - min).takeIf { it > 0f } ?: 1f
        val stepX = size.width / (samples.size - 1)
        val pad = 2.dp.toPx()
        val h = size.height - pad * 2
        fun yOf(v: Float) = pad + h * (1f - (v - min) / range)
        val line = Path()
        samples.forEachIndexed { i, v ->
            val x = i * stepX
            val y = yOf(v)
            if (i == 0) line.moveTo(x, y) else line.lineTo(x, y)
        }
        val fill = Path().apply {
            addPath(line)
            lineTo(size.width, size.height)
            lineTo(0f, size.height)
            close()
        }
        drawPath(
            fill,
            brush = Brush.verticalGradient(
                listOf(Accent.copy(alpha = 0.20f), Color.Transparent),
                startY = 0f, endY = size.height,
            ),
        )
        drawPath(line, Accent.copy(alpha = 0.9f), style = Stroke(1.5.dp.toPx(), cap = StrokeCap.Round))
        drawCircle(Accent, 2.dp.toPx(), androidx.compose.ui.geometry.Offset(size.width, yOf(samples.last())))
    }
}

// ---------------------------------------------------------------- blocks

/** Deep-set mono console block (diagnostic trail, raw frames). */
@Composable
fun ConsoleBlock(title: String, lines: List<String>) {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(9.dp))
            .background(Ink)
            .border(1.dp, LineSoft, RoundedCornerShape(9.dp))
            .padding(10.dp)
    ) {
        Text(
            title.uppercase(), color = TextFaint, fontFamily = Barlow, fontSize = 9.sp,
            letterSpacing = 1.6.sp, fontWeight = FontWeight.Bold,
        )
        Spacer(Modifier.height(5.dp))
        lines.forEach { line ->
            Text(
                line, color = TextFaint, fontSize = 10.sp,
                fontFamily = Mono, lineHeight = 15.sp,
            )
        }
    }
}

/** Numbered manual-procedure step. */
@Composable
fun StepRow(index: Int, text: String) {
    Row(Modifier.padding(vertical = 4.dp)) {
        Box(
            Modifier
                .size(22.dp)
                .clip(RoundedCornerShape(6.dp))
                .background(Accent.copy(alpha = 0.16f))
                .border(1.dp, Accent.copy(alpha = 0.3f), RoundedCornerShape(6.dp)),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                "$index", color = Accent, fontFamily = Mono,
                fontSize = 10.sp, fontWeight = FontWeight.Medium,
            )
        }
        Spacer(Modifier.width(10.dp))
        Text(
            text, color = TextMuted, fontFamily = Barlow,
            fontSize = 14.sp, lineHeight = 20.sp,
            modifier = Modifier.padding(top = 1.dp),
        )
    }
}

/** Orange caution callout. */
@Composable
fun CautionBox(cautions: List<String>) {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(9.dp))
            .background(Warn.copy(alpha = 0.08f))
            .border(1.dp, Warn.copy(alpha = 0.32f), RoundedCornerShape(9.dp))
            .padding(11.dp)
    ) {
        cautions.forEachIndexed { i, line ->
            if (i > 0) Spacer(Modifier.height(5.dp))
            Row {
                Text(
                    "!", color = Warn, fontFamily = Barlow, fontSize = 13.sp,
                    fontWeight = FontWeight.Bold, modifier = Modifier.width(14.dp),
                )
                Text(
                    line, color = Warn, fontFamily = Barlow,
                    fontSize = 13.sp, lineHeight = 18.sp,
                )
            }
        }
    }
}

// ---------------------------------------------------------------- input skins

@Composable
fun harnessFieldColors(): TextFieldColors = OutlinedTextFieldDefaults.colors(
    focusedTextColor = TextMain,
    unfocusedTextColor = TextMain,
    focusedBorderColor = Accent.copy(alpha = 0.7f),
    unfocusedBorderColor = Line,
    focusedLabelColor = Accent,
    unfocusedLabelColor = TextFaint,
    cursorColor = Accent,
)

@Composable
fun harnessSwitchColors(): SwitchColors = SwitchDefaults.colors(
    checkedThumbColor = AccentInk,
    checkedTrackColor = Accent,
    checkedBorderColor = Accent,
    uncheckedThumbColor = TextFaint,
    uncheckedTrackColor = PanelHi,
    uncheckedBorderColor = Line,
)
