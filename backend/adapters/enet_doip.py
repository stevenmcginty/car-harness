"""ENET / DoIP adapter -- the BMW F/G-series native transport.

This wraps the existing, unchanged ``backend.core.interfaces`` and
``backend.core.doip`` code. ``test()`` only confirms that a link-local
(169.254.x.x) ENET adapter is up; it cannot fully verify DoIP without a car on
the wire, and says so honestly. ``raw_probe()`` performs the exact
interfaces+doip exchange the BMW profile uses today, so the profile's native
path and this adapter yield the identical probe result.
"""

from backend.adapters.base import Adapter
from backend.core import doip, interfaces
from backend.core.models import AdapterTestResult


class EnetDoipAdapter(Adapter):
    id = "enet-doip"
    name = "BMW ENET (Ethernet / DoIP)"
    description = (
        "BMW F/G-series over an ENET (Ethernet-to-OBD) cable using DoIP "
        "(ISO 13400). The link-local adapter is detected automatically, so "
        "there is nothing to configure."
    )
    builtin = True
    fields = []  # fully auto-detected -- no user configuration

    @classmethod
    def test(cls, config: dict = None) -> AdapterTestResult:
        candidate = interfaces.best_candidate()
        if candidate is None:
            return AdapterTestResult(
                ok=False,
                detail=(
                    "No link-local (169.254.x.x) Ethernet adapter found. Plug the "
                    "ENET cable into the car's OBD port and the laptop, then wait for a "
                    "169.254.x.x address. DoIP can only be fully confirmed with a car "
                    "present and the ignition on."
                ),
                info={"candidate": None},
            )
        return AdapterTestResult(
            ok=True,
            detail=(
                f"ENET adapter '{candidate['name']}' is up with link-local address "
                f"{candidate['ip']} (ENET cable detected). Note: without a car on the "
                "wire this only confirms the adapter, not a live DoIP link."
            ),
            info={"candidate": candidate},
        )

    @classmethod
    def raw_probe(cls, config: dict = None) -> dict:
        """The exact interfaces+doip exchange the BMW profile renders.

        Returns ``{"candidate": <best_candidate or None>, "probe": <doip probe>}``.
        This is a genuine, read-only UDP Vehicle Identification exchange.
        """
        candidate = interfaces.best_candidate()
        probe = doip.probe(candidate)
        return {"candidate": candidate, "probe": probe}
