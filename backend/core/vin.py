"""Who made this car? -- the World Manufacturer Identifier, decoded honestly.

The harness supports nine brand profiles, and until now nothing on the backend
could look at a VIN and say which one to use. That produced a genuinely
misleading result in the field: the app opens on BMW, the BMW probe broadcasts
ENET discovery, a *Porsche* Taycan answers it (both speak DoIP on the same
pins), and the connection card cheerfully reports a Porsche VIN as a BMW
"G-series". The link was real; the badge was wrong.

This module is the missing piece. Give it a VIN and it names the make, and
suggests which profile should be driving -- taking the transport into account,
because a Porsche reached over the ENET cable wants ``porsche_enet`` (DoIP)
while the same car on a dongle wants ``porsche``.

Deliberately limited scope: **make only, never model or year.** Position 10 is
a model-year code only under US/Canadian law; European VINs put a serial digit
there, and guessing a year from one is how a dealer's screen ends up confidently
wrong (that lesson is already baked into the Android VinDecode). An unknown WMI
returns None -- "I don't know" is a valid and useful answer here.
"""

from typing import List, Optional

# WMI (first three VIN characters) -> make label. Sources are the standard
# published WMI allocations; the same set the Android app's VinDecode uses.
WMI_MAKES = {
    # BMW group
    "WBA": "BMW", "WBS": "BMW", "WBX": "BMW", "WBY": "BMW", "WB1": "BMW",
    "4US": "BMW", "5UX": "BMW", "5YM": "BMW", "3MW": "BMW",
    "WMW": "MINI", "WMZ": "MINI",
    # Porsche
    "WP0": "Porsche", "WP1": "Porsche", "WP2": "Porsche",
    # VW group
    "WVW": "Volkswagen", "WV1": "Volkswagen", "WV2": "Volkswagen",
    "WVG": "Volkswagen", "3VW": "Volkswagen", "1VW": "Volkswagen",
    "9BW": "Volkswagen", "AAV": "Volkswagen",
    "WAU": "Audi", "WA1": "Audi", "WUA": "Audi", "TRU": "Audi",
    "TMB": "Skoda", "VSS": "SEAT",
    # Mercedes-Benz
    "WDB": "Mercedes-Benz", "WDC": "Mercedes-Benz", "WDD": "Mercedes-Benz",
    "WDF": "Mercedes-Benz", "W1K": "Mercedes-Benz", "W1N": "Mercedes-Benz",
    "W1V": "Mercedes-Benz", "VSA": "Mercedes-Benz", "4JG": "Mercedes-Benz",
    # PSA / Stellantis French
    "VF3": "Peugeot", "VF7": "Citroen", "VR3": "Peugeot", "VR7": "Citroen",
    "VR1": "DS",
    # FCA Italian
    "ZFA": "Fiat", "ZFC": "Fiat", "ZAR": "Alfa Romeo", "ZLA": "Lancia",
    # Jaguar Land Rover
    "SAJ": "Jaguar", "SAL": "Land Rover",
    # Mazda
    "JMZ": "Mazda", "JM1": "Mazda", "JM7": "Mazda", "JM0": "Mazda",
    "4F2": "Mazda", "4F4": "Mazda",
    # Present so they are named rather than guessed at -- no profile supports them.
    "5YJ": "Tesla", "7SA": "Tesla", "LRW": "Tesla", "XP7": "Tesla",
}

# Make -> the brand profiles that can drive it, best first. Where a make has both
# a native-cable and a dongle profile, the transport decides -- see suggest().
MAKE_PROFILES = {
    "BMW": ["bmw"],
    "MINI": ["bmw"],
    "Porsche": ["porsche_enet", "porsche"],
    "Volkswagen": ["vw"], "Audi": ["vw"], "Skoda": ["vw"], "SEAT": ["vw"],
    "Mercedes-Benz": ["mercedes"],
    "Peugeot": ["peugeot"], "Citroen": ["peugeot"], "DS": ["peugeot"],
    "Fiat": ["fiat"], "Alfa Romeo": ["fiat"], "Lancia": ["fiat"],
    "Jaguar": ["jlr"], "Land Rover": ["jlr"],
    "Mazda": ["mazda"],
}

# Profiles that only work over a native Ethernet cable (DoIP/HSFZ).
_CABLE_PROFILES = {"porsche_enet"}


def normalise(vin: Optional[str]) -> Optional[str]:
    """Trim and upper-case a VIN, or None if it is not plausibly one."""
    if not vin:
        return None
    cleaned = "".join(ch for ch in str(vin).strip().upper() if ch.isalnum())
    return cleaned if len(cleaned) >= 3 else None


def make_from_vin(vin: Optional[str]) -> Optional[str]:
    """The manufacturer, or None when the WMI is not one we know."""
    cleaned = normalise(vin)
    return WMI_MAKES.get(cleaned[:3]) if cleaned else None


def profiles_for_vin(vin: Optional[str]) -> List[str]:
    """Every profile that could drive this car, best first. [] if unknown."""
    make = make_from_vin(vin)
    return list(MAKE_PROFILES.get(make, [])) if make else []


def suggest(
    vin: Optional[str],
    protocol: Optional[str] = None,
    available: Optional[List[str]] = None,
) -> Optional[str]:
    """Which profile should be driving this car?

    ``protocol`` is what answered discovery ("DoIP", "HSFZ", or None for a
    dongle). A cable-only profile is only ever suggested when the car actually
    answered over the cable, so a Porsche on a dongle is not sent down the
    ENET path and told to plug in something it already has.

    ``available`` restricts the answer to profiles the installation really has.
    """
    candidates = profiles_for_vin(vin)
    if not candidates:
        return None
    on_cable = (protocol or "").upper() in ("DOIP", "HSFZ")
    for profile in candidates:
        if profile in _CABLE_PROFILES and not on_cable:
            continue
        if available is not None and profile not in available:
            continue
        return profile
    return None
