"""Compile the SLK R172 rough-running report from saved session runs.

Run:  .venv\\Scripts\\python.exe -m backend.tools.mercedes_misfire_report --car slk-r172

Reads every stored run for the car from captures/mercedes/runs/ -- identify,
scan and capture -- and writes one self-contained HTML file that answers the
customer's complaint with evidence:

  * what the car is (VIN, engine variant from step 0)
  * what faults are stored, engine first
  * how the live data stacks up: which misfire counter climbed, when
    (cold vs warm), what trims and rail pressure did alongside -- charts
    drawn inline as SVG so the file opens anywhere, no dependencies
  * what online research says could cause that signature on this exact
    engine, each row with its source link
  * the honest verdict and next steps

Charts are inline SVG drawn here rather than through a plotting library.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.profiles.mercedes import misfire as mb_misfire  # noqa: E402

RUNS = Path(__file__).resolve().parents[2] / "captures" / "mercedes" / "runs"
REPORTS = Path(__file__).resolve().parents[2] / "captures" / "reports"

_CSS = """
body{font-family:'Segoe UI',system-ui,sans-serif;margin:0;background:#f4f2ee;color:#1c1c1c}
main{max-width:900px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:26px;margin:0 0 4px} h2{margin-top:34px;border-bottom:2px solid #1c1c1c;padding-bottom:4px}
.sub{color:#666;margin-bottom:20px}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 18px;margin:12px 0}
.verdict{font-size:19px;font-weight:600}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:14px}
th,td{border:1px solid #ccc;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#ece9e3}
.muted{color:#777;font-size:13px}
.chart{width:100%;height:auto}
.grid{stroke:#e2ded6;stroke-width:1} .tick{fill:#888;font-size:11px}
.s1{fill:none;stroke:#b3402a;stroke-width:2} .s2{fill:none;stroke:#2a5fb3;stroke-width:2}
.s3{fill:none;stroke:#2a8a4a;stroke-width:2} .s4{fill:none;stroke:#8a2ab3;stroke-width:2}
.s5{fill:none;stroke:#c07f1f;stroke-width:2} .s6{fill:none;stroke:#555;stroke-width:1.4;stroke-dasharray:4 3}
.legend{font-size:13px;margin:4px 0 10px}.legend span{margin-right:16px}
code{background:#eee;padding:1px 5px;border-radius:3px;font-size:13px}
li{margin:4px 0}
"""

_COLOURS = ["s1", "s2", "s3", "s4", "s5", "s6"]


def _load(car):
    runs = []
    for path in sorted(RUNS.glob(f"{car}-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data["_path"] = path.name
        runs.append(data)
    return runs


def _latest(runs, command):
    picks = [r for r in runs if r.get("command") == command]
    return picks[-1] if picks else None


def _series(run, key):
    return [(s["t"], s[key]) for s in (run.get("result") or {}).get("samples") or []
            if isinstance(s, dict) and s.get(key) is not None and s.get("t") is not None]


def _svg_line(series_list, width=800, height=280, xlabel="", ylabel=""):
    pad_l, pad_r, pad_t, pad_b = 56, 150, 14, 38
    points = [p for _, _, pts in series_list for p in pts]
    if not points:
        return '<p class="muted">No data.</p>'
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = min(ys), max(ys)
    if y_hi - y_lo < 1e-9:
        y_lo -= 1
        y_hi += 1
    span_y = (y_hi - y_lo) * 0.08
    y_lo, y_hi = y_lo - span_y, y_hi + span_y
    if x_hi == x_lo:
        x_hi = x_lo + 1

    def px(x):
        return pad_l + (x - x_lo) / (x_hi - x_lo) * (width - pad_l - pad_r)

    def py(y):
        return height - pad_b - (y - y_lo) / (y_hi - y_lo) * (height - pad_t - pad_b)

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" '
           f'preserveAspectRatio="xMidYMid meet">']
    for i in range(5):
        y = y_lo + (y_hi - y_lo) * i / 4
        yy = py(y)
        out.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" '
                   f'y2="{yy:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l - 7}" y="{yy + 4:.1f}" class="tick" '
                   f'text-anchor="end">{y:.4g}</text>')
    for i in range(5):
        x = x_lo + (x_hi - x_lo) * i / 4
        xx = px(x)
        out.append(f'<text x="{xx:.1f}" y="{height - pad_b + 16}" class="tick" '
                   f'text-anchor="middle">{x:.3g}</text>')
    for idx, (label, colour, pts) in enumerate(series_list):
        path = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in pts)
        out.append(f'<polyline points="{path}" class="{colour}" '
                   f'fill="none" stroke-linejoin="round"/>')
    out.append(f'<text x="{pad_l}" y="11" class="tick">{ylabel}</text>')
    out.append(f'<text x="{width - pad_r}" y="{height - 6}" class="tick" '
               f'text-anchor="end">{xlabel}</text>')
    out.append("</svg>")
    legend = "".join(
        f'<span><span class="{colour}" style="display:inline-block;width:22px;'
        f'border-top:2.5px solid;vertical-align:middle"></span> {label}</span>'
        for label, colour, _ in series_list)
    return f'<div class="legend">{legend}</div>' + "".join(out)


def _dtc_table(scan_run):
    result = scan_run.get("result") or {}
    rows = []
    for module in result.get("modules") or []:
        for dtc in module.get("dtcs") or []:
            rows.append(
                f"<tr><td>{module.get('code')} ({module.get('address')})</td>"
                f"<td><code>{dtc.get('code', '?')}</code></td>"
                f"<td>{dtc.get('description') or ''}</td>"
                f"<td>{', '.join(dtc.get('flags') or [])}</td></tr>")
    if not rows:
        return ('<p class="card">No fault codes stored in the latest scan '
                '(or no scan run saved yet).</p>')
    return ("<table><tr><th>Module</th><th>Code</th><th>Description</th>"
            "<th>Status</th></tr>" + "".join(rows) + "</table>")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--car", default="slk-r172")
    args = parser.parse_args()

    runs = _load(args.car)
    ident = _latest(runs, "identify")
    scan_run = _latest(runs, "scan")
    capture = _latest(runs, "capture")
    if capture is None:
        print(f"No capture runs found for '{args.car}' in {RUNS}.")
        return 1

    cap_result = capture.get("result") or {}
    ecu_key = cap_result.get("ecu")
    assessment = cap_result.get("assessment") or {}
    samples = cap_result.get("samples") or []
    known = assessment.get("known_issues_matched") or \
        mb_misfire.match_known_issues(assessment.get("verdict"), ecu_key,
                                      assessment.get("leaders") or [])

    cyl_keys = sorted({k for s in samples for k in s if k.startswith("cyl")},
                      key=lambda k: int(k[3:]))
    misfire_series = [
        (key.replace("cyl", "cyl "), _COLOURS[i % len(_COLOURS)], _series(capture, key))
        for i, key in enumerate(cyl_keys)]
    rpm_series = [("rpm", "s6", _series(capture, "rpm"))]

    charts_html = (
        "<h2>Misfire counters over time</h2>"
        + (_svg_line(misfire_series + rpm_series, ylabel="counts / rpm")
           if any(pts for _, _, pts in misfire_series)
           else '<p class="muted">No misfire-counter ticks decoded.</p>'))

    issues_rows = "".join(
        f"<tr><td>{i['name']}</td><td>{i['symptoms']}</td>"
        f"<td>{i['signature_hint'] or ''}</td><td>{i['fix']}<br>"
        + "".join(f'<a href="{u}">{u.split("/")[2]}</a><br>' for u in i["sources"])
        + "</td></tr>"
        for i in known)

    ident_rows = ""
    if ident:
        r = ident.get("result") or {}
        engine = r.get("engine") or r.get("ecu") or "not identified"
        vin = r.get("vin") or "?"
        idents = "<br>".join(f"{i['label']}: {i['value']}"
                             for i in r.get("ident") or [])
        ident_rows = (f"<p><b>Engine:</b> {engine}<br><b>VIN:</b> {vin}"
                      f"<br>{idents}</p>")

    notes_html = "".join(f"<li>{n}</li>" for n in assessment.get("notes") or []) \
        or "<li>No significant movement recorded.</li>"
    cold_warm = ""
    if assessment.get("cold_gain") is not None:
        cold_warm = (f"<p>Cold-window gain {assessment['cold_gain']} counts, "
                     f"warm-window gain {assessment['warm_gain']}.</p>")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>SLK rough-running report -- {args.car}</title><style>{_CSS}</style></head>
<main>
<h1>Rough-running diagnostic report</h1>
<div class="sub">Car: {args.car} &middot; compiled {now} &middot;
source runs in captures/mercedes/runs/ &middot;
plan: captures/mercedes/slk.md</div>

<h2>The car</h2>
<div class="card">{ident_rows or '<p class="muted">No identify run saved -- run mercedes_misfire_session identify first.</p>'}</div>

<h2>Fault memory</h2>
{_dtc_table(scan_run) if scan_run else '<p class="card muted">No scan run saved yet.</p>'}

<h2>How the live data stacks up</h2>
<div class="card">
<div class="verdict">Verdict: {assessment.get('verdict', 'unknown')}</div>
<ul>{notes_html}</ul>
{cold_warm}
<p class="muted">Capture: {cap_result.get('sampleCount', 0)} ticks /
{cap_result.get('elapsed_s', 0):.0f}s on {ecu_key or '?'};
rpm range {cap_result.get('rpm_range') or 'n/a'}. Counters are cumulative per drive
cycle -- small drift is normal; what matters is which hole moves and when.</p>
</div>
{charts_html}
<h2>What the research says (this engine)</h2>
<p class="muted">Other cars' failure statistics for this engine family --
a reading aid ranked against today's signature, never the diagnosis itself.
Full reference: backend/profiles/mercedes/data/slk_rough_running.json.</p>
<table><tr><th>Known issue</th><th>Symptoms</th><th>Signature match hint</th><th>Fix + sources</th></tr>
{issues_rows or '<tr><td colspan="4">Nothing matched.</td></tr>'}</table>

<h2>Next steps</h2>
<div class="card"><ol>
<li>If a cylinder led the counters: swap its coil with a neighbour and re-run a
short capture -- if the climb follows the coil, it is the coil; if it stays,
injector/compression.</li>
<li>Check the plug wells of the leading cylinders for oil (cam solenoid O-ring
leak pattern on both engines).</li>
<li>Re-run the cold-idle window before touching parts: the carbon/PCV patterns
live below 60 C coolant only.</li>
<li>Promote answered DIDs from candidate to verified in
backend/profiles/mercedes/data/xentry_dids.json (live_&lt;vin&gt; key), so the
next job starts from verified data.</li>
</ol></div>
</main></html>"""
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPORTS / f"slk-{args.car}-{stamp}.html"
    out.write_text(html, encoding="utf-8")
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
