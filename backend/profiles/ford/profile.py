"""Ford profile: turns a generic ELM327 dongle into an API ConnectionStatus.

Unlike BMW (which has a native ENET transport it auto-detects), Ford has no
special cable -- it is diagnosed over whatever ELM327-style adapter is selected,
on the car's HS-CAN bus. status("real", adapter) therefore runs a genuine,
read-only check over that adapter: is the dongle there, does the car answer, and
can we read a VIN. status("sim") returns a clearly-labelled fake so the UI can
be demoed with no car present -- simulated data must NEVER leak into real mode.
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
                   detail="SIMULATED - pretend the car answered 0100 on HS-CAN (11-bit/500k)."),
        StepStatus(key="vin", label="Vehicle identified", state="ok",
                   detail="SIMULATED - fake VIN, no real car connected."),
    ]
    return ConnectionStatus(
        profile="ford", mode="sim", simulated=True, overall="connected", steps=steps,
        vehicle=Vehicle(vin="SIMULATED0000FORD"),
        message="SIMULATED - no real car connected.", timestamp=_now(),
    )


def _needs_adapter() -> ConnectionStatus:
    return ConnectionStatus(
        profile="ford", mode="real", simulated=False, overall="disconnected",
        steps=[StepStatus(
            key="adapter", label="OBD adapter", state="fail",
            detail="Ford is diagnosed over an ELM327-style dongle, not a native cable. "
                   "Pick a configured ELM327 adapter (or use Simulate).",
        )],
        vehicle=None,
        message="Select an ELM327-style adapter to check the Ford connection.",
        timestamp=_now(),
    )


def _real_over_adapter(adapter: dict) -> ConnectionStatus:
    """Open the ELM327 over ``adapter`` and run a read-only connectivity probe."""
    from backend.obd import service as obd_service
    from backend.obd.elm327 import ObdError
    from backend.obd.transport import TransportError

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
            profile="ford", mode="real", simulated=False, overall="disconnected",
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
            key="bus", label="HS-CAN (module scan)",
            state="ok" if on_can else "fail",
            detail=("On 11-bit/500k HS-CAN - manufacturer-level module scan is available."
                    if on_can else
                    f"Car is on {info['protocol'] or 'a non-standard bus'}; the Ford module "
                    "scan expects 11-bit/500k HS-CAN."),
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
        f"Connected to the Ford over {name}. Read-only - nothing was written to the car."
        if on_can else
        f"{name} reached the car, but it is not on the HS-CAN the module scan needs."
    )
    return ConnectionStatus(
        profile="ford", mode="real", simulated=False, overall=overall,
        steps=steps, vehicle=vehicle, message=message, timestamp=_now(),
    )
