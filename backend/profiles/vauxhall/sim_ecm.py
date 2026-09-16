"""A simulated Astra J VXR engine controller (E39A), for running the engine
analyse with no car attached.

WHY THIS IS A WHOLE FAKE MODULE AND NOT A CANNED RESULT
-------------------------------------------------------
gearbox.py's simulate mode is a fixed dictionary, and that is the right shape
there: its numbers were read off a real 6T35, so a canned result is a replay of
something that happened.

Nothing in engine.py has ever happened. Every name and scaling in it comes out
of GM's dealer database and no A20NFT has answered one of them. A canned engine
result would therefore be fiction presented as a demonstration, and worse, it
would demonstrate nothing: the paths that will decide whether the real run is
worth anything are the DECODE and the REFUSAL, and a dictionary exercises
neither.

So this is a module that answers frames, exactly as sim_tcm.py is, and
engine.py's own reader cannot tell it from an ELM327 UDS client -- it exposes
the same ``request(dst, data, timeout)`` interface. What that proves is real:
that the pack's scaling round-trips, that NRC 31 is counted as "this engine
does not carry that parameter" rather than crashing the read, that the
desired-versus-actual findings fire on divergence and stay quiet without it,
and that the applicability split comes out of a run with both kinds of answer
in it.

It is a model of the dealer data, not of an engine. Every value below is
encoded by INVERTING the pack's own scaling for that parameter, so the sim
never states a raw byte the data would not produce -- but the engineering
numbers themselves are invented. A plausible warm idle, a plausible misfire
split, a plausible boost-control failure. They prove the harness handles those
shapes. They prove nothing about what an A20NFT does.

THE REFUSAL SET
---------------
``REFUSED`` is the part of this fake that is doing real work. The pack's PID
set is GM's shared "E39 4cyl" list, which carries bank-2, cylinder-5/6,
secondary-air and cylinder-deactivation parameters that a four-cylinder
transverse A20NFT cannot have, plus a long tail of sensors this car was never
built with. Every request not named in ``VALUES`` comes back NRC 31, and a
handful of CORE parameters are refused deliberately as well, so the core read
itself has to survive the negative path rather than only the sweep.

Which specific parameters a real A20NFT refuses is exactly the thing nobody
knows. The refusals here are a guess at the SHAPE of that answer, not a claim
about its content.
"""

from typing import Dict, List, Optional, Tuple

from backend.obd.elm327 import ObdError

from . import dealer_data

ECM = 0x7E0

SVC_READ_BY_PID = 0x22
SVC_READ_DATA_BY_ID = 0x1A
SVC_TESTER_PRESENT = 0x3E
SVC_READ_DIAG_INFO = 0xA9
SVC_CLEAR_DIAG_INFO = 0x04
SVC_DEVICE_CONTROL = 0xAE

SCENARIOS: Dict[str, str] = {
    "healthy": "A warm A20NFT idling with nothing wrong: boost and rail "
               "pressure track their commands, cam phasers sit where they are "
               "asked, every misfire counter reads zero.",
    "misfire-cyl3": "Cylinder 3 carries almost every counted misfire event "
                    "while the other three stay near zero -- the split that "
                    "points at one coil, plug or injector rather than at "
                    "anything shared.",
    "boost-fault": "The ECM asks for full boost and the engine delivers about "
                   "half of it while the wastegate command sits on 0xFF, the "
                   "end of what that byte can express. Boost control has run "
                   "out of authority.",
}

# Fault memory per scenario, as GMLAN A9 81 records:
# (dtc high byte, dtc low byte, failure type, status).
DTCS: Dict[str, List[Tuple[int, int, int, int]]] = {
    "healthy": [],
    # P0303 Cylinder 3 Misfire Detected, P0300 Engine Misfire Detected.
    "misfire-cyl3": [(0x03, 0x03, 0x00, 0x08), (0x03, 0x00, 0x00, 0x08)],
    # P0299 Turbocharger/Supercharger Underboost Condition.
    "boost-fault": [(0x02, 0x99, 0x00, 0x08)],
}

# GMLAN $1A identification. Values are invented; the ENCODING is not -- part
# numbers are the 4-byte big-endian binary GM really uses, which is what
# software.py decodes, and the VIN is plain ASCII.
VIN = "W0LGP6EG0G1045512"
IDENT: Dict[int, bytes] = {
    0x90: VIN.encode("ascii"),
    0x92: b"E39A-S3",
    0x97: b"A20NFT 2.0L",
    0xB5: b"5512",
    0xC0: (12645887).to_bytes(4, "big"),      # boot software part number
    0xCB: (12658804).to_bytes(4, "big"),      # end model part number
    0xCC: (12658790).to_bytes(4, "big"),      # base model part number
    0xC1: (12658811).to_bytes(4, "big"),      # software module 1
    0xC2: (12658812).to_bytes(4, "big"),
    0xC3: (12658813).to_bytes(4, "big"),
}

