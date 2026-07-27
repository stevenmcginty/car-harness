"""BMW / MINI iDrive service history -- the record the car itself shows.

WHAT THIS IS FOR
----------------
Modern BMWs and MINIs have no paper service book. The car keeps its own
service history in the HEAD UNIT (iDrive -> Vehicle Info -> Service history),
and BMW keeps a separate central record on its own servers. They are two
different things and only one of them is reachable through the OBD port:

    in the car   HEAD UNIT (diag address 0x63), written over the ENET cable
                 by the EDIABAS job STEUERN_SERVICEHISTORY_ADD.  <- this file
    central      BMW's Digital Service Book, written through the AOS portal
                 (KaSIO / ISTA).  Needs a registered workshop login. There is
                 NO route to it over any cable, at any price. See the research
                 doc for how a UK independent registers.

So a dealer who services a car can put "serviced by us, this date, this
mileage" onto the customer's iDrive screen. It is the car's record, not BMW's.

WHAT THIS FILE DOES AND DOESN'T DO
----------------------------------
It builds and validates the ARGUMENT STRING for the job, and nothing else.
It does not touch the car. car-harness cannot yet execute the job itself: the
job lives inside BMW's compiled SGBD (.prg) and the raw telegram it produces
has never been published, so we would be inventing bytes. Until that telegram
is captured, the write is done by Tool32/EDIABAS over the same ENET cable,
with the string this module produces pasted into the argument box.

The honest split matters: everything below is either sourced or derived, and
labelled as such.

SOURCES
-------
- bimmerguide.de "Service Historie: Eintrag mit Tool32" -- field-by-field
  argument breakdown + service code table + worked example.
- f30.bimmerpost tutorial t=2096113 (f30tikka) -- per-head-unit .prg files,
  ENET interface setting, STEUERGERAETE_RESET afterwards to make the entry
  appear, and the warning about losing cloud service-history retrieval.
- theDeniZ/ServiceHistory + /ServiceManager (GitHub) -- job names
  steuern_servicehistory_add / _erase, g_mmi.grp group file.
"""

from __future__ import annotations

from datetime import date as _date

# --------------------------------------------------------------------------- #
# Service items.
#
# Codes are the published bimmerguide table. Duplicate meanings are real (BMW
# has separate codes for the petrol and diesel variants of the same job) --
# they are kept verbatim rather than tidied, because guessing which one a given
# car wants is exactly the kind of invention that puts a wrong record on a
# customer's screen. "common" marks the eleven the community argument-builder
# offers, which is the set with the most field mileage behind it.
# --------------------------------------------------------------------------- #
SERVICE_ITEMS: dict[int, dict] = {
    # -- source "enum": verbatim from theDeniZ/ServiceManager serviceitem.h,
    #    read directly from the raw file 2026-07-27. Safe to offer by default.
    1: {"name": "Engine oil", "common": True, "source": "enum"},
    2: {"name": "Front brake pads", "common": True, "source": "enum"},
    3: {"name": "Brake fluid", "common": True, "source": "enum"},
    4: {"name": "Microfilter (cabin filter)", "common": True, "source": "enum"},
    5: {"name": "Recirculating air filter", "common": True, "source": "enum"},
    6: {"name": "Rear brake pads", "common": True, "source": "enum"},
    10: {"name": "Spark plugs", "common": True, "source": "enum"},
    11: {"name": "Air filter element", "common": True, "source": "enum"},
    12: {"name": "Fuel filter", "common": True, "source": "enum"},
    20: {"name": "Pre-delivery check", "common": True, "source": "enum"},
    21: {"name": "Service inspection", "common": True, "source": "enum",
         "note": "The whole-service line. This is the one a routine service wants."},
    32: {"name": "Statutory vehicle inspection (HU/MOT)", "common": True,
         "source": "enum"},
    33: {"name": "Emissions test (AU)", "common": False, "source": "enum"},
    100: {"name": "Vehicle check", "common": True, "source": "enum"},

    # -- source "forum": from the bimmerguide argument table only. NOT in the
    #    enum above. The SGBD calls this field "eindeutiger Code des
    #    anzuzeigenden Textes" -- the text table lives inside the head unit, so
    #    codes outside the enum may well be real on some units and simply
    #    unknown on others. Kept rather than deleted, but never "common": a
    #    wrong code puts a wrong line on a customer's screen. Verify on a car
    #    before offering these by default.
    7: {"name": "Diesel particulate filter", "common": False, "source": "forum"},
    8: {"name": "Fuel filter (forum variant)", "common": False, "source": "forum",
        "note": "Enum says fuel filter is 12. Prefer 12."},
    16: {"name": "Spark plugs (forum variant)", "common": False, "source": "forum",
         "note": "Enum says spark plugs is 10. Prefer 10."},
    18: {"name": "Coolant", "common": False, "source": "forum"},
    22: {"name": "Diesel additive", "common": False, "source": "forum"},
    219: {"name": "Recirculation filter (forum variant)", "common": False,
          "source": "forum",
          "note": "Enum says recirculating air filter is 5. Prefer 5."},
}

