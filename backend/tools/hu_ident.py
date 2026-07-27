"""One-shot: read the head unit's identity so we know WHICH .prg it needs.

READ-ONLY. Sends nothing but 0x22 ReadDataByIdentifier. Cannot change the car.

    .venv\\Scripts\\python.exe -m backend.tools.hu_ident [ip]

Default ip 169.254.21.23 -- the ZGW address the F56 came up on.

WHY: g_mmi.grp resolves the head-unit variant by reading F150 and matching the
3-byte SGBD index. F150 is also one of the very few DIDs this family answers at
all -- F187/F191/F189/F197 all return NRC 0x31, and KWP 0x1A returns NRC 0x11,
so text-matching a variant name off a UDS scan is a dead end. F150 is the way.

Known SGBD indexes, read out of the decompiled SGBDs (docs/private/sgbd/):

    0F1BE0 -> entrynav.prg   Entry HeadUnit mit und ohne NAV, addr 63
    0F1CD0 -> rad_ukl.prg    Radio UKL -- F55/F56/F45/F48

Our F56 answered 0F1A30, which is NEITHER of those. So do not assume from the
badge on the dash. If you get an index that isn't in the table, that is a real
finding -- record it in profiles/bmw/head_units.json "seen" and match it against
the local .prg set once C:\\EDIABAS\\ECU\\ exists. Or sidestep the whole question
and drive the job through g_mmi.grp, which resolves the variant itself.
"""

from __future__ import annotations

import sys

from backend.core.hsfz import HsfzClient
from backend.core import uds

HU = 0x63

# Index -> .prg, from the decompiled SGBD headers. Add rows as we learn them.
KNOWN_INDEXES = {
    "0F1BE0": "entrynav.prg  (Entry HeadUnit mit und ohne NAV)",
    "0F1CD0": "rad_ukl.prg   (Radio UKL -- F55/F56/F45/F48)",
}

DIDS = [
    (0xF150, "SGBD index  <-- THE ANSWER"),
    (0xF18C, "ECU serial number"),
    (0xF18B, "manufacturing date"),
    (0xF190, "VIN"),
    (0xF186, "active diagnostic session"),
    (0xF101, "SVK (software/hardware inventory)"),
]


def main() -> int:
    ip = sys.argv[1] if len(sys.argv) > 1 else "169.254.21.23"
    print(f"connecting to {ip}:6801, head unit 0x{HU:02X}")
    print("read-only -- 0x22 ReadDataByIdentifier only, nothing is written\n")

    sgbd_index = None
    try:
        with HsfzClient(ip) as client:
            for did, label in DIDS:
                try:
                    data = uds.read_did(client, HU, did)
                    hexs = data.hex().upper()
                    ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
                    if did == 0xF150:
                        sgbd_index = hexs
                    print(f"  {did:04X}  {label}")
                    print(f"        hex   {hexs}")
                    print(f"        ascii {ascii_}")
                except Exception as exc:            # noqa: BLE001
                    print(f"  {did:04X}  {label}")
                    print(f"        -- no answer ({type(exc).__name__}: {exc})")
                print()
    except Exception as exc:                        # noqa: BLE001
        print(f"could not connect: {exc}")
        print("is the ignition ON? the adapter reads Disconnected with the car")
        print("asleep and comes up 100 Mbps seconds after ignition. if the ASIX")
        print("dongle has hung, physically unplug and replug the USB.")
        return 1

    print("-" * 60)
    if sgbd_index is None:
        print("F150 did not answer -- without it the variant cannot be resolved.")
        print("Load g_mmi.grp and let EDIABAS work it out instead.")
        return 1

    match = KNOWN_INDEXES.get(sgbd_index)
    if match:
        print(f"SGBD index {sgbd_index} -> {match}")
        print("That is the .prg to pull out of C:\\EDIABAS\\ECU\\.")
    else:
        print(f"SGBD index {sgbd_index} is NOT in our table yet.")
        print("Not a failure -- a finding. Record it in head_units.json 'seen',")
        print("then either match it against the local .prg set or just drive the")
        print("job through g_mmi.grp, which resolves the variant on its own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
