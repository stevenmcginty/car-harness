"""OM651 preflight over the running backend's REST API, not a fresh process.

WHY THIS FILE EXISTS
--------------------
The phone relay socket is held by the backend server on 127.0.0.1:8765. Any
OTHER process -- the MCP tools, mercedes_coldstart_session -- has its own
adapter registry and its own idea of what is connected, so it reports "No phone
relay is connected" while the relay is plainly up and the phone is sitting in
the car. That trap cost an hour on 2026-08-23 and it cost another attempt
today.

So when the car is on the far end of a relay, the preflight has to be driven
through the server that owns the socket. `/api/raw/did` already reads arbitrary
service-0x22 DIDs from one module, which is exactly what the preflight is, and
it needs no new endpoint and no server restart -- which matters when the car is
sitting there going cold and a restart would drop the relay.

This reads the mined CRD3MFA1 table through that endpoint, decodes every reply
BOTH byte orders using the same code the capture uses, votes on the byte order,
and saves the run beside every other capture.

Run:  .venv\\Scripts\\python.exe -m backend.tools.mercedes_coldstart_rest [--host ...] [--note ...]

Read-only: service 0x22 only, ignition on, engine off.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.profiles.mercedes import coldstart as mb_cold  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "captures" / "mercedes" / "runs"
BATCH = 20      # /api/raw/did caps at 24 per call; leave headroom
ENGINE = "7E0"


def _post(host: str, path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{host}{path}", method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(host: str, path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"{host}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="http://127.0.0.1:8765")
    parser.add_argument("--car", default="cla-w117")
    parser.add_argument("--soak", default="hours",
                        choices=["overnight", "hours", "warm", "unknown"])
    parser.add_argument("--note", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    try:
        relay = _get(args.host, "/api/remote/status")
    except (urllib.error.URLError, OSError) as exc:
        print(f"Cannot reach the backend at {args.host}: {exc}")
        print("Start it, or pass --host.")
        return 1
    print(f"relay: connected={relay.get('connected')} "
          f"device={relay.get('deviceName')!r} since={relay.get('connectedAt')}")
    if not relay.get("connected"):
        print("The relay is down. Switch Relay on in the phone app.")
        return 1

    entries = mb_cold._entries()
    table = mb_cold._by_did()
    dids = [e["did"] for e in entries]
    print(f"reading {len(dids)} mined CRD3MFA1 DIDs on 0x{ENGINE} "
          f"in {((len(dids) - 1) // BATCH) + 1} batch(es)...")

    raws: dict = {}
    vin = None
    # F190 first: it is the one read that proves the ECU is really answering
    # rather than the dongle echoing, and it names the car in the saved file.
    for batch in [["F190"]] + [dids[i:i + BATCH]
                               for i in range(0, len(dids), BATCH)]:
        try:
            out = _post(args.host, "/api/raw/did",
                        {"target": ENGINE, "dids": batch}, args.timeout)
        except (urllib.error.URLError, OSError) as exc:
            print(f"  batch failed: {exc}")
            continue
        if not out.get("ok"):
            print(f"  {out.get('message')}")
        for row in out.get("results") or []:
            did = (row.get("did") or "").upper()
            if not row.get("ok") or not row.get("raw"):
                continue
            if did == "F190":
                vin = (row.get("ascii") or "").strip()[:17] or None
                continue
            raws[did] = row["raw"].upper()
        print(f"  batch of {len(batch)}: "
              f"{sum(1 for r in (out.get('results') or []) if r.get('ok'))} answered")

    if vin:
        print(f"\nVIN: {vin}")

    endian = mb_cold.pick_endian(raws)
    rows = []
    for entry in entries:
        did = entry["did"].upper()
        raw_hex = raws.get(did)
        row = {"did": entry["did"], "name": entry["name"],
               "family": entry["family"], "unit": entry.get("unit") or None,
               "answered": raw_hex is not None, "raw": raw_hex}
        if raw_hex:
            reply = bytes.fromhex(raw_hex)
            row["value_little"] = mb_cold.decode_entry(entry, reply, "little")
            row["value_big"] = mb_cold.decode_entry(entry, reply, "big")
            row["value"] = row[f"value_{endian['endian']}"]
            row["state"] = mb_cold.enum_name(table[did], row["value"])
        rows.append(row)

    answered = [r for r in rows if r["answered"]]
    print(f"\n{len(answered)}/{len(rows)} DIDs answered.\n")
    if answered:
        print(f"  {'DID':6} {'family':18} {'raw':>12} "
              f"{'little':>12} {'big':>12}  name")
        print("  " + "-" * 96)
        for r in answered:
            print(f"  {r['did']:6} {r['family']:18} {str(r['raw']):>12} "
                  f"{str(r['value_little']):>12} {str(r['value_big']):>12}  "
                  f"{r['name']}"
                  + (f" [{r['state']}]" if r.get("state") else ""))
    silent = [r["did"] for r in rows if not r["answered"]]
    if silent:
        print(f"\n  SILENT ({len(silent)}): {', '.join(silent)}")

    print(f"\n  byte order: {endian['endian']} "
          f"({'confident' if endian['confident'] else 'UNCONFIRMED'})")
    print(f"  {endian['note']}")
    for row in endian.get("evidence") or []:
        print(f"    {row['did']} {row['name']}: little={row.get('little')} "
              f"big={row.get('big')} (plausible "
              f"{row['range'][0]}..{row['range'][1]})")

    crank = [d for d in mb_cold.CRANK_DIDS if d in raws]
    dropped = [d for d in mb_cold.CRANK_DIDS if d not in raws]
    print(f"\n  crank list ready: {', '.join(crank) or 'NONE'}")
    if dropped:
        print(f"  DROPPED (silent): {', '.join(dropped)}")

    result = {"ok": bool(answered), "simulated": False, "vin": vin,
              "dids": rows, "answeredCount": len(answered),
              "totalCount": len(rows), "endian": endian,
              "crankPlan": crank, "crankPlanDropped": dropped,
              "via": "REST /api/raw/did on the relay-holding server",
              "timestamp": datetime.now(timezone.utc).isoformat()}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"{args.car}-probe-{args.soak}-{stamp}.json"
    path.write_text(json.dumps({
        "car": args.car, "command": "probe", "soak": args.soak,
        "note": args.note, "adapter": relay.get("deviceName"),
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "result": result}, indent=2), encoding="utf-8")
    print(f"\nSaved: {path}")
    return 0 if answered else 1


if __name__ == "__main__":
    raise SystemExit(main())
