"""ELM327 over a serial / USB COM port (via pyserial).

``test()`` opens the port, writes the ELM327 reset command ``ATZ\\r`` and reads
back the reply for a second or two. It is ok only if the reply identifies as an
ELM327. It fails honestly when the port does not exist, cannot be opened, or
nothing answers. This is a connectivity check only -- no OBD diagnostics are
performed and nothing is written to any car beyond the ELM327 reset.
"""

from backend.adapters.base import Adapter
from backend.core.models import AdapterField, AdapterTestResult


class Elm327SerialAdapter(Adapter):
    id = "elm327-serial"
    name = "ELM327 (USB / serial)"
    description = "A generic ELM327 OBD-II dongle on a USB or serial COM port."
    builtin = True
    fields = [
        AdapterField(key="port", label="COM port", type="comport", required=True),
        AdapterField(
            key="baud",
            label="Baud rate",
            type="select",
            required=False,
            options=["38400", "115200", "9600"],
        ),
    ]

    DEFAULT_BAUD = 38400
    READ_TIMEOUT = 2.0  # seconds to wait for the ATZ reply

    @classmethod
    def test(cls, config: dict = None) -> AdapterTestResult:
        # Imported lazily so the module still imports on machines without pyserial
        # (the connectivity test is what actually needs it).
        import serial

        config = config or {}
        port = str(config.get("port") or "").strip()
        if not port:
            return AdapterTestResult(
                ok=False, detail="No COM port specified.", info={}
            )
        try:
            baud = int(config.get("baud") or cls.DEFAULT_BAUD)
        except (TypeError, ValueError):
            baud = cls.DEFAULT_BAUD

        try:
            ser = serial.Serial(port=port, baudrate=baud, timeout=cls.READ_TIMEOUT)
        except serial.SerialException as exc:
            return AdapterTestResult(
                ok=False,
                detail=(
                    f"Could not open {port}: {exc}. Check the adapter is plugged in "
                    "and that this is the right COM port."
                ),
                info={"port": port, "baud": baud},
            )
        except Exception as exc:  # e.g. bad port string / OS error
            return AdapterTestResult(
                ok=False,
                detail=f"Could not open {port}: {exc}.",
                info={"port": port, "baud": baud},
            )

        try:
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            ser.write(b"ATZ\r")
            raw = ser.read(256)  # blocks up to READ_TIMEOUT for the reply
        except Exception as exc:
            return AdapterTestResult(
                ok=False,
                detail=f"{port} opened but the ATZ exchange failed: {exc}.",
                info={"port": port, "baud": baud},
            )
        finally:
            try:
                ser.close()
            except Exception:
                pass

        text = raw.decode("ascii", errors="replace").strip()
        if "ELM327" in text.upper():
            return AdapterTestResult(
                ok=True,
                detail=f"ELM327 answered on {port}: {text!r}.",
                info={"port": port, "baud": baud, "reply": text},
            )
        if raw:
            return AdapterTestResult(
                ok=False,
                detail=(
                    f"{port} opened and something answered, but it did not identify "
                    f"as an ELM327 (got {text!r})."
                ),
                info={"port": port, "baud": baud, "reply": text},
            )
        return AdapterTestResult(
            ok=False,
            detail=(
                f"{port} opened but nothing answered within {cls.READ_TIMEOUT:.0f}s. "
                "Is an ELM327 adapter connected and powered?"
            ),
            info={"port": port, "baud": baud},
        )
