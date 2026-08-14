"""Run the EP6/THP oil-pressure workflow against a real car and save the result.

Run:  .venv\\Scripts\\python.exe -m backend.tools.peugeot_oil_session <command> [options]

Commands:
    probe                     map the engine ECU's diagnostic surface + oil DIDs
    capture                   cold-start / running time-series, decoded + assessed
    sweep FIRST LAST          walk a mode-22 DID range (hex bounds, e.g. D400 D4FF)
    scan                      module fault memory (peugeot_scan.scan)

Options:
    --car NAME                label stored with the capture (e.g. rcz-1, 308cc)
    --adapter ID              adapter instance id; defaults to the only ELM327 one
    --seconds N               capture length (default 120)
    --dids A,B,C              capture DIDs (default: the imported catalog)
    --address psa|obd         which engine address to talk to (default psa)
    --extended                open the read-safe PSA session before reading
    --note TEXT               free-text note stored with the run
    --quiet                   print the summary only, not the sample table

Why this exists: the MCP tools call the same functions, but a stored file is what
the RCZ-baseline-vs-308 comparison actually needs. Every run is written to
captures/peugeot-oil/<car>-<command>-<utc timestamp>.json with the car label, the
adapter used and the full result, so a baseline can never be confused with a
patient car. Read-only against the vehicle throughout.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.adapters import registry as adapter_registry  # noqa: E402
from backend.profiles.peugeot import oil as peugeot_oil  # noqa: E402
from backend.profiles.peugeot import scan as peugeot_scan  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "captures" / "peugeot-oil"

# Columns printed for a capture, in the order that reads like a story:
# when, how fast the engine turned, what pressure came of it, how warm it was.
_COLUMNS = [
    ("t", "t(s)", 7),
    ("engine_rpm", "rpm", 7),
    ("oil_pressure_bar", "oil bar", 8),
    ("oil_pressure_mirror_bar", "mirror", 7),
    ("oil_pressure_delta_bar", "delta", 6),
    ("pump_command", "pump", 5),
    ("pump_command_pct", "pump%", 6),
    ("did_D429", "D429", 6),
    ("oil_temp_c", "oil C", 6),
    ("coolant_c", "clt C", 6),
    ("module_voltage", "volts", 6),
]


def _elm_adapter(adapter_id=None):
    """The ELM327 adapter instance dict, or None if there isn't a usable one."""
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
    parser.add_argument(
        "command",
        choices=["probe", "capture", "sweep", "scan", "blocks", "coldstart",
                 "spot", "drive"])
    parser.add_argument("bounds", nargs="*", help="sweep: FIRST LAST as hex")
    parser.add_argument("--car", default="unknown")
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--dids", default=None)
    parser.add_argument("--address", default="psa", choices=["psa", "obd"])
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--note", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    adapter = _elm_adapter(args.adapter)
    if adapter is None:
        print("No ELM327 adapter configured. Run list_adapters / the wizard first.")
        return 1

    if args.command == "probe":
        result = peugeot_oil.probe(adapter)
    elif args.command == "drive":
        result = peugeot_oil.drive(adapter, args.seconds / 60.0
                                   if args.seconds > 60 else args.seconds)
    elif args.command == "spot":
        result = peugeot_oil.spot(adapter, int(args.seconds) if args.seconds < 10 else 3)
    elif args.command == "coldstart":
        blocks = None if args.dids is None else [
            b.strip() for b in args.dids.split(",") if b.strip()
        ]
        result = peugeot_oil.coldstart(adapter, args.seconds, blocks)
    elif args.command == "blocks":
        blocks = None if args.dids is None else [
            b.strip() for b in args.dids.split(",") if b.strip()
        ]
        result = peugeot_oil.block_capture(adapter, args.seconds, blocks)
    elif args.command == "scan":
        result = peugeot_scan.scan(adapter)
    elif args.command == "sweep":
        if len(args.bounds) != 2:
            print("sweep needs FIRST and LAST, e.g. sweep D400 D4FF")
            return 1
        first, last = (int(b, 16) for b in args.bounds)
        result = peugeot_oil.sweep(adapter, first, last, args.address, args.extended)
    else:
        dids = None if args.dids is None else [
            d.strip() for d in args.dids.split(",") if d.strip()
        ]
        result = peugeot_oil.capture(
            adapter, args.seconds, dids, args.address, args.extended)

    path = _save(args.car, args.command, result, args.note, adapter)

    samples = result.get("samples") or []
    if samples and not args.quiet:
        _print_samples(samples)
        print()
    if result.get("assessment"):
        print("ASSESSMENT: " + json.dumps(result["assessment"], indent=2))
    if args.command == "probe":
        print("IDENTITY: " + json.dumps(result.get("identity") or {}))
        for row in result.get("addresses") or []:
            print(f"  {row['address']} ({row['label']}): answers={row['answers']} "
                  f"22={row['service_22']} 10C0={row['session_10C0']} "
                  f"10 03={row['session_1003']} ident={row['ident']}")
        for row in result.get("oil_dids") or []:
            print(f"  OIL {row['did']} @{row['address']}: raw={row['raw']} "
                  f"{row['name']}={row['value']} {row['unit'] or ''}")
        print("OBD PIDs: " + ", ".join(
            f"{p['name']}={p['value']}{p['unit']}" for p in result.get("obd_pids") or []))
    if args.command == "sweep":
        for row in result.get("answered") or []:
            print(f"  {row['did']}: {row['payload']}")
    print()
    print(result.get("message", ""))
    print(f"Saved: {path}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