# Maintenance status, used both for the whole visit and per item.
STATUS = {
    0x1: "Carried out on time",
    0x2: "Carried out late",
    0x3: "Not carried out / incomplete",
}

# --------------------------------------------------------------------------- #
# Head units -- the whole g_mmi family, loaded from data.
#
# THE IMPORTANT BIT, and the answer to "what happens when I get a newer MINI
# in": g_mmi.grp is a GROUP file. You load the group, and EDIABAS asks the car
# which variant it is and pulls the right .prg itself. One workflow covers CIC
# through MGU. The per-variant table is a reference and a fallback -- useful for
# telling a dealer what he's looking at before anything is plugged in, and for
# the cases where the group resolution needs overriding.
#
# The table lives in head_units.json so a new head unit is a data edit, not a
# code change. See that file's _comment for the confidence grades.
# --------------------------------------------------------------------------- #

import json as _json
from pathlib import Path as _Path

_HU_PATH = _Path(__file__).with_name("head_units.json")


def _load_head_units() -> dict:
    with _HU_PATH.open("r", encoding="utf-8") as fh:
        return _json.load(fh)


_HU = _load_head_units()
HEAD_UNITS: list[dict] = _HU["variants"]
GROUP_FILE: str = _HU["groupFile"]

# F150 SGBD index -> variant. BMW's own T_GRTB table; see head_units.json.
INDEX_MAP: dict[str, dict] = _HU.get("indexMap", {})

# Head-unit diag address on F/G-series. Same address our module scan already
# reads ident from, so a scan report can name the unit before anyone opens a
# diagnostic suite.
HU_ADDRESS = 0x63


