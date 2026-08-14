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
                "This is the important negative result: on these numbers the oil "
                "circuit is doing what both healthy cars did. If the warning lamp "
                "still lights, the fault is then far more likely to be the lamp's "
                "trigger -- the sensor's own circuit, or the cluster/BSI side -- than "
                "the pump, and that is where the next hour should go rather than into "
                "more mechanical parts.")
    return verdict_line, lines


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

    spot_points = []
    for run in runs:
        for entry in (run.get("result") or {}).get("curve") or []:
            spot_points.append((entry["rpm"], entry["bar"]))
    if spot_points:
        charts.append(("Pressure against engine speed, warm",
                       _svg_scatter(spot_points, HEALTHY_RPM_POINTS),
                       "Each point pairs a real rpm reading with the pressure at that "
                       "moment. The healthy cars ran 1.4 bar at idle and 3.0-3.4 bar "
                       "at 2500-3000 rpm."))

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
 li {{ margin-bottom: 8px; }}
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
