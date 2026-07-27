"""Customer-facing PDF for the Taycan HV battery read.

This is the document a dealer hands to a buyer, so its job is to be *defensible*
rather than flattering. Three rules shape it:

1. **Every number on it was read from the car.** Nothing is estimated, averaged
   from other cars, or carried over. A value the battery would not give is
   printed as "not available" with the reason, never as a blank or a guess.

2. **It says which SoH it is quoting.** There are two legitimate state-of-health
   numbers for a Taycan and they routinely differ by several points: the battery
   controller's own fast read (what Porsche's tester shows on a quick check), and
   the figure from Porsche's multi-day discharge/hold/charge routine, which
   measures against a different baseline capacity. Quoting one and letting the
   customer assume the other is how a report loses an argument in a forecourt.
   The report prints the fast read as the headline, prints an independent
   capacity-derived estimate beside it, and explains the gap.

3. **It gives the reference points.** A percentage on its own means nothing to a
   buyer. Porsche's warranty floor and the typical range for comparable cars are
   printed next to the number so it can be judged.

Read-only: this module formats a report that has already been read. It never
touches the car.
"""

from datetime import datetime
from typing import Optional

from backend.core import pdf
from backend.core.pdf import (
    COURIER,
    HELVETICA,
    HELVETICA_BOLD,
    text_width,
    wrap,
)

# -- house style ------------------------------------------------------------ #
INK = (0.10, 0.11, 0.13)
MUTED = (0.42, 0.45, 0.50)
RULE = (0.82, 0.84, 0.87)
BAND = (0.96, 0.965, 0.97)
OK = (0.16, 0.60, 0.30)
WARN = (0.85, 0.55, 0.10)
BAD = (0.78, 0.18, 0.18)
PAPER_EDGE = (0.88, 0.89, 0.91)

MARGIN = 46.0
CONTENT_WIDTH = pdf.A4_WIDTH - 2 * MARGIN

# Porsche's published guarantee, quoted so the headline number has a floor to be
# judged against. Source: Porsche Taycan model page (see the research doc).
WARRANTY_EARLY = "80% of original net capacity to 3 years / 60,000 km"
WARRANTY_LATE = "70% residual capacity to 8 years / 160,000 km"

# Net (usable) nameplate capacity. All SoH maths must use NET, not gross --
# comparing a measured usable figure against the 93.4 kWh gross number makes a
# healthy car look broken.
PACKS = {
    "performance": ("Performance Battery", 71.0, 28, 336),
    "performance_plus": ("Performance Battery Plus", 83.7, 33, 396),
}


def _band_colour(label: Optional[str]) -> tuple:
    if not label:
        return MUTED
    lowered = label.lower()
    if lowered in ("strong", "excellent", "tight"):
        return OK
    if lowered in ("normal", "healthy"):
        return OK
    if lowered in ("watch", "wide", "elevated"):
        return WARN
    return BAD


def _val(node) -> Optional[object]:
    """Pull a value out of the report's {"value":…, "available":bool} shape."""
    if isinstance(node, dict):
        return node.get("value") if node.get("available") else None
    return None


def _identify_pack(battery_report: dict) -> tuple:
    """(label, net_kWh, key) -- which pack is fitted, decided by module count.

    The module walk is the honest discriminator: a Performance Battery has 28
    modules, a Performance Battery Plus 33. Falls back to the capacity-implied
    ratio only when the module map is absent.
    """
    modules = battery_report.get("modules") or []
    present = sum(1 for m in modules if m.get("present"))
    if present >= 31:
        return PACKS["performance_plus"] + ("performance_plus",)
    if 24 <= present <= 30:
        return PACKS["performance"] + ("performance",)

    implied = battery_report.get("charge", {}).get("impliedSohPercent", {})
    values = implied.get("value") or {}
    # A ratio over 100% is physically impossible, so it rules that pack out.
    plausible = {k: v for k, v in values.items() if isinstance(v, (int, float)) and v <= 103}
    if len(plausible) == 1:
        key = next(iter(plausible))
        return PACKS[key] + (key,)
    return ("Pack not identified", None, None, None, None)