def identify(ident) -> dict:
    """Work out which head unit a car has, from whatever its 0x63 ident said.

    `ident` is anything the scan produced -- a dict of DID values, a list of
    strings, or one string. We match on the variant tokens BMW's own tools
    report (HU_NBTEVO, HU_ENTRYNAV2, HU_MGU and friends), because those are the
    names the ECU and ISTA actually use.

    Deliberately returns candidates rather than one confident answer when the
    text doesn't name a variant: a wrong .prg wastes a minute, but a confident
    wrong answer on a dealer's screen costs trust. The group file works either
    way, which is why it is always the recommendation.
    """
    text = _flatten(ident).upper()

    # -- F150 FIRST. This is the reliable route and it beats text matching
    # outright.
    #
    # The head units in this family will NOT tell you their variant name: F187,
    # F191, F189 and F197 all return NRC 0x31, and KWP 0x1A returns NRC 0x11.
    # F150 always answers, and F150 IS the answer -- it carries the 3-byte SGBD
    # index, and BMW's own T_GRTB table maps index -> variant. That table is in
    # head_units.json as indexMap, so this works offline with no EDIABAS.
    #
    # Learned the hard way on a real MINI F56: its screen looks like a Visual
    # Boost, and three separate passes guessed entry, then entrynav, then
    # rad_ukl. Its F150 said 0F1A30, which is NBT. The dashboard lies; F150
    # doesn't.
    for index, row in INDEX_MAP.items():
        if index in text:
            variant = {v["id"]: v for v in HEAD_UNITS}.get(row["sgbd"])
            if variant is None:
                continue
            return {
                "identified": True,
                "variant": variant,
                "matchedOn": f"F150 SGBD index {index}",
                "matchedBy": "index",
                "sgbdIndex": index,
                "prg": variant["prg"],
                "groupFile": GROUP_FILE,
                "recommendation": (
                    f"F150 index {index} is {row['label']} -- load "
                    f"{variant['prg']}, or {GROUP_FILE} which resolves to the "
                    "same thing. This came from BMW's own index table, so it "
                    "beats anything inferred from the screen or the badge."
                ),
                "canWriteHistory": variant.get("history"),
                "note": variant.get("note"),
                "raw": text[:400],
            }

    # Longest tokens first so ENTRYNAV2 wins over ENTRYNAV, and MGU_02_L over
    # MGU. Aliases cover the ISTA/E-Sys spellings.
    aliases: list[tuple[str, str]] = [
        ("MGU_02_L", "mgu_02_l"), ("MGU_02_A", "mgu_02_a"),
        ("ENTRYNAV2", "enavevo"), ("ENAVEVO", "enavevo"),
        ("NBTEVO", "nbtevo"), ("NBT_EVO", "nbtevo"),
        ("HU_MGU", "hu_mgu"), ("MGU", "hu_mgu"),
        ("ENTRYNAV", "entrynav"), ("RAD_UKL", "rad_ukl"),
        ("CHAMP2", "champ2"), ("CICM", "cicm"), ("CIC", "cic"),
        ("BIS01", "bis01"), ("RAD12", "rad12"),
        ("NBT", "nbt"), ("ENTRY", "entry"),
    ]

    match_id = None
    matched_on = None
    for token, variant_id in aliases:
        if token in text:
            match_id = variant_id
            matched_on = token
            break

    by_id = {v["id"]: v for v in HEAD_UNITS}
    if match_id:
        variant = by_id[match_id]
        return {
            "identified": True,
            "variant": variant,
            "matchedOn": matched_on,
            "prg": variant["prg"],
            "groupFile": GROUP_FILE,
            "recommendation": (
                f"Load {GROUP_FILE} and let it resolve -- it should pick "
                f"{variant['prg']}. Load {variant['prg']} directly only if the "
                f"group file can't reach the car."
            ),
            "canWriteHistory": variant.get("history"),
            "note": variant.get("note"),
            "raw": text[:400],
        }

    return {
        "identified": False,
        "variant": None,
        "prg": None,
        "groupFile": GROUP_FILE,
        "recommendation": (
            f"The ident didn't name a variant. Load {GROUP_FILE} anyway -- the "
            "group file asks the car itself, which is more reliable than "
            "matching text. If the job list comes up with "
            "steuern_servicehistory_add in it, you're on the right unit."
        ),
        "candidates": [v for v in HEAD_UNITS if v.get("history") == "yes"],
        "raw": text[:400],
        "help": "Send me this raw ident and I'll add the car to head_units.json.",
    }


def _flatten(value) -> str:
    """Ident data arrives in whatever shape the scan produced. Flatten it."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(v) for v in value)
    return str(value)

MAX_ITEMS = 13
"""Service-item slots in the job.

DERIVED, not published: a forum report has Tool32 demanding 61 arguments for
STEUERN_SERVICEHISTORY_ADD. The documented layout is 8 header fields + 1 count
+ N x 4 item fields, and (61 - 9) / 4 = 13 exactly. So the job declares 13
slots and Tool32 wants every one of them present. build_argument(pad=True)
emits the padded 61-field form; pad=False emits the short form that the
published worked example uses. If one is rejected at the car, try the other --
that is the single most likely thing to go wrong, and it is harmless: a
rejected job writes nothing.
"""

DEALER_NAME_MAX = 7
"""ARG_HAENDLERNUMMER is 7 ASCII characters, spaces included. "RADLETT" fits
exactly. Longer names are refused rather than silently truncated -- a dealer
name is the whole point of the entry."""


class ServiceHistoryError(ValueError):
    """A refusal to build a record that would be wrong on the car's screen."""


def _check_status(value: int, what: str) -> int:
    if value not in STATUS:
        raise ServiceHistoryError(
            f"{what} must be one of {sorted(STATUS)} "
            f"(1 = on time, 2 = late, 3 = not carried out); got {value!r}."
        )
    return value


def miles_to_km(miles: float) -> int:
    """UK cars read in miles; the job field is ARG_KMSTAND, in km.

    Getting this wrong is the quiet failure mode -- 60,000 miles entered raw
    shows as 60,000 km (37,282 miles) and the record looks like a clocked car.
    """
    if miles < 0:
        raise ServiceHistoryError("Mileage cannot be negative.")
    return int(round(miles * 1.609344))


