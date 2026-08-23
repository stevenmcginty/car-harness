"""Run the SLK R172 rough-running workflow against a real car and save it.

Run:  .venv\\Scripts\\python.exe -m backend.tools.mercedes_misfire_session <command> [options]

Commands:
    identify          which engine is fitted + VIN (slk.md step 0)
    scan              engine + gearbox fault memory with freeze-frame context
                      (slk.md step 1 -- mercedes_scan.scan)
    capture           the rough-running drive log + assessment
                      (slk.md step 4)

Options:
    --car NAME        label stored with the run (e.g. slk-r172)
    --adapter ID      adapter instance id; defaults to the only ELM327 one
    --seconds N       capture length in seconds (default 300)
    --ecu KEY         skip identification: sim271de20_m271_turbo | med177_m276_v6
    --note TEXT       free-text note stored with the run
    --quiet           print the summary only, not the sample table

Every run is written to captures/mercedes/runs/<car>-<command>-<utc>.json --
the same discipline that keeps the Peugeot baseline runs separable from the
patient car's. Read-only against the vehicle throughout.

The drive profile to run during a capture (captures/mercedes/slk.md step 4):
    cold start, untouched idle 3 min -> warm idle 2 min -> hold ~2000 rpm
    -> hold ~3000 rpm -> (if load-dependent) a road loop.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.adapters import registry as adapter_registry  # noqa: E402
from backend.profiles.mercedes import misfire as mb_misfire  # noqa: E402
from backend.profiles.mercedes import scan as mb_scan  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "captures" / "mercedes" / "runs"

_COLUMNS = [
    ("t", "t(s)", 7),
    ("rpm", "rpm", 7),
    ("cyl1", "cyl1", 6),
    ("cyl2", "cyl2", 6),
    ("cyl3", "cyl3", 6),
    ("cyl4", "cyl4", 6),
    ("cyl5", "cyl5", 6),
    ("cyl6", "cyl6", 6),
    ("did_2046", "lambda", 7),
    ("did_2013", "clt C", 7),
    ("did_2071", "fuel", 8),
]


def _elm_adapter(adapter_id=None):
    instances = [i.model_dump() for i in adapter_registry.list_instances()]
    if adapter_id:
        for inst in instances:
            if inst.get("id") == adapter_id:
                return inst
        return None
    elms = [i for i in instances if str(i.get("typeId", "")).startswith("elm327")]
    return elms[0] if len(elms) == 1 else (elms[0] if elms else None)


def _save(car, command, result, note, adapter):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"{car}-{command}-{stamp}.json"
    path.write_text(json.dumps({
        "car": car,
        "command": command,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["identify", "scan", "capture"])
    parser.add_argument("--car", default="slk-r172")
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--ecu", default=None,
                        choices=[None, "sim271de20_m271_turbo", "med177_m276_v6"])
    parser.add_argument("--note", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    adapter = _elm_adapter(args.adapter)
    if adapter is None:
        print("No ELM327 adapter configured. Run list_adapters / the wizard first.")
        return 1

    if args.command == "identify":
        result = mb_misfire.identify(adapter)
    elif args.command == "scan":
        result = mb_scan.scan(adapter)
    else:
        result = mb_misfire.capture(adapter, args.seconds, args.ecu)

    path = _save(args.car, args.command, result, args.note, adapter)

    samples = result.get("samples") or []
    if samples and not args.quiet:
        _print_samples(samples)
        print()
    if result.get("assessment"):
        print("ASSESSMENT: " + json.dumps(result["assessment"], indent=2))
    if args.command == "identify":
        for item in result.get("ident") or []:
            print(f"  {item['label']}: {item['value']}")
        print(f"ECU: {result.get('engine') or result.get('ecu')}")
    if args.command == "scan":
        for module in result.get("modules") or []:
            codes = ", ".join(d.get("code", "?") for d in module.get("dtcs") or [])
            print(f"  {module.get('code')} {module.get('address')}: "
                  f"{'present' if module.get('present') else 'absent'}"
                  + (f" faults: {codes}" if codes else ""))
    print()
    print(result.get("message", ""))
    print(f"Saved: {path}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
