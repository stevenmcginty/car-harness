"""Jaguar / Land Rover profile: turns a generic ELM327 dongle into an API
ConnectionStatus.

Like Mazda, Peugeot, VW, Mercedes and Fiat (and unlike BMW, which has a native
ENET transport), JLR has no special cable in this harness -- it is diagnosed
over whatever ELM327-style adapter is selected, on the car's HS-CAN diagnostic
bus. status("real", adapter) runs a genuine, read-only check over that adapter:
is the dongle there, does the car answer, is it on the 11-bit/500k CAN JLR
module diagnostics use, and can we read a VIN. status("sim") returns a
clearly-labelled fake so the UI can be demoed with no car present -- simulated
data must NEVER leak into real mode.

JLR note (docs/private/jlr-research-architecture.md): the module scan targets
the 2006-2016 Ford/PLA CAN era on 11-bit/500k. MY17+ cars moved manufacturer
diagnostics to DoIP (Ethernet) -- generic OBD still answers, module depth
doesn't; Evoque L538 / Disco Sport L550 gateway-filter the port the same way.
S-Type/X-Type (J1850) and BMW-era L322 may not connect on a CAN-only dongle at
all. The probe reports whichever it sees, honestly.
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
        profile="jlr", mode="sim", simulated=True, overall="connected", steps=steps,
        vehicle=Vehicle(vin="SIMULATEDDISCO401"),
        message="SIMULATED - no real car connected.", timestamp=_now(),
    )


def _needs_adapter() -> ConnectionStatus:
    return ConnectionStatus(
        profile="jlr", mode="real", simulated=False, overall="disconnected",
        steps=[StepStatus(
            key="adapter", label="OBD adapter", state="fail",
            detail="Jaguar / Land Rover is diagnosed over an ELM327-style dongle, not a native "
                   "cable. Pick a configured ELM327 adapter (or use Simulate).",
        )],
        vehicle=None,
        message="Select an ELM327-style adapter to check the Jaguar / Land Rover connection.",
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
            profile="jlr", mode="real", simulated=False, overall="disconnected",
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
            detail=("On 11-bit/500k CAN - the JLR driveline scan runs (2006-2016 CAN-era cars "
                    "give engine + gearbox + e-diff depth; MY17+ DoIP cars and the gateway-"
                    "filtered Evoque L538 / Disco Sport L550 give generic OBD only, and the "
                    "scan says which it saw)."
                    if on_can else
                    f"Car is on {info['protocol'] or 'a non-standard bus'}; JLR module "
                    "diagnostics expect 11-bit/500k CAN. Early S-Type/X-Type are J1850 and "
                    "the 2002-05 BMW-era L322 often won't talk to a CAN dongle at all."),
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
        f"Connected to the Jaguar / Land Rover over {name}. Read-only - nothing was written "
        "to the car."
        if on_can else
        f"{name} reached the car, but it is not on the 11-bit/500k CAN the module scan needs."
    )
    return ConnectionStatus(
        profile="jlr", mode="real", simulated=False, overall=overall,
        steps=steps, vehicle=vehicle, message=message, timestamp=_now(),
    )
