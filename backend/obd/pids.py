"""Standard OBD-II mode-01 PIDs: definitions, decoders and support detection.

Each PID decoder receives the data bytes AFTER the '41 <pid>' prefix and
returns a numeric value, a short string (for the enumerated PIDs), or None
when this car's answer doesn't make sense. Formulas are the SAE J1979
standard ones (the same maths Torque uses).

Every value here works on ANY car that answers OBD-II, whatever the badge --
that is the whole point of this table. Brand-specific extras (mode 22) live in
backend/profiles/<brand>/pids.py and stack on top of it.

Several PIDs carry more than one reading (an oxygen sensor row reports both a
voltage and a fuel trim; an exhaust-temperature row reports four sensors). Those
are simply several entries sharing one `pid` number, each with its own unique
`key` -- so PIDS_BY_KEY stays a flat lookup and PIDS_FOR_PID gives everything a
single query can decode.

WHAT IS DELIBERATELY ABSENT: PIDs 0x66 (MAF sensor detail), 0x6B (EGR
temperature), 0x73 (exhaust pressure), 0x74 (turbo RPM) and 0x7A/0x7B/0x7C (DPF
pressure and temperature) are composite rows whose byte layout is not settled
across the public sources. Guessing a DPF soot number for a diesel a dealer is
about to buy is worse than showing nothing, so they are gaps, not entries.
Several brands expose the same data through mode 22, where it IS documented.
"""


# Display groups. The front end renders one section per group, in this order.
ENGINE = "Engine"
FUEL_AIR = "Fuel & air"
EMISSIONS = "Emissions"
TEMPS = "Temperatures"
TURBO = "Turbo & exhaust"
ELECTRICAL = "Electrical"
TRIP = "Trip & diagnostics"

GROUP_ORDER = [ENGINE, FUEL_AIR, TURBO, TEMPS, EMISSIONS, ELECTRICAL, TRIP]


class Pid:
    """One reading decoded from one mode-01 PID.

    lo/hi are sanity bounds: a value outside them is treated as "this car does
    not really support that PID" rather than displayed. Clone dongles and
    part-implemented ECUs answer plenty of PIDs with rubbish, and a wrong
    number on a dealer's screen is worse than a missing one.
    """

    def __init__(self, pid, key, label, unit, nbytes, fn,
                 group=ENGINE, lo=None, hi=None):
        self.pid = pid
        self.key = key
        self.label = label
        self.unit = unit
        self.nbytes = nbytes
        self.fn = fn
        self.group = group
        self.lo = lo
        self.hi = hi

    def decode(self, data: bytes):
        if len(data) < self.nbytes:
            return None
        try:
            value = self.fn(data)
        except Exception:
            return None
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if self.lo is not None and value < self.lo:
            return None
        if self.hi is not None and value > self.hi:
            return None
        return round(value, 2) if isinstance(value, float) else value


def _u16(d, i=0):
    return d[i] * 256 + d[i + 1]


def _pct255(d, i=0):
    return d[i] * 100.0 / 255.0


def _trim(d, i=0):
    return (d[i] - 128) * 100.0 / 128.0


def _enum(table, default="Unknown"):
    """Enumerated PIDs answer with a code, not a measurement."""
    def decode(d):
        return table.get(d[0], f"{default} (0x{d[0]:02X})")
    return decode


FUEL_SYSTEM_STATUS = {
    0x00: "Not present",
    0x01: "Open loop — engine not warm",
    0x02: "Closed loop — using O2 sensors",
    0x04: "Open loop — load or deceleration",
    0x08: "Open loop — system fault",
    0x10: "Closed loop — one O2 sensor faulty",
}

FUEL_TYPE = {
    0x00: "Not available", 0x01: "Petrol", 0x02: "Methanol", 0x03: "Ethanol",
    0x04: "Diesel", 0x05: "LPG", 0x06: "CNG", 0x07: "Propane", 0x08: "Electric",
    0x09: "Bifuel petrol", 0x0A: "Bifuel methanol", 0x0B: "Bifuel ethanol",
    0x0C: "Bifuel LPG", 0x0D: "Bifuel CNG", 0x0E: "Bifuel propane",
    0x0F: "Bifuel electric", 0x11: "Hybrid petrol", 0x12: "Hybrid ethanol",
    0x13: "Hybrid diesel", 0x14: "Hybrid electric", 0x18: "Hybrid regenerative",
}