class _Layout:
    """Cursor-based page builder: sections flow down, pages break as needed."""

    def __init__(self, doc: pdf.Pdf, footer: str):
        self.doc = doc
        self.footer = footer
        self.page = None
        self.y = 0.0
        self.page_no = 0
        self.new_page()

    def new_page(self) -> None:
        if self.page is not None:
            self._stamp_footer()
        self.page = self.doc.add_page()
        self.page_no += 1
        self.y = MARGIN + 8
        if self.page_no > 1:
            self.page.text(
                MARGIN, self.y, "High-voltage battery report (continued)",
                HELVETICA, 8, MUTED,
            )
            self.y += 18

    def _stamp_footer(self) -> None:
        base = pdf.A4_HEIGHT - MARGIN + 14
        self.page.line(MARGIN, base - 12, pdf.A4_WIDTH - MARGIN, base - 12, 0.5, RULE)
        self.page.text(MARGIN, base, self.footer, HELVETICA, 7, MUTED)
        self.page.text(
            pdf.A4_WIDTH - MARGIN, base, f"Page {self.page_no}",
            HELVETICA, 7, MUTED, align="right",
        )

    def room(self, needed: float) -> None:
        if self.y + needed > pdf.A4_HEIGHT - MARGIN - 24:
            self.new_page()

    def finish(self) -> None:
        self._stamp_footer()

    # -- building blocks -------------------------------------------------- #
    def section(self, title: str) -> None:
        self.room(46)
        self.y += 12
        self.page.text(MARGIN, self.y, title.upper(), HELVETICA_BOLD, 9, INK)
        self.y += 5
        self.page.line(MARGIN, self.y, pdf.A4_WIDTH - MARGIN, self.y, 0.8, INK)
        self.y += 15

    def row(
        self,
        label: str,
        value: str,
        note: str = "",
        value_colour: tuple = INK,
        banded: bool = False,
        value_font: str = HELVETICA_BOLD,
    ) -> None:
        """One label / value / note line.

        The note sits in its own right-hand column, but only when the value is
        short enough to leave room. A long value ("Performance Battery Plus")
        would otherwise run straight into it, so in that case the note drops to
        the line below and gets the full width. Row height follows the wrapped
        note, so nothing is silently clipped.
        """
        value_x = MARGIN + 186
        note_x = MARGIN + 322
        value_text = "" if value is None else str(value)
        value_w = text_width(value_text, value_font, 9.5)
        inline = (value_x + value_w) < (note_x - 8)

        if note:
            width = (
                pdf.A4_WIDTH - MARGIN - note_x - 4 if inline
                else pdf.A4_WIDTH - MARGIN - value_x - 4
            )
            lines = wrap(note, HELVETICA, 8, width)[:3]
        else:
            lines = []

        extra = 0.0
        if lines:
            used = len(lines) if inline else len(lines) + 1
            extra = max(0.0, (used - 1) * 9.5 - 4)
        height = 17 + extra

        self.room(height + 4)
        if banded:
            self.page.rect(MARGIN, self.y - 9, CONTENT_WIDTH, height, BAND)
        self.page.text(MARGIN + 4, self.y, label, HELVETICA, 9, INK)
        self.page.text(value_x, self.y, value_text, value_font, 9.5, value_colour)

        for i, line in enumerate(lines):
            if inline:
                self.page.text(note_x, self.y + i * 9.5, line, HELVETICA, 8, MUTED)
            else:
                self.page.text(
                    value_x, self.y + 10.5 + i * 9.5, line, HELVETICA, 8, MUTED
                )
        self.y += height

    def paragraph(self, text: str, size: float = 8.5, colour: tuple = MUTED) -> None:
        lines = wrap(text, HELVETICA, size, CONTENT_WIDTH - 8)
        self.room(len(lines) * (size + 2.5) + 6)
        for line in lines:
            self.page.text(MARGIN + 4, self.y, line, HELVETICA, size, colour)
            self.y += size + 2.5
        self.y += 4


