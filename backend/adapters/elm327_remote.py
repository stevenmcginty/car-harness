"""ELM327 reached through the phone relay.

The laptop is at home; the car isn't. The phone in the car holds the BLE link
to the dongle and relays raw ELM bytes to this server over its WebSocket, so
every tool here -- generic scan, live data, brand profiles, MCP -- works on
the far car exactly as it does on a local dongle.

There is nothing to configure: the adapter either has a phone behind it right
now or it doesn't. test() says which.
"""

from backend.adapters.base import Adapter
from backend.core.models import AdapterTestResult


class Elm327RemoteAdapter(Adapter):
    id = "elm327-remote"
    name = "ELM327 via phone relay"
    description = (
        "The car's own connection, beamed home: your phone sits in the car "
        "connected to the Veepeak (or any BLE/Bluetooth/WiFi dongle) and "
        "relays it to this desktop. Save this adapter while Relay mode is on "
        "in the phone app."
    )
    builtin = True
    fields = []

    @classmethod
    def test(cls, config: dict = None) -> AdapterTestResult:
        from backend.obd.remote_link import RelayError, hub

        st = hub.status()
        if not st["connected"]:
            return AdapterTestResult(
                ok=False,
                detail=(
                    "No phone is relaying right now. Open Car Harness in the "
                    "car, connect to the dongle there, and switch Relay on."
                ),
                info=st,
            )
        try:
            raw = hub.exchange(b"ATZ\r", timeout=10.0)
        except RelayError as exc:
            return AdapterTestResult(ok=False, detail=str(exc), info=st)
        text = raw.decode("ascii", errors="replace").replace(">", " ").strip()
        info = dict(st, reply=text)
        if "ELM327" in text.upper():
            return AdapterTestResult(
                ok=True,
                detail=f"The relayed dongle answered: {text!r} ({st['deviceName']}).",
                info=info,
            )
        if text:
            return AdapterTestResult(
                ok=False,
                detail=f"Relay is up but the dongle answered oddly: {text!r}.",
                info=info,
            )
        return AdapterTestResult(
            ok=False,
            detail="Relay is up but nothing answered the ATZ reset in 10s.",
            info=info,
        )
