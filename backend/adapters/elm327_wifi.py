"""ELM327 over WiFi (TCP).

WiFi ELM327 dongles run their own access point (usually SSID 'WiFi_OBDII')
and listen on 192.168.0.10:35000. The laptop must be joined to that network.
``test()`` opens the TCP socket, sends ATZ and checks for the ELM327 banner --
a connectivity check only; nothing is written to any car beyond the reset.
"""

from backend.adapters.base import Adapter
from backend.obd.elm327 import is_elm_banner
from backend.core.models import AdapterField, AdapterTestResult
from backend.obd.transport import TcpTransport, TransportError


class Elm327WifiAdapter(Adapter):
    id = "elm327-wifi"
    name = "ELM327 (WiFi)"
    description = (
        "A WiFi ELM327 dongle. Join the laptop to the dongle's WiFi network "
        "(usually 'WiFi_OBDII') first; almost every model listens on "
        "192.168.0.10 port 35000."
    )
    builtin = True
    fields = [
        AdapterField(
            key="host",
            label="IP address",
            type="ip",
            required=False,
            help="Default 192.168.0.10 fits almost every WiFi ELM327.",
        ),
        AdapterField(
            key="port",
            label="Port",
            type="text",
            required=False,
            help="Default 35000. A few clones use 23.",
        ),
    ]

    @classmethod
    def test(cls, config: dict = None) -> AdapterTestResult:
        config = config or {}
        host = str(config.get("host") or "192.168.0.10").strip()
        try:
            port = int(config.get("port") or 35000)
        except (TypeError, ValueError):
            port = 35000

        transport = TcpTransport(host, port)
        try:
            transport.open()
            raw = transport.exchange(b"ATZ\r", timeout=6.0)
        except TransportError as exc:
            return AdapterTestResult(ok=False, detail=str(exc), info={"host": host, "port": port})
        finally:
            transport.close()

        text = raw.decode("ascii", errors="replace").replace(">", " ").strip()
        if is_elm_banner(text):
            return AdapterTestResult(
                ok=True,
                detail=f"ELM327 answered at {host}:{port}: {text!r}.",
                info={"host": host, "port": port, "reply": text},
            )
        if text:
            return AdapterTestResult(
                ok=False,
                detail=(
                    f"{host}:{port} answered but did not identify as an ELM327 "
                    f"(got {text!r})."
                ),
                info={"host": host, "port": port, "reply": text},
            )
        return AdapterTestResult(
            ok=False,
            detail=(
                f"Connected to {host}:{port} but nothing answered the ATZ reset "
                "within 6s."
            ),
            info={"host": host, "port": port},
        )
