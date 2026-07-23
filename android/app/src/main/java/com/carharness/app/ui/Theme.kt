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
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextFieldColors
import androidx.compose.material3.SwitchColors
import androidx.compose.material3.Typography
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.remember
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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.carharness.app.AppState
import com.carharness.app.ConnState
import com.carharness.app.R
import com.carharness.app.obd.Brands

// ---------------------------------------------------------------- tokens
// "Blackline Instrument": a professional workshop instrument, not a phone
// toy. Carbon-black ground, hairline panels, ONE signal-green accent;
// amber/red/blue are status-only. Everything the car said is monospaced;
// everything a human wrote is Barlow. Restraint reads as expensive.

val Bg = Color(0xFF07090D)
val Panel = Color(0xFF0E1319)
val PanelRaised = Color(0xFF151C25)
val BorderSoft = Color(0xFF1E2732)
val RuleInner = Color(0xFF18202A)
val TextMain = Color(0xFFECF1F6)
val TextMuted = Color(0xFF97A4B2)
val TextFaint = Color(0xFF5A6675)
val Green = Color(0xFF46D98A)
val GreenInk = Color(0xFF062B18)
val Amber = Color(0xFFEFB35A)
val Red = Color(0xFFF0655E)
val Blue = Color(0xFF6FADF7)

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
        fontFamily = Barlow, fontWeight = FontWeight.SemiBold,
        fontSize = 19.sp, letterSpacing = (-0.2).sp,
    ),
    titleMedium = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.SemiBold, fontSize = 16.sp,
    ),
    bodyLarge = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Normal,
        fontSize = 14.sp, lineHeight = 20.sp,
    ),
    bodyMedium = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Normal,
        fontSize = 13.sp, lineHeight = 18.sp,
    ),
    bodySmall = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Normal,
        fontSize = 12.sp, lineHeight = 17.sp,
    ),
    labelLarge = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.SemiBold,
        fontSize = 14.sp, letterSpacing = 0.2.sp,
    ),
    labelMedium = TextStyle(
        fontFamily = Barlow, fontWeight = FontWeight.Medium, fontSize = 12.sp,
    ),
)

// ---------------------------------------------------------------- primitives

@Composable
fun SectionCard(content: @Composable () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 5.dp),
        colors = CardDefaults.cardColors(containerColor = Panel),
        border = BorderStroke(1.dp, BorderSoft),
        shape = RoundedCornerShape(16.dp),
    ) {
        Column(Modifier.padding(16.dp)) { content() }
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
        fontWeight = FontWeight.SemiBold,
        letterSpacing = 1.8.sp,
        modifier = Modifier.padding(bottom = 10.dp),
    )
}

@Composable
fun HairlineDivider(vertical: Dp = 10.dp) {
    Box(
        Modifier
            .fillMaxWidth()
            .padding(vertical = vertical)
            .height(1.dp)
            .background(RuleInner)
    )
}

