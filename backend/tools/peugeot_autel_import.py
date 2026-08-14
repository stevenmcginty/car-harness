"""Import an Autel (or Diagbox) live-data CSV into the harness capture store.

Run:  .venv\\Scripts\\python.exe -m backend.tools.peugeot_autel_import LOG.csv --car 308cc

Why this exists: a dongle cannot read the ECU's oil-pressure SETPOINT or its
named valve duty cycle, but a proper scan tool can. This converts that tool's
log into the same shape the harness stores its own captures in, so an Autel run
and a dongle run land on the same charts in peugeot_oil_report and can be
compared against the known-good RCZ baselines directly.

It is deliberately forgiving about format, because scan-tool exports vary wildly:

  * comma or semicolon separated, with or without quoting;
  * decimal commas (European locale) as well as decimal points;
  * a preamble of vehicle-information lines before the real header row;
  * English or French column names (Diagbox exports "consigne", "regime");
  * pressure in bar, mbar, kPa or psi, temperature in C or F.

Columns are matched on their names, units are taken from the header where the
tool wrote them and inferred from the values where it did not, and every
decision it makes is printed so a wrong guess is visible rather than silent.
"""

import argparse
import csv
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

OUT_DIR = Path(__file__).resolve().parents[2] / "captures" / "peugeot-oil"

# What we are looking for, and the words a scan tool might use for it. Matched
# case-insensitively against the header; the FIRST pattern that hits wins, so
# the more specific spellings come first (setpoint before plain pressure).
FIELDS = [
    ("time_s", ["time", "temps", "timestamp", "seconds", "sec"]),
    ("oil_pressure_setpoint_bar",
     ["oil pressure setpoint", "oil press setpoint", "pressure setpoint",
      "consigne pression", "pression consigne", "setpoint", "consigne",
      "target pressure", "requested pressure", "reference value",
      "ref oil pressure", "oil pressure ref", "reference oil pressure",
      "demanded oil pressure", "desired oil pressure"]),
    ("pump_duty_pct",
     ["oil pump valve", "pump valve duty", "duty cycle", "rapport cyclique",
      "rco", "solenoid duty", "valve duty", "oil pump duty", "opv",
      "solenoid valve ref", "solenoid valve", "electrovanne"]),
    ("oil_pressure_bar",
     ["measured oil pressure", "oil pressure measured", "oil pressure",
      "pression d'huile", "pression huile", "oil press", "engine oil pressure"]),
    ("engine_rpm",
     ["engine speed", "engine rpm", "regime moteur", "régime moteur",
      "rpm", "regime", "régime"]),
    ("coolant_c",
     ["coolant", "liquide de refroidissement", "water temp", "eau moteur",
      "engine temp"]),
    ("oil_temp_c", ["oil temp", "temperature d'huile", "température d'huile",
                    "huile temp"]),
]

# Unit spellings found in headers like "Oil pressure (mbar)" or "Regime [tr/min]".
_UNIT_RE = re.compile(r"[\(\[]\s*([^)\]]+?)\s*[\)\]]")


def _norm(text):
    """Lower-case, and treat underscores and hyphens as spaces.

    Machine-written headers arrive as measured_oil_pressure_mbar rather than
    "Oil Pressure (mbar)", and both have to match the same patterns.
    """
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", (text or "").strip().lower()))


# Units written as a trailing word instead of in brackets: ..._mbar, ..._pct.
_SUFFIX_UNITS = ("mbar", "kpa", "psi", "bar", "pct", "percent", "rpm", "degc", "c", "f")


def _header_unit(header):
    found = _UNIT_RE.search(header or "")
    if found:
        return _norm(found.group(1))
    words = _norm(header).split()
    if words and words[-1] in _SUFFIX_UNITS:
        return words[-1]
    return ""


