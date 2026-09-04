"""Mine the OM651 cold-start toolkit out of the XENTRY Caesar layer.

Source (already unpacked on the data SSD, see docs/dataset-registry.md):
    E:\\car\\mercedes\\mined\\CRD3MFA1.json
      <- xdos/caesar_cbf/Xentry/MB_PKW/Caesar/cbf/CRD3MFA1.cbf
      (CRD3-MFA1 = OM651 2.1 diesel, transverse MFA platform: W117 CLA 220 CDI,
       W176 A-Class, X156 GLA)

`build_xentry_cla.py` already took the *actual values* (service 0x22) out of
that file. This script takes the two things that plan did not need and the
cold-start job does:

  1. ROUTINES (service 0x31). XENTRY's own guided tests for exactly this
     complaint are in there -- the dynamic hydraulic test that spins the
     starter and reports averaged rail pressure during cranking, the high
     pressure rise/leak test, the rail air exhaustion (bleed) routine, the
     lift pump test and the glow control test. These are the dealer answer to
     "did the rail take five seconds to come up, and if so which side leaked".
     They are ACTIVE tests: they command the car, so they are mined with their
     start/stop/results triple kept together and every one flagged.

  2. The DTCs (service 0x19) whose text names the fuel, glow, start or
     synchronisation path, with XENTRY's German fault text carried through.
     1,041 DTCs is too many to read; the ~200 that can explain a long crank
     are worth having next to the capture.

Output: backend/profiles/mercedes/data/om651_tests.json

Grading follows the same rule as every other mined table in this repo: every
entry is `candidate` until it has answered on a real car.
"""

import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.dealer_index.paths import corpus_root  # noqa: E402

OUT = REPO / "backend" / "profiles" / "mercedes" / "data" / "om651_tests.json"
VARIANT = "Diag_3300h"

# The routine groups worth having, in the order the cold-start plan runs them.
# key -> (routine id, human title, why it is here, does it crank the engine)
ROUTINE_GROUPS = OrderedDict([
    ("dyn_hydraulic", ("F004", "Dynamic hydraulic test",
                       "Spins the starter and reports the AVERAGED rail "
                       "pressure during cranking, the pressure after the up "
                       "and down states, how long the pressure took to fall, "
                       "and the engine speed at the end. This is the closest "
                       "dealer equivalent of the extended-crank complaint.",
                       True)),
    ("high_pressure", ("032A", "High pressure test",
                       "Rise time, leak time, pressure reached and pressure "
                       "at the end of the test. Separates 'the pump cannot "
                       "build it' from 'it builds and then leaks away'.",
                       True)),
    ("rail_air", ("0327", "Rail air exhaustion (bleed) test",
                  "Purges air from the high pressure circuit. Run it AFTER "
                  "the cold-start capture: if the next cold start is normal, "
                  "air ingress is proven.",
                  True)),
    ("lift_pump", ("F10D", "Fuel pump / lift pump test",
                   "Runs the in-tank low pressure pump on demand. Tests the "
                   "supply side that a drain-back fault sits on.",
                   False)),
    ("glow_control", ("F104", "Glow control desired function test",
                      "Commands the glow controller and reports its result. "
                      "The pre-heat half of the diagnosis.",
                      False)),
    ("glow_indicator", ("F10F", "Glow plug indicator test",
                        "Drives the dash glow lamp -- proves the tester is "
                        "actually talking to the glow path.",
                        False)),
    ("compression", ("0325", "Compression test (classic / modified)",
                     "The ECU's own cranking compression comparison. Directly "
                     "relevant after a cylinder head rebuild.",
                     True)),
    ("fuel_pump_pressure", ("03B7", "Controlled fuel pump pressure test",
                            "Commands a pressure and reports whether it held.",
                            True)),
    ("injector_0", ("F115", "Injector 1 test", "Per-injector activation.", True)),
    ("injector_1", ("F116", "Injector 2 test", "Per-injector activation.", True)),
    ("injector_2", ("F117", "Injector 3 test", "Per-injector activation.", True)),
    ("injector_3", ("F118", "Injector 4 test", "Per-injector activation.", True)),
    ("imv_diagnosis", ("F00F", "Inlet metering valve diagnosis",
                       "Reads back the rail pressure error table -- the "
                       "metering valve is the usual reason a rail will not "
                       "come up on cranking.", True)),
])

