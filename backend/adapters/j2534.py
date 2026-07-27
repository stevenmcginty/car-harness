"""J2534 Pass-Thru adapter.

A J2534 device speaks a binary C API through a vendor DLL. ``test()`` here only
loads that DLL (a read-only connectivity check -- no PassThru calls, nothing
written to any car). The real diagnostics live in backend/obd/j2534_link.py: the
Peugeot / PSA module scan drives a J2534 link when this adapter is selected, so
a genuine PSA "DiagBox / Lexia" cable (wired to the pins-3/8 diagnostic CAN,
which a standard dongle can't reach) can scan the body modules too.

Windows-only, and the DLL's bitness must match the Python running the backend --
a 32-bit DLL will not load into 64-bit Python (WinError 193).
"""

import ctypes
import os

from backend.adapters.base import Adapter
from backend.core.models import AdapterField, AdapterTestResult


class J2534Adapter(Adapter):
    id = "j2534"
    name = "J2534 Pass-Thru cable"
    description = (
        "SAE J2534 pass-thru pro cable (incl. a PSA DiagBox/Lexia cable via a "
        "J2534 DLL). Used by the Peugeot module scan to reach the pins-3/8 "
        "diagnostic CAN. The test only loads the vendor DLL; real reads happen "
        "during a scan. Windows-only; DLL bitness must match the backend's Python."
    )
    builtin = True
    fields = [
        AdapterField(key="dll", label="J2534 DLL path", type="file", required=True),
    ]

    @classmethod
    def test(cls, config: dict = None) -> AdapterTestResult:
        config = config or {}
        dll = str(config.get("dll") or "").strip()
        if not dll:
            return AdapterTestResult(
                ok=False,
                detail="PLACEHOLDER: no J2534 DLL path was given.",
                info={},
            )
        if not os.path.isfile(dll):
            return AdapterTestResult(
                ok=False,
                detail=f"PLACEHOLDER: J2534 DLL not found at {dll}.",
                info={"dll": dll},
            )
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return AdapterTestResult(
                ok=False,
                detail="PLACEHOLDER: J2534 is Windows-only (ctypes.WinDLL unavailable).",
                info={"dll": dll},
            )
        try:
            win_dll(dll)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 193:
                return AdapterTestResult(
                    ok=False,
                    detail=(
                        f"{dll} is the wrong architecture for this Python -- a 32-bit "
                        "J2534 DLL needs 32-bit Python. Run the backend on a Python "
                        "matching the DLL's bitness."
                    ),
                    info={"dll": dll},
                )
            return AdapterTestResult(
                ok=False,
                detail=f"Found {dll} but it failed to load ({exc}).",
                info={"dll": dll},
            )
        return AdapterTestResult(
            ok=True,
            detail=(
                f"{dll} loaded. A Peugeot module scan on this adapter will drive "
                "real J2534 PassThru reads. (This test made no PassThru calls and "
                "did not touch a car -- run a scan to confirm the cable + car.)"
            ),
            info={"dll": dll},
        )