# The engineering values each scenario presents, keyed by the pack's own
# (request, name). Anything not listed is refused with NRC 31.
_BASE: Dict[Tuple[str, str], float] = {
    ("22 00 0C", "Engine Speed"): 812.0,
    ("22 00 04", "Engine Load"): 21.0,
    ("22 00 0D", "Vehicle Speed"): 0.0,
    ("22 00 05", "ECT Sensor"): 89.0,
    ("22 00 0F", "IAT Sensor"): 31.0,
    ("22 11 54", "Calculated Engine Oil Temperature"): 84.0,
    ("22 00 10", "MAF Sensor"): 3.2,
    ("22 12 B1", "Throttle Position"): 4.1,
    ("22 00 4C", "Desired Throttle Position"): 4.0,
    ("22 13 1F", "Accelerator Pedal Position"): 0.0,
    ("22 00 06", "Short Term Fuel Trim"): 1.6,
    ("22 00 07", "Long Term Fuel Trim"): 3.1,
    ("22 00 44", "Air/Fuel Equivalence Ratio Command"): 1.0,
    ("22 23 20", "HO2S 1"): 0.45,
    ("22 00 23", "Fuel Rail Pressure Sensor"): 5.1,
    ("22 30 8A", "Desired Fuel Rail Pressure"): 5.0,
    ("22 32 98", "Boost Pressure Sensor"): 101.0,
    ("22 32 8A", "Desired Boost Pressure"): 101.0,
    ("22 32 8B", "Turbocharger Wastegate Solenoid Valve Command"): 18.0,
    ("22 32 8D", "Turbocharger Bypass Solenoid Valve Command"): 0.0,
    ("22 00 0E", "Ignition Timing"): 12.0,
    ("22 12 D9", "Total Knock Retard"): 0.0,
    ("22 11 A6", "Knock Retard"): 0.0,
    ("22 16 8E", "Intake Camshaft Position"): 4.0,
    ("22 16 92", "Desired Intake Camshaft Position"): 4.0,
    ("22 16 8D", "Exhaust Camshaft Position"): 2.0,
    ("22 16 91", "Desired Exhaust Camshaft Position"): 2.0,
    ("22 12 00", "Total Misfire"): 0.0,
    ("22 12 01", "Cylinder 1 History Misfire Counter"): 0.0,
    ("22 12 02", "Cylinder 2 History Misfire Counter"): 0.0,
    ("22 12 03", "Cylinder 3 History Misfire Counter"): 0.0,
    ("22 12 04", "Cylinder 4 History Misfire Counter"): 0.0,
}

# What each fault scenario changes about that baseline.
_OVERRIDES: Dict[str, Dict[Tuple[str, str], float]] = {
    "healthy": {},
    "misfire-cyl3": {
        ("22 00 0C", "Engine Speed"): 744.0,
        ("22 12 00", "Total Misfire"): 71.0,
        ("22 12 01", "Cylinder 1 History Misfire Counter"): 2.0,
        ("22 12 02", "Cylinder 2 History Misfire Counter"): 1.0,
        ("22 12 03", "Cylinder 3 History Misfire Counter"): 214.0,
        ("22 12 04", "Cylinder 4 History Misfire Counter"): 3.0,
        ("22 00 06", "Short Term Fuel Trim"): 9.4,
    },
    "boost-fault": {
        ("22 00 0C", "Engine Speed"): 4180.0,
        ("22 00 04", "Engine Load"): 96.0,
        ("22 00 0D", "Vehicle Speed"): 92.0,
        ("22 13 1F", "Accelerator Pedal Position"): 99.0,
        ("22 12 B1", "Throttle Position"): 92.0,
        ("22 00 4C", "Desired Throttle Position"): 93.0,
        ("22 32 8A", "Desired Boost Pressure"): 212.0,
        ("22 32 98", "Boost Pressure Sensor"): 128.0,
        # 0xFF -- the end of the single command byte, reached through the
        # pack's own 0.3922 %/bit scaling rather than written as 100.
        ("22 32 8B", "Turbocharger Wastegate Solenoid Valve Command"): 100.0,
        ("22 00 10", "MAF Sensor"): 88.0,
        ("22 12 D9", "Total Knock Retard"): 1.4,
    },
}

# Core parameters this fake refuses on purpose, so the NRC-31 path is exercised
# by the DEFAULT read and not only by a sweep. Four-cylinder car, no oil
# pressure transducer in this list, no manifold pressure parameter on this
# calibration, no post-catalyst sensor on this request: a plausible shape for
# what a shared GM parameter list gets wrong about one engine.
REFUSED_CORE = (
    "22 14 70",   # Engine Oil Pressure Sensor
    "22 20 01",   # Intake Manifold Pressure
    "22 23 21",   # HO2S 2
)