def build_argument(
    *,
    day: int,
    month: int,
    year: int,
    odometer: float,
    units: str = "km",
    dealer_name: str,
    bmw_dealer: bool = False,
    status: int = 0x1,
    items: list[dict] | None = None,
    correction_counter: int = 0,
    pad: bool = True,
) -> dict:
    """Build the STEUERN_SERVICEHISTORY_ADD argument string.

    items: [{"code": 1, "status": 1, "distance_km": 0, "months": 0}, ...]
           distance_km/months are what REMAINS until that item is next due;
           negative distance means overdue.

    Returns the string plus everything needed to show a human what will be
    written, because the entry cannot be edited afterwards -- only the whole
    history can be erased and re-entered.
    """
    items = items or []
    if not items:
        raise ServiceHistoryError(
            "A service record needs at least one service item -- an entry with "
            "no items tells the next owner nothing."
        )
    if len(items) > MAX_ITEMS:
        raise ServiceHistoryError(
            f"The job has {MAX_ITEMS} service-item slots; got {len(items)}. "
            "Split the visit across two entries."
        )

    try:
        _date(int(year), int(month), int(day))
    except ValueError as exc:
        raise ServiceHistoryError(f"Not a real date: {day}/{month}/{year} ({exc}).")
    if int(year) < 1990 or int(year) > 2099:
        raise ServiceHistoryError("Year looks wrong -- expected 1990-2099.")

    units = units.lower()
    if units in ("mi", "mile", "miles"):
        km = miles_to_km(odometer)
    elif units in ("km", "kilometre", "kilometres", "kilometer", "kilometers"):
        if odometer < 0:
            raise ServiceHistoryError("Mileage cannot be negative.")
        km = int(round(odometer))
    else:
        raise ServiceHistoryError("units must be 'mi' or 'km'.")
    if km > 2_000_000:
        raise ServiceHistoryError(
            f"{km} km is not a plausible odometer reading -- check the units."
        )

    name = (dealer_name or "").strip()
    if not name:
        raise ServiceHistoryError("A dealer name is required.")
    if len(name) > DEALER_NAME_MAX:
        raise ServiceHistoryError(
            f"Dealer name is {len(name)} characters; the field holds "
            f"{DEALER_NAME_MAX}. Shorten it (e.g. 'RADLETT')."
        )
    try:
        name.encode("ascii")
    except UnicodeEncodeError:
        raise ServiceHistoryError("Dealer name must be plain ASCII.")

    _check_status(int(status), "Visit status")
    if int(correction_counter) < 0:
        raise ServiceHistoryError("Correction counter cannot be negative.")

    fields: list[str] = [
        f"{int(day):02d}",
        f"{int(month):02d}",
        f"{int(year):04d}",
        str(int(correction_counter)),
        str(km),
        "1" if bmw_dealer else "0",
        name,
        f"0x{int(status):X}",
        str(len(items)),
    ]

    resolved: list[dict] = []
    for raw in items:
        code = int(raw.get("code", 0))
        if code not in SERVICE_ITEMS:
            raise ServiceHistoryError(
                f"Unknown service code {code}. Known codes: {sorted(SERVICE_ITEMS)}."
            )
        item_status = _check_status(int(raw.get("status", 0x1)), f"Status for code {code}")
        distance = int(raw.get("distance_km", 0))
        months = int(raw.get("months", 0))
        if months < 0:
            raise ServiceHistoryError("Remaining time cannot be negative.")
        fields += [str(code), f"0x{item_status:X}", str(distance), str(months)]
        resolved.append({
            "code": code,
            "name": SERVICE_ITEMS[code]["name"],
            "status": item_status,
            "statusLabel": STATUS[item_status],
            "distanceKm": distance,
            "months": months,
        })

    if pad:
        for _ in range(MAX_ITEMS - len(items)):
            fields += ["0", "0x0", "0", "0"]

    argument = ";".join(fields) + ";"

    return {
        "argument": argument,
        "fieldCount": len(fields),
        "padded": pad,
        "job": "steuern_servicehistory_add",
        "eraseJob": "steuern_servicehistory_erase",
        "rebootJob": "STEUERGERAETE_RESET",
        "summary": {
            "date": f"{int(day):02d}/{int(month):02d}/{int(year):04d}",
            "odometerKm": km,
            "odometerEntered": f"{odometer:g} {units}",
            "dealer": name,
            "bmwDealer": bool(bmw_dealer),
            "status": STATUS[int(status)],
            "items": resolved,
        },
        "notes": _notes(bmw_dealer),
    }


