"""Placeholder job module: EntryNav Firmware Flash (BMW X4 F26, HU_ENTRY).

This module performs NO flashing. It exists to prove the pluggable job-module
system end to end. A future version will wrap or launch E-Sys to reflash the
HU_ENTRY head unit; for now run() is a stub that does nothing to the car.
"""


def run(*args, **kwargs) -> dict:
    """Stub entry point. Returns a placeholder result; changes nothing."""
    return {
        "ok": False,
        "status": "placeholder",
        "message": "not implemented - will wrap/launch E-Sys",
    }
