"""Shared executor for service-reset definitions (all brands).

A service reset is a small data-driven script: a list of UDS steps run against
one module. Each brand's profiles/<brand>/resets.py holds the definitions (with
provenance and a confidence grade) and picks the right transport client; this
module only interprets the steps, so the byte-level behaviour is identical on
every brand and trivially testable offline.

Step forms (dicts, JSON-friendly so the same tables can ship to the phone):

    {"session": 3}                          DiagnosticSessionControl 0x10 <level>
    {"routine": 0xA123, "sub": 1, "data": "AABB"}   RoutineControl 0x31
    {"write": 0x2000, "data": "00"}         WriteDataByIdentifier 0x2E
    {"read": 0xF190}                        ReadDataByIdentifier (sanity check)
    {"delay": 0.3}                          plain wait, seconds

Raises UdsNegativeResponse / ObdError on the first failing step -- a service
reset is all-or-nothing, there is no point continuing after a refusal.
"""

import time
from typing import Callable, List, Optional

from backend.core import uds


def describe_step(step: dict) -> str:
    if "session" in step:
        return f"Extended session (10 {step['session']:02X})"
    if "routine" in step:
        sub = step.get("sub", 0x01)
        data = step.get("data", "")
        return f"Routine 31 {sub:02X} {step['routine']:04X}" + (f" {data}" if data else "")
    if "write" in step:
        return f"Write DID {step['write']:04X} <- {step.get('data', '')}"
    if "read" in step:
        return f"Read DID {step['read']:04X}"
    if "delay" in step:
        return f"Wait {step['delay']}s"
    return "?"


def execute(
    client,
    address: int,
    steps: List[dict],
    on_progress: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """Run one reset's steps against ``address`` over an aimed UDS client.

    Returns the human-readable log of executed steps (for the API response).
    """
    log: List[str] = []
    for step in steps:
        text = describe_step(step)
        if on_progress:
            on_progress(text)
        if "session" in step:
            uds.start_session(client, address, int(step["session"]))
        elif "routine" in step:
            uds.routine_control(
                client, address,
                int(step["routine"]),
                int(step.get("sub", 0x01)),
                bytes.fromhex(step.get("data", "")),
            )
        elif "write" in step:
            uds.write_did(client, address, int(step["write"]), bytes.fromhex(step.get("data", "")))
        elif "read" in step:
            uds.read_did(client, address, int(step["read"]))
        elif "delay" in step:
            time.sleep(float(step["delay"]))
        else:
            raise ValueError(f"Unknown reset step: {step!r}")
        log.append(text)
    return log


def public_view(reset: dict) -> dict:
    """A reset definition -> the JSON shape the API/UI serves.

    Numbers that are addresses/DIDs/routines go out as hex strings so the
    phone and web UI never have to guess a radix.
    """
    out = {
        "id": reset["id"],
        "name": reset["name"],
        "description": reset.get("description", ""),
        "method": reset["method"],
        "confidence": reset.get("confidence", "experimental"),
        "source": reset.get("source", ""),
        "models": reset.get("models", ""),
        "cautions": reset.get("cautions", []),
    }
    if reset["method"] == "uds":
        target = reset["target"]
        out["target"] = {
            "code": target["code"],
            "name": target.get("name", ""),
            "address": f"0x{target['address']:03X}",
        }
        out["steps"] = [describe_step(s) for s in reset["steps"]]
    else:
        out["procedure"] = reset.get("procedure", [])
    return out
