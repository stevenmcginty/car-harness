"""ENET adapter -- the BMW native transport over an Ethernet-to-OBD cable.

F-series cars (2011-2018ish, like the F26 X4) answer HSFZ; G-series answer
DoIP (ISO 13400). ``raw_probe()`` tries HSFZ first, then DoIP, and reports
which protocol answered. ``test()`` only confirms that a link-local
(169.254.x.x) ENET adapter is up; it cannot fully verify the car link without
a car on the wire, and says so honestly.
"""

from backend.adapters.base import Adapter
from backend.core import doip, hsfz, interfaces
from backend.core.models import AdapterTestResult


class EnetDoipAdapter(Adapter):
    id = "enet-doip"
    name = "BMW ENET (Ethernet / HSFZ + DoIP)"
    description = (
        "BMW F/G-series over an ENET (Ethernet-to-OBD) cable. F-series answer "
        "HSFZ, G-series answer DoIP -- both are tried automatically, so "
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
        """Read-only identification probe: HSFZ (F-series) then DoIP (G-series).

        Returns ``{"candidate": <best_candidate or None>, "probe": <dict>}``
        where probe keeps the historical doip.probe() keys plus ``protocol``
        ("HSFZ" | "DoIP" | None) so the profile can say which one answered.
        """
        candidate = interfaces.best_candidate()

        ident = hsfz.identify(candidate)
        if ident["reached"]:
            probe = {
                "reached_doip": True,  # historical key: "the car answered"
                "protocol": "HSFZ",
                "sent": ident["sent"],
                "vin": ident["vin"],
                "logical_address": None,
                "eid": None,
                "gid": None,
                "raw_from_ip": ident["ip"],
                "bind_ip": ident["bind_ip"],
                "targets": ident["targets"],
                "detail": ident["detail"],
                "error": ident["error"],
            }
            return {"candidate": candidate, "probe": probe}

        probe = doip.probe(candidate)
        probe["protocol"] = "DoIP" if probe["reached_doip"] else None
        # If DoIP also failed, surface both attempts in the detail trail.
        if not probe["reached_doip"]:
            probe["sent"] = probe["sent"] or ident["sent"]
            probe["detail"] = (ident["detail"] + " " + probe["detail"]).strip()
            probe["error"] = probe["error"] or ident["error"]
        return {"candidate": candidate, "probe": probe}
