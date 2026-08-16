"""Persist real-car tool results as the dataset under captures/.

The peugeot oil job set the pattern (backend/tools/peugeot_oil_session.py):
every real run lands on disk as captures/<brand>/<car>-<kind>-<utc>.json so
the dataset grows with the work and yesterday's capture is still there when
the diagnosis needs it. This module is the same habit for the generic brand
routes and MCP tools -- called after the profile module has already produced
its result dict.

Two rules, both load-bearing:

1. Simulated results are NEVER saved. Every profile marks its fake with
   ``"simulated": True``; save() returns None for those so demo runs can
   never contaminate the real-car dataset.
2. Only brands listed in KINDS are saved -- an explicit opt-in per brand and
   kind, so a future profile doesn't silently start filling the disk.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from backend.core import registry

CAPTURES_DIR = registry.PROJECT_ROOT / "captures"

# brand -> the kinds of run worth keeping. Read-only results only: a fault
# clear or a reset leaves no dataset value behind it.
KINDS: dict[str, tuple[str, ...]] = {
    "jlr": ("scan", "discover", "sweep", "adblue-health", "drive-log"),
}


def _car_label(result: dict) -> str:
    """A filename-safe car label: the VIN when the run read one, else 'car'."""
    vin = ((result.get("vehicle") or {}).get("vin")) or ""
    label = re.sub(r"[^A-Za-z0-9]", "-", vin.strip())[:11]
    return label or "car"


def drive_log_csv(samples: list[dict]) -> str:
    """The drive log's samples as CSV: fixed columns, then every DID column,
    then every decoded aftertreatment column that appeared -- blanks where a
    tick didn't read that value (the every-fifth-tick baseline leaves ragged
    columns; blanks are the honest representation, not zero)."""
    fixed = ["t", "engine_rpm", "coolant_c", "module_voltage"]
    did_keys = sorted(
        {k for s in samples if isinstance(s, dict) for k in s if k.startswith("did_")}
    )
    extra_keys = sorted(
        {k for s in samples if isinstance(s, dict) for k in s
         if k not in fixed and not k.startswith("did_")}
    )
    columns = fixed + did_keys + extra_keys

    def cell(value) -> str:
        if value is None:
            return ""
        return str(value)

    lines = [",".join(columns)]
    for sample in samples:
        lines.append(",".join(cell(sample.get(c)) for c in columns))
    return "\r\n".join(lines) + "\r\n"


def save(
    brand: str,
    kind: str,
    result: dict,
    adapter: dict | None = None,
    note: str = "",
) -> dict | None:
    """Write one real-run result to captures/<brand>/, JSON (+ CSV sidecar
    for a drive log). Returns the paths stamped, or None when nothing was
    saved (sim run, brand/kind not opted in, or a failed run with no car)."""
    if not isinstance(result, dict) or result.get("simulated"):
        return None
    if kind not in KINDS.get(brand, ()):
        return None
    if not result.get("ok"):
        # A failed run still goes in -- an honest dataset keeps its misses --
        # unless it produced nothing at all to keep.
        if not result.get("modules") and not result.get("samples") and not (
            result.get("answered") or result.get("found")
        ):
            return None

    out_dir = CAPTURES_DIR / brand
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    car = _car_label(result)
    stem = out_dir / f"{car}-{kind}-{stamp}"

    saved = {
        "json": str(stem.with_suffix(".json")),
        "csv": None,
    }
    stem.with_suffix(".json").write_text(
        json.dumps(
            {
                "car": car,
                "kind": kind,
                "note": note,
                "adapter": (adapter or {}).get("name"),
                "savedAt": datetime.now(timezone.utc).isoformat(),
                "result": result,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if kind == "drive-log" and isinstance(result.get("samples"), list):
        csv_path = stem.with_suffix(".csv")
        csv_path.write_text(drive_log_csv(result["samples"]), encoding="utf-8")
        saved["csv"] = str(csv_path)
    return saved
