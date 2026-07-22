"""ELM327 over Bluetooth.

On Windows a paired Bluetooth ELM327 is exposed as an outgoing COM port, so the
connectivity test is identical to the serial ELM327 -- this subclass only
changes the metadata and the field help text. Pair the adapter in Windows
first, then pick its outgoing COM port.
"""

from backend.adapters.elm327_serial import Elm327SerialAdapter
from backend.core.models import AdapterField


class Elm327BluetoothAdapter(Elm327SerialAdapter):
    id = "elm327-bluetooth"
    name = "ELM327 (Bluetooth)"
    description = (
        "A Bluetooth ELM327 dongle. Pair it in Windows first; Windows then "
        "exposes it as an outgoing COM port that this adapter talks to."
    )
    builtin = True
    fields = [
        AdapterField(
            key="port",
            label="Bluetooth COM port",
            type="comport",
            required=True,
            help="Pair the adapter in Windows first, then pick its outgoing COM port.",
        ),
    ]