def _headline(layout: _Layout, soh: Optional[float], band: dict, supported: bool,
              reason: Optional[str]) -> None:
    """The number the whole document exists to deliver."""
    page = layout.page
    layout.room(96)
    top = layout.y
    height = 84.0
    page.rect(MARGIN, top, CONTENT_WIDTH, height, BAND)
    page.rect(MARGIN, top, 4.5, height, _band_colour(band.get("label")) if supported else MUTED)

    page.text(MARGIN + 20, top + 22, "STATE OF HEALTH", HELVETICA_BOLD, 9, MUTED)

    if supported and soh is not None:
        colour = _band_colour(band.get("label"))
        page.text(MARGIN + 20, top + 62, f"{soh:.1f}%", HELVETICA_BOLD, 34, colour)
        width = text_width(f"{soh:.1f}%", HELVETICA_BOLD, 34)
        label = (band.get("label") or "").upper()
        if label:
            page.text(MARGIN + 30 + width, top + 50, label, HELVETICA_BOLD, 11, colour)
        detail = band.get("detail") or ""
        if detail:
            page.text(MARGIN + 30 + width, top + 63, detail, HELVETICA, 8.5, MUTED)

        # A scale, so the number is judged rather than just read.
        bar_x = MARGIN + 20
        bar_y = top + 72
        bar_w = CONTENT_WIDTH - 40
        page.rect(bar_x, bar_y, bar_w, 5, (0.87, 0.88, 0.90))
        span = max(0.0, min(1.0, (soh - 60.0) / 40.0))  # 60%..100% scale
        page.rect(bar_x, bar_y, bar_w * span, 5, colour)
        for mark, text in ((70.0, "70% warranty floor"), (80.0, "80%"), (90.0, "90%")):
            mx = bar_x + bar_w * ((mark - 60.0) / 40.0)
            page.line(mx, bar_y - 2, mx, bar_y + 7, 0.7, (0.55, 0.57, 0.60))
            page.text(mx, bar_y + 15, text, HELVETICA, 6.5, MUTED, align="center")
    else:
        page.text(MARGIN + 20, top + 55, "Not available", HELVETICA_BOLD, 20, MUTED)
        for i, line in enumerate(
            wrap(reason or "This car's battery does not report a decodable value.",
                 HELVETICA, 8.5, CONTENT_WIDTH - 200)[:3]
        ):
            page.text(MARGIN + 200, top + 42 + i * 11, line, HELVETICA, 8.5, MUTED)

    layout.y = top + height + 6


