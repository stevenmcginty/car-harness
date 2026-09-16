"""Astra J VXR (A20NFT / E39A) on-car runner -- the REST/MCP-free way to drive
backend/profiles/vauxhall/engine.py from a terminal at the car.

    .venv\\Scripts\\python.exe -m backend.tools.vauxhall_engine probe
    .venv\\Scripts\\python.exe -m backend.tools.vauxhall_engine probe --sweep
    .venv\\Scripts\\python.exe -m backend.tools.vauxhall_engine faults
    .venv\\Scripts\\python.exe -m backend.tools.vauxhall_engine capture --seconds 900 --label drive
    .venv\\Scripts\\python.exe -m backend.tools.vauxhall_engine report
    .venv\\Scripts\\python.exe -m backend.tools.vauxhall_engine report <capture.jsonl>

Uses the saved default adapter unless --adapter is given. ``--sim`` runs the
whole thing offline against the simulated E39A in sim_ecm.py, and ``--scenario``
picks which engine that fake is pretending to be.

Read-only. No clear, no coding, no adaptation, and no DeviceControl -- the $AE
frames GM's data carries are printed by ``probe --controls`` and never sent.

NOTHING THIS PRINTS HAS BEEN CONFIRMED ON AN ENGINE. Every name, scaling and
unit is GM dealer data read statically out of GDS2; the Astra VXR has not yet
been connected. The applicability table ``probe`` prints is the thing the first
real run exists to produce.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.adapters import registry as adapter_registry  # noqa: E402
from backend.profiles.vauxhall import engine as vx_engine  # noqa: E402
from backend.profiles.vauxhall import sim_ecm as vx_sim_ecm  # noqa: E402


def _adapter(adapter_id):
    inst = adapter_registry.resolve(adapter_id)
    if inst is None:
        sys.exit("No adapter: set a default with set_default_adapter or pass --adapter.")
    return inst.model_dump()


def _print_probe(result: dict, show_controls: bool) -> None:
    ecm = result.get("ecm") or {}
    vehicle = result.get("vehicle") or {}
    print(f"\n[{ecm.get('address')} {ecm.get('name')}] dialect "
          f"{ecm.get('dialect')}   VIN {vehicle.get('vin') or 'not reported'}")

    print("\nIdentification")
    for row in result.get("ident") or []:
        print(f"  {str(row.get('label'))[:36]:<36} {row.get('value')}")
    if not result.get("ident"):
        print("  (nothing answered)")

    print("\nFaults")
    for dtc in result.get("dtcs") or []:
        name = dtc.get("gmName") or dtc.get("description") or ""
        print(f"  {dtc.get('display') or dtc.get('code'):<12} {name}")
    if not result.get("dtcs"):
        print("  (none stored)")

    print(f"\n  {'Group':<8} {'Parameter':<48} {'Value':>12} {'Unit':<7} "
          f"{'Request':<10} Raw int")
    for row in result.get("live") or []:
        print(f"  {str(row.get('group')):<8} {str(row.get('name'))[:46]:<48} "
              f"{str(row.get('value')):>12} {str(row.get('unit')):<7} "
              f"{str(row.get('request')):<10} {row.get('raw')}")
    if not result.get("live"):
        print("  (nothing in the core block answered)")

    report = result.get("didReport") or {}
    print(f"\nApplicability: {len(report.get('answered') or [])} answered, "
          f"{len(report.get('refused') or [])} refused, "
          f"{len(report.get('silent') or [])} silent, "
          f"{len(report.get('other') or [])} other")
    for row in report.get("refused") or []:
        print(f"  {row['request']:<10} NRC {row.get('nrc')} "
              f"({row.get('nrcName')})  {', '.join(n for n in row['names'] if n)}")
    for row in report.get("silent") or []:
        print(f"  {row['request']:<10} silent           "
              f"{', '.join(n for n in row['names'] if n)}")

    if show_controls:
        print("\nDeviceControl ($AE) frames GM's data carries for this ECM. "
              "NOT SENT by this tool.")
        for control in result.get("deviceControls") or []:
            print(f"  {str(control.get('name'))[:52]:<52} {control.get('request')}")

    print("\nFindings")
    for line in result.get("findings") or []:
        print(f"  - {line}")
    print("\n" + result.get("message", ""))


def _print_faults(result: dict) -> None:
    print(f"\n{'Code':<12} {'St':<4} {'List':<8} Text")
    for dtc in result.get("dtcs") or []:
        print(f"{dtc.get('display') or dtc.get('code'):<12} "
              f"{str(dtc.get('status')):<4} {str(dtc.get('gmNameList') or ''):<8} "
              f"{dtc.get('gmName') or dtc.get('description') or ''}")
    if not result.get("dtcs"):
        print("(no stored faults on the engine controller)")
    if result.get("error"):
        print("\nfault read note:", result["error"])
    print("\n" + result.get("message", ""))


def main() -> int:
    ap = argparse.ArgumentParser(prog="vauxhall_engine")
    ap.add_argument("cmd", choices=["probe", "faults", "capture", "report"])
    ap.add_argument("path", nargs="?",
                    help="capture .jsonl to summarise (report only); omit to "
                         "write the analyse worksheet instead")
    ap.add_argument("--adapter")
    ap.add_argument("--sim", action="store_true", help="offline, no dongle")
    ap.add_argument("--scenario", default="healthy",
                    choices=sorted(vx_sim_ecm.SCENARIOS),
                    help="which engine the simulator pretends to be (--sim only)")
    ap.add_argument("--sweep", action="store_true",
                    help="probe: read EVERY service 0x22 request GM's data "
                         "lists (hundreds of frames, minutes over Bluetooth) "
                         "instead of the 35-parameter core block")
    ap.add_argument("--controls", action="store_true",
                    help="probe: also list the $AE DeviceControl frames GM's "
                         "data carries. They are never transmitted")
    ap.add_argument("--seconds", type=float, default=600.0)
    ap.add_argument("--label", default="drive",
                    choices=["idle", "cold", "drive", "pull", "other"])
    ap.add_argument("--full", action="store_true",
                    help="capture: read the whole core block every tick "
                         "instead of the ten load-sensitive parameters")
    args = ap.parse_args()
    mode = "sim" if args.sim else "real"
    adapter = None if args.sim else _adapter(args.adapter)

    if args.cmd == "report":
        result = vx_engine.report(args.path, adapter, mode, args.scenario)
        print(result.get("message", ""))
        if result.get("savedTo"):
            print("Saved:", result["savedTo"])
        return 0 if result.get("ok") else 1

    if args.cmd == "probe":
        result = vx_engine.analyze(adapter, mode, args.sweep, args.scenario)
        _print_probe(result, args.controls)
    elif args.cmd == "faults":
        result = vx_engine.faults(adapter, mode, args.scenario)
        _print_faults(result)
    else:
        print("Capturing. Ctrl+C stops and keeps the log.", flush=True)
        stop = {"now": False}
        try:
            signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))
        except ValueError:
            pass  # not on the main thread -- Ctrl+C simply won't be caught
        result = vx_engine.capture(
            adapter, args.seconds, mode, not args.full, args.label,
            args.scenario, should_stop=lambda: stop["now"])
        print(result.get("message", ""))
        if result.get("savedTo"):
            print("Saved:", result["savedTo"])
    if not result.get("ok"):
        print(json.dumps({"message": result.get("message")}, indent=1))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
