"""Offline self-test for the BMW/MINI iDrive service-history builder.

Run:  cd car-harness; .venv\\Scripts\\python.exe -m backend.tools.service_history_selftest

No car, no dongle, no network -- this module only ever produces text. What it
proves is that the text is right, because the text goes into a job that writes
a permanent, un-editable record onto a customer's dashboard:

  * the argument string matches the published worked example field for field,
  * padded form is exactly 61 fields (the count Tool32 demands),
  * miles are converted to km -- the quiet failure that reads as a clocked car,
  * a 7-character dealer name is accepted and an 8-character one refused,
  * every refusal that protects a record is actually refused,
  * build -> parse round-trips,
  * the HTTP routes serve the catalogue and build a record.

Exits 0 on PASS, 1 on FAIL.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.profiles.bmw import service_history as sh  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f" -- {detail}"
    print(line)


def refuses(name, **kwargs):
    """A build that must be refused, with a message a human can act on."""
    base = dict(day=1, month=6, year=2026, odometer=50000, units="km",
                dealer_name="RADLETT", items=[{"code": 1, "status": 1}])
    base.update(kwargs)
    try:
        sh.build_argument(**base)
    except sh.ServiceHistoryError as exc:
        check(name, len(str(exc)) > 20, f"message too terse: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"wrong exception type: {type(exc).__name__}: {exc}")
        return
    check(name, False, "was accepted but should have been refused")


# --------------------------------------------------------------------------- #
# 1. The published worked example, rebuilt field for field.
#    bimmerguide.de: 13;06;2020;0;104414;1;WERKE;0x2;3;1;0x1;0;0;3;0x2;0;3;4;0x3;2200;3;
# --------------------------------------------------------------------------- #
example = sh.build_argument(
    day=13, month=6, year=2020, odometer=104414, units="km",
    dealer_name="WERKE", bmw_dealer=True, status=0x2,
    items=[
        {"code": 1, "status": 0x1, "distance_km": 0, "months": 0},
        {"code": 3, "status": 0x2, "distance_km": 0, "months": 3},
        {"code": 4, "status": 0x3, "distance_km": 2200, "months": 3},
    ],
    pad=False,
)
expected = "13;06;2020;0;104414;1;WERKE;0x2;3;1;0x1;0;0;3;0x2;0;3;4;0x3;2200;3;"
check("published worked example reproduced exactly",
      example["argument"] == expected,
      f"got {example['argument']}")
check("unpadded example is 21 fields", example["fieldCount"] == 21,
      str(example["fieldCount"]))

# --------------------------------------------------------------------------- #
# 2. Padding to the 61 arguments Tool32 reports wanting.
# --------------------------------------------------------------------------- #
padded = sh.build_argument(
    day=13, month=6, year=2020, odometer=104414, units="km",
    dealer_name="WERKE", bmw_dealer=True, status=0x2,
    items=[
        {"code": 1, "status": 0x1, "distance_km": 0, "months": 0},
        {"code": 3, "status": 0x2, "distance_km": 0, "months": 3},
        {"code": 4, "status": 0x3, "distance_km": 2200, "months": 3},
    ],
    pad=True,
)
check("padded form is exactly 61 fields", padded["fieldCount"] == 61,
      str(padded["fieldCount"]))
check("padded form keeps the real items in front",
      padded["argument"].startswith(expected.rstrip(";")),
      padded["argument"][:80])
check("padding is zero slots",
      padded["argument"].rstrip(";").endswith("0;0x0;0;0"),
      padded["argument"][-40:])
check("9 + 13*4 == 61 (the derivation MAX_ITEMS rests on)",
      9 + sh.MAX_ITEMS * 4 == 61)

# --------------------------------------------------------------------------- #
# 3. Miles -> km. Steve's cars read in miles; the field is km.
# --------------------------------------------------------------------------- #
check("60000 miles converts to 96561 km (96560.64, rounded)",
      sh.miles_to_km(60000) == 96561, str(sh.miles_to_km(60000)))
check("100 miles converts to 161 km", sh.miles_to_km(100) == 161,
      str(sh.miles_to_km(100)))
mi = sh.build_argument(day=1, month=6, year=2026, odometer=60000, units="mi",
                       dealer_name="RADLETT", items=[{"code": 1, "status": 1}],
                       pad=False)
check("miles build writes km into the argument",
      ";96561;" in mi["argument"], mi["argument"])
check("miles build reports both readings back",
      mi["summary"]["odometerKm"] == 96561
      and "mi" in mi["summary"]["odometerEntered"],
      str(mi["summary"]))
km = sh.build_argument(day=1, month=6, year=2026, odometer=60000, units="km",
                       dealer_name="RADLETT", items=[{"code": 1, "status": 1}],
                       pad=False)
check("km build is left alone", ";60000;" in km["argument"], km["argument"])

# --------------------------------------------------------------------------- #
# 4. The dealer-name field. "RADLETT" is exactly the 7 characters allowed.
# --------------------------------------------------------------------------- #
radlett = sh.build_argument(day=27, month=7, year=2026, odometer=48210, units="mi",
                            dealer_name="RADLETT", bmw_dealer=False, status=0x1,
                            items=[{"code": 1, "status": 1}, {"code": 4, "status": 1}],
                            pad=False)
check("RADLETT (7 chars) is accepted", ";RADLETT;" in radlett["argument"],
      radlett["argument"])
check("independent flag writes 0, not 1",
      radlett["argument"].split(";")[5] == "0",
      radlett["argument"])
check("independent build warns about the BMW-tag quirk",
      any("BMW tag" in n for n in radlett["notes"]))
refuses("8-character dealer name refused", dealer_name="RADLETTS")
refuses("empty dealer name refused", dealer_name="   ")
refuses("non-ASCII dealer name refused", dealer_name="RÄDLETT")

# --------------------------------------------------------------------------- #
# 5. Everything else that protects the record.
# --------------------------------------------------------------------------- #
refuses("no service items refused", items=[])
refuses("unknown service code refused", items=[{"code": 999, "status": 1}])
refuses("bad item status refused", items=[{"code": 1, "status": 9}])
refuses("bad visit status refused", status=7)
refuses("impossible date refused", day=31, month=2, year=2026)
refuses("negative mileage refused", odometer=-5)
refuses("absurd mileage refused", odometer=9_000_000)
refuses("miles entered as km beyond plausibility refused",
        odometer=3_000_000, units="mi")
refuses("bad units refused", units="furlongs")
refuses("14 items refused (13 slots)",
        items=[{"code": 1, "status": 1} for _ in range(14)])

check("13 items accepted (the exact limit)",
      sh.build_argument(day=1, month=6, year=2026, odometer=1000, units="km",
                        dealer_name="RADLETT", pad=False,
                        items=[{"code": 1, "status": 1} for _ in range(13)]
                        )["fieldCount"] == 9 + 13 * 4)

overdue = sh.build_argument(day=1, month=6, year=2026, odometer=1000, units="km",
                            dealer_name="RADLETT", pad=False,
                            items=[{"code": 2, "status": 0x2, "distance_km": -1500,
                                    "months": 0}])
check("negative remaining distance survives (means overdue)",
      ";-1500;" in overdue["argument"], overdue["argument"])

# --------------------------------------------------------------------------- #
# 6. Round-trip.
# --------------------------------------------------------------------------- #
back = sh.parse_argument(example["argument"])
check("round-trip: date", (back["day"], back["month"], back["year"]) == (13, 6, 2020))
check("round-trip: km", back["km"] == 104414)
check("round-trip: dealer + flag",
      back["dealer_name"] == "WERKE" and back["bmw_dealer"] is True)
check("round-trip: visit status", back["status"] == 0x2)
check("round-trip: three items with names",
      len(back["items"]) == 3
      and back["items"][0]["name"] == "Engine oil"
      and back["items"][2]["code"] == 4,
      str(back["items"]))
check("round-trip: padded form parses to the same 3 items",
      len(sh.parse_argument(padded["argument"])["items"]) == 3)

# --------------------------------------------------------------------------- #
# 7. Catalogue shape (this is what the UI renders).
# --------------------------------------------------------------------------- #
cat = sh.catalogue()
check("catalogue lists every service item",
      len(cat["items"]) == len(sh.SERVICE_ITEMS))
check("catalogue marks the common items",
      0 < sum(1 for i in cat["items"] if i["common"]) < len(cat["items"]))
check("catalogue names the job and group file",
      cat["job"] == "steuern_servicehistory_add" and cat["groupFile"] == "g_mmi.grp")
check("catalogue points at head unit 0x63", cat["huAddress"] == "0x63")
check("catalogue carries the whole g_mmi head-unit family with .prg names",
      all("prg" in hu and "confidence" in hu and "label" in hu
          for hu in cat["headUnits"])
      and {"enavevo", "nbtevo", "nbt", "entry", "hu_mgu"}
      <= {hu["id"] for hu in cat["headUnits"]},
      str(sorted(hu["id"] for hu in cat["headUnits"])))
check("catalogue leads with the group file, not a per-car .prg",
      cat["groupFile"] == "g_mmi.grp" and "asks the car" in cat["groupFileNote"])

# identify() is what stops this being a one-car tool.
check("identify: ISTA's HU_ENTRYNAV2 spelling resolves to enavevo",
      sh.identify("HU_ENTRYNAV2")["variant"]["id"] == "enavevo")
check("identify: HU_NBTEVO resolves to nbtevo, not nbt",
      sh.identify({"name": "HU_NBTEVO"})["variant"]["id"] == "nbtevo")
check("identify: ENTRYNAV alone is not mistaken for ENTRYNAV2",
      sh.identify("HU_ENTRYNAV")["variant"]["id"] == "entrynav")
check("identify: MGU resolves", sh.identify(["HU_MGU"])["variant"]["id"] == "hu_mgu")
check("identify: always names the group file even when it can't tell",
      sh.identify("")["identified"] is False
      and sh.identify("")["groupFile"] == "g_mmi.grp"
      and sh.identify("")["candidates"])
# The real MINI F56 ident. This USED to be the "refuses honestly" case, because
# nothing in it spells a variant name -- F187/F191/F189/F197 all refuse on this
# family. Then we lifted BMW's own index table out of T_GRTB.PRG, and F150 turned
# out to BE the answer: 0F1A30 = NBT. Three earlier guesses off the screen
# (entry, entrynav, rad_ukl) were all wrong. Now it resolves, and exactly.
_f56 = sh.identify({"F150": "0F1A30", "F18C": "0007972089",
                    "F190": "WMWXS72030T850490"})
check("identify: the real F56 ident resolves via F150 index to NBT",
      _f56["identified"] is True
      and _f56["variant"]["id"] == "nbt"
      and _f56["prg"] == "nbt.prg"
      and _f56["matchedBy"] == "index")
check("identify: the index route beats a misleading text token",
      sh.identify({"F150": "0F1A30", "note": "looks like an ENTRY unit"})
      ["variant"]["id"] == "nbt")
check("identify: every indexMap row points at a real variant",
      all(row["sgbd"] in {v["id"] for v in sh.HEAD_UNITS}
          for row in sh.INDEX_MAP.values()))
check("catalogue is honest that the central record is not reachable",
      cat["central"]["reachableOverCable"] is False)
check("catalogue names the confirmed RoutineControl id",
      cat["routine"] == "0xA081")
check("catalogue is honest that the routine is confirmed on NBT only",
      "Unconfirmed on other head-unit variants" in cat["routineNote"])

# --------------------------------------------------------------------------- #
# 7b. The raw telegram. Regression-locked against the ONE frame a real car
#     accepted (MINI F56, NBT, 2026-07-27) -- if this ever changes, we are no
#     longer sending what was proven to work.
# --------------------------------------------------------------------------- #
_PROVEN = ("3101A0811B0707EA000000D76B005241444C4554540102"
           "01010000000000006401000000000000")
_tg = sh.build_telegram(day=27, month=7, year=2026, odometer=34267,
                        units="miles", dealer_name="RADLETT", bmw_dealer=False,
                        status=1,
                        items=[{"code": 1, "status": 1, "distance_km": 0, "months": 0},
                               {"code": 100, "status": 1, "distance_km": 0, "months": 0}])
check("telegram reproduces the exact frame the car accepted", _tg["telegram"] == _PROVEN)
check("telegram is 39 bytes", _tg["telegramBytes"] == 39)
check("telegram opens with RoutineControl startRoutine 0xA081",
      _tg["telegram"].startswith("3101A081"))
check("odometer is encoded in km, not miles", "0000D76B" in _tg["telegram"])
check("dealer name is ASCII in the frame", "5241444C455454" in _tg["telegram"])

_short = sh.build_telegram(day=1, month=2, year=2020, odometer=1000, units="km",
                           dealer_name="ABC", bmw_dealer=True, status=1,
                           items=[{"code": 1, "status": 1, "distance_km": 0, "months": 0}])
check("short dealer name is padded to the fixed 7-byte field",
      _short["telegramBytes"] == 39 - 8)
check("bmw_dealer flag reaches the frame", "01" in _short["telegram"])

try:
    sh.write_entry(None, day=27, month=7, year=2026, odometer=100, units="km",
                   dealer_name="RADLETT", items=[{"code": 1, "status": 1,
                                                  "distance_km": 0, "months": 0}])
    check("write_entry refuses without confirm=True", False)
except sh.ServiceHistoryError:
    check("write_entry refuses without confirm=True", True)

# --------------------------------------------------------------------------- #
# 8. The HTTP surface.
# --------------------------------------------------------------------------- #
try:
    from fastapi.testclient import TestClient  # noqa: E402

    from backend.main import app  # noqa: E402

    client = TestClient(app)  # no "with" -- avoids the startup UDP responder

    r = client.get("/api/bmw/service-history")
    check("GET /api/bmw/service-history serves the catalogue",
          r.status_code == 200 and r.json()["job"] == "steuern_servicehistory_add",
          f"{r.status_code} {r.text[:120]}")

    r = client.post("/api/bmw/service-history/build", json={
        "day": 27, "month": 7, "year": 2026, "odometer": 48210, "units": "mi",
        "dealer_name": "RADLETT", "bmw_dealer": False, "status": 1,
        "items": [{"code": 1, "status": 1}, {"code": 4, "status": 1}],
        "pad": True,
    })
    ok = r.status_code == 200
    check("POST /api/bmw/service-history/build returns an argument",
          ok and r.json()["fieldCount"] == 61 and "RADLETT" in r.json()["argument"],
          f"{r.status_code} {r.text[:160]}")

    r = client.post("/api/bmw/service-history/build", json={
        "day": 27, "month": 7, "year": 2026, "odometer": 48210, "units": "mi",
        "dealer_name": "RADLETT CARS", "items": [{"code": 1, "status": 1}],
    })
    check("a bad record is a 400 with the reason, not a 500",
          r.status_code == 400 and "7" in r.text,
          f"{r.status_code} {r.text[:160]}")

    r = client.get("/api/mazda/service-history")
    check("a brand without the capability 404s", r.status_code == 404,
          str(r.status_code))

    r = client.get("/api/profiles")
    profiles = r.json()
    bmw = next((p for p in (profiles if isinstance(profiles, list)
                            else profiles.get("profiles", [])) if p.get("id") == "bmw"), None)
    check("BMW profile advertises the serviceHistory feature",
          bool(bmw) and bmw.get("features", {}).get("serviceHistory") is True,
          str(bmw.get("features") if bmw else profiles)[:200])
except Exception as exc:  # noqa: BLE001
    check("HTTP routes", False, f"{type(exc).__name__}: {exc}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