OBD_STANDARD = {
    0x01: "OBD-II (California ARB)", 0x02: "OBD (US EPA)", 0x03: "OBD and OBD-II",
    0x04: "OBD-I", 0x05: "Not OBD compliant", 0x06: "EOBD (Europe)",
    0x07: "EOBD and OBD-II", 0x08: "EOBD and OBD", 0x09: "EOBD, OBD and OBD-II",
    0x0A: "JOBD (Japan)", 0x0B: "JOBD and OBD-II", 0x0C: "JOBD and EOBD",
    0x0D: "JOBD, EOBD and OBD-II", 0x11: "EMD", 0x12: "EMD+", 0x13: "HD OBD-C",
    0x14: "HD OBD", 0x15: "WWH OBD", 0x17: "HD EOBD-I", 0x21: "HD EOBD-II",
}

SECONDARY_AIR = {
    0x01: "Upstream", 0x02: "Downstream of catalyst",
    0x04: "Outside atmosphere / off", 0x08: "Diagnostics in progress",
}


PIDS = [
    # ---- Engine ------------------------------------------------------------
    Pid(0x04, "load", "Engine load", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x0C, "rpm", "Engine RPM", "rpm", 2, lambda d: _u16(d) / 4.0, ENGINE, 0, 16000),
    Pid(0x0D, "speed", "Vehicle speed", "km/h", 1, lambda d: d[0], ENGINE, 0, 255),
    Pid(0x0E, "timing", "Timing advance", "°", 1, lambda d: d[0] / 2.0 - 64, ENGINE, -64, 64),
    Pid(0x11, "throttle", "Throttle", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x1F, "runtime", "Run time", "s", 2, _u16, ENGINE, 0, 65535),
    Pid(0x43, "abs_load", "Absolute load", "%", 2, lambda d: _u16(d) * 100.0 / 255.0, ENGINE, 0, 25700),
    Pid(0x45, "throttle_rel", "Relative throttle", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x47, "throttle_b", "Throttle position B", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x48, "throttle_c", "Throttle position C", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x49, "pedal_d", "Accelerator pedal D", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x4A, "pedal_e", "Accelerator pedal E", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x4B, "pedal_f", "Accelerator pedal F", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x5A, "pedal_rel", "Relative pedal position", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x4C, "throttle_cmd", "Commanded throttle actuator", "%", 1, _pct255, ENGINE, 0, 100),
    Pid(0x61, "torque_demand", "Driver's demanded torque", "%", 1, lambda d: d[0] - 125, ENGINE, -125, 130),
    Pid(0x62, "torque_actual", "Actual engine torque", "%", 1, lambda d: d[0] - 125, ENGINE, -125, 130),
    Pid(0x63, "torque_ref", "Engine reference torque", "Nm", 2, _u16, ENGINE, 0, 65535),
    Pid(0x1E, "pto_active", "Power take-off", "", 1,
        lambda d: "Active" if d[0] & 0x01 else "Not active", ENGINE),

    # ---- Fuel & air --------------------------------------------------------
    Pid(0x03, "fuel_system", "Fuel system status", "", 1,
        _enum(FUEL_SYSTEM_STATUS, "Manufacturer-specific"), FUEL_AIR),
    Pid(0x06, "stft1", "Short fuel trim B1", "%", 1, _trim, FUEL_AIR, -100, 100),
    Pid(0x07, "ltft1", "Long fuel trim B1", "%", 1, _trim, FUEL_AIR, -100, 100),
    Pid(0x08, "stft2", "Short fuel trim B2", "%", 1, _trim, FUEL_AIR, -100, 100),
    Pid(0x09, "ltft2", "Long fuel trim B2", "%", 1, _trim, FUEL_AIR, -100, 100),
    Pid(0x0A, "fuel_pressure", "Fuel pressure (gauge)", "kPa", 1, lambda d: d[0] * 3.0, FUEL_AIR, 0, 765),
    Pid(0x0B, "map", "Intake manifold", "kPa", 1, lambda d: d[0], FUEL_AIR, 0, 255),
    Pid(0x10, "maf", "MAF air flow", "g/s", 2, lambda d: _u16(d) / 100.0, FUEL_AIR, 0, 655),
    Pid(0x50, "maf_max", "Maximum MAF", "g/s", 1, lambda d: d[0] * 10.0, FUEL_AIR, 0, 2550),
    Pid(0x22, "rail_pressure_rel", "Fuel rail pressure (vs manifold)", "kPa", 2,
        lambda d: _u16(d) * 0.079, FUEL_AIR, 0, 5178),
    Pid(0x23, "rail_pressure", "Fuel rail gauge pressure", "kPa", 2,
        lambda d: _u16(d) * 10.0, FUEL_AIR, 0, 655350),
    Pid(0x59, "rail_pressure_abs", "Fuel rail absolute pressure", "kPa", 2,
        lambda d: _u16(d) * 10.0, FUEL_AIR, 0, 655350),
    Pid(0x2F, "fuel_level", "Fuel level", "%", 1, _pct255, FUEL_AIR, 0, 100),
    Pid(0x5E, "fuel_rate", "Fuel rate", "L/h", 2, lambda d: _u16(d) / 20.0, FUEL_AIR, 0, 3276),
    Pid(0x51, "fuel_type", "Fuel type", "", 1, _enum(FUEL_TYPE), FUEL_AIR),
    Pid(0x52, "ethanol", "Ethanol content", "%", 1, _pct255, FUEL_AIR, 0, 100),
    Pid(0x44, "lambda_cmd", "Commanded lambda", "", 2,
        lambda d: _u16(d) * 2.0 / 65536, FUEL_AIR, 0, 2),
    Pid(0x5D, "injection_timing", "Fuel injection timing", "°", 2,
        lambda d: (_u16(d) - 26880) / 128.0, FUEL_AIR, -210, 302),

    # ---- Turbo & exhaust ---------------------------------------------------
    # 0x78/0x79: byte A is a bitmap of which of the four sensors are present,
    # then four 16-bit temperatures. Sensors the car doesn't have read as 0xFFFF
    # and fall outside the bounds, so they drop out on their own.
    *[Pid(0x78, f"egt_b1_s{n}", f"Exhaust gas temp B1 S{n}", "°C", 1 + 2 * n,
          (lambda n: lambda d: _u16(d, 1 + 2 * (n - 1)) / 10.0 - 40)(n),
          TURBO, -40, 6513) for n in (1, 2, 3, 4)],
    *[Pid(0x79, f"egt_b2_s{n}", f"Exhaust gas temp B2 S{n}", "°C", 1 + 2 * n,
          (lambda n: lambda d: _u16(d, 1 + 2 * (n - 1)) / 10.0 - 40)(n),
          TURBO, -40, 6513) for n in (1, 2, 3, 4)],

    # ---- Temperatures ------------------------------------------------------
    Pid(0x05, "coolant", "Coolant temp", "°C", 1, lambda d: d[0] - 40, TEMPS, -40, 215),
    Pid(0x0F, "intake_temp", "Intake air temp", "°C", 1, lambda d: d[0] - 40, TEMPS, -40, 215),
    Pid(0x46, "ambient", "Ambient temp", "°C", 1, lambda d: d[0] - 40, TEMPS, -40, 215),
    Pid(0x5C, "oil_temp", "Oil temp", "°C", 1, lambda d: d[0] - 40, TEMPS, -40, 215),
    Pid(0x3C, "cat_temp_b1s1", "Catalyst temp B1 S1", "°C", 2,
        lambda d: _u16(d) / 10.0 - 40, TEMPS, -40, 6513),
    Pid(0x3D, "cat_temp_b2s1", "Catalyst temp B2 S1", "°C", 2,
        lambda d: _u16(d) / 10.0 - 40, TEMPS, -40, 6513),
    Pid(0x3E, "cat_temp_b1s2", "Catalyst temp B1 S2", "°C", 2,
        lambda d: _u16(d) / 10.0 - 40, TEMPS, -40, 6513),
    Pid(0x3F, "cat_temp_b2s2", "Catalyst temp B2 S2", "°C", 2,
        lambda d: _u16(d) / 10.0 - 40, TEMPS, -40, 6513),

    # ---- Emissions ---------------------------------------------------------
    Pid(0x2C, "egr_cmd", "Commanded EGR", "%", 1, _pct255, EMISSIONS, 0, 100),
    Pid(0x2D, "egr_error", "EGR error", "%", 1, _trim, EMISSIONS, -100, 100),
    Pid(0x2E, "evap_purge", "Commanded evap purge", "%", 1, _pct255, EMISSIONS, 0, 100),
    Pid(0x32, "evap_pressure", "Evap vapour pressure", "Pa", 2,
        lambda d: (_u16(d) - 65536 if _u16(d) > 32767 else _u16(d)) / 4.0,
        EMISSIONS, -8192, 8192),
    Pid(0x53, "evap_pressure_abs", "Evap vapour pressure (absolute)", "kPa", 2,
        lambda d: _u16(d) / 200.0, EMISSIONS, 0, 328),
    Pid(0x54, "evap_pressure_alt", "Evap vapour pressure (alternate)", "Pa", 2,
        lambda d: _u16(d) - 32767, EMISSIONS, -32767, 32768),
    Pid(0x12, "secondary_air", "Secondary air system", "", 1,
        _enum(SECONDARY_AIR), EMISSIONS),
    Pid(0x55, "o2_trim_st_b1", "Secondary O2 short trim B1/B3", "%", 1, _trim, EMISSIONS, -100, 100),
    Pid(0x56, "o2_trim_lt_b1", "Secondary O2 long trim B1/B3", "%", 1, _trim, EMISSIONS, -100, 100),
    Pid(0x57, "o2_trim_st_b2", "Secondary O2 short trim B2/B4", "%", 1, _trim, EMISSIONS, -100, 100),
    Pid(0x58, "o2_trim_lt_b2", "Secondary O2 long trim B2/B4", "%", 1, _trim, EMISSIONS, -100, 100),
    Pid(0x5F, "emission_req", "Emission requirements", "", 1,
        _enum({0x0E: "Euro heavy-duty", 0x0F: "Euro light-duty"}, "Design requirement"),
        EMISSIONS),

    # ---- Electrical --------------------------------------------------------
    Pid(0x42, "module_voltage", "Module voltage", "V", 2,
        lambda d: _u16(d) / 1000.0, ELECTRICAL, 0, 65.5),
    Pid(0x5B, "hybrid_battery", "Hybrid battery remaining", "%", 1, _pct255, ELECTRICAL, 0, 100),

    # ---- Trip & diagnostics ------------------------------------------------
    Pid(0x33, "baro", "Barometric", "kPa", 1, lambda d: d[0], TRIP, 0, 255),
    Pid(0x21, "mil_distance", "Distance with MIL", "km", 2, _u16, TRIP, 0, 65535),
    Pid(0x4D, "mil_minutes", "Time run with MIL on", "min", 2, _u16, TRIP, 0, 65535),
    # "How recently was this car's fault memory wiped?" -- the four PIDs that
    # answer it. A clean fault list means nothing if the codes were cleared four
    # kilometres ago, so these are worth more to a buyer than most live data.
    Pid(0x30, "warmups_since_clear", "Warm-ups since codes cleared", "count", 1,
        lambda d: d[0], TRIP, 0, 255),
    Pid(0x31, "distance_since_clear", "Distance since codes cleared", "km", 2, _u16, TRIP, 0, 65535),
    # 0x4E: the name and 2-byte width are straight from the J1979 PID table; the
    # unit is the direct twin of 0x4D and is universally minutes, but it is not
    # spelled out in the row, so cross-check it against 0x31 on a real car.
    Pid(0x4E, "minutes_since_clear", "Time since codes cleared", "min", 2, _u16, TRIP, 0, 65535),
    Pid(0x1C, "obd_standard", "OBD standard", "", 1, _enum(OBD_STANDARD), TRIP),
]