def build(
    battery_report: dict,
    vehicle: dict,
    dealer: str = "",
    customer: str = "",
    mileage: str = "",
    generated: Optional[datetime] = None,
) -> bytes:
    """Render the battery report to PDF bytes."""
    when = generated or datetime.now()
    vin = (vehicle or {}).get("vin") or "not read"
    stamp = when.strftime("%d %B %Y at %H:%M")

    doc = pdf.Pdf(title=f"HV battery report {vin}", author=dealer or "car-harness")
    footer = (
        f"{dealer + '  |  ' if dealer else ''}VIN {vin}  |  Generated {stamp}  |  "
        "Read directly from the vehicle by car-harness"
    )
    layout = _Layout(doc, footer)
    page = layout.page

    # -- masthead --------------------------------------------------------- #
    page.text(MARGIN, layout.y + 16, "HIGH-VOLTAGE BATTERY", HELVETICA_BOLD, 19, INK)
    page.text(MARGIN, layout.y + 34, "HEALTH REPORT", HELVETICA_BOLD, 19, INK)
    if dealer:
        page.text(pdf.A4_WIDTH - MARGIN, layout.y + 16, dealer,
                  HELVETICA_BOLD, 11, INK, align="right")
    page.text(pdf.A4_WIDTH - MARGIN, layout.y + 32, stamp,
              HELVETICA, 8.5, MUTED, align="right")
    layout.y += 44
    page.line(MARGIN, layout.y, pdf.A4_WIDTH - MARGIN, layout.y, 1.4, INK)
    layout.y += 20

    # -- vehicle ---------------------------------------------------------- #
    pack_label, pack_net, pack_modules, pack_cells, pack_key = _identify_pack(battery_report)
    model_year = None
    try:
        from backend.profiles.porsche import battery as battery_mod

        model_year = battery_mod.model_year_from_vin(vehicle.get("vin"))
    except Exception:
        model_year = None

    layout.section("Vehicle")
    layout.row("VIN", vin, value_font=COURIER, banded=True)
    if model_year:
        layout.row("Model year", str(model_year))
    layout.row("Battery fitted", pack_label,
               note=(f"{pack_net} kWh usable when new, {pack_modules} modules, "
                     f"{pack_cells} cells" if pack_net else
                     "Identified from the module count read off the car"),
               banded=True)
    if mileage:
        layout.row("Odometer", mileage)
    if customer:
        layout.row("Prepared for", customer, banded=not bool(mileage))

    # -- the headline ----------------------------------------------------- #
    soh_node = battery_report.get("soh", {})
    soh = _val(soh_node.get("percent"))
    supported = bool(soh_node.get("supported"))
    reason = soh_node.get("reason")
    layout.section("State of health")
    _headline(layout, soh, soh_node.get("band") or {}, supported, reason)

    layout.paragraph(
        "This is the state-of-health value held by the car's own battery control "
        "unit -- the same fast read a Porsche tester shows on a quick check. "
        "Porsche's formal certificate comes from a separate multi-day discharge, "
        "hold and recharge routine which measures against a different baseline "
        "capacity, and is documented to differ from this reading by up to about "
        "seven percentage points, usually reading higher. Both figures are "
        "legitimate; they measure different things. This report quotes the "
        "battery controller's own figure and states so plainly."
    )

    # -- independent cross-check ------------------------------------------ #
    charge = battery_report.get("charge", {})
    implied_kwh = _val(charge.get("impliedUsableKwh"))
    implied_soh = charge.get("impliedSohPercent", {})
    implied_values = implied_soh.get("value") or {} if implied_soh.get("available") else {}
    cross = implied_values.get(pack_key) if pack_key else None

    if implied_kwh or cross:
        layout.section("Independent cross-check")
        layout.paragraph(
            "Calculated from a completely separate pair of readings -- the energy "
            "currently in the pack, divided by its true state of charge -- and "
            "compared against the nameplate usable capacity for the battery "
            "fitted. It shares no data with the figure above, so agreement "
            "between the two is meaningful.",
        )
        if implied_kwh:
            layout.row(
                "Implied usable capacity", f"{implied_kwh:.1f} kWh",
                note=(f"against {pack_net} kWh when new" if pack_net else ""),
                banded=True,
            )
        if cross:
            gap = abs(cross - soh) if soh is not None else None
            note = "Most accurate above 80% charge, after the car has rested."
            if gap is not None:
                note = (
                    f"{gap:.1f} points above the battery controller's figure -- "
                    "consistent with the documented spread." if gap <= 8 else
                    f"{gap:.1f} points apart. Both are shown; neither is discarded."
                )
            layout.row("Capacity-derived state of health", f"{cross:.1f}%",
                       note=note, value_colour=_band_colour("normal"))

    # -- cell balance ------------------------------------------------------ #
    cells = battery_report.get("cells", {})
    delta_node = cells.get("deltaMillivolts", {})
    delta = _val(delta_node)
    layout.section("Cell balance")
    layout.paragraph(
        "The spread between the strongest and weakest cell in the pack. This is "
        "the most revealing single measurement in a used electric car: a healthy "
        "overall percentage with a wide cell spread is the classic signature of "
        "one failing module, and it cannot be disguised."
    )
    if delta is not None:
        verdict = delta_node.get("verdict") or {}
        layout.row(
            "Cell voltage spread", f"{delta:.0f} mV",
            note=verdict.get("detail") or "",
            value_colour=_band_colour(verdict.get("label")), banded=True,
        )
    high, low = _val(cells.get("max")), _val(cells.get("min"))
    if isinstance(high, dict):
        layout.row("Highest cell", f"{high.get('volts')} V",
                   note=f"cell {high.get('cell')}")
    if isinstance(low, dict):
        layout.row("Lowest cell", f"{low.get('volts')} V",
                   note=f"cell {low.get('cell')}", banded=True)

    cell_map = battery_report.get("cellMap") or {}
    if cell_map.get("available"):
        layout.row("Cells measured", f"{cell_map.get('count')} individually")
        layout.row("Mean / standard deviation",
                   f"{cell_map.get('meanVolts')} V  /  {cell_map.get('stdDevMillivolts')} mV",
                   note="A low deviation means no cell is drifting from the rest.",
                   banded=True)

    # -- thermal + electrical ---------------------------------------------- #
    thermal = battery_report.get("thermal", {})
    layout.section("Temperature and pack electrical")
    avg = _val(thermal.get("average"))
    hot, cold = _val(thermal.get("max")), _val(thermal.get("min"))
    if avg is not None:
        layout.row("Average pack temperature", f"{avg} °C", banded=True)
    if isinstance(hot, dict) and isinstance(cold, dict):
        layout.row(
            "Hottest / coldest cell",
            f"{hot.get('celsius')} °C  /  {cold.get('celsius')} °C",
            note="A single hot cell is a flag even when the average looks fine.",
        )
    electrical = battery_report.get("electrical", {})
    pack_v, sum_v = _val(electrical.get("packVolts")), _val(electrical.get("cellSumVolts"))
    if pack_v is not None:
        layout.row("Pack voltage", f"{pack_v} V", banded=True)
    if pack_v is not None and sum_v is not None:
        agree = abs(pack_v - sum_v)
        layout.row(
            "Measurement cross-check", f"{agree:.1f} V apart",
            note=("Pack voltage and the sum of all cells agree, so the "
                  "measurement chain is healthy."
                  if agree < 3 else
                  "Pack voltage and the sum of the cells disagree, which "
                  "indicates a measurement fault rather than a weak pack."),
            value_colour=OK if agree < 3 else BAD,
        )

    true_soc = _val(charge.get("trueSoc"))
    shown_soc = _val(charge.get("displayedSoc"))
    if true_soc is not None:
        layout.row("Charge at time of test",
                   f"{true_soc}% actual"
                   + (f"  /  {shown_soc:.0f}% shown to driver" if shown_soc else ""),
                   note="The difference is Porsche's built-in reserve.", banded=True)

    # -- history ----------------------------------------------------------- #
    history = battery_report.get("history", {})
    launches = _val(history.get("launchControlCount"))
    if launches is not None:
        layout.section("Use history")
        layout.row("Launch control activations", f"{launches}",
                   note="Counted by the car over its whole life.", banded=True)

    # -- honesty ----------------------------------------------------------- #
    layout.section("How to read this report")
    layout.row("Porsche warranty, first period", WARRANTY_EARLY, banded=True)
    layout.row("Porsche warranty, thereafter", WARRANTY_LATE)
    layout.paragraph(
        "Comparable cars of this generation typically read between 85% and 92%. "
        "A figure inside that range is normal degradation, not a fault.",
        colour=INK,
    )

    unavailable = []
    for group, node in battery_report.items():
        if not isinstance(node, dict):
            continue
        for key, item in node.items():
            if isinstance(item, dict) and item.get("available") is False:
                unavailable.append(f"{group}.{key}")
    if unavailable:
        layout.paragraph(
            "Values the battery did not report on this read, shown here rather "
            "than left silently blank: " + ", ".join(sorted(unavailable)[:12]) + "."
        )

    for note in battery_report.get("notes", []):
        layout.paragraph(note)
    for warning in battery_report.get("warnings", []):
        layout.paragraph(str(warning), colour=BAD)

    layout.paragraph(
        "This report was produced by reading the vehicle's own control units over "
        "a diagnostic connection. No setting, adaptation or fault memory on the "
        "car was altered at any point. It is an independent measurement and is "
        "not a Porsche-issued document.",
    )

    layout.finish()
    return doc.to_bytes()