def _to_float(raw):
    """Parse a cell, tolerating decimal commas, thousands separators and units."""
    if raw is None:
        return None
    text = str(raw).strip().replace(" ", " ")
    if not text or text.lower() in ("na", "n/a", "-", "--", "null", "error"):
        return None
    text = re.sub(r"[^0-9,.\-+eE]", "", text)
    if not text:
        return None
    # 1.234,5 -> European; 1,234.5 -> Anglo; 1234,5 -> European decimal comma.
    if "," in text and "." in text:
        text = (text.replace(".", "").replace(",", ".")
                if text.rfind(",") > text.rfind(".") else text.replace(",", ""))
    elif "," in text:
        text = text.replace(",", ".") if len(text.split(",")[-1]) <= 3 else text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _to_seconds(raw):
    """Time cell to seconds. Handles 12.5, 00:26 and 01:02:03."""
    if raw is None:
        return None
    text = str(raw).strip()
    if ":" in text:
        parts = text.split(":")
        try:
            values = [float(p.replace(",", ".")) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for value in values:
            seconds = seconds * 60 + value
        return seconds
    return _to_float(text)


def _find_header(rows):
    """The header is the first row where at least two known field names match.

    Scan tools often print vehicle information above the table, so the first
    row of the file is frequently not the header.
    """
    best, best_hits = 0, 0
    for index, row in enumerate(rows[:40]):
        cells = [_norm(c) for c in row]
        hits = sum(1 for _key, names in FIELDS
                   if any(any(n in cell for n in names) for cell in cells if cell))
        if hits > best_hits:
            best, best_hits = index, hits
    return (best, best_hits) if best_hits >= 2 else (None, best_hits)


def _map_columns(header):
    """{field: column index}, each column claimed by at most one field."""
    mapping, taken = {}, set()
    for key, names in FIELDS:
        for index, cell in enumerate(header):
            if index in taken:
                continue
            text = _norm(cell)
            if text and any(name in text for name in names):
                mapping[key] = index
                taken.add(index)
                break
    return mapping


def _pressure_scale(unit, values):
    """Return (multiplier to bar, how it was decided)."""
    if "mbar" in unit:
        return 0.001, "header said mbar"
    if "kpa" in unit:
        return 0.01, "header said kPa"
    if "psi" in unit:
        return 0.0689476, "header said psi"
    if "bar" in unit:
        return 1.0, "header said bar"
    if not values:
        return 1.0, "no values, assumed bar"
    # No unit written: decide on magnitude. A THP oil pressure is roughly
    # 1-5 bar, which is 1000-5000 mbar, 100-500 kPa or 15-75 psi.
    median = statistics.median(values)
    if median > 500:
        return 0.001, f"no unit; median {median:.0f} looks like mbar"
    if median > 60:
        return 0.01, f"no unit; median {median:.0f} looks like kPa"
    if median > 12:
        return 0.0689476, f"no unit; median {median:.0f} looks like psi"
    return 1.0, f"no unit; median {median:.1f} looks like bar"


def _temp_scale(unit, values):
    if "f" in unit and "c" not in unit:
        return "F", "header said Fahrenheit"
    if values and statistics.median(values) > 130:
        return "F", "no unit; values too high for Celsius"
    return "C", "assumed Celsius"


def parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = [r for r in csv.reader(text.splitlines(), delimiter=delimiter) if any(c.strip() for c in r)]
    if not rows:
        raise SystemExit(f"{path.name}: no rows found.")

    header_index, hits = _find_header(rows)
    if header_index is None:
        raise SystemExit(
            f"{path.name}: could not find a header row naming any known values. "
            f"Best match had {hits} recognised column(s). Expected columns for "
            "engine speed and oil pressure at least.")
    header = rows[header_index]
    mapping = _map_columns(header)
    body = rows[header_index + 1:]

    raw = {key: [] for key in mapping}
    for row in body:
        for key, index in mapping.items():
            if index >= len(row):
                raw[key].append(None)
            elif key == "time_s":
                raw[key].append(_to_seconds(row[index]))
            else:
                raw[key].append(_to_float(row[index]))

    notes = []
    for key, index in mapping.items():
        notes.append(f"{key} <- column {index + 1} \"{header[index].strip()}\"")

    press_scale = 1.0
    if "oil_pressure_bar" in mapping:
        values = [v for v in raw["oil_pressure_bar"] if v is not None]
        press_scale, why = _pressure_scale(_header_unit(header[mapping["oil_pressure_bar"]]), values)
        notes.append(f"oil pressure: {why}")
    set_scale = press_scale
    if "oil_pressure_setpoint_bar" in mapping:
        values = [v for v in raw["oil_pressure_setpoint_bar"] if v is not None]
        set_scale, why = _pressure_scale(
            _header_unit(header[mapping["oil_pressure_setpoint_bar"]]), values)
        notes.append(f"oil pressure setpoint: {why}")

    temp_units = {}
    for key in ("coolant_c", "oil_temp_c"):
        if key in mapping:
            values = [v for v in raw[key] if v is not None]
            unit, why = _temp_scale(_header_unit(header[mapping[key]]), values)
            temp_units[key] = unit
            notes.append(f"{key}: {why}")

    # Duty: a tool may log 0-1, 0-100 or 0-255. Normalise to percent.
    duty_scale = 1.0
    if "pump_duty_pct" in mapping:
        values = [v for v in raw["pump_duty_pct"] if v is not None]
        top = max(values) if values else 0
        if top <= 1.5:
            duty_scale, why = 100.0, "0-1 fraction scaled to percent"
        elif top > 101:
            duty_scale, why = 100.0 / 255.0, "0-255 raw scaled to percent"
        else:
            duty_scale, why = 1.0, "already percent"
        notes.append(f"pump duty: {why}")

    count = max((len(v) for v in raw.values()), default=0)
    samples = []
    for i in range(count):
        def get(key):
            values = raw.get(key)
            return values[i] if values and i < len(values) else None

        sample_row = {}
        elapsed = get("time_s")
        sample_row["t"] = round(elapsed, 3) if elapsed is not None else float(i)
        rpm = get("engine_rpm")
        if rpm is not None:
            sample_row["engine_rpm"] = round(rpm, 1)
        press = get("oil_pressure_bar")
        if press is not None:
            sample_row["oil_pressure_bar"] = round(press * press_scale, 3)
        setp = get("oil_pressure_setpoint_bar")
        if setp is not None:
            sample_row["oil_pressure_setpoint_bar"] = round(setp * set_scale, 3)
        duty = get("pump_duty_pct")
        if duty is not None:
            sample_row["pump_duty_pct"] = round(duty * duty_scale, 1)
            # The report and assessment speak in the 0-255 command the dongle
            # reads, so give them an equivalent they can plot alongside it.
            sample_row["pump_command"] = round(sample_row["pump_duty_pct"] * 255 / 100)
        for key in ("coolant_c", "oil_temp_c"):
            value = get(key)
            if value is not None:
                if temp_units.get(key) == "F":
                    value = (value - 32) * 5.0 / 9.0
                sample_row[key] = round(value, 1)
        if len(sample_row) > 1:
            samples.append(sample_row)

    curve = [{"rpm": s["engine_rpm"], "bar": s["oil_pressure_bar"]}
             for s in samples
             if s.get("engine_rpm") and s.get("oil_pressure_bar") is not None]
    return {"samples": samples, "curve": curve, "notes": notes,
            "columns": {k: header[v].strip() for k, v in mapping.items()}}


def summarise(parsed: dict) -> str:
    samples = parsed["samples"]
    if not samples:
        return "No usable rows."
    lines = []
    press = [s["oil_pressure_bar"] for s in samples if s.get("oil_pressure_bar") is not None]
    if press:
        lines.append(f"oil pressure {min(press):.2f}-{max(press):.2f} bar")
    duty = [s["pump_duty_pct"] for s in samples if s.get("pump_duty_pct") is not None]
    if duty:
        lines.append(f"valve duty {min(duty):.0f}-{max(duty):.0f} %")
        if max(duty) - min(duty) < 3:
            lines.append("WARNING: duty barely moved across this log")
    setp = [s["oil_pressure_setpoint_bar"] for s in samples
            if s.get("oil_pressure_setpoint_bar") is not None]
    if setp and press:
        gaps = [s["oil_pressure_bar"] - s["oil_pressure_setpoint_bar"] for s in samples
                if s.get("oil_pressure_bar") is not None
                and s.get("oil_pressure_setpoint_bar") is not None]
        if gaps:
            worst = max(gaps, key=abs)
            lines.append(f"setpoint {min(setp):.2f}-{max(setp):.2f} bar, "
                         f"measured minus setpoint worst {worst:+.2f} bar")
    rpm = [s["engine_rpm"] for s in samples if s.get("engine_rpm")]
    if rpm:
        lines.append(f"rpm {min(rpm):.0f}-{max(rpm):.0f}")
        if max(rpm) - min(rpm) < 400:
            lines.append("WARNING: rpm barely moved, so nothing can be told apart by speed")
    return "; ".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="the scan tool's exported log")
    parser.add_argument("--car", required=True, help="car label, e.g. 308cc or rcz-1")
    parser.add_argument("--note", default="", help="what was happening during the log")
    parser.add_argument("--rows", type=int, default=12, help="rows to print")
    args = parser.parse_args()

    path = Path(args.csv)
    if not path.exists():
        raise SystemExit(f"No such file: {path}")
    parsed = parse(path)

    print("Column mapping and unit decisions:")
    for note in parsed["notes"]:
        print(f"  {note}")
    print()
    samples = parsed["samples"]
    print(f"{len(samples)} usable row(s). {summarise(parsed)}")
    print()
    keys = ["t", "engine_rpm", "oil_pressure_bar", "oil_pressure_setpoint_bar",
            "pump_duty_pct", "coolant_c", "oil_temp_c"]
    present = [k for k in keys if any(k in s for s in samples)]
    print("  ".join(k.rjust(11) for k in present))
    step = max(1, len(samples) // max(1, args.rows))
    for sample in samples[::step][:args.rows]:
        print("  ".join(("" if sample.get(k) is None else str(sample.get(k))).rjust(11)
                        for k in present))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_DIR / f"{args.car}-autel-{stamp}.json"
    out.write_text(json.dumps({
        "car": args.car,
        "command": "autel",
        "note": args.note or f"imported from {path.name}",
        "adapter": "scan tool CSV",
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "source": str(path),
        "result": {
            "ok": True, "simulated": False,
            "samples": samples, "curve": parsed["curve"],
            "columns": parsed["columns"], "importNotes": parsed["notes"],
            "message": summarise(parsed),
        },
    }, indent=2), encoding="utf-8")
    print()
    print(f"Saved: {out}")
    print(f"Now run:  .venv\\Scripts\\python.exe -m backend.tools.peugeot_oil_report --car {args.car}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