/** LABEL + mono value chip — for facts the car reported. */
@Composable
fun InfoChip(label: String, value: String, valueColor: Color = TextMain) {
    Row(
        Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(PanelRaised)
            .border(1.dp, BorderSoft, RoundedCornerShape(999.dp))
            .padding(horizontal = 11.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            label.uppercase(),
            color = TextFaint, fontFamily = Barlow, fontSize = 9.sp,
            fontWeight = FontWeight.SemiBold, letterSpacing = 1.2.sp,
        )
        Spacer(Modifier.width(7.dp))
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
            .clip(RoundedCornerShape(999.dp))
            .background(color.copy(alpha = 0.12f))
            .border(1.dp, color.copy(alpha = 0.28f), RoundedCornerShape(999.dp))
            .padding(horizontal = 10.dp, vertical = 4.dp)
    ) {
        Text(
            text, color = color, fontFamily = Barlow,
            fontSize = 12.sp, fontWeight = FontWeight.Medium,
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
            tween(800, easing = FastOutSlowInEasing), RepeatMode.Reverse
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
        Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                title, color = TextMain, fontFamily = Barlow,
                fontSize = 26.sp, fontWeight = FontWeight.SemiBold,
                letterSpacing = (-0.5).sp,
            )
            if (subtitle != null) {
                Text(subtitle, color = TextFaint, fontFamily = Barlow, fontSize = 12.sp)
            }
        }
        Row(
            Modifier
                .clip(RoundedCornerShape(999.dp))
                .background(Panel)
                .border(1.dp, BorderSoft, RoundedCornerShape(999.dp))
                .padding(horizontal = 10.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            when (val cs = state.connState) {
                is ConnState.Connected -> {
                    PulseDot(Green)
                    Spacer(Modifier.width(7.dp))
                    Text(
                        cs.voltage.ifEmpty { "LINKED" },
                        color = Green, fontFamily = Mono,
                        fontSize = 11.sp, fontWeight = FontWeight.Medium,
                    )
                }
                is ConnState.Connecting -> {
                    CircularProgressIndicator(
                        Modifier.size(10.dp), strokeWidth = 1.5.dp, color = Amber
                    )
                    Spacer(Modifier.width(7.dp))
                    Text(
                        "LINKING", color = Amber, fontFamily = Barlow,
                        fontSize = 10.sp, fontWeight = FontWeight.SemiBold,
                        letterSpacing = 1.2.sp,
                    )
                }
                is ConnState.Error -> {
                    StatusDot(Red)
                    Spacer(Modifier.width(7.dp))
                    Text(
                        "LINK ERROR", color = Red, fontFamily = Barlow,
                        fontSize = 10.sp, fontWeight = FontWeight.SemiBold,
                        letterSpacing = 1.2.sp,
                    )
                }
                else -> {
                    StatusDot(TextFaint.copy(alpha = 0.5f))
                    Spacer(Modifier.width(7.dp))
                    Text(
                        "OFFLINE", color = TextFaint, fontFamily = Barlow,
                        fontSize = 10.sp, fontWeight = FontWeight.SemiBold,
                        letterSpacing = 1.2.sp,
                    )
                }
            }
        }
    }
}

// ---------------------------------------------------------------- buttons

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
        modifier = modifier,
        shape = RoundedCornerShape(12.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = Green,
            contentColor = GreenInk,
            disabledContainerColor = PanelRaised,
            disabledContentColor = TextFaint,
        ),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(
            horizontal = 20.dp, vertical = 12.dp
        ),
    ) {
        Text(
            text, fontFamily = Barlow, fontSize = 14.sp,
            fontWeight = FontWeight.SemiBold, letterSpacing = 0.2.sp,
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
        modifier = modifier,
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(1.dp, BorderSoft),
        colors = ButtonDefaults.outlinedButtonColors(
            contentColor = tint,
            disabledContentColor = TextFaint.copy(alpha = 0.6f),
        ),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(
            horizontal = 16.dp, vertical = 12.dp
        ),
    ) {
        Text(
            text, fontFamily = Barlow, fontSize = 14.sp,
            fontWeight = FontWeight.Medium,
        )
    }
}