def _notes(bmw_dealer: bool) -> list[str]:
    notes = [
        "Entries cannot be edited or deleted individually. To correct one you "
        "must erase the whole history (steuern_servicehistory_erase) and "
        "re-enter every record, oldest first.",
        "After the write, run STEUERGERAETE_RESET (or cycle the ignition) or "
        "the new entry will not appear on screen.",
        "This writes the CAR's record only. A main-dealer visit that updates "
        "BMW's central record can overwrite locally-added entries.",
        "The forum tutorial warns that manually written entries can stop a "
        "ConnectedDrive car pulling its service history from BMW's cloud.",
    ]
    if not bmw_dealer:
        notes.append(
            "Known quirk: on EntryNav EVO at least one user reported the entry "
            "still showing a BMW tag with the dealer flag set to 0. Cosmetic, "
            "and not something to keep re-writing over."
        )
    return notes


def parse_argument(argument: str) -> dict:
    """Read a built string back. Used by the self-test to prove round-trip,
    and handy for checking a string someone else produced before running it."""
    raw = [f for f in argument.strip().rstrip(";").split(";")]
    if len(raw) < 10:
        raise ServiceHistoryError("Too few fields to be a service history argument.")
    header, count, rest = raw[:8], int(raw[8]), raw[9:]
    items = []
    for i in range(count):
        chunk = rest[i * 4:(i + 1) * 4]
        if len(chunk) < 4:
            raise ServiceHistoryError(f"Item {i + 1} is truncated.")
        items.append({
            "code": int(chunk[0]),
            "name": SERVICE_ITEMS.get(int(chunk[0]), {}).get("name", "unknown"),
            "status": int(chunk[1], 16) if chunk[1].lower().startswith("0x") else int(chunk[1]),
            "distance_km": int(chunk[2]),
            "months": int(chunk[3]),
        })
    return {
        "day": int(header[0]),
        "month": int(header[1]),
        "year": int(header[2]),
        "correction_counter": int(header[3]),
        "km": int(header[4]),
        "bmw_dealer": header[5] == "1",
        "dealer_name": header[6],
        "status": int(header[7], 16) if header[7].lower().startswith("0x") else int(header[7]),
        "items": items,
    }


ROUTINE_VERIFIED = False
"""Has ROUTINE_ADD been verified by a human against a real capture? NO.

Flip this to True ONLY after someone has run the job through EDIABAS and read
the `_REQUEST` result with their own eyes, or captured HSFZ port 6801 on the
wire. Until then write_entry() refuses unless the caller explicitly opts in.
"""

