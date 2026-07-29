"""Raw diagnostic channel -- the escape hatch for anything the curated packs
don't cover.

Built for the Claude Code / MCP workflow: when a car holds a value no brand
pack curated (a DPF soot mass, an odd body DID), the assistant researches the
right identifier and reads it live through the same session-cached ELM327
stack the brand profiles use. Two entry points:

read_dids()     ReadDataByIdentifier (service 0x22) against any module.
                addressing="11bit" is the standard ISO 15765-4 pairing the
                Mazda/PSA/VW/JLR profiles use (request 0x7E0-style id, answer
                on +8 unless resp_id overrides it -- VAG's body block answers
                at +0x6A, for example). addressing="29bit" is the FCA scheme
                (request 18DAxxF1, response 18DAF1xx; target is the one-byte
                ECU address). Read-only by construction: the only services
                ever sent are 0x22 and, when asked, a 10 03 extended-session
                open for modules that refuse reads in the default session.

elm_commands()  A raw ELM327 sequence -- AT setup plus arbitrary hex requests
                -- for everything read_dids can't express. The ELM is ALWAYS
                handed back restored to the defaults the generic OBD stack
                relies on, so a follow-up scan can't inherit a stray header.
                Confirm-gated at the API/MCP layer: arbitrary hex can write.

Decoding is deliberately thin (hex, printable ASCII, big-endian int): the
caller is the analyst here. Sanity-scaling raw bytes is the curated packs' job.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327Uds29Client, Elm327UdsClient

# FCA TPMS -- the immobilizer trap (docs/private/fiat-alfa-research.md §7).
# 29-bit 18DAxxF1 addressing IS the FCA scheme, so on that scheme 0xC7 is
# refused unconditionally, same as the fiat profile's own guard.
FCA_TPMS_ADDR = 0xC7

# ELM commands the raw channel refuses. Monitor modes hang the exchange (they
# need the snapshot code's special space-no-CR stop), resets/low-power drop the
# session init state, and baud changes kill the link mid-conversation.
_BLOCKED_PREFIXES = ("ATMA", "ATMR", "ATMT", "ATZ", "ATWS", "ATPC", "ATBRD", "ATBRT", "ATLP")
_BLOCKED_MESSAGE = (
    "Refused: monitor (use the brand snapshot feature), reset, low-power and "
    "baud-rate commands would strand the dongle session."
)

_MAX_DIDS = 24        # BLE is slow; keep one call bounded
_MAX_COMMANDS = 32
_DID_TIMEOUT = 2.5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hex_int(value, name: str, hi: int) -> int:
    """A user-supplied hex string ('7E0', '0x1310') -> int, range-checked."""
    try:
        parsed = int(str(value).strip(), 16)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a hex string, got {value!r}.")
    if not 0 <= parsed <= hi:
        raise ValueError(f"{name} {value!r} is outside 0..{hi:X}.")
    return parsed


def _printable(body: bytes) -> Optional[str]:
    """Printable-ASCII rendering when the payload is mostly text, else None."""
    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
    if text and len(text) >= (len(body) + 1) // 2:
        return text
    return None


def _row(did: int, reply: Optional[bytes], error: Optional[str] = None) -> dict:
    """One per-DID result row: raw truth plus thin convenience decodes."""
    row: dict = {"did": f"{did:04X}", "ok": False}
    if error is not None:
        row["error"] = error
        return row
    row["raw"] = reply.hex().upper()
    if len(reply) >= 3 and reply[0] == 0x62:
        payload = reply[3:]
        row["ok"] = True
        row["payload"] = payload.hex().upper()
        if reply[1] != did >> 8 or reply[2] != did & 0xFF:
            row["note"] = f"answered for DID {reply[1]:02X}{reply[2]:02X}, not the one asked"
        text = _printable(payload)
        if text is not None:
            row["ascii"] = text
        if 1 <= len(payload) <= 4:
            row["asInt"] = int.from_bytes(payload, "big")
    elif len(reply) >= 3 and reply[0] == 0x7F:
        nrc = reply[2]
        row["error"] = f"NRC {nrc:02X} ({uds.NRC_NAMES.get(nrc, 'unknown')})"
    else:
        row["error"] = "unexpected reply (raw kept)"
    return row


def _open_extended(client, target_key: int, trace: List[str]) -> None:
    """Best-effort UDS extended session (10 03); the outcome goes in the trace
    and a refusal is never fatal -- plenty of modules answer 0x22 without it."""
    try:
        reply = client.request(target_key, bytes([0x10, 0x03]), timeout=2.0)
    except (ObdError, TransportError):
        trace.append("10 03 -> no answer (continuing in default session)")
        return
    if reply and reply[0] == 0x50:
        trace.append("10 03 -> extended session open")
    elif reply and reply[0] == 0x7F and len(reply) >= 3:
        trace.append(f"10 03 -> NRC {reply[2]:02X} (continuing in default session)")


def read_dids(
    adapter: Optional[dict],
    mode: str,
    target: str,
    dids: List[str],
    addressing: str = "11bit",
    resp_id: Optional[str] = None,
    extended_session: bool = False,
) -> dict:
    """Read arbitrary mode-22 DIDs from one module over the dongle. Read-only."""
    try:
        if addressing not in ("11bit", "29bit"):
            raise ValueError("addressing must be '11bit' or '29bit'.")
        if not dids:
            raise ValueError("Give at least one DID (hex, e.g. '1310').")
        if len(dids) > _MAX_DIDS:
            raise ValueError(f"At most {_MAX_DIDS} DIDs per call -- BLE dongles are slow.")
        if addressing == "11bit":
            tgt = _hex_int(target, "target", 0x7FF)
            resp = _hex_int(resp_id, "resp_id", 0x7FF) if resp_id else None
        else:
            tgt = _hex_int(target, "target", 0xFF)
            resp = None
            if resp_id:
                raise ValueError("resp_id only applies to 11bit (29bit pairing is fixed).")
            if tgt == FCA_TPMS_ADDR:
                raise ValueError(
                    "Refused: 0xC7 (18DAC7F1) is the FCA TPMS -- polling it can engage "
                    "the immobilizer and stall the car. Never addressed."
                )
        did_list = [_hex_int(d, "did", 0xFFFF) for d in dids]
    except ValueError as exc:
        return {"ok": False, "simulated": False, "results": [],
                "message": str(exc), "timestamp": _now()}

    if mode == "sim":
        results = [
            {"did": f"{d:04X}", "ok": True, "raw": f"62{d:04X}53494D",
             "payload": "53494D", "ascii": "SIM"}
            for d in did_list
        ]
        return {"ok": True, "simulated": True, "results": results,
                "message": "SIMULATED -- no real car contacted.", "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        trace: List[str] = []
        results: List[dict] = []

        def work(client, target_key):
            if extended_session:
                _open_extended(client, target_key, trace)
            for did in did_list:
                req = bytes([0x22, did >> 8, did & 0xFF])
                try:
                    reply = client.request(target_key, req, timeout=_DID_TIMEOUT)
                except (ObdError, TransportError) as exc:
                    results.append(_row(did, None, error=f"no answer ({exc})"))
                    continue
                results.append(_row(did, reply))

        if addressing == "11bit":
            if session.protocol_num not in (6, None):
                return {
                    "ok": False, "simulated": False, "results": [],
                    "message": (
                        f"The car answered {session.protocol or 'an unknown protocol'}, "
                        "not 11-bit/500k CAN -- try addressing='29bit' if this is a "
                        "UDS-era FCA car."
                    ),
                    "timestamp": _now(),
                }
            client = Elm327UdsClient(session, id_map={tgt: resp} if resp else None)
            client.begin()
            try:
                work(client, tgt)
            finally:
                client.restore()
        else:
            discovered = session.protocol_num
            session.at("ATSP7")
            trace.append("switched to 29-bit (ATSP7)")
            client = Elm327Uds29Client(session)
            client.begin()
            try:
                work(client, tgt)
            finally:
                client.restore()
                try:
                    session.at(f"ATSP{discovered:X}" if discovered is not None else "ATSP0")
                    session.at("ATSH7DF")
                except Exception:
                    pass

        answered = sum(1 for r in results if r["ok"])
        return {
            "ok": True, "simulated": False, "results": results, "trace": trace,
            "message": f"{answered} of {len(results)} DID(s) answered.",
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "results": [],
                "message": str(exc), "timestamp": _now()}


def elm_commands(adapter: Optional[dict], mode: str, commands: List[str],
                 timeout: float = 3.0) -> dict:
    """Run a raw ELM327 command sequence, then hand the ELM back restored."""
    cleaned = [str(c).strip() for c in (commands or []) if str(c).strip()]
    if not cleaned:
        return {"ok": False, "simulated": False, "replies": [],
                "message": "Give at least one command.", "timestamp": _now()}
    if len(cleaned) > _MAX_COMMANDS:
        return {"ok": False, "simulated": False, "replies": [],
                "message": f"At most {_MAX_COMMANDS} commands per call.",
                "timestamp": _now()}
    for cmd in cleaned:
        compact = cmd.replace(" ", "").upper()
        if compact.startswith(_BLOCKED_PREFIXES) or compact == "ATD":
            return {"ok": False, "simulated": False, "replies": [],
                    "message": f"{_BLOCKED_MESSAGE} ('{cmd}')", "timestamp": _now()}
    timeout = min(max(float(timeout), 0.5), 10.0)

    if mode == "sim":
        replies = [{"command": c, "reply": "OK (SIMULATED)"} for c in cleaned]
        return {"ok": True, "simulated": True, "replies": replies,
                "message": "SIMULATED -- no real dongle contacted.", "timestamp": _now()}

    def run(session, entry):
        replies: List[dict] = []
        sp_touched = False
        discovered = session.protocol_num
        try:
            for cmd in cleaned:
                compact = cmd.replace(" ", "").upper()
                if compact.startswith("ATSP"):
                    sp_touched = True
                try:
                    text = session._raw(compact.encode("ascii") + b"\r", timeout)
                except TransportError as exc:
                    replies.append({"command": cmd, "reply": f"TRANSPORT ERROR: {exc}"})
                    break
                text = text.replace(">", "").replace("\r", "\n")
                replies.append({"command": cmd,
                                "reply": "\n".join(t for t in text.split("\n") if t.strip())})
        finally:
            # Hand the ELM back the way the generic OBD stack expects it,
            # whatever the sequence did (superset of the profiles' restore).
            restore = ["ATCRA", "ATCEA", "ATCAF1", "ATH0", "ATSH7DF",
                       "ATFCSM0", "ATAT1", "ATST32"]
            if sp_touched:
                restore.append(f"ATSP{discovered:X}" if discovered is not None else "ATSP0")
            for cmd in restore:
                try:
                    session.at(cmd)
                except Exception:
                    pass
        return {"ok": True, "simulated": False, "replies": replies,
                "message": f"{len(replies)} command(s) executed; ELM restored to defaults.",
                "timestamp": _now()}

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "replies": [],
                "message": str(exc), "timestamp": _now()}
