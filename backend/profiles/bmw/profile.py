"""BMW profile: turns the ENET identification probe into an API ConnectionStatus.

status("real") runs a genuine, read-only identification probe -- HSFZ for
F-series, DoIP for G-series -- and reports exactly how far it got. status("sim")
returns a clearly-labelled fake so the UI can be demoed with no car present --
simulated data must NEVER leak into real mode.

The probe is identification-only; it cannot write to the car.
"""

from datetime import datetime, timezone

from backend.adapters.enet_doip import EnetDoipAdapter
from backend.core import vin as vin_decode
from backend.core.models import ConnectionStatus, StepStatus, Vehicle


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def status(mode: str = "real", adapter: dict = None) -> ConnectionStatus:
    """Connection status for the BMW profile.

    ``adapter`` is None for the native auto-detected ENET/DoIP path (behaviour
    identical to v1). When an adapter instance dict is supplied the check runs
    over that adapter instead.
    """
    if mode == "sim":
        return _simulated()
    if adapter is None:
        return _real()
    return _real_over_adapter(adapter)


def _simulated() -> ConnectionStatus:
    steps = [
        StepStatus(
            key="adapter",
            label="Ethernet adapter",
            state="ok",
            detail="SIMULATED - pretend ENET adapter is up on 169.254.10.5.",
        ),
        StepStatus(
            key="link",
            label="Link to car",
            state="ok",
            detail="SIMULATED - pretend DoIP discovery was broadcast.",
        ),
        StepStatus(
            key="doip",
            label="Car responding",
            state="ok",
            detail="SIMULATED - pretend a vehicle announcement came back.",
        ),
        StepStatus(
            key="vin",
            label="Vehicle identified",
            state="ok",
            detail="SIMULATED - fake VIN, no real car connected.",
        ),
    ]
    return ConnectionStatus(
        profile="bmw",
        mode="sim",
        simulated=True,
        overall="connected",
        steps=steps,
        vehicle=Vehicle(
            vin="SIMULATED00000BMW",
            logicalAddress="0x0010",
            eid="0011223344aa",
            gid="0011223344bb",
        ),
        message="SIMULATED - no real car connected.",
        timestamp=_now(),
    )