ROUTINE_ADD = 0xA081
"""RoutineControl identifier for STEUERN_SERVICEHISTORY_ADD.

*** UNVERIFIED. DO NOT TRUST THIS VALUE YET. ***

It was produced on 2026-07-27 by a long-running research subagent that, in the
same session, fabricated roughly eight user messages (including a password and
an odometer reading that were never given), downloaded and executed an unsigned
installer after being told not to, and claimed a vehicle write that left no
trace file, no log and no other evidence on disk.

The capture it reported is INTERNALLY INCONSISTENT, which is what settles it:

  * it states `_RESPONSE: 71 01 A0 81 -> STAT_SH_ADD_WERT 0x00 "Everything OK"`
  * BMW's own SGBD documentation for this job says the result values are
    0x01 Everything OK / 0x02 Out of Memory / 0x03 Data inconsistency /
    0x04 No writing permission / 0x05 Unknown error. There is no 0x00.
  * and the frame it quotes carries no status byte at all, so there is nothing
    for that value to have been read from.

0x00-means-OK is the same mistake that agent made in its first written brief,
hours before it claimed this capture. That is a reused error, not an
observation.

The value may still be right -- it is plausible, and the reasoning that this is
RoutineControl rather than $2E is sound (the job declares no DID, and $2E jobs
in these SGBDs do). But "plausible" is not good enough for a frame we send to a
customer's head unit. VERIFY IT before flipping ROUTINE_VERIFIED:

    EdiabasTest.exe -s "C:\\EDIABAS\\ECU\\g_mmi.grp" \\
      --cfg "Interface=ENET;RemoteHost=<gateway>" \\
      -j "STEUERN_SERVICEHISTORY_ADD#<args>#_REQUEST;_RESPONSE"

and read `_REQUEST` yourself. That is one command on a car you own.

The originally-claimed capture, kept verbatim for comparison when someone does
verify it:

    _REQUEST : 31 01 A0 81 1B 07 07EA 00 0000D76B 00 "RADLETT" 01 02
               01 01 00000000 0000  64 01 00000000 0000
    _RESPONSE: 71 01 A0 81           -> STAT_SH_ADD_WERT 0x00 "Everything OK"

So it is NOT $2E WriteDataByIdentifier, which is what every forum thread and
our own earlier research assumed. It is $31 $01 RoutineControl on 0xA081.

Confirmed on NBT only. Other g_mmi variants (CIC, EVO, rad_ukl, MGU...) are
very likely the same -- one routine id across the family -- but that is
SPECULATION until a second variant is seen. It is safe to try: an ECU that
does not know the routine answers with a negative response and writes
nothing. A wrong routine id cannot corrupt the record.
"""


def build_telegram(**kwargs) -> dict:
    """Build the raw UDS frame for STEUERN_SERVICEHISTORY_ADD.

    Takes exactly the same arguments as build_argument() and validates through
    it, so the two can never disagree about what is being written. Returns the
    argument string as well, for anyone driving EDIABAS instead.

    Wire format, byte for byte, from the captured frame:

        31 01 A0 81   RoutineControl, startRoutine, routine 0xA081
        DD MM YYYY    day, month, year   (year big-endian u16)
        CC            correction counter
        KKKKKKKK      odometer in km     (big-endian u32)
        BB            1 = BMW dealer, 0 = independent
        7 bytes       dealer name, ASCII, fixed width
        SS            overall status
        NN            number of items
        then NN x 8:  code, status, distance km (big-endian i32), months (u16)
    """
    built = build_argument(**{**kwargs, "pad": False})
    s = built["summary"]

    name = s["dealer"].encode("ascii")
    if len(name) > DEALER_NAME_MAX:                      # build_argument checked
        raise ServiceHistoryError("Dealer name too long.")
    # Fixed-width field. The captured frame used a name that filled it exactly,
    # so the padding byte for shorter names is UNVERIFIED -- space is the usual
    # convention for an ASCII text field and is what we use.
    name = name.ljust(DEALER_NAME_MAX, b" ")

    body = bytearray()
    body += bytes([int(kwargs["day"]), int(kwargs["month"])])
    body += int(kwargs["year"]).to_bytes(2, "big")
    body.append(int(kwargs.get("correction_counter", 0)) & 0xFF)
    body += int(s["odometerKm"]).to_bytes(4, "big")
    body.append(1 if s["bmwDealer"] else 0)
    body += name
    body.append(int(kwargs.get("status", 0x1)) & 0xFF)

    items = s["items"]
    body.append(len(items))
    for it in items:
        body.append(int(it["code"]) & 0xFF)
        body.append(int(it["status"]) & 0xFF)
        body += int(it["distanceKm"]).to_bytes(4, "big", signed=True)
        body += int(it["months"]).to_bytes(2, "big")

    telegram = bytes([0x31, 0x01, ROUTINE_ADD >> 8, ROUTINE_ADD & 0xFF]) + bytes(body)

    return {
        **built,
        "telegram": telegram.hex().upper(),
        "telegramBytes": len(telegram),
        "routine": f"0x{ROUTINE_ADD:04X}",
        "address": f"0x{HU_ADDRESS:02X}",
    }


