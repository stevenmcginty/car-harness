"""JLR engine DID sweep ("mapping") over a generic OBD-II dongle.

The diesel aftertreatment values the X260 AdBlue job wants -- DPF soot mass,
regen status, DEF level/quality, NOx ppm, EGTs -- have NO public JLR DID list
(docs/private/jlr-x260-adblue-research.md §2 Q3: "treat every specific DID on
this car as capture-first"). This is the capture tool: walk a 0x22
(ReadDataByIdentifier) range on one module one DID at a time and keep every
raw answer, exactly the Peugeot-sweep pattern.

How a signal gets identified (never by guessing): sweep once at idle and once
with the engine warm / held at speed, then diff. A DID whose payload moves
with rpm and load is a live signal; one that steps between two states is a
command or status; a temperature drifts one way only. Only after a DID has
been identified on the real car does anything downstream decode it -- this
module stores raw hex and invents nothing.

Classification per DID:
  ANSWERED   a positive 62 <did> -- payload stored as raw hex (no scaling),
  REFUSED    NRC 0x31 (request out of range) -- the module is listening and
             the id simply isn't implemented; COUNTED, not listed (on a
             real car these are the vast majority),
  NRC-OTHER  any other negative response -- listed with its named NRC (a
             0x7F 22 22 cluster, for instance, means "ask again in the right
             session" -- try extended=True),
  SILENT     no reply at all.

Read-only: service 0x22 only, plus an optional best-effort 10 03 extended
session before the walk (never fatal if refused). The target must answer a
presence probe first -- the sweep never blind-sprays a dead bus.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient
from backend.profiles.jlr.scan import _open_extended, _probe

# Keep one call proportionate: 4096 ids at ~0.6 s each is already the better
# part of an hour over Bluetooth. Split wider ranges; the research's default
# ranges (0300-05FF, 2100-21FF, 2200-22FF, F400-F4FF on the PCM) all fit.
_MAX_SWEEP = 4096

# Per-DID read timeout. Most answers land well inside 0.6 s; refusals are
# immediate; only an unimplemented DID on a slow bus pays the full window.
_DID_TIMEOUT = 0.6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sweep(adapter: Optional[dict], mode: str = "real", first: str = "0300",
          last: str = "05FF", target: str = "7E0", extended: bool = False) -> dict:
    """Walk a 0x22 DID range on one JLR module and keep everything that answers.

    first/last: hex DID bounds like "0300" and "05FF" (max 4096 ids per call).
    target: the module's 11-bit request id in hex, default the PCM ("7E0",
        response = request + 8).
    extended: open the UDS extended session (10 03) first -- best-effort,
        never fatal; some DIDs only answer there.
    Everything comes back raw. Read-only.
    """
    empty = {"answered": [], "refused31": 0, "nrcOther": [], "silent": 0}
    if mode == "sim":
        # A plausible PCM scatter over the requested range: one 1-byte, one
        # 2-byte and one 4-byte value, a refused stretch, and one
        # conditions-not-correct NRC. The classification counts always sum to
        # the swept width, as they do on a real sweep.
        try:
            lo, hi = int(first, 16), int(last, 16)
        except (TypeError, ValueError):
            lo, hi = 0, 0xFFFF
        width = max(0, hi - lo + 1)
        answered = [
            {"did": "033E", "payload": "00C4"},          # 2 bytes
            {"did": "03F3", "payload": "58"},            # 1 byte
            {"did": "2104", "payload": "00A100B4"},      # 4 bytes, 2 words
            {"did": "F405", "payload": "5A"},
        ]
        nrc_other = [{"did": "2201", "nrc": "22",
                      "nrcName": uds.NRC_NAMES[0x22]}]
        answered = [a for a in answered
                    if lo <= int(a["did"], 16) <= hi]
        nrc_other = [n for n in nrc_other
                     if lo <= int(n["did"], 16) <= hi]
        n_silent = max(0, width - len(answered) - len(nrc_other) - 1)
        refused = max(0, width - len(answered) - len(nrc_other) - n_silent)
        return {
            "ok": True, "simulated": True,
            "answered": answered, "refused31": refused, "nrcOther": nrc_other,
            "silent": n_silent,
            "range": f"{first.upper()}-{last.upper()}",
            "target": target.upper(), "extendedSession": extended,
            "message": "SIMULATED -- no real car connected. This is the shape a real "
                       "sweep returns: raw payloads (no scaling invented), the 0x31 "
                       "refusals counted rather than listed, other NRCs named, the "
                       "rest silent.",
            "timestamp": _now(),
        }

    try:
        first_did, last_did = int(first, 16), int(last, 16)
        addr = int(target, 16)
    except (TypeError, ValueError):
        return {"ok": False, "simulated": False, **empty,
                "message": "first/last/target must be hex like '0300' / '05FF' / '7E0'.",
                "timestamp": _now()}
    if not (0 <= first_did <= last_did <= 0xFFFF):
        return {"ok": False, "simulated": False, **empty,
                "message": "Need 0 <= first <= last <= 0xFFFF (hex).",
                "timestamp": _now()}
    if last_did - first_did + 1 > _MAX_SWEEP:
        return {"ok": False, "simulated": False, **empty,
                "message": f"Range too wide ({last_did - first_did + 1} ids); max "
                           f"{_MAX_SWEEP} per call. Split it.",
                "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, **empty,
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not "
                    "the 11-bit/500k CAN the JLR DID sweep needs."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session)
        client.begin()
        answered: List[dict] = []
        refused31 = 0
        nrc_other: List[dict] = []
        silent = 0
        opened: set = set()
        try:
            # Guard: never blind-spray a dead bus -- the target must first
            # prove something lives at the address.
            if not _probe(client, addr, []):
                return {
                    "ok": False, "simulated": False, **empty,
                    "message": (
                        f"Nothing answered at 0x{addr:03X} (3E 00 / 22 F190 both "
                        "silent) -- refusing to sweep a dead address. Run "
                        "jlr_discover_modules to find the modules this car actually "
                        "exposes."
                    ),
                    "timestamp": _now(),
                }
            if extended:
                _open_extended(client, addr, opened, [])   # best-effort only
            for did in range(first_did, last_did + 1):
                try:
                    payload = client.request(
                        addr, bytes([0x22, did >> 8, did & 0xFF]),
                        timeout=_DID_TIMEOUT)
                except (ObdError, TransportError):
                    silent += 1
                    continue
                if not payload:
                    silent += 1
                elif payload[0] == 0x62 and len(payload) >= 3:
                    answered.append({"did": f"{did:04X}",
                                     "payload": payload[3:].hex().upper()})
                elif payload[0] == 0x7F and len(payload) >= 3:
                    if payload[2] == 0x31:
                        refused31 += 1          # counted, not listed
                    else:
                        nrc_other.append({
                            "did": f"{did:04X}", "nrc": f"{payload[2]:02X}",
                            "nrcName": uds.NRC_NAMES.get(payload[2], "unknown code"),
                        })
                else:
                    silent += 1
        finally:
            client.restore()

        return {
            "ok": True, "simulated": False,
            "answered": answered, "refused31": refused31, "nrcOther": nrc_other,
            "silent": silent,
            "range": f"{first_did:04X}-{last_did:04X}",
            "target": f"{addr:03X}", "extendedSession": extended,
            "message": (
                f"Swept {last_did - first_did + 1} id(s) at 0x{addr:03X}"
                + (" after a 10 03 extended session" if extended else "")
                + f": {len(answered)} answered with data, {refused31} refused with "
                  f"NRC 31 (listening, id not implemented), {len(nrc_other)} other "
                  f"NRC(s), {silent} silent. Payloads are raw hex on purpose -- "
                  "identify a signal by how it MOVES between an idle sweep and a "
                  "warm/loaded sweep, then decode it against the car. Read-only."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, **empty,
                "message": str(exc), "timestamp": _now()}