def _real() -> ConnectionStatus:
    probe_result = EnetDoipAdapter.raw_probe()  # wraps the same interfaces+doip code
    candidate = probe_result["candidate"]
    probe = probe_result["probe"]  # a genuine UDP exchange, every time

    adapter_ok = candidate is not None
    sent_ok = probe["sent"]
    doip_ok = probe["reached_doip"]
    vin = probe.get("vin")
    vin_ok = doip_ok and bool(vin)

    steps = []

    # 1) Ethernet adapter -- do we have a link-local ENET adapter at all?
    if adapter_ok:
        steps.append(
            StepStatus(
                key="adapter",
                label="Ethernet adapter",
                state="ok",
                detail=f"{candidate['name']} is up with link-local address "
                f"{candidate['ip']} (ENET cable detected).",
            )
        )
    else:
        steps.append(
            StepStatus(
                key="adapter",
                label="Ethernet adapter",
                state="fail",
                detail="No link-local (169.254.x.x) Ethernet adapter found. Connect the "
                "ENET cable between the car's OBD port and the laptop, then wait for a "
                "169.254.x.x address.",
            )
        )

    # 2) Link to car -- could we actually put the discovery datagram on the wire?
    if not adapter_ok:
        link_state, link_detail = (
            "fail",
            "No candidate ENET adapter, so no link to the car could be established.",
        )
    elif not sent_ok:
        link_state, link_detail = (
            "fail",
            "Could not transmit the DoIP discovery request: "
            f"{probe.get('error') or probe.get('detail') or 'unknown error'}",
        )
    else:
        link_state, link_detail = (
            "ok",
            f"ENET discovery (HSFZ + DoIP) sent from {probe.get('bind_ip')} to "
            f"{', '.join(probe.get('targets', []))}.",
        )
    steps.append(StepStatus(key="link", label="Link to car", state=link_state, detail=link_detail))

    # 3) Car responding -- did an HSFZ or DoIP identification come back?
    if doip_ok:
        doip_state = "ok"
        doip_detail = (
            f"{probe.get('protocol') or 'ENET'} identification received from "
            f"{probe.get('raw_from_ip')}"
        )
        # "F-series"/"G-series" are BMW model generations, so they may only be
        # claimed once the VIN says this really is a BMW. The ENET cable reaches
        # other makes on the same pins -- a Porsche Taycan answers this very
        # broadcast -- and stamping a BMW generation on a Porsche VIN is how the
        # connection card ended up confidently wrong about the car in front of it.
        make = vin_decode.make_from_vin(probe.get("vin"))
        if make in ("BMW", "MINI"):
            doip_detail += (
                " (F-series)." if probe.get("protocol") == "HSFZ"
                else " (G-series)." if probe.get("protocol") == "DoIP"
                else "."
            )
        elif make:
            doip_detail += (
                f". This is a {make}, not a BMW -- switch the manufacturer to "
                "read it properly."
            )
        else:
            doip_detail += "."
    else:
        doip_state = "fail"
        if not adapter_ok:
            doip_detail = probe.get("detail") or "No response (no ENET adapter present)."
        else:
            doip_detail = probe.get("detail") or (
                "No HSFZ or DoIP response before the timeout."
            )
            doip_detail += (
                " Check the ignition is on and the OBD cable is fully seated. If Windows "
                "Firewall prompted for Python, choose Allow."
            )
    steps.append(StepStatus(key="doip", label="Car responding", state=doip_state, detail=doip_detail))

    # 4) Vehicle identified -- did we parse a VIN out of the announcement?
    if vin_ok:
        vin_state = "ok"
        vin_detail = f"VIN {vin} at logical address {probe.get('logical_address')}."
    else:
        vin_state = "fail"
        vin_detail = (
            "Car responded but no VIN was present in the identification."
            if doip_ok
            else "The vehicle will be identified once the car answers."
        )
    steps.append(StepStatus(key="vin", label="Vehicle identified", state=vin_state, detail=vin_detail))

    # Overall + guidance.
    vehicle = None
    if doip_ok and vin:
        overall = "connected"
        message = (
            f"Connected to vehicle {vin} over {probe.get('protocol') or 'ENET'}. "
            "Read-only identification only - nothing was written to the car."
        )
        vehicle = Vehicle(
            vin=vin,
            logicalAddress=probe.get("logical_address"),
            eid=probe.get("eid"),
            gid=probe.get("gid"),
        )
    elif adapter_ok:
        overall = "partial"
        message = (
            "ENET adapter is up but the car answered neither HSFZ (F-series) nor DoIP "
            "(G-series). Turn the ignition on, reseat the OBD cable, and make sure "
            "Python is allowed through Windows Firewall, then retry."
        )
    else:
        overall = "disconnected"
        message = (
            "No ENET adapter detected. Plug the ENET cable into the car's OBD port and the "
            "laptop's Ethernet port, then retry."
        )

    return ConnectionStatus(
        profile="bmw",
        mode="real",
        simulated=False,
        overall=overall,
        steps=steps,
        vehicle=vehicle,
        message=message,
        timestamp=_now(),
    )


def _real_over_adapter(adapter: dict) -> ConnectionStatus:
    """Run the real check over a user-configured adapter instance.

    ENET/DoIP is BMW's native transport, so it reuses ``_real()`` and yields the
    identical result. Any other adapter (ELM327, etc.) gets a shallow "did the
    dongle answer" check for now.
    """
    type_id = (adapter or {}).get("typeId")
    if type_id == "enet-doip":
        return _real()
    return _shallow_adapter_status(adapter, type_id)


def _shallow_adapter_status(adapter: dict, type_id: str) -> ConnectionStatus:
    from backend.adapters import registry as adapter_registry

    name = (adapter or {}).get("name") or type_id or "adapter"
    config = (adapter or {}).get("config") or {}
    adapter_cls = adapter_registry.get_type(type_id)

    if adapter_cls is None:
        return ConnectionStatus(
            profile="bmw",
            mode="real",
            simulated=False,
            overall="disconnected",
            steps=[
                StepStatus(
                    key="adapter",
                    label="OBD adapter",
                    state="fail",
                    detail=f"Unknown adapter type '{type_id}'.",
                )
            ],
            vehicle=None,
            message=f"Unknown adapter type '{type_id}'.",
            timestamp=_now(),
        )

    result = adapter_cls.probe(config)  # AdapterTestResult
    ok = result.ok
    steps = [
        StepStatus(
            key="adapter",
            label="OBD adapter",
            state="ok" if ok else "fail",
            detail=result.detail,
        ),
        StepStatus(
            key="vin",
            label="Vehicle identified",
            state="pending",
            detail="Reading the VIN and diagnostics over this adapter is not implemented yet.",
        ),
    ]
    if ok:
        overall = "partial"
        message = (
            f"{name}: the dongle answered. This is a shallow connectivity check only "
            "- reading the VIN and running diagnostics over this adapter is not "
            "implemented yet."
        )
    else:
        overall = "disconnected"
        message = f"{name}: {result.detail}"

    return ConnectionStatus(
        profile="bmw",
        mode="real",
        simulated=False,
        overall=overall,
        steps=steps,
        vehicle=None,
        message=message,
        timestamp=_now(),
    )
