"""Porsche profile: turns a generic ELM327 dongle into an API ConnectionStatus.

Like Mazda, Peugeot, VW, Mercedes, Fiat and JLR (and unlike BMW, which has a
native ENET transport), Porsche has no special cable in this harness -- it is
diagnosed over whatever ELM327-style adapter is selected, on the car's HS-CAN
diagnostic bus (or, on the oldest cars, its K-line). status("real", adapter)
runs a genuine, read-only check over that adapter: is the dongle there, does the
car answer, is it on the 11-bit/500k CAN Porsche module diagnostics use (or the
K-line the 996/986 use for generic EOBD), and can we read a VIN. status("sim")
returns a clearly-labelled fake so the UI can be demoed with no car present --
simulated data must NEVER leak into real mode.

Porsche note (docs/private/porsche-research-architecture.md §7): the module scan
targets the 2005-2016 Porsche-own CAN era on 11-bit/500k (997/987/981/991.1/970),
plus the VAG address block on the Cayenne 958 / Macan (MLB). 996/986 are K-line
KWP2000 -- the dongle reaches the DME on K1 (pin 7) for generic EOBD only; their
body modules are on a second K-line on pin 3 no ELM327 can reach. Cayenne 955/957
(VW TP2.0) and MY17+/Taycan (DoIP over Ethernet) give generic OBD only. The probe
reports whichever it sees, honestly.
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
        profile="porsche", mode="sim", simulated=True, overall="connected", steps=steps,
        vehicle=Vehicle(vin="SIMULATEDBOXSTER1"),
        message="SIMULATED - no real car connected.", timestamp=_now(),
    )


def _needs_adapter() -> ConnectionStatus:
    return ConnectionStatus(
        profile="porsche", mode="real", simulated=False, overall="disconnected",
        steps=[StepStatus(
            key="adapter", label="OBD adapter", state="fail",
            detail="Porsche is diagnosed over an ELM327-style dongle, not a native cable. "
                   "Pick a configured ELM327 adapter (or use Simulate).",
        )],
        vehicle=None,
        message="Select an ELM327-style adapter to check the Porsche connection.",
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
            profile="porsche", mode="real", simulated=False, overall="disconnected",
            steps=[
                StepStatus(key="adapter", label="OBD adapter", state="ok",
                           detail=f"{name} is configured."),
                StepStatus(key="car", label="Car responding", state="fail", detail=str(exc)),
            ],
            vehicle=None, message=str(exc), timestamp=_now(),
        )

    on_can = info["protocol_num"] in (6, None)
    on_kline = info["protocol_num"] in (3, 4, 5)

    if on_can:
        bus_state = "ok"
        bus_detail = (
            "On 11-bit/500k CAN - the Porsche driveline scan runs (2005-2016 Porsche-own "
            "CAN cars give engine depth on the standard block plus a best-effort Tiptronic/"
            "PDK probe, and a Cayenne 958 / Macan is best-effort walked on the VAG address "
            "block; Cayenne 955/957 TP2.0 and MY17+ DoIP cars give generic OBD only, and the "
            "scan says which it saw)."
        )
    elif on_kline:
        bus_state = "pending"
        bus_detail = (
            f"Car is on {info['protocol'] or 'K-line'} - that is a 996/986 (911/Boxster "
            "~1997-2005). The dongle reaches the DME/Tiptronic/alarm on K1 (pin 7) for "
            "generic EOBD only; the CAN module walk cannot run over K-line, and the ABS/"
            "airbag/cluster/HVAC body modules are on a second K-line on pin 3 no ELM327 can "
            "reach. Pre-2001 cars may pre-date EOBD."
        )
    else:
        bus_state = "fail"
        bus_detail = (
            f"Car is on {info['protocol'] or 'a non-standard bus'}; Porsche module "
            "diagnostics expect 11-bit/500k CAN (or K-line on the 996/986). If this is the "
            "Porsche, turn the ignition ON and retry."
        )

    steps = [
        StepStatus(key="adapter", label="OBD adapter", state="ok",
                   detail=f"{name}: ELM327 {info['firmware']} answered ({info['voltage']})."),
        StepStatus(key="car", label="Car responding", state="ok",
                   detail=f"Car answered on {info['protocol'] or 'CAN'}."),
        StepStatus(key="bus", label="Diagnostic CAN (module scan)",
                   state=bus_state, detail=bus_detail),
        StepStatus(
            key="vin", label="Vehicle identified",
            state="ok" if info["vin"] else "pending",
            detail=(f"VIN {info['vin']}." if info["vin"]
                    else "Connected, but the car did not report a VIN over generic OBD."),
        ),
    ]
    overall = "connected" if on_can else "partial"
    if on_can:
        message = (
            f"Connected to the Porsche over {name}. Read-only - nothing was written to the car."
        )
    elif on_kline:
        message = (
            f"{name} reached a 996/986 on K-line: generic EOBD on the DME (pin 7) is available, "
            "but the CAN module scan and the pin-3 body modules are out of reach on this dongle."
        )
    else:
        message = (
            f"{name} reached the car, but it is not on the 11-bit/500k CAN the module scan needs."
        )
    vehicle = Vehicle(vin=info["vin"]) if info["vin"] else None
    return ConnectionStatus(
        profile="porsche", mode="real", simulated=False, overall=overall,
        steps=steps, vehicle=vehicle, message=message, timestamp=_now(),
    )