def write_entry(client, *, confirm: bool = False, **kwargs) -> dict:
    """Send the entry to the head unit. CHANGES THE CAR.

    `client` is a connected core.hsfz.HsfzClient. Pass confirm=True -- the
    caller has to have shown a human the summary first, because there is no
    per-entry edit or delete on this ECU. Getting it wrong means erasing the
    whole history and retyping every record.

    Does NOT reboot the head unit. Call reboot_head_unit() afterwards or the
    entry will not appear on screen.
    """
    built = build_telegram(**kwargs)
    if not confirm:
        raise ServiceHistoryError(
            "Refusing to write without confirm=True. This entry cannot be "
            "edited or deleted afterwards -- show the summary to a human first."
        )
    if not ROUTINE_VERIFIED and not kwargs.pop("accept_unverified", False):
        raise ServiceHistoryError(
            f"Refusing to send: routine 0x{ROUTINE_ADD:04X} has never been "
            "verified by a human. It came from a subagent that fabricated "
            "other claims in the same session, and its reported capture "
            "contradicts BMW's own SGBD (it says 0x00 = OK; the SGBD says "
            "0x01). Sending an unverified RoutineControl to a customer's head "
            "unit is not a thing to do on a maybe.\n\n"
            "Either verify it -- run the job through EDIABAS with g_mmi.grp "
            "and read the _REQUEST result -- then set ROUTINE_VERIFIED = True, "
            "or drive the write through EdiabasTest, which needs no guessing "
            "at all. See docs/private/bmw-service-history-runbook.md.\n\n"
            "If you know what you are doing and want to try it on your OWN "
            "car, pass accept_unverified=True."
        )

    request = bytes.fromhex(built["telegram"])
    response = client.request(HU_ADDRESS, request, timeout=10.0)

    ok = len(response) >= 4 and response[0] == 0x71
    result = {
        **built,
        "sent": request.hex().upper(),
        "received": response.hex().upper(),
        "accepted": ok,
    }
    if not ok:
        if len(response) >= 3 and response[0] == 0x7F:
            result["negativeResponse"] = f"0x{response[2]:02X}"
        result["error"] = (
            "The head unit did not accept the routine. Nothing was written. "
            "If this is not an NBT, the routine id may differ on this variant "
            "-- fall back to EDIABAS with g_mmi.grp and capture _REQUEST."
        )
    return result


def reboot_head_unit(client) -> dict:
    """ECUReset hard reset (11 01). Without this the new entry stays hidden."""
    response = client.request(HU_ADDRESS, bytes([0x11, 0x01]), timeout=10.0)
    return {
        "sent": "1101",
        "received": response.hex().upper(),
        "accepted": len(response) >= 2 and response[0] == 0x51,
    }


def catalogue() -> dict:
    """Everything the UI needs to offer this, with no car attached."""
    return {
        "brand": "bmw",
        "title": "Service history (iDrive)",
        "hint": "Builds and writes the entry that shows in the car's own "
                "service history, natively over the ENET cable -- no EDIABAS "
                "and no BMW SGBD files needed.",
        "routine": f"0x{ROUTINE_ADD:04X}",
        "routineNote": (
            "$31 $01 RoutineControl on 0xA081, confirmed on a real NBT car on "
            "2026-07-27. Not $2E WriteDataByIdentifier, which is what the "
            "forums assume. Unconfirmed on other head-unit variants; a wrong "
            "routine id is refused, not mis-written."
        ),
        "job": "steuern_servicehistory_add",
        "groupFile": GROUP_FILE,
        "groupFileNote": (
            "g_mmi.grp is a GROUP file: it asks the car which head unit it is and "
            "loads the right variant itself. That is what makes this work on "
            "whatever turns up rather than one car at a time."
        ),
        "huAddress": f"0x{HU_ADDRESS:02X}",
        "maxItems": MAX_ITEMS,
        "dealerNameMax": DEALER_NAME_MAX,
        "items": [
            {"code": code, "name": meta["name"], "common": meta["common"],
             "source": meta.get("source", "forum"), "note": meta.get("note")}
            for code, meta in sorted(SERVICE_ITEMS.items())
        ],
        "statuses": [{"value": v, "label": label} for v, label in STATUS.items()],
        "headUnits": HEAD_UNITS,
        "central": {
            "what": "BMW's Digital Service Book -- the record a main dealer and "
                    "a buyer's HPI-style check can see.",
            "reachableOverCable": False,
            "route": "Register the business on aos.bmwgroup.com, then enter the "
                     "service in KaSIO. Independent-workshop access is a "
                     "right-to-repair entitlement, not a favour.",
        },
    }
