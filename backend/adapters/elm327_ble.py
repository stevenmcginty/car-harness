"""ELM327 over Bluetooth Low Energy (BLE) -- e.g. the Veepeak OBDCheck BLE.

Why this is separate from the Classic-Bluetooth ELM327 adapter: a *Classic*
Bluetooth ELM327 (SPP/RFCOMM) is exposed by Windows as an outgoing COM port, so
it can reuse the serial adapter. A *BLE* dongle is NOT -- Windows never gives a
BLE device a COM port. We must speak GATT to it directly, which the shared
BleUartLink (backend/obd/ble_link.py) does with the ``bleak`` library.

The critical lesson (verified live against a real VEEPEAK): the write and
notify characteristics MUST come from the same vendor UART service (FFF0 on
the Veepeak -- notify FFF1, write FFF2). Picking the first notify
characteristic on the whole device grabs the standard GATT "Service Changed"
channel and the ELM's replies are never seen.

``test()`` is a read-only connectivity check; nothing is written to any car
beyond the ELM327's own reset command.

IMPORTANT (told to the user): an OBD dongle is powered by the car's OBD port
(pin 16). Sitting in its box on a desk it is UNPOWERED and will not advertise
over Bluetooth -- so this test can only succeed with the dongle plugged into a
car. The dongle itself answers with the ignition off; reading the car's data
needs the ignition on.
"""

from backend.adapters.base import Adapter
from backend.core.models import AdapterField, AdapterTestResult


class Elm327BleAdapter(Adapter):
    id = "elm327-ble"
    name = "ELM327 (Bluetooth LE)"
    description = (
        "A Bluetooth Low Energy ELM327 dongle such as the Veepeak OBDCheck BLE. "
        "Talks GATT directly (via bleak), so -- unlike a Classic-Bluetooth "
        "dongle -- it does NOT need pairing or a COM port. The dongle only "
        "powers on when plugged into a car's OBD port."
    )
    builtin = True
    fields = [
        AdapterField(
            key="address",
            label="BLE address (optional)",
            type="text",
            required=False,
            help="Exact Bluetooth address of the dongle. Leave blank to scan by name.",
        ),
        AdapterField(
            key="name",
            label="Device name contains",
            type="text",
            required=False,
            help="Advertised-name fragment, e.g. 'OBD' or 'Veepeak'. Blank = try common OBD names.",
        ),
    ]

    @classmethod
    def test(cls, config: dict = None) -> AdapterTestResult:
        try:
            from backend.obd.ble_link import BleUartLink
        except Exception:
            return AdapterTestResult(
                ok=False,
                detail=(
                    "The 'bleak' Bluetooth-LE library is not installed. Run "
                    "`.venv\\Scripts\\python.exe -m pip install bleak` and try again."
                ),
                info={},
            )

        config = config or {}
        link = BleUartLink(
            address=str(config.get("address") or "").strip(),
            name_hint=str(config.get("name") or "").strip(),
        )
        try:
            info = link.open()
            raw = link.exchange(b"ATZ\r", timeout=8.0)
        except Exception as exc:
            link.close()
            return AdapterTestResult(ok=False, detail=str(exc), info={})

        text = raw.decode("ascii", errors="replace").replace(">", " ").strip()
        base_info = dict(info)
        base_info["reply"] = text
        try:
            if "ELM327" in text.upper():
                label = info.get("name") or info.get("address") or "dongle"
                return AdapterTestResult(
                    ok=True,
                    detail=(
                        f"ELM327 answered over BLE ({label}, UART service "
                        f"{info.get('service')}): {text!r}."
                    ),
                    info=base_info,
                )
            if text:
                return AdapterTestResult(
                    ok=False,
                    detail=(
                        f"Connected and it answered, but it did not identify as an "
                        f"ELM327 (got {text!r})."
                    ),
                    info=base_info,
                )
            return AdapterTestResult(
                ok=False,
                detail=(
                    "Connected but nothing answered within 8s of the ATZ reset "
                    f"(tried UART service {info.get('service')}, write "
                    f"{info.get('write')}, notify {info.get('notify')})."
                ),
                info=base_info,
            )
        finally:
            link.close()