class SimulatedEcm:
    """A fake E39A at 0x7E0 that answers the frames engine.py sends.

    Speaks GMW3110 and refuses UDS, like a real Global A powertrain module:
    0x19 comes back "service not supported" and the F1xx identifier block
    "request out of range", so the dialect ladder in scan.py has to earn its
    answer here rather than be handed it.
    """

    def __init__(self, scenario: str = "healthy", latency: float = 0.04):
        if scenario not in SCENARIOS:
            raise ValueError(
                f"Unknown simulation scenario '{scenario}'. "
                f"Try one of: {', '.join(SCENARIOS)}."
            )
        self.scenario = scenario
        self.latency = latency
        self.now = 0.0
        self.frames: List[str] = []
        self.vin = VIN
        self.values = dict(_BASE)
        self.values.update(_OVERRIDES[scenario])

    # ------------------------------------------------------------------ #
    # the Elm327UdsClient surface engine.py uses
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        """No-op: there is no ELM to configure."""

    def restore(self) -> None:
        """No-op: nothing was configured, so nothing needs putting back."""

    def clock(self) -> float:
        """Virtual time, so a capture of ten simulated minutes runs instantly."""
        return self.now

    # ------------------------------------------------------------------ #
    def _encode(self, request: str, name: str, value: float) -> Optional[bytes]:
        """Turn an engineering value into the bytes the pack says carry it.

        Inverts the pack's own factor/offset rather than writing raw bytes
        here, which is what keeps the fake honest: if a scaling in the data
        were wrong, this sim would be wrong in the same direction as the reader
        and the round trip would still hold -- but nothing in the sim can
        disagree with the file it is modelling.
        """
        entry = dealer_data.ecm_parameter(request, name)
        if entry is None:
            return None
        scaling = entry.get("scaling") or {}
        factor = scaling.get("factor")
        offset = scaling.get("offset", 0.0)
        length = entry.get("responseLength") or 1
        if not factor:
            return None
        raw = int(round((value - offset) / factor))
        raw = max(0, min((1 << (8 * length)) - 1, raw))
        return raw.to_bytes(length, "big")

    def _payload_for(self, request: str) -> Optional[bytes]:
        """The bytes this fake would put on the wire for one request frame."""
        if request in REFUSED_CORE:
            return None
        wanted = [(req, name) for (req, name) in self.values if req == request]
        if not wanted:
            return None
        # One request can carry several named signals; this fake only models
        # the ones it has a value for, and those all sit at payload offset 0
        # in the core block, so the longest encoding wins.
        best = None
        for req, name in wanted:
            body = self._encode(req, name, self.values[(req, name)])
            if body is not None and (best is None or len(body) > len(best)):
                best = body
        return best

    # ------------------------------------------------------------------ #
    def request(self, dst: int, data: bytes, timeout: float = 2.0) -> bytes:
        """One request in, the raw reply out.

        Negative responses come back AS BYTES, exactly as the real ELM327 UDS
        client returns them: reading a 0x7F is the caller's job, not the
        transport's, and engine.py's applicability report depends on getting
        the NRC rather than an exception.
        """
        self.frames.append(data.hex().upper())
        self.now += self.latency
        if dst != ECM:
            raise ObdError("The car returned no data for this request.",
                           kind="no_data")

        service = data[0]

        if service == SVC_TESTER_PRESENT:
            return bytes([SVC_TESTER_PRESENT + 0x40])

        if service == SVC_READ_DATA_BY_ID and len(data) >= 2:
            body = IDENT.get(data[1])
            if body is None:
                return bytes([0x7F, SVC_READ_DATA_BY_ID, 0x31])
            return bytes([SVC_READ_DATA_BY_ID + 0x40, data[1]]) + body

        if service == SVC_READ_DIAG_INFO:
            return self._read_dtcs(data)

        if service == SVC_READ_BY_PID and len(data) >= 3:
            return self._read_pid(data)

        # UDS, which a Global A powertrain module does not speak.
        if service in (0x19, 0x14):
            return bytes([0x7F, service, 0x11])

        if service == SVC_DEVICE_CONTROL:
            # Nothing in this profile sends one. If something ever did, this
            # fake refuses rather than pretending an actuator moved.
            return bytes([0x7F, SVC_DEVICE_CONTROL, 0x33])

        return bytes([0x7F, service, 0x11])

    # ------------------------------------------------------------------ #
    def _read_dtcs(self, data: bytes) -> bytes:
        """0xA9 0x81 <mask> -- fault memory, GMLAN style."""
        if len(data) < 2 or data[1] != 0x81:
            # 0xA9 0x82 is the presence probe; answer it without a fault list.
            return bytes([SVC_READ_DIAG_INFO + 0x40, 0x82, 0x00])
        mask = data[2] if len(data) >= 3 else 0xFF
        out = bytearray([SVC_READ_DIAG_INFO + 0x40, 0x81, mask])
        for hi, lo, ftype, status in DTCS[self.scenario]:
            out += bytes([hi, lo, ftype, status])
        return bytes(out)

    def _read_pid(self, data: bytes) -> bytes:
        """0x22 <hi> <lo>. Anything this fake has no value for is NRC 31."""
        request = f"22 {data[1]:02X} {data[2]:02X}"
        payload = self._payload_for(request)
        if payload is None:
            return bytes([0x7F, SVC_READ_BY_PID, 0x31])
        return bytes([SVC_READ_BY_PID + 0x40, data[1], data[2]]) + payload


def describe_scenarios() -> List[dict]:
    """The scenario list for the operator-facing surfaces, each with what it
    is actually demonstrating rather than just its name."""
    return [{"id": name, "what": text} for name, text in SCENARIOS.items()]