def _add_oxygen_sensors():
    """PIDs 0x14-0x1B, 0x24-0x2B and 0x34-0x3B are eight sensors each, laid out
    bank 1 sensors 1-4 then bank 2 sensors 1-4 (the convention PID 0x13 reports
    against). Generated rather than hand-typed: 24 PIDs x 2 readings is 48 rows
    where one wrong copy-paste would be invisible.

    A narrowband car answers the 0x14 block; a wideband car answers 0x24 and/or
    0x34. Whichever it is, unsupported rows are never even queried.
    """
    rows = []
    for i in range(8):
        bank, sensor = (i // 4) + 1, (i % 4) + 1
        tag = f"b{bank}s{sensor}"
        name = f"B{bank} S{sensor}"

        # Narrowband: voltage + the short trim used with it (0xFF = trim unused).
        rows.append(Pid(0x14 + i, f"o2_{tag}_v", f"O2 {name} voltage", "V", 2,
                        lambda d: d[0] / 200.0, EMISSIONS, 0, 1.275))
        rows.append(Pid(0x14 + i, f"o2_{tag}_trim", f"O2 {name} fuel trim", "%", 2,
                        lambda d: None if d[1] == 0xFF else _trim(d, 1),
                        EMISSIONS, -100, 100))

        # Wideband, voltage flavour.
        rows.append(Pid(0x24 + i, f"o2_{tag}_lambda", f"O2 {name} lambda", "", 4,
                        lambda d: _u16(d) * 2.0 / 65536, EMISSIONS, 0, 2))
        rows.append(Pid(0x24 + i, f"o2_{tag}_wb_v", f"O2 {name} voltage (wideband)", "V", 4,
                        lambda d: _u16(d, 2) * 8.0 / 65536, EMISSIONS, 0, 8))

        # Wideband, current flavour.
        rows.append(Pid(0x34 + i, f"o2_{tag}_lambda_i", f"O2 {name} lambda (current)", "", 4,
                        lambda d: _u16(d) * 2.0 / 65536, EMISSIONS, 0, 2))
        rows.append(Pid(0x34 + i, f"o2_{tag}_current", f"O2 {name} current", "mA", 4,
                        lambda d: _u16(d, 2) / 256.0 - 128, EMISSIONS, -128, 128))
    return rows


PIDS.extend(_add_oxygen_sensors())

PIDS_BY_KEY = {p.key: p for p in PIDS}

# First row wins, so existing callers that ask "what is PID 0x05?" keep the
# answer they always got. PIDS_FOR_PID is the one to use when decoding a
# response, because it returns every reading that response carries.
PIDS_BY_PID = {}
PIDS_FOR_PID = {}
for _p in PIDS:
    PIDS_BY_PID.setdefault(_p.pid, _p)
    PIDS_FOR_PID.setdefault(_p.pid, []).append(_p)
del _p

# The default Torque-style dashboard, in display order.
DASHBOARD_KEYS = [
    "rpm", "speed", "coolant", "load", "throttle", "intake_temp",
    "maf", "fuel_level", "oil_temp", "module_voltage",
]


def parse_support_bitmap(base: int, payload: bytes) -> set:
    """Payload is the response to 01 <base> WITHOUT the '41 <base>' prefix:
    4 bytes whose bits mark PIDs base+1 .. base+32 as supported."""
    supported = set()
    for i, byte in enumerate(payload[:4]):
        for bit in range(8):
            if byte & (0x80 >> bit):
                supported.add(base + i * 8 + bit + 1)
    return supported


def read_supported_pids(session) -> set:
    """Query the 01 00/20/40/60/80/A0/C0 support bitmaps. Honest: returns only
    what the car actually reports. Each bitmap's top bit says whether the next
    block exists, so we stop the moment the car says there is no more."""
    supported = set()
    base = 0x00
    while base <= 0xC0:
        try:
            payloads = session.query(0x01, base, timeout=6.0)
        except Exception:
            break
        merged = set()
        for payload in payloads:  # one per ECU; union them
            if len(payload) >= 6 and payload[1] == base:
                merged |= parse_support_bitmap(base, payload[2:6])
        supported |= merged
        nxt = base + 0x20
        if nxt not in merged:
            break
        base = nxt
    return supported
