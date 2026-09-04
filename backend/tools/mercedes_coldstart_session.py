"""Run the OM651 cold-start workflow against a real car, save it, compare runs.

Run:  .venv\\Scripts\\python.exe -m backend.tools.mercedes_coldstart_session <command> [options]

Commands:
    probe       key-on preflight -- which of the 41 mined CRD3MFA1 DIDs answer,
                decoded both byte orders, and which order the plausible
                channels vote for (cla-w117-coldstart.md step 0)
    capture     the start itself: key-on baseline -> crank -> run-up, with the
                assessment (step 1)
    compare     read two saved capture files and print what changed between
                them -- the whole point of taking a baseline

Options:
    --car NAME        label stored with the run (default cla-w117)
    --adapter ID      adapter instance id; defaults to the only ELM327 one
    --seconds N       capture length in seconds (default 300)
    --soak WORD       how cold the car was: overnight | hours | warm | unknown.
                      Stored with the run, because a capture is only readable
                      next to how long the car had been standing.
    --endian ORDER    force little | big; omit to let the preflight vote
    --no-preflight    skip the key-on DID sweep (only when probe already ran)
    --note TEXT       free-text note stored with the run
    --quiet           print the summary only, not the sample table
    --baseline PATH   compare: the earlier run
    --against PATH    compare: the later run

Every run is written to captures/mercedes/runs/<car>-coldstart-<soak>-<utc>.json,
the same discipline the SLK and Peugeot sessions use. Read-only against the
vehicle throughout: services 0x22 and 0x3E only.

WHY --soak MATTERS. The complaint only appears on the first start after a long
stand. A start taken a few hours after the engine last ran is NOT the fault
condition -- the fuel circuit is still primed from that run -- so it is a
BASELINE: it proves which DIDs answer, settles the byte order, and gives a
known-good crank trace. `compare` then subtracts it from the real cold start.
A baseline that ALSO struggles is itself a finding: it means the circuit drains
in hours rather than overnight, which points at a leak rather than seepage.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.adapters import registry as adapter_registry  # noqa: E402
from backend.profiles.mercedes import coldstart as mb_cold  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "captures" / "mercedes" / "runs"

SOAKS = ["overnight", "hours", "warm", "unknown"]

# The columns worth watching go by, in the order they answer the question.
_COLUMNS = [
    ("t", "t(s)", 7),
    ("phase", "phase", 7),
    ("did_5017", "rpm", 7),
    ("did_d058", "rail", 7),
    ("did_600a", "railDem", 8),
    ("did_60f9", "sync", 5),
    ("did_6000", "injMg", 7),
    ("did_d017_s", "glow", 14),
]

# What compare() lines up. (key, label, unit, lower-is-better-or-None)
_METRICS = [
    ("crank_seconds", "crank length", "s"),
    ("rail_at_crank_bar", "rail at first crank sample", "bar"),
    ("t_rail_release", "time rail reached release", "s"),
    ("rail_climb_seconds", "climb to release from crank start", "s"),
    ("release_to_fire_seconds", "release -> fire", "s"),
    ("rail_at_fire_bar", "rail at fire", "bar"),
    ("crank_rpm_min", "lowest cranking speed", "rpm"),
    ("stumble_rpm_min", "lowest rpm after it caught", "rpm"),
    ("settle_seconds", "time to hold idle", "s"),
    ("sync_seconds", "cam/crank sync", "s"),
    ("crank_voltage_min", "lowest battery volts", "V"),
]


def _elm_adapter(adapter_id=None):
    instances = [i.model_dump() for i in adapter_registry.list_instances()]
    if adapter_id:
        for inst in instances:
            if inst.get("id") == adapter_id:
                return inst
        return None
    elms = [i for i in instances if str(i.get("typeId", "")).startswith("elm327")]
    return elms[0] if elms else None


def _save(car, command, soak, result, note, adapter):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"{car}-{command}-{soak}-{stamp}.json"
    path.write_text(json.dumps({
        "car": car,
        "command": command,
        "soak": soak,
        "note": note,
        "adapter": (adapter or {}).get("name"),
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }, indent=2), encoding="utf-8")
    return path


def _print_samples(samples):
    header = "  ".join(label.rjust(width) for _, label, width in _COLUMNS)
    print(header)
    print("-" * len(header))
    for s in samples:
        row = []
        for key, _label, width in _COLUMNS:
            value = s.get(key)
            row.append(("" if value is None else str(value)).rjust(width))
        print("  ".join(row))


def _print_probe(result):
    rows = result.get("dids") or []
    answered = [r for r in rows if r.get("answered")]
    print(f"{len(answered)}/{len(rows)} DIDs answered.\n")
    if answered:
        print(f"  {'DID':6} {'family':18} {'value':>12}  name")
        print("  " + "-" * 74)
        for r in answered:
            value = r.get("state") if r.get("state") is not None else r.get("value")
            unit = f" {r['unit']}" if r.get("unit") and r.get("state") is None else ""
            print(f"  {r['did']:6} {r['family']:18} "
                  f"{str(value):>12}{unit}  {r['name']}")
    silent = [r["did"] for r in rows if not r.get("answered")]
    if silent:
        print(f"\n  SILENT ({len(silent)}): {', '.join(silent)}")
    endian = result.get("endian") or {}
    print(f"\n  byte order: {endian.get('endian')} "
          f"({'confident' if endian.get('confident') else 'UNCONFIRMED'})")
    print(f"  {endian.get('note', '')}")
    for row in endian.get("evidence") or []:
        print(f"    {row['did']} {row['name']}: "
              f"little={row.get('little')} big={row.get('big')} "
              f"(plausible {row['range'][0]}..{row['range'][1]})")
    print(f"\n  crank list: {', '.join(result.get('crankPlan') or []) or 'NONE'}")
    dropped = result.get("crankPlanDropped") or []
    if dropped:
        print(f"  DROPPED from the crank list: {', '.join(dropped)}")


def _load_run(path: Path) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    # Accept either a session file or a bare capture result, so a run pasted
    # out of the MCP tool can still be compared without being rewrapped.
    return doc if "result" in doc else {"result": doc, "soak": "unknown",
                                        "savedAt": None, "note": ""}


def _fmt(value, unit):
    if value is None:
        return "--"
    return f"{value:g} {unit}".strip()


def compare(baseline_path: Path, against_path: Path) -> int:
    base, later = _load_run(baseline_path), _load_run(against_path)
    a = (base["result"].get("assessment") or {})
    b = (later["result"].get("assessment") or {})
    print(f"BASELINE  {Path(baseline_path).name}"
          f"  soak={base.get('soak')}  verdict={a.get('verdict')}")
    print(f"AGAINST   {Path(against_path).name}"
          f"  soak={later.get('soak')}  verdict={b.get('verdict')}")
    print()
    print(f"  {'':36} {'baseline':>12} {'against':>12} {'change':>12}")
    print("  " + "-" * 74)
    for key, label, unit in _METRICS:
        x, y = a.get(key), b.get(key)
        if x is None and y is None:
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            delta = f"{y - x:+g}"
        else:
            delta = "--"
        print(f"  {label:36} {_fmt(x, unit):>12} {_fmt(y, unit):>12} "
              f"{delta:>12}")

    print()
    crank_a, crank_b = a.get("crank_seconds"), b.get("crank_seconds")
    if isinstance(crank_a, (int, float)) and isinstance(crank_b, (int, float)):
        grew = crank_b - crank_a
        if grew >= 0.5:
            print(f"  The crank grew by {grew:.1f}s against the baseline. "
                  "That growth IS the fault -- read the rail column over the "
                  "extra seconds.")
        elif grew <= -0.5:
            print(f"  The crank SHRANK by {-grew:.1f}s. Whatever changed "
                  "between these two runs helped.")
        else:
            print("  The crank barely moved between the two runs. If the "
                  "second one was a true cold soak and the first was not, the "
                  "fault did not reproduce -- do not read anything else here "
                  "as evidence.")
    for note in b.get("notes") or []:
        print(f"  - {note}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["probe", "capture", "compare"])
    parser.add_argument("--car", default="cla-w117")
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--soak", default="unknown", choices=SOAKS)
    parser.add_argument("--endian", default=None, choices=["little", "big"])
    parser.add_argument("--no-preflight", action="store_true")
    parser.add_argument("--note", default="")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--against", default=None)
    args = parser.parse_args()

    if args.command == "compare":
        if not args.baseline or not args.against:
            print("compare needs --baseline PATH and --against PATH.")
            return 1
        return compare(Path(args.baseline), Path(args.against))

    adapter = _elm_adapter(args.adapter)
    if adapter is None:
        print("No ELM327 adapter configured. Run list_adapters / the wizard first.")
        return 1

    if args.command == "probe":
        result = mb_cold.probe(adapter)
    else:
        if args.soak == "unknown":
            print("NOTE: --soak not given. A start is only readable next to "
                  "how long the car had been standing; this run is saved as "
                  "'unknown' and compare cannot tell you much.\n")
        result = mb_cold.coldstart(adapter, args.seconds, args.endian, None,
                                   not args.no_preflight)

    path = _save(args.car, args.command, args.soak, result, args.note, adapter)

    if args.command == "probe":
        _print_probe(result)
    else:
        samples = result.get("samples") or []
        if samples and not args.quiet:
            _print_samples(samples)
            print()
        if result.get("assessment"):
            print("ASSESSMENT: " + json.dumps(result["assessment"], indent=2))
    print()
    print(result.get("message", ""))
    print(f"Saved: {path}")
    if args.command == "capture":
        print("\nCompare it against another run with:")
        print("  .venv\\Scripts\\python.exe -m backend.tools."
              "mercedes_coldstart_session compare \\")
        print(f"      --baseline {path} --against <the next run>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
