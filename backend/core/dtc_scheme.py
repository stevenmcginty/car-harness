"""Whose fault-code numbering is this? -- suppressing confidently wrong labels.

``uds.decode_dtc`` turns a 3-byte UDS DTC into a J2012-style string (P0202,
B0030, U1120) by reading the top two bits for the letter and the rest as digits.
That conversion is structurally correct for every manufacturer. **The published
meaning attached to those strings is not.**

SAE J2012 only defines the meaning of *generic* powertrain codes. Manufacturers
are free to use their own numbering inside the same space, and the VAG group --
which includes Porsche -- does exactly that. The consequence, seen on a real
MY2022 Taycan (2026-07-27), is a scan that reports:

    P0202  "Injector circuit, cylinder 2"     on a battery-electric car
    P0402  "EGR flow excessive"               with no EGR valve
    P0450  "Evaporative emission pressure"    with no evap canister
    P0140  "O2 sensor, no activity"           with no O2 sensor

sitting in the gateway and the driver-assistance module, which would not own
such faults on any car. The link was real and the fault numbers were read
correctly; only the *names* were fiction.

For a tool a dealer shows a customer, a confidently wrong label is worse than no
label. So on manufacturer-numbered brands this module replaces the J2012 string
with an honest one and keeps everything that IS reliable:

  * the raw hex fault number -- which is what the factory tester displays;
  * which module reported it;
  * the status bits (active now / historic / failed this drive);
  * the UDS failure-type byte.

The structural conversion is preserved under ``genericCode`` rather than thrown
away, so a future per-brand fault database can use it as a lookup key -- but
nothing renders it as though its generic meaning applied.
"""

from typing import Optional

# Brands whose fault numbering is their own, not SAE generic. VAG group (which
# Porsche belongs to) is the confirmed case; the others are listed only where a
# manufacturer-specific scheme is already known to apply to their UDS DTCs.
MANUFACTURER_NUMBERED = {"porsche_enet", "porsche", "vw"}

LABEL = "manufacturer code"

NOTE = (
    "Manufacturer-specific fault number. This brand does not use the generic "
    "SAE code meanings, so no plain-English description is shown rather than a "
    "wrong one. The hex number is what the factory tester displays."
)


def is_manufacturer_numbered(brand: str) -> bool:
    return brand in MANUFACTURER_NUMBERED


def mark(entry: dict, brand: Optional[str] = None) -> dict:
    """Rewrite one decoded DTC so nothing claims a generic meaning it lacks.

    Mutates and returns ``entry`` -- the dict ``uds.decode_dtc`` produced.
    Safe to call twice; the second call is a no-op.
    """
    if not isinstance(entry, dict) or entry.get("codeScheme") == "manufacturer":
        return entry
    generic = entry.get("code")
    if generic:
        entry["genericCode"] = generic
    entry["code"] = LABEL
    entry["codeScheme"] = "manufacturer"
    entry["codeNote"] = NOTE
    if brand:
        entry["codeBrand"] = brand
    return entry


def mark_all(entries, brand: Optional[str] = None) -> list:
    """``mark`` over a list of decoded DTCs."""
    return [mark(entry, brand) for entry in (entries or [])]
