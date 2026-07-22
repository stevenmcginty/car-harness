"""J2534 Pass-Thru adapter -- PLACEHOLDER.

A real SAE J2534 integration would load the vendor DLL and issue PassThru API
calls. This placeholder does NONE of that: ``test()`` only checks that the
configured DLL path exists and can be loaded via ``ctypes.WinDLL``. No PassThru
calls are made and nothing is written to any car.
"""

import ctypes
import os

from backend.adapters.base import Adapter
from backend.core.models import AdapterField, AdapterTestResult


class J2534Adapter(Adapter):
    id = "j2534"
    name = "J2534 Pass-Thru device (placeholder)"
    description = (
        "PLACEHOLDER for a SAE J2534 Pass-Thru pro device. Only checks the "
        "vendor DLL exists and loads -- no PassThru calls are made and nothing "
        "is written to any car."
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
            return AdapterTestResult(
                ok=False,
                detail=f"PLACEHOLDER: found {dll} but it failed to load ({exc}).",
                info={"dll": dll},
            )
        return AdapterTestResult(
            ok=True,
            detail=(
                f"PLACEHOLDER: {dll} exists and loaded. No PassThru calls were "
                "made -- full J2534 support is not implemented yet."
            ),
            info={"dll": dll},
        )
