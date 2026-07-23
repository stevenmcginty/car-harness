"""Mercedes-Benz profile: turns a generic ELM327 dongle into an API
ConnectionStatus.

Like Mazda, Peugeot and VW (and unlike BMW, which has a native ENET transport),
Mercedes has no special cable in this harness -- it is diagnosed over whatever
ELM327-style adapter is selected, on the car's diagnostic CAN bus.
status("real", adapter) runs a genuine, read-only check over that adapter: is
the dongle there, does the car answer, is it on the 11-bit/500k CAN the Mercedes
engine+gearbox read needs, and can we read a VIN. status("sim") returns a
clearly-labelled fake so the UI can be demoed with no car present -- simulated
data must NEVER leak into real mode.

Mercedes note (docs/private/mercedes-research.md §1-2): a plain ELM327 on the
OBD port reaches only the engine (0x7E0/0x7E8) and automatic transmission
(0x7E1/0x7E9) at manufacturer depth, plus generic OBD-II. Body/chassis/comfort
modules use Mercedes' proprietary low-id scheme behind a filtering gateway and
are STAR/XENTRY-only -- this probe only confirms the car is answering and on the
right bus. ~2018+ cars are DoIP/Ethernet and give generic OBD-II only.
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
        profile="mercedes", mode="sim", simulated=True, overall="connected", steps=steps,
        vehicle=Vehicle(vin="SIMULATEDMB000001"),
        message="SIMULATED - no real car connected.", timestamp=_now(),
    )


def _needs_adapter() -> ConnectionStatus:
    return ConnectionStatus(
        profile="mercedes", mode="real", simulated=False, overall="disconnected",
        steps=[StepStatus(
            key="adapter", label="OBD adapter", state="fail",
            detail="Mercedes-Benz is diagnosed over an ELM327-style dongle, not a native cable. "
                   "Pick a configured ELM327 adapter (or use Simulate).",
        )],
        vehicle=None,
        message="Select an ELM327-style adapter to check the Mercedes-Benz connection.",
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
            profile="mercedes", mode="real", simulated=False, overall="disconnected",
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
            key="bus", label="Diagnostic CAN (engine + gearbox)",
            state="ok" if on_can else "fail",
            detail=("On 11-bit/500k CAN - the Mercedes engine + automatic-gearbox deep read is "
                    "available (body modules stay STAR-only; ~2018+ DoIP cars give generic "
                    "OBD-II only)."
                    if on_can else
                    f"Car is on {info['protocol'] or 'a non-standard bus'}; the Mercedes "
                    "engine/gearbox read expects 11-bit/500k CAN (a ~2004-2018 car). A handful "
                    "of 2001-2003 cars are K-line for emissions engine data only."),
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
        f"Connected to the Mercedes-Benz over {name}. Read-only - nothing was written to the car."
        if on_can else
        f"{name} reached the car, but it is not on the 11-bit/500k CAN the engine/gearbox read needs."
    )
    return ConnectionStatus(
        profile="mercedes", mode="real", simulated=False, overall=overall,
        steps=steps, vehicle=vehicle, message=message, timestamp=_now(),
    )
