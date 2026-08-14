"""Build a diagnostic report comparing a car's oil captures with the healthy ones.

Run:  .venv\\Scripts\\python.exe -m backend.tools.peugeot_oil_report --car 308cc

Reads every stored capture for a car from captures/peugeot-oil/, plots its
cold-start pressure and pump-command curves against the known-good RCZ
baselines, states what the numbers rule in and out, and writes a single
self-contained HTML file that prints straight to PDF (Ctrl+P -> Save as PDF).

Charts are inline SVG drawn here rather than through a plotting library, so the
report opens anywhere with no dependencies and no internet.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.profiles.peugeot import oil as peugeot_oil  # noqa: E402

CAPTURES = Path(__file__).resolve().parents[2] / "captures" / "peugeot-oil"
REPORTS = Path(__file__).resolve().parents[2] / "captures" / "reports"

# The healthy cold-start curve both RCZs produced, as (seconds, bar).
HEALTHY_CURVE = [(6, 3.96), (15, 4.61), (25, 4.67), (34, 3.73), (44, 2.29),
                 (63, 2.04), (82, 2.01), (121, 2.06), (178, 1.93)]
HEALTHY_PUMP = [(6, 240), (25, 240), (44, 27), (63, 35), (121, 35), (178, 35)]
# Warm command against engine speed, from RCZ #1 held at 3000 rpm and back to
# idle: the command FALLS as revs rise. An earlier reading of 255 at 2436 rpm
# was discarded -- its twin byte read 0, which is the block's not-available
# marker rather than a full-scale command.
HEALTHY_PUMP_VS_RPM = [(760, 35), (3000, 9)]
HEALTHY_RPM_POINTS = [(800, 1.43), (2436, 3.38), (2500, 3.0), (3000, 3.41)]


def _load(car):
    runs = []
    for path in sorted(CAPTURES.glob(f"{car}-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data["_path"] = path.name
        # Re-decode raw blocks so an older capture gains newer signals.
        for sample in (data.get("result") or {}).get("samples") or []:
            if sample.get("blk_C2"):
                try:
                    sample.update(peugeot_oil.decode_c2(bytes.fromhex(sample["blk_C2"])))
                except ValueError:
                    pass
        runs.append(data)
    return runs


def _series(run, key):
    return [(s["t"], s[key]) for s in (run.get("result") or {}).get("samples") or []
            if s.get(key) is not None and s.get("t") is not None]


def _svg_line(series_list, width=760, height=300, xlabel="", ylabel="",
              ymin=None, ymax=None, xmax=None):
    """Line chart. series_list is [(label, colour, [(x, y), ...]), ...]."""
    pad_l, pad_r, pad_t, pad_b = 58, 130, 16, 40
    points = [p for _, _, pts in series_list for p in pts]
    if not points:
        return '<p class="muted">No data.</p>'
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    x_hi = xmax if xmax is not None else max(xs)
    x_lo = min(xs + [0])
    y_hi = ymax if ymax is not None else max(ys) * 1.15
    y_lo = ymin if ymin is not None else 0
    if x_hi == x_lo:
        x_hi = x_lo + 1
    if y_hi == y_lo:
        y_hi = y_lo + 1

    def px(x):
        return pad_l + (x - x_lo) / (x_hi - x_lo) * (width - pad_l - pad_r)

    def py(y):
        return height - pad_b - (y - y_lo) / (y_hi - y_lo) * (height - pad_t - pad_b)

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
           f'preserveAspectRatio="xMidYMid meet">']
    # grid + y axis labels
    for i in range(5):
        y = y_lo + (y_hi - y_lo) * i / 4
        yy = py(y)
        out.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" '
                   f'y2="{yy:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l - 8}" y="{yy + 4:.1f}" class="tick" '
                   f'text-anchor="end">{y:.1f}</text>')
    for i in range(6):
        x = x_lo + (x_hi - x_lo) * i / 5
        xx = px(x)
        out.append(f'<text x="{xx:.1f}" y="{height - pad_b + 18:.1f}" class="tick" '
                   f'text-anchor="middle">{x:.0f}</text>')
    out.append(f'<line x1="{pad_l}" y1="{py(y_lo):.1f}" x2="{width - pad_r}" '
               f'y2="{py(y_lo):.1f}" class="axis"/>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" '
               f'y2="{py(y_lo):.1f}" class="axis"/>')

    for index, (label, colour, pts) in enumerate(series_list):
        if not pts:
            continue
        path = " ".join(f"{'M' if i == 0 else 'L'}{px(x):.1f},{py(y):.1f}"
                        for i, (x, y) in enumerate(sorted(pts)))
        dash = ' stroke-dasharray="6 4"' if index else ""
        out.append(f'<path d="{path}" fill="none" stroke="{colour}" '
                   f'stroke-width="2.4"{dash}/>')
        for x, y in sorted(pts):
            out.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="2.6" '
                       f'fill="{colour}"/>')
        ly = pad_t + 16 + index * 20
        out.append(f'<line x1="{width - pad_r + 10}" y1="{ly}" '
                   f'x2="{width - pad_r + 34}" y2="{ly}" stroke="{colour}" '
                   f'stroke-width="2.4"{dash}/>')
        out.append(f'<text x="{width - pad_r + 40}" y="{ly + 4}" '
                   f'class="legend">{label}</text>')

    out.append(f'<text x="{(width - pad_r + pad_l) / 2:.0f}" y="{height - 6}" '
               f'class="axislabel" text-anchor="middle">{xlabel}</text>')
    out.append(f'<text x="14" y="{height / 2:.0f}" class="axislabel" '
               f'text-anchor="middle" transform="rotate(-90 14 {height / 2:.0f})">'
               f'{ylabel}</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_scatter(points, healthy, width=760, height=300):
    series = [("healthy RCZs", "#2e7d32", healthy)]
    if points:
        series.insert(0, ("this car", "#c62828", points))
    return _svg_line(series, width, height, xlabel="engine rpm",
                     ylabel="oil pressure (bar)")


# Fault codes worth naming in plain English. Anything else is listed raw.
DTC_MEANINGS = {
    "P15A0": "Oil pressure regulation -- the ECU is not satisfied with what the "
             "oil pressure control is doing.",
    "P15A9": "Oil pressure regulation, second code in the same family.",
    "P0945": "Oil pressure regulation solenoid valve circuit.",
    "P0521": "Oil pressure sensor / switch range or performance.",
    "P1336": "Flywheel (crankshaft target) adaptation not learned.",
    "P1339": "Misfire detected.",
    "P0318": "Rough road / crankshaft signal disturbance, usually recorded "
             "alongside misfire.",
    "U1113": "CAN communication fault recorded by the engine ECU.",
}


def _regulation(runs):
    """Compare pressure and pump command across rpm with the healthy cars.

    This is the test the cold-start verdict cannot make. A regulated pump holds
    pressure roughly in a band as engine speed rises, and its command MOVES as
    conditions change. An unregulated one lets pressure follow pump speed, and
    its command sits still. The healthy RCZs did the former.
    """
    points, commands = [], []
    for run in runs:
        for reading in (run.get("result") or {}).get("readings") or []:
            rpm = reading.get("engine_rpm")
            bar = reading.get("oil_pressure_bar")
            cmd = reading.get("pump_command")
            if rpm and bar:
                points.append((rpm, bar))
            if rpm and cmd is not None:
                commands.append((rpm, cmd))
    if not points:
        return None

    idle = [b for r, b in points if r < 1000]
    revs = [b for r, b in points if r >= 2000]
    out = {
        "idle_bar": round(sum(idle) / len(idle), 2) if idle else None,
        "rev_bar": round(sum(revs) / len(revs), 2) if revs else None,
        "points": sorted(points),
        "commands": sorted(commands),
        "commandValues": sorted({c for _, c in commands}),
        "healthy_idle_bar": 1.43,
        "healthy_rev_bar": 3.38,
    }
    out["commandFixed"] = len(out["commandValues"]) <= 1 and len(commands) >= 4

    # Did the solenoid actually DO anything when the command changed? Find the
    # cold-start step and look at the pressure either side of it. This is the
    # single most useful fact for a car whose solenoid has already been renewed.
    for run in runs:
        samples = [s for s in (run.get("result") or {}).get("samples") or []
                   if s.get("pump_command") is not None
                   and s.get("oil_pressure_bar") is not None]
        for before, after in zip(samples, samples[1:]):
            if before["pump_command"] - after["pump_command"] >= 60:
                drop = before["oil_pressure_bar"] - after["oil_pressure_bar"]
                if drop > 0.8:
                    out["respondedOnStep"] = True
                    out["stepFrom"] = before["oil_pressure_bar"]
                    out["stepTo"] = after["oil_pressure_bar"]
                break

    if out["idle_bar"] and out["rev_bar"]:
        out["spread"] = round(out["rev_bar"] / out["idle_bar"], 2)
        out["healthy_spread"] = round(3.38 / 1.43, 2)
    return out


def _findings(car, runs):
    """The written conclusion: what the numbers say, in order of consequence."""
    cold = [r for r in runs if r.get("command") == "coldstart"]
    spots = [r for r in runs if r.get("command") == "spot"]
    lines = []
    verdict_line = "No cold-start capture found, so the startup complaint is unjudged."
    if cold:
        latest = cold[-1]
        result = latest.get("result") or {}
        samples = result.get("samples") or []
        assessment = peugeot_oil.assess_coldstart(samples, result.get("baseline"))
        verdict = assessment.get("verdict")
        pump = assessment.get("pumpVerdict")
        headline = {
            "in_spec": "Oil pressure behaves like the two known-good cars.",
            "low_cold": "Oil pressure never reaches what both healthy cars made when cold.",
            "low_not_delivering": "There is essentially no oil pressure being reported.",
            "stuck_high": "Pressure stays high and never regulates down as the engine warms.",
            "sensor_rail": "The pressure signal is pinned at the top of the sensor's range.",
            "split": "The ECU's two pressure values disagree with each other.",
            "no_pressure_data": "No pressure was decoded from this capture.",
        }.get(verdict, str(verdict))
        verdict_line = headline
        lines.extend(assessment.get("notes") or [])

        if verdict == "sensor_rail":
            lines.append(
                "A rail reading is 5 V sitting on the signal wire. That is a circuit "
                "fault -- sensor, connector or wiring back to the ECU -- and it is "
                "not evidence about the pump at all. Replacing pump parts cannot "
                "change it. Check the sensor connector and the wire to the ECU "
                "before anything mechanical.")
        elif verdict == "stuck_high" or pump == "never_stepped_down":
            lines.append(
                "Pressure that never comes down as the oil warms is the documented "
                "signature of the regulation solenoid not being acted on -- the pump "
                "stays in full-delivery mode. Since the solenoid itself has already "
                "been replaced, the next thing to prove is that it is actually being "
                "driven: check the solenoid's connector, its wiring and its feed from "
                "the ECU rather than fitting another one.")
        elif pump == "stuck":
            lines.append(
                "The pump command never moved at all. The ECU is not modulating the "
                "solenoid, so the part in the sump is not the thing to look at next "
                "-- the command, the wiring or the ECU driver is.")
        elif verdict in ("low_cold", "low_not_delivering"):
            lines.append(
                "Low pressure with a healthy-looking command points at delivery, not "
                "control: pickup, pump, relief valve or genuine bearing clearance. "
                "Given the pump and bearings have been renewed, prove the reading "
                "first with a mechanical gauge on the galley before going back in.")
        elif verdict == "in_spec" and pump in (None, "ok"):
            lines.append(
                "The cold start on its own looks like the healthy cars: pressure "
                "high while cold, stepping down as the oil warms.")

    # The running behaviour, which is where this car separates from the good ones.
    reg = _regulation(runs)
    if reg and reg.get("idle_bar") and reg.get("rev_bar"):
        lines.append(
            f"Warm running: {reg['idle_bar']} bar at idle against the healthy car's "
            f"{reg['healthy_idle_bar']}, and {reg['rev_bar']} bar off idle against "
            f"the healthy car's {reg['healthy_rev_bar']}. So this engine is BELOW "
            "the good car at idle and WELL ABOVE it under revs.")
        if reg.get("spread") and reg["spread"] > reg["healthy_spread"] * 1.3:
            lines.append(
                f"Pressure rises {reg['spread']}x from idle to 2500 rpm; on the "
                f"healthy car it rises {reg['healthy_spread']}x. Pressure climbing "
                "with engine speed that steeply is what an UNREGULATED pump does -- "
                "the pressure is following pump speed instead of being held to a "
                "target.")
            verdict_line = ("Oil pressure is not being regulated: too low at idle, "
                            "far too high under revs.")
        if reg.get("commandFixed"):
            lines.append(
                f"Warm, the pump control value stayed at {reg['commandValues'][0]} "
                "at every engine speed, where the healthy car dropped to about 9 "
                "when held at 3000 rpm. So the control is not backing off as engine "
                "speed rises the way the good car's does.")
    if reg and reg.get("respondedOnStep"):
        lines.append(
            "IMPORTANT, and it points away from the parts already replaced: on the "
            "cold start this car's control value stepped from 240 to 35 and the "
            "pressure fell from " + f"{reg['stepFrom']} to {reg['stepTo']} bar in "
            "the same moment. The solenoid was therefore commanded AND physically "
            "responded. A solenoid that was dead, disconnected or unpowered could "
            "not move the pressure like that, so a third solenoid and a wiring hunt "
            "are not where this points.")
    return verdict_line, lines


def _advice(runs, reg):
    """What to do next, in the order that costs least and proves most."""
    steps = []
    codes = {d.get("code") for run in runs
             for d in (run.get("result") or {}).get("dtcs") or []}
    unregulated = bool(reg and reg.get("spread") and reg.get("healthy_spread")
                       and reg["spread"] > reg["healthy_spread"] * 1.3)

    if unregulated or {"P15A0", "P15A9", "P0945"} & codes:
        steps.append((
            "Confirm the reading with a mechanical gauge before any more parts.",
            "Tee a mechanical gauge into the oil galley and repeat idle and "
            "2500 rpm. If the gauge agrees (low at idle, over 4 bar at revs) the "
            "pressure really is unregulated. If the gauge reads normal and only "
            "the ECU sees these numbers, the sensor or its wiring is lying and "
            "nothing inside the engine is wrong. This one test splits the whole "
            "problem in two and costs nothing but time."))
        if reg and reg.get("respondedOnStep"):
            steps.append((
                "Do NOT replace the solenoid again, and do not start with wiring.",
                "The capture shows the command stepping down and the pressure "
                f"falling from {reg['stepFrom']} to {reg['stepTo']} bar at the same "
                "moment, so the solenoid is being energised and is physically "
                "moving. An electrical fault is the one thing this data rules out. "
                "A quick confirmation while you are there: resistance across the "
                "solenoid should read a sensible coil value rather than open "
                "circuit -- but expect it to be fine."))
        else:
            steps.append((
                "Prove the regulation solenoid is actually being driven.",
                "Back-probe the solenoid connector with the engine running and "
                "watch for a drive signal, and check continuity back to the ECU. A "
                "new solenoid that is never energised behaves exactly like a failed "
                "one."))
        steps.append((
            "Look at what the valve is controlling, not the valve itself.",
            "The solenoid moves, yet pressure is low at idle and overshoots under "
            "revs -- so the regulation has the wrong authority over the circuit. "
            "The things that do that are a control spool that moves but does not "
            "seal or travel fully, a blocked or mis-seated control gallery in the "
            "pump housing, a relief valve stuck or wrongly rated, or a pump variant "
            "that does not match this engine. Since the pump was renewed, check its "
            "part number against the 5FV specification and that its control ports "
            "were clear and correctly seated on assembly."))
        steps.append((
            "Check oil grade and level before stripping anything.",
            "Pressure that runs low at idle and high under revs is also what the "
            "wrong oil viscosity produces, and it is free to rule out. Confirm the "
            "grade in the sump is what this engine specifies and that the level and "
            "the filter are right, especially if the oil was changed during the "
            "recent work."))
    if {"P1336"} & codes:
        steps.append((
            "Carry out the flywheel (crankshaft target) adaptation.",
            "P1336 says the adaptation has not been learned. Until it is, the "
            "misfire counters cannot be trusted, so do this before drawing any "
            "conclusion from misfire codes. It needs a tool that can run the "
            "PSA adaptation procedure."))
    if {"P1339", "P0318"} & codes:
        steps.append((
            "Deal with the misfire separately from the oil problem.",
            "P1339 and P0318 point at ignition or crank-signal disturbance, not "
            "at oil pressure. Your spark-plug theory fits; fix that on its own "
            "merits and re-read the codes, so it is not confused with the "
            "pressure work."))
    steps.append((
        "Re-run this capture after each change.",
        "The same two commands produce a directly comparable set of curves. If "
        "the change worked, pressure at 2500 rpm comes down toward the healthy "
        "car's 3.4 bar and idle comes up toward 1.4, and the control value starts "
        "moving with engine speed."))
    return steps


def build(car: str) -> Path:
    runs = _load(car)
    if not runs:
        raise SystemExit(f"No captures found for '{car}' in {CAPTURES}")

    cold = [r for r in runs if r.get("command") == "coldstart"]
    verdict_line, findings = _findings(car, runs)

    charts = []
    if cold:
        latest = cold[-1]
        press = _series(latest, "oil_pressure_bar")
        mirror = _series(latest, "oil_pressure_mirror_bar")
        pump = _series(latest, "pump_command")
        charts.append(("Cold-start oil pressure against the healthy baseline",
                       _svg_line([("this car", "#c62828", press),
                                  ("healthy RCZ", "#2e7d32", HEALTHY_CURVE)],
                                 xlabel="seconds since capture started",
                                 ylabel="oil pressure (bar)"),
                       "The healthy shape is high while the oil is cold and thick, "
                       "then a decay to about 2 bar as it warms. Deviation from that "
                       "shape matters more than any single number."))
        if mirror:
            charts.append(("The ECU's two pressure values against each other",
                           _svg_line([("value A", "#c62828", press),
                                      ("value B", "#1565c0", mirror)],
                                     xlabel="seconds", ylabel="bar"),
                           "On both healthy cars these tracked within 0.25 bar at all "
                           "times. If they separate here, the ECU is seeing a "
                           "difference between what it wants and what it gets."))
        if pump:
            charts.append(("Oil pump command against the healthy baseline",
                           _svg_line([("this car", "#c62828", pump),
                                      ("healthy RCZ", "#2e7d32", HEALTHY_PUMP)],
                                     xlabel="seconds", ylabel="command (0-255)"),
                           "The healthy cars command about 240 while cold and step "
                           "down to about 35 once warm. A flat line means the "
                           "solenoid is never being modulated."))

    reg = _regulation(runs)
    spot_points = (reg or {}).get("points") or []
    if spot_points:
        charts.append(("Pressure against engine speed, warm",
                       _svg_scatter(spot_points, HEALTHY_RPM_POINTS),
                       "Each point pairs a real rpm reading with the pressure at that "
                       "moment. The healthy cars ran 1.4 bar at idle and 3.0-3.4 bar "
                       "at 2500-3000 rpm. A line that climbs far more steeply than "
                       "the green one is pressure following pump speed rather than "
                       "being regulated to a target."))
    if reg and reg.get("commands"):
        charts.append(("Pump control value against engine speed, warm",
                       _svg_line([("this car", "#c62828", reg["commands"]),
                                  ("healthy RCZ", "#2e7d32", HEALTHY_PUMP_VS_RPM)],
                                 xlabel="engine rpm", ylabel="control value (0-255)"),
                       "Held at 3000 rpm the healthy car's control value falls to "
                       "about 9. This car holds its warm-idle value at every speed, "
                       "so the control is not backing off as engine speed rises."))

    dtc_rows = []
    seen_codes = set()
    for run in runs:
        for dtc in (run.get("result") or {}).get("dtcs") or []:
            code = dtc.get("code")
            if code in seen_codes:
                continue
            seen_codes.add(code)
            dtc_rows.append(
                f"<tr><td><strong>{code}</strong></td>"
                f"<td>{DTC_MEANINGS.get(code, 'Raw code, no plain-English mapping held.')}"
                f"</td></tr>")

    advice_html = "".join(
        f"<li><strong>{title}</strong><br>{body}</li>"
        for title, body in _advice(runs, reg))

    rows = []
    for run in runs:
        result = run.get("result") or {}
        rows.append(
            f"<tr><td>{run.get('command')}</td><td>{run.get('savedAt', '')[:19]}Z</td>"
            f"<td>{len(result.get('samples') or [])}</td>"
            f"<td>{(run.get('note') or '')[:90]}</td></tr>")

    baseline = ((cold[-1].get("result") or {}).get("baseline") if cold else {}) or {}
    baseline_html = "".join(
        f"<tr><td>{k.replace('_', ' ')}</td><td>{v}</td></tr>" for k, v in baseline.items())

    charts_html = "".join(
        f'<section class="chart-block"><h3>{title}</h3>{svg}'
        f'<p class="caption">{caption}</p></section>'
        for title, svg, caption in charts)

    findings_html = "".join(f"<li>{line}</li>" for line in findings)

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Oil pressure diagnostic report - {car}</title>
<style>
 @page {{ size: A4; margin: 14mm; }}
 body {{ font: 15px/1.55 -apple-system, "Segoe UI", Roboto, sans-serif;
        color: #1a1a1a; max-width: 900px; margin: 0 auto; padding: 24px; }}
 h1 {{ font-size: 26px; margin: 0 0 4px; }}
 h2 {{ font-size: 19px; margin: 32px 0 10px; border-bottom: 2px solid #ddd;
       padding-bottom: 6px; }}
 h3 {{ font-size: 16px; margin: 0 0 8px; }}
 .sub {{ color: #666; margin: 0 0 20px; }}
 .verdict {{ background: #f4f7fb; border-left: 5px solid #1565c0; padding: 14px 18px;
             margin: 18px 0; font-size: 17px; }}
 .chart {{ width: 100%; height: auto; }}
 .chart-block {{ page-break-inside: avoid; margin: 22px 0 30px; }}
 .caption {{ color: #555; font-size: 13.5px; margin: 4px 0 0; }}
 .grid {{ stroke: #e6e6e6; stroke-width: 1; }}
 .axis {{ stroke: #999; stroke-width: 1; }}
 .tick {{ font-size: 11px; fill: #666; }}
 .legend {{ font-size: 12px; fill: #333; }}
 .axislabel {{ font-size: 12px; fill: #444; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
 th, td {{ border: 1px solid #ddd; padding: 6px 9px; text-align: left; }}
 th {{ background: #f5f5f5; }}
 li {{ margin-bottom: 10px; }}
 ol.advice li {{ margin-bottom: 14px; }}
 ol.advice strong {{ color: #0d47a1; }}
 .muted {{ color: #777; }}
 footer {{ margin-top: 34px; color: #777; font-size: 12.5px;
           border-top: 1px solid #ddd; padding-top: 10px; }}
</style></head><body>
<h1>Oil pressure diagnostic report</h1>
<p class="sub">Vehicle: <strong>{car}</strong> &middot; EP6 / 1.6 THP &middot;
 generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</p>

<div class="verdict"><strong>Finding:</strong> {verdict_line}</div>

<h2>What the data says</h2>
<ul>{findings_html or '<li class="muted">No findings recorded.</li>'}</ul>

<h2>Fault codes stored in the engine ECU</h2>
<table><tr><th>code</th><th>meaning</th></tr>{''.join(dtc_rows) or
 '<tr><td colspan="2" class="muted">No fault codes captured.</td></tr>'}</table>

<h2>What to do next, in order</h2>
<ol class="advice">{advice_html}</ol>

<h2>Charts</h2>
{charts_html or '<p class="muted">No plottable captures.</p>'}

<h2>Condition at the start of the cold capture</h2>
<table><tr><th>reading</th><th>value</th></tr>{baseline_html or
 '<tr><td colspan="2" class="muted">No baseline recorded.</td></tr>'}</table>

<h2>Captures used</h2>
<table><tr><th>type</th><th>taken</th><th>samples</th><th>note</th></tr>
{''.join(rows)}</table>

<h2>How these numbers were obtained</h2>
<p>This engine's ECU does not support the modern read service, so the published
oil identifiers for the 1.6 THP are unreachable on it. The values here come from
the PSA measurement block C2, read over the diagnostic session: oil pressure is a
16-bit value in hundredths of a bar, and the pump command is a single byte. Both
were identified on two known-good RCZs by changing something physical and keeping
only what moved the way it had to &mdash; pressure that rose and fell with engine
speed and returned to the same idle value, and a command that stepped down at the
exact moment cold pressure collapsed. The healthy figures those cars produced are
the comparison used throughout this report.</p>
<p>Everything was read-only. No setting was changed on any vehicle.</p>

<footer>Generated by car-harness &middot; captures stored in
 captures/peugeot-oil/ &middot; baselines in
 backend/profiles/peugeot/data/ep6_oil.json</footer>
</body></html>"""

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS / f"{car}-oil-report-{stamp}.html"
    path.write_text(html, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--car", required=True, help="car label used when capturing")
    args = parser.parse_args()
    path = build(args.car)
    print(f"Report written: {path}")
    print("Open it in a browser and print to PDF (Ctrl+P, Save as PDF).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
