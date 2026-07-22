"""Standard OBD-II mode-01 PIDs: definitions, decoders and support detection.

Each PID decoder receives the data bytes AFTER the '41 <pid>' prefix and
returns a numeric value. Formulas are the SAE J1979 standard ones (the same
maths Torque uses).
"""


class Pid:
    def __init__(self, pid, key, label, unit, nbytes, fn):
        self.pid = pid
        self.key = key
        self.label = label
        self.unit = unit
        self.nbytes = nbytes
        self.fn = fn

    def decode(self, data: bytes):
        if len(data) < self.nbytes:
            return None
        try:
            value = self.fn(data)
        except Exception:
            return None
        return round(value, 2) if isinstance(value, float) else value


PIDS = [
    Pid(0x04, "load", "Engine load", "%", 1, lambda d: d[0] * 100.0 / 255.0),
    Pid(0x05, "coolant", "Coolant temp", "°C", 1, lambda d: d[0] - 40),
    Pid(0x06, "stft1", "Short fuel trim B1", "%", 1, lambda d: (d[0] - 128) * 100.0 / 128.0),
    Pid(0x07, "ltft1", "Long fuel trim B1", "%", 1, lambda d: (d[0] - 128) * 100.0 / 128.0),
    Pid(0x0B, "map", "Intake manifold", "kPa", 1, lambda d: d[0]),
    Pid(0x0C, "rpm", "Engine RPM", "rpm", 2, lambda d: (d[0] * 256 + d[1]) / 4.0),
    Pid(0x0D, "speed", "Vehicle speed", "km/h", 1, lambda d: d[0]),
    Pid(0x0E, "timing", "Timing advance", "°", 1, lambda d: d[0] / 2.0 - 64),
    Pid(0x0F, "intake_temp", "Intake air temp", "°C", 1, lambda d: d[0] - 40),
    Pid(0x10, "maf", "MAF air flow", "g/s", 2, lambda d: (d[0] * 256 + d[1]) / 100.0),
    Pid(0x11, "throttle", "Throttle", "%", 1, lambda d: d[0] * 100.0 / 255.0),
    Pid(0x1F, "runtime", "Run time", "s", 2, lambda d: d[0] * 256 + d[1]),
    Pid(0x21, "mil_distance", "Distance with MIL", "km", 2, lambda d: d[0] * 256 + d[1]),
    Pid(0x2F, "fuel_level", "Fuel level", "%", 1, lambda d: d[0] * 100.0 / 255.0),
    Pid(0x33, "baro", "Barometric", "kPa", 1, lambda d: d[0]),
    Pid(0x42, "module_voltage", "Module voltage", "V", 2, lambda d: (d[0] * 256 + d[1]) / 1000.0),
    Pid(0x46, "ambient", "Ambient temp", "°C", 1, lambda d: d[0] - 40),
    Pid(0x5C, "oil_temp", "Oil temp", "°C", 1, lambda d: d[0] - 40),
    Pid(0x5E, "fuel_rate", "Fuel rate", "L/h", 2, lambda d: (d[0] * 256 + d[1]) / 20.0),
]

PIDS_BY_KEY = {p.key: p for p in PIDS}
PIDS_BY_PID = {p.pid: p for p in PIDS}

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
    """Query the 01 00/20/40 support bitmaps. Honest: returns only what the
    car actually reports."""
    supported = set()
    base = 0x00
    while True:
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
        if nxt > 0x40 or nxt not in merged:
            break
        base = nxt
    return supported