# What can explain a long crank. Matched against BOTH halves of a XENTRY
# fault description: the ASCII symbolic name the ECU's own software uses
# (GLOW_PLUG1_DIAG_OPN_LD) and the German text after the colon. The symbolic
# name is the reliable half -- it is plain ASCII and it survives any encoding
# accident, which the German text historically did not.
DTC_PATTERNS = OrderedDict([
    ("fuel_pressure", r"rail\w*p|railp|RPC_|HPP|MEUN|IMV|"
                      r"raildruck|kraftstoffdruck|hochdruck|zumesseinheit|"
                      r"druckregelventil|niederdruck"),
    ("injector", r"INJ|INJ_|injector|injektor|einspritzventil|"
                 r"einspritzmenge|mengenabgleich"),
    ("glow", r"glow|gl[uü]hkerze|gl[uü]hzeit|gl[uü]hsteuer|vorgl[uü]h|"
             r"GPU|preheat|vorheiz"),
    ("start_sync", r"APS_|crank|cam_|camshaft|synchron|START|starter|"
                   r"kurbelwelle|nockenwelle|drehzahlgeber|anlasser"),
    ("fuel_supply", r"fuel|lift_?pump|kraftstoffpumpe|vorf[oö]rderpumpe|"
                    r"kraftstoff|leckage|luft im|entl[uü]ft|leak"),
])


def _fix_text(text: str) -> str:
    """Undo the CBF dump's encoding accident.

    The parser wrote UTF-8 bytes and something downstream read them back as
    ISO-8859-10 (Latin-6), so every German umlaut arrived as a two-character
    pair -- "KÃžhlerjalousie" for "Kühlerjalousie". Round-tripping through that
    codec puts them back. Text that does not round-trip is left exactly as it
    was rather than guessed at.
    """
    try:
        return text.encode("iso8859_10").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _load_variant(root: Path) -> dict:
    src = root / "mined" / "CRD3MFA1.json"
    if not src.exists():
        raise SystemExit(f"missing {src} -- see docs/dataset-registry.md")
    doc = json.loads(src.read_text(encoding="utf-8", errors="replace"))
    for variant in doc.get("variants", []):
        if variant.get("name") == VARIANT:
            return variant
    raise SystemExit(f"variant {VARIANT} not found in {src}")


