"""Peugeot / PSA profile: turns a generic ELM327 dongle into an API
ConnectionStatus.

Like Mazda (and unlike BMW, which has a native ENET transport), Peugeot has no
special cable in this harness -- it is diagnosed over whatever ELM327-style
adapter is selected, on the car's diagnostic CAN bus. status("real", adapter)
runs a genuine, read-only check over that adapter: is the dongle there, does the
car answer, is it on the 11-bit/500k CAN the PSA module scan needs, and can we
read a VIN. status("sim") returns a clearly-labelled fake so the UI can be
demoed with no car present -- simulated data must NEVER leak into real mode.

PSA note: a plain ELM327 on the OBD port reaches the diagnostic CAN bus behind
the BSI gateway. The module scan (scan.py) speaks UDS to each module's own PSA
CAN id pair; this probe only confirms the car is answering and on the right bus.
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
        profile="peugeot", mode="sim", simulated=True, overall="connected", steps=steps,
        vehicle=Vehicle(vin="SIMULATED00PEUGEOT"),
        message="SIMULATED - no real car connected.", timestamp=_now(),
    )


def _needs_adapter() -> ConnectionStatus:
    return ConnectionStatus(
        profile="peugeot", mode="real", simulated=False, overall="disconnected",
        steps=[StepStatus(
            key="adapter", label="OBD adapter", state="fail",
            detail="Peugeot is diagnosed over an ELM327-style dongle, not a native cable. "
                   "Pick a configured ELM327 adapter (or use Simulate).",
        )],
        vehicle=None,
        message="Select an ELM327-style adapter to check the Peugeot connection.",
        timestamp=_now(),
    )


def _real_over_adapter(adapter: dict) -> ConnectionStatus:
    """Open the ELM327 over ``adapter`` and run a read-only connectivity probe."""
    from backend.obd import service as obd_service
    from backend.profiles.peugeot.scan import read_vin as psa_read_vin

    name = (adapter or {}).get("name") or "adapter"

    def probe(session, entry):
        session.ensure_protocol()  # raises ObdError('link') if the car is silent
        # The PSA VIN ladder, not just generic Mode 09: KWP-era engines
        # (MED17 petrol / DCM diesel, ~2004-2013) never answer Mode 09, and
        # reporting "no VIN" for a healthy 308 CC of that era was wrong.
        vin = psa_read_vin(session)
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
            profile="peugeot", mode="real", simulated=False, overall="disconnected",
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
            detail=("On 11-bit/500k CAN - PSA module scan (BSI, engine, airbag, ABS, ...) "
                    "is available."
                    if on_can else
                    f"Car is on {info['protocol'] or 'a non-standard bus'}; the Peugeot module "
                    "scan expects 11-bit/500k CAN (an AEE2010-era, ~2010+ car)."),
        ),
        StepStatus(
            key="vin", label="Vehicle identified",
            state="ok" if info["vin"] else "pending",
            detail=(f"VIN {info['vin']}." if info["vin"]
                    else "Connected, but the car did not report a VIN on any "
                         "rung (Mode 09, UDS F190, KWP ident zones)."),
        ),
    ]
    overall = "connected" if on_can else "partial"
    vehicle = Vehicle(vin=info["vin"]) if info["vin"] else None
    message = (
        f"Connected to the Peugeot over {name}. Read-only - nothing was written to the car."
        if on_can else
        f"{name} reached the car, but it is not on the 11-bit/500k CAN the module scan needs."
    )
    return ConnectionStatus(
        profile="peugeot", mode="real", simulated=False, overall=overall,
        steps=steps, vehicle=vehicle, message=message, timestamp=_now(),
    )
