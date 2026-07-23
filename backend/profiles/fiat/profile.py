"""Fiat / Alfa Romeo (FCA / Stellantis) profile: turns a generic ELM327 dongle
into an API ConnectionStatus.

Like Mazda, Peugeot, VW and Mercedes (and unlike BMW, which has a native ENET
transport), FCA has no special cable in this harness -- it is diagnosed over
whatever ELM327-style adapter is selected, on the car's diagnostic CAN bus.
status("real", adapter) runs a genuine, read-only check over that adapter: is
the dongle there, does the car answer, is it on the 11-bit/500k CAN generic OBD
uses, and can we read a VIN. status("sim") returns a clearly-labelled fake so the
UI can be demoed with no car present -- simulated data must NEVER leak into real
mode.

FCA note (docs/private/fiat-alfa-research.md §1-3): generic OBD-II answers on
11-bit/500k for every FCA car ~2008+, so this probe confirms the car is on the
right bus and reports the VIN. The module scan (scan.py) then switches to the
29-bit 18DAxxF1 addressing the UDS era needs (ATSP7), falling back to the 11-bit
engine on a CAN-era car. Reads pass the 2018+ Security Gateway; K-line-era cars
(Alfa 147/156/GT, older Punto) aren't reachable over a plain dongle at all.
"""

from datetime import datetime, timezone

from backend.core.models import ConnectionStatus, StepStatus, Vehicle


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def status(mode: str = "real", adapter: dict = None) -> ConnectionStatus:
    if mode == "sim":
        return _simulated()
    if adapter is None:
        return _needs_adapter()
    return _real_over_adapter(adapter)


def _simulated() -> ConnectionStatus:
    steps = [
        StepStatus(key="adapter", label="OBD adapter", state="ok",
                   detail="SIMULATED - pretend an ELM327 dongle is connected."),
        StepStatus(key="link", label="Dongle answered", state="ok",
                   detail="SIMULATED - pretend the ELM327 reset with its banner."),
        StepStatus(key="car", label="Car responding", state="ok",
                   detail="SIMULATED - pretend the car answered 0100 on CAN (11-bit/500k)."),
        StepStatus(key="vin", label="Vehicle identified", state="ok",
                   detail="SIMULATED - fake VIN, no real car connected."),
    ]
    return ConnectionStatus(
        profile="fiat", mode="sim", simulated=True, overall="connected", steps=steps,
        vehicle=Vehicle(vin="SIMULATEDGIULIA01"),
        message="SIMULATED - no real car connected.", timestamp=_now(),
    )


def _needs_adapter() -> ConnectionStatus:
    return ConnectionStatus(
        profile="fiat", mode="real", simulated=False, overall="disconnected",
        steps=[StepStatus(
            key="adapter", label="OBD adapter", state="fail",
            detail="Fiat / Alfa Romeo is diagnosed over an ELM327-style dongle, not a native cable. "
                   "Pick a configured ELM327 adapter (or use Simulate).",
        )],
        vehicle=None,
        message="Select an ELM327-style adapter to check the Fiat / Alfa Romeo connection.",
        timestamp=_now(),
    )


def _real_over_adapter(adapter: dict) -> ConnectionStatus:
    """Open the ELM327 over ``adapter`` and run a read-only connectivity probe."""
    from backend.obd import service as obd_service

    name = (adapter or {}).get("name") or "adapter"

    def probe(session, entry):
        session.ensure_protocol()  # raises ObdError('link') if the car is silent
        vin = obd_service._read_vin(session)
        return {
            "firmware": session.firmware,
            "protocol": session.protocol,
            "protocol_num": session.protocol_num,
            "voltage": session.voltage(),
            "vin": vin,
        }

    try:
        info = obd_service._with_session(adapter, probe)
    except obd_service.ObdServiceError as exc:
        # The dongle or car did not cooperate -- report exactly how far we got.
        return ConnectionStatus(
            profile="fiat", mode="real", simulated=False, overall="disconnected",
            steps=[
                StepStatus(key="adapter", label="OBD adapter", state="ok",
                           detail=f"{name} is configured."),
                StepStatus(key="car", label="Car responding", state="fail", detail=str(exc)),
            ],
            vehicle=None, message=str(exc), timestamp=_now(),
        )

    on_can = info["protocol_num"] in (6, None)
    steps = [
        StepStatus(key="adapter", label="OBD adapter", state="ok",
                   detail=f"{name}: ELM327 {info['firmware']} answered ({info['voltage']})."),
        StepStatus(key="car", label="Car responding", state="ok",
                   detail=f"Car answered on {info['protocol'] or 'CAN'}."),
        StepStatus(
            key="bus", label="Diagnostic CAN (module scan)",
            state="ok" if on_can else "fail",
            detail=("On 11-bit/500k CAN - the FCA module scan runs (UDS-era cars read in full over "
                    "29-bit CAN; CAN-era Fiats give engine + ABS, body needs a B-CAN adapter). "
                    "SGW on 2018+ cars blocks writes only, not these reads."
                    if on_can else
                    f"Car is on {info['protocol'] or 'a non-standard bus'}; the FCA module scan "
                    "expects 11-bit/500k CAN. Older Alfa 147/156/GT and Punto mk2 are K-line and "
                    "aren't supported on this dongle."),
        ),
        StepStatus(
            key="vin", label="Vehicle identified",
            state="ok" if info["vin"] else "pending",
            detail=(f"VIN {info['vin']}." if info["vin"]
                    else "Connected, but the car did not report a VIN over generic OBD."),
        ),
    ]
    overall = "connected" if on_can else "partial"
    vehicle = Vehicle(vin=info["vin"]) if info["vin"] else None
    message = (
        f"Connected to the Fiat / Alfa Romeo over {name}. Read-only - nothing was written to the car."
        if on_can else
        f"{name} reached the car, but it is not on the 11-bit/500k CAN the module scan needs."
    )
    return ConnectionStatus(
        profile="fiat", mode="real", simulated=False, overall=overall,
        steps=steps, vehicle=vehicle, message=message, timestamp=_now(),
    )