def _param(raw: dict) -> dict:
    """One output parameter, normalised to the same shape cla_dids.json uses.

    start_bit is absolute in the whole response frame including the service
    byte, so byte_offset is start_bit // 8 -- no extra 3-byte header offset
    the way a 0x22 read has, because a routine reply's header length varies
    with the routine id.
    """
    fmt = raw.get("data_format") or {}
    out = {
        "name": raw.get("name"),
        "unit": (raw.get("unit") or "").strip() or None,
        "byte_offset": raw["start_bit"] // 8,
        "bit_offset": raw["start_bit"] % 8,
        "length_bits": raw["length_bits"],
        "byte_size": max(1, raw["length_bits"] // 8),
        # Carried through for the record, NOT trusted: the CBF declares
        # BigEndian for every parameter of every Mercedes ECU mined so far,
        # including the SIM271DE20 that then answered LITTLE-endian on the car
        # on 2026-08-23. Treat it as a default the car may overrule.
        "declared_byte_order": raw.get("byte_order"),
    }
    if "Linear" in fmt:
        out["conversion"] = {"type": "linear",
                             "multiplier": fmt["Linear"].get("multiplier", 1.0),
                             "offset": fmt["Linear"].get("offset", 0.0)}
    elif "Table" in fmt:
        out["conversion"] = {"type": "table", "states": [
            {"name": s["name"], "start": s["start"], "end": s["end"]}
            for s in fmt["Table"]]}
    elif "Bool" in fmt:
        out["conversion"] = {"type": "bool",
                             "true": fmt["Bool"].get("pos_name"),
                             "false": fmt["Bool"].get("neg_name")}
    elif "String" in fmt:
        out["conversion"] = {"type": "string", "encoding": fmt["String"]}
    else:
        out["conversion"] = {"type": "unknown", "raw": list(fmt)}
    return out


def build_routines(variant: dict) -> dict:
    """Group the mined functions by routine id into start / stop / results."""
    by_rid: dict = {}
    for fn in variant.get("functions", []):
        payload = (fn.get("payload") or "").upper()
        if not payload.startswith("31") or len(payload) < 8:
            continue
        sub, rid = payload[2:4], payload[4:8]
        by_rid.setdefault(rid, {"start": [], "stop": [], "results": []})
        slot = {"01": "start", "02": "stop", "03": "results"}.get(sub)
        if slot is None:
            continue
        by_rid[rid][slot].append(fn)

    routines = OrderedDict()
    for key, (rid, title, why, cranks) in ROUTINE_GROUPS.items():
        found = by_rid.get(rid)
        if not found:
            continue
        # Every "results" function repeats the same 31 03 <rid> request and
        # differs only in which slice of the reply it names, so they collapse
        # into ONE request plus a list of fields. That is what makes a routine
        # readable in a single round trip instead of eight.
        fields, seen = [], set()
        results_payload = None
        for fn in found["results"]:
            results_payload = results_payload or (fn.get("payload") or "").upper()
            for raw in fn.get("output_params", []):
                field = _param(raw)
                field["label"] = (fn.get("description") or fn["name"])
                sig = (field["byte_offset"], field["length_bits"], field["label"])
                if sig in seen:
                    continue
                seen.add(sig)
                fields.append(field)
        fields.sort(key=lambda f: f["byte_offset"])

        # Prefer the plainest start variant: no input parameters means no
        # injector code to supply, which is the only form we can actually run.
        starts = sorted(found["start"],
                        key=lambda f: (len(f.get("input_params") or []),
                                       len(f.get("payload") or "")))
        routines[key] = {
            "routine_id": rid,
            "title": title,
            "why": why,
            "cranks_or_commands_engine": cranks,
            "start": {"payload": (starts[0].get("payload") or "").upper(),
                      "name": starts[0]["name"],
                      "needs_input": bool(starts[0].get("input_params"))} if starts else None,
            "start_variants": [{"name": f["name"],
                                "payload": (f.get("payload") or "").upper(),
                                "description": f.get("description"),
                                "needs_input": bool(f.get("input_params"))}
                               for f in starts],
            "stop": {"payload": (found["stop"][0].get("payload") or "").upper(),
                     "name": found["stop"][0]["name"]} if found["stop"] else None,
            "results": {"payload": results_payload, "fields": fields},
            "grade": "candidate",
        }
    return routines


def build_dtcs(variant: dict) -> list:
    compiled = [(fam, re.compile(pat, re.I)) for fam, pat in DTC_PATTERNS.items()]
    out = []
    for err in variant.get("errors", []):
        text = _fix_text(err.get("description") or "").strip()
        # XENTRY writes "SYMBOLIC_NAME: German sentence." The symbol is the
        # ECU software's own label for the fault and is often more precise
        # than the sentence, so it is kept as its own field.
        symbol, _, german = text.partition(":")
        if not german:
            symbol, german = "", text
        families = [fam for fam, rx in compiled if rx.search(text)]
        if not families:
            continue
        out.append({
            "code": err.get("error_name"),
            "families": families,
            "symbol": symbol.strip() or None,
            "description": german.strip().rstrip("_ ").strip() or None,
            "grade": "candidate",
        })
    out.sort(key=lambda d: d["code"] or "")
    return out


def main() -> None:
    variant = _load_variant(Path(corpus_root("mercedes")))
    routines = build_routines(variant)
    dtcs = build_dtcs(variant)
    doc = {
        "title": "Mercedes OM651 (CRD3-MFA1) - XENTRY guided tests and "
                 "cold-start fault codes",
        "provenance": {
            "package": "XENTRY_Diagnostics_OpenShell_26.6.4.zip "
                       "(chain in xentry_dids.json)",
            "source_file": "xdos/caesar_cbf/Xentry/MB_PKW/Caesar/cbf/"
                           "CRD3MFA1.cbf -> mined/CRD3MFA1.json",
            "variant": VARIANT,
            "builder": "tools/dealer_index/build_xentry_om651_tests.py",
            "connection": "HSCAN UDS 500 kbit, 0x7E0/0x7E8, global id 0x441",
        },
        "notes": [
            "Every entry is graded 'candidate' -- none has run on a car yet.",
            "ROUTINES ARE ACTIVE TESTS. Service 0x31 commands the ECU; several "
            "of them spin the starter, drive injectors or run the fuel pump. "
            "They are not the read-only 0x22 reads the capture uses. Run them "
            "only deliberately, with the car safe to crank, and expect an "
            "extended session (10 03) and possibly SecurityAccess first.",
            "The CBF declares BigEndian on every parameter of every Mercedes "
            "ECU mined so far, yet the SLK's SIM271DE20 answered LITTLE-endian "
            "on the car (2026-08-23). declared_byte_order is recorded but not "
            "trusted; decode both ways and keep the raw bytes.",
            "Result field byte_offset is absolute in the whole reply frame "
            "(service byte included), unlike the 0x22 tables whose offsets "
            "start after the 62 <DID> header.",
        ],
        "routines": routines,
        "dtcs": dtcs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(f"wrote {OUT}: {len(routines)} routines, {len(dtcs)} cold-start DTCs")
    for key, r in routines.items():
        print(f"  {key:20s} {r['routine_id']}  "
              f"{len(r['results']['fields'])} result field(s)")


if __name__ == "__main__":
    main()