/** Selectable pill (transport / adapter choices). */
@Composable
fun SelectPill(label: String, selected: Boolean, onClick: () -> Unit) {
    Box(
        Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(if (selected) Green.copy(alpha = 0.14f) else PanelRaised)
            .border(
                1.dp,
                if (selected) Green.copy(alpha = 0.45f) else BorderSoft,
                RoundedCornerShape(999.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 8.dp)
    ) {
        Text(
            label,
            color = if (selected) Green else TextMuted,
            fontFamily = Barlow, fontSize = 13.sp,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
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
            .clip(RoundedCornerShape(12.dp))
            .background(Bg)
            .border(1.dp, BorderSoft, RoundedCornerShape(12.dp))
            .padding(3.dp),
        horizontalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        options.forEach { (id, label) ->
            val active = selected == id
            Box(
                Modifier
                    .weight(1f)
                    .clip(RoundedCornerShape(9.dp))
                    .background(if (active) Green.copy(alpha = 0.14f) else Color.Transparent)
                    .clickable { onSelect(id) }
                    .padding(vertical = 9.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    label,
                    color = if (active) Green else TextFaint,
                    fontFamily = Barlow, fontSize = 12.sp,
                    fontWeight = if (active) FontWeight.SemiBold else FontWeight.Medium,
                    maxLines = 1, overflow = TextOverflow.Visible, softWrap = false,
                )
            }
        }
    }
}

// ---------------------------------------------------------------- brand rail

private val MONOGRAMS = mapOf(
    "bmw" to "BMW", "mazda" to "MZD", "peugeot" to "PSA", "vw" to "VAG",
    "mercedes" to "MB", "fiat" to "FCA", "jlr" to "JLR",
)

/**
 * The manufacturer picker: a horizontally scrolling rail of monogram tiles.
 * Never wraps, never squashes — however many brands ship.
 */
@Composable
fun BrandRail(selected: String, onChoose: (String) -> Unit) {
    LazyRow(
        contentPadding = androidx.compose.foundation.layout.PaddingValues(
            horizontal = 14.dp, vertical = 5.dp
        ),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        items(Brands.all.size) { i ->
            val b = Brands.all[i]
            val active = selected == b.id
            Column(
                Modifier
                    .clip(RoundedCornerShape(13.dp))
                    .background(if (active) Green.copy(alpha = 0.10f) else Panel)
                    .border(
                        1.dp,
                        if (active) Green.copy(alpha = 0.45f) else BorderSoft,
                        RoundedCornerShape(13.dp),
                    )
                    .clickable { onChoose(b.id) }
                    .widthIn(min = 66.dp)
                    .padding(horizontal = 12.dp, vertical = 9.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Box(
                    Modifier
                        .size(34.dp)
                        .clip(RoundedCornerShape(9.dp))
                        .background(if (active) Green.copy(alpha = 0.18f) else PanelRaised),
                    contentAlignment = Alignment.Center,
                ) {
                    Text(
                        MONOGRAMS[b.id] ?: b.label.take(3).uppercase(),
                        color = if (active) Green else TextMuted,
                        fontFamily = Barlow, fontSize = 11.sp,
                        fontWeight = FontWeight.Bold, letterSpacing = 0.5.sp,
                    )
                }
                Spacer(Modifier.height(6.dp))
                Text(
                    b.label,
                    color = if (active) TextMain else TextFaint,
                    fontFamily = Barlow, fontSize = 11.sp,
                    fontWeight = if (active) FontWeight.SemiBold else FontWeight.Medium,
                    maxLines = 1, softWrap = false,
                )
            }
        }
    }
}

// ---------------------------------------------------------------- scan ticker

/** Animated green sweep — "the tool is working". */
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
            .height(3.dp)
            .clip(RoundedCornerShape(99.dp))
    ) {
        drawRect(PanelRaised)
        val barW = size.width * 0.35f
        val x = t * (size.width + barW) - barW
        drawRect(
            brush = Brush.horizontalGradient(
                colors = listOf(Color.Transparent, Green, Color.Transparent),
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
                fontSize = 9.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 1.4.sp,
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
 * amber = answered oddly / no data. Center carries the verdict. Fills once,
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
            val stroke = Stroke(width = 7.dp.toPx(), cap = StrokeCap.Round)
            val inset = 7.dp.toPx() / 2
            val arcSize = androidx.compose.ui.geometry.Size(
                size.width - inset * 2, size.height - inset * 2
            )
            val topLeft = androidx.compose.ui.geometry.Offset(inset, inset)
            drawArc(BorderSoft, 0f, 360f, false, topLeft, arcSize, style = stroke)
            val total = (ok + faulted + noData).coerceAtLeast(1)
            var start = -90f
            listOf(Green to ok, Red to faulted, Amber to noData).forEach { (color, n) ->
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
                    "$faultCount", color = Red, fontFamily = Mono,
                    fontSize = 24.sp, fontWeight = FontWeight.Medium,
                )
                Text(
                    if (faultCount == 1) "FAULT" else "FAULTS",
                    color = TextFaint, fontFamily = Barlow, fontSize = 8.sp,
                    fontWeight = FontWeight.SemiBold, letterSpacing = 1.2.sp,
                )
            } else {
                Text(
                    "OK", color = Green, fontFamily = Barlow,
                    fontSize = 20.sp, fontWeight = FontWeight.Bold,
                )
                Text(
                    "ALL CLEAR", color = TextFaint, fontFamily = Barlow, fontSize = 8.sp,
                    fontWeight = FontWeight.SemiBold, letterSpacing = 1.2.sp,
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
                listOf(Green.copy(alpha = 0.16f), Color.Transparent),
                startY = 0f, endY = size.height,
            ),
        )
        drawPath(line, Green.copy(alpha = 0.85f), style = Stroke(1.5.dp.toPx(), cap = StrokeCap.Round))
        drawCircle(Green, 2.dp.toPx(), androidx.compose.ui.geometry.Offset(size.width, yOf(samples.last())))
    }
}

// ---------------------------------------------------------------- blocks

/** Deep-set mono console block (diagnostic trail, raw frames). */
@Composable
fun ConsoleBlock(title: String, lines: List<String>) {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(Bg)
            .border(1.dp, RuleInner, RoundedCornerShape(10.dp))
            .padding(10.dp)
    ) {
        Text(
            title.uppercase(), color = TextFaint, fontFamily = Barlow, fontSize = 9.sp,
            letterSpacing = 1.4.sp, fontWeight = FontWeight.SemiBold,
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
    Row(Modifier.padding(vertical = 3.dp)) {
        Box(
            Modifier
                .size(20.dp)
                .clip(RoundedCornerShape(6.dp))
                .background(PanelRaised),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                "$index", color = TextMuted, fontFamily = Mono,
                fontSize = 10.sp, fontWeight = FontWeight.Medium,
            )
        }
        Spacer(Modifier.width(10.dp))
        Text(
            text, color = TextMuted, fontFamily = Barlow,
            fontSize = 13.sp, lineHeight = 18.sp,
            modifier = Modifier.padding(top = 1.dp),
        )
    }
}

/** Amber caution callout. */
@Composable
fun CautionBox(cautions: List<String>) {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(Amber.copy(alpha = 0.07f))
            .border(1.dp, Amber.copy(alpha = 0.30f), RoundedCornerShape(10.dp))
            .padding(10.dp)
    ) {
        cautions.forEachIndexed { i, line ->
            if (i > 0) Spacer(Modifier.height(4.dp))
            Row {
                Text(
                    "!", color = Amber, fontFamily = Barlow, fontSize = 12.sp,
                    fontWeight = FontWeight.Bold, modifier = Modifier.width(13.dp),
                )
                Text(
                    line, color = Amber, fontFamily = Barlow,
                    fontSize = 12.sp, lineHeight = 17.sp,
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
    focusedBorderColor = Green.copy(alpha = 0.6f),
    unfocusedBorderColor = BorderSoft,
    focusedLabelColor = Green,
    unfocusedLabelColor = TextFaint,
    cursorColor = Green,
)

@Composable
fun harnessSwitchColors(): SwitchColors = SwitchDefaults.colors(
    checkedThumbColor = TextMain,
    checkedTrackColor = Green.copy(alpha = 0.5f),
    checkedBorderColor = Green,
    uncheckedThumbColor = TextFaint,
    uncheckedTrackColor = PanelRaised,
    uncheckedBorderColor = BorderSoft,
)
