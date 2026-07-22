"""Filesystem-driven discovery of manufacturer profiles and job modules.

Layout:
    backend/profiles/<profile>/profile.json          -- a manufacturer profile
    backend/profiles/<profile>/profile.py            -- its status() probe
    backend/profiles/<profile>/modules/<id>/module.json  -- built-in modules
    modules_store/<id>/module.json                   -- user-imported modules

Built-in modules ship inside a profile and cannot be removed. Imported modules
live in modules_store/ and are deletable to reclaim disk.
"""

import importlib.util
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

# registry.py -> core -> backend -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = PROJECT_ROOT / "backend" / "profiles"
MODULES_STORE = PROJECT_ROOT / "modules_store"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _dir_size(path: Path) -> int:
    """Sum of file sizes under a folder (on-disk footprint of a module)."""
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _read_json(path: Path) -> dict:
    # utf-8-sig tolerates a UTF-8 BOM (common in module.json authored on Windows)
    # while still reading plain UTF-8 correctly.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _module_record(mod_dir: Path, builtin_default: bool) -> dict:
    """Build a full module record from a module folder containing module.json."""
    data = _read_json(mod_dir / "module.json")
    mod_id = data.get("id", mod_dir.name)
    return {
        "id": mod_id,
        "profile": data.get("profile", ""),
        "name": data.get("name", mod_id),
        "description": data.get("description", ""),
        "installed": data.get("installed", True),
        "builtin": data.get("builtin", builtin_default),
        "version": data.get("version"),
        "status": data.get("status"),
        "requires": data.get("requires", []),
        "entry": data.get("entry"),
        "sizeBytes": _dir_size(mod_dir),
        "path": str(mod_dir),
    }


def _iter_builtin_module_dirs():
    """Yield every <profile>/modules/<id> folder that has a module.json."""
    if not PROFILES_DIR.is_dir():
        return
    for profile_dir in sorted(PROFILES_DIR.iterdir()):
        modules_dir = profile_dir / "modules"
        if not modules_dir.is_dir():
            continue
        for mod_dir in sorted(modules_dir.iterdir()):
            if (mod_dir / "module.json").is_file():
                yield mod_dir


def _iter_store_module_dirs():
    """Yield every modules_store/<id> folder that has a module.json."""
    if not MODULES_STORE.is_dir():
        return
    for mod_dir in sorted(MODULES_STORE.iterdir()):
        if (mod_dir / "module.json").is_file():
            yield mod_dir


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def list_modules(profile: Optional[str] = None) -> List[dict]:
    """All job modules (built-in + imported), optionally filtered by profile."""
    records: List[dict] = []
    for mod_dir in _iter_builtin_module_dirs():
        records.append(_module_record(mod_dir, builtin_default=True))
    for mod_dir in _iter_store_module_dirs():
        records.append(_module_record(mod_dir, builtin_default=False))
    if profile is not None:
        records = [r for r in records if r["profile"] == profile]
    return records


def list_profiles() -> List[dict]:
    """Every manufacturer profile with a compact summary of its modules."""
    profiles: List[dict] = []
    if not PROFILES_DIR.is_dir():
        return profiles
    all_modules = list_modules()
    for profile_dir in sorted(PROFILES_DIR.iterdir()):
        profile_json = profile_dir / "profile.json"
        if not profile_json.is_file():
            continue
        data = _read_json(profile_json)
        pid = data.get("id", profile_dir.name)
        summaries = [
            {
                "id": m["id"],
                "name": m["name"],
                "description": m["description"],
                "installed": m["installed"],
                "builtin": m["builtin"],
                "sizeBytes": m["sizeBytes"],
                "status": m["status"],
            }
            for m in all_modules
            if m["profile"] == pid
        ]
        profiles.append(
            {
                "id": pid,
                "name": data.get("name", pid),
                "transport": data.get("transport", ""),
                "description": data.get("description", ""),
                "panel": data.get("panel"),
                "modules": summaries,
            }
        )
    return profiles


_probe_cache: dict = {}


def load_profile_probe(profile_id: str):
    """Dynamically import a profile's probe module (its profile.py) by file path.

    Returns the imported module (expected to expose ``status(mode)``) or None if
    the profile or its probe file is missing or fails to import. Cached per id
    so /api/status doesn't re-execute the module on every poll.
    """
    if profile_id in _probe_cache:
        return _probe_cache[profile_id]
    profile_dir = PROFILES_DIR / profile_id
    profile_json = profile_dir / "profile.json"
    if not profile_json.is_file():
        return None
    data = _read_json(profile_json)
    probe_file = data.get("probe", "profile.py")
    probe_path = profile_dir / probe_file
    if not probe_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        f"carharness_profile_{profile_id}", probe_path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None  # a broken profile.py must not 500 every /api/status call
    _probe_cache[profile_id] = module
    return module


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip, refusing any member that would escape ``dest`` (zip-slip)."""
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError(f"Unsafe path in zip archive: {member}")
    zf.extractall(dest)


def _find_module_root(base: Path) -> Optional[Path]:
    """Locate the folder containing module.json (top level, or shallowest match)."""
    if (base / "module.json").is_file():
        return base
    candidates = sorted(base.rglob("module.json"), key=lambda p: len(p.parts))
    return candidates[0].parent if candidates else None


def import_module(source_path: str) -> dict:
    """Import a job module from a folder or .zip on disk into modules_store/.

    Validates that a module.json exists, copies the module folder to
    modules_store/<id>/ and returns the new module record.
    """
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"Import source not found: {source}")

    tmp_extract: Optional[Path] = None
    try:
        if source.is_file() and source.suffix.lower() == ".zip":
            tmp_extract = Path(tempfile.mkdtemp(prefix="carharness_import_"))
            with zipfile.ZipFile(source) as zf:
                _safe_extract(zf, tmp_extract)
            mod_root = _find_module_root(tmp_extract)
        elif source.is_dir():
            mod_root = _find_module_root(source)
        else:
            raise ValueError("Import source must be a folder or a .zip file.")

        if mod_root is None:
            raise ValueError("No module.json found in the import source.")

        data = _read_json(mod_root / "module.json")
        mod_id = data.get("id")
        if not mod_id:
            raise ValueError("module.json is missing an 'id'.")
        if any(sep in mod_id for sep in ("/", "\\", "..")):
            raise ValueError(f"Invalid module id: {mod_id!r}")

        MODULES_STORE.mkdir(parents=True, exist_ok=True)
        dest = MODULES_STORE / mod_id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(mod_root, dest)
        return _module_record(dest, builtin_default=False)
    finally:
        if tmp_extract is not None and tmp_extract.exists():
            shutil.rmtree(tmp_extract, ignore_errors=True)


def remove_module(module_id: str) -> dict:
    """Delete an imported module folder from modules_store/ and reclaim its bytes.

    Refuses built-in modules (they ship inside a profile).
    """
    for record in list_modules():
        if record["id"] != module_id:
            continue
        if record["builtin"]:
            raise PermissionError(
                f"Module '{module_id}' is built-in and cannot be removed."
            )
        store_path = MODULES_STORE / module_id
        if not store_path.exists():
            raise FileNotFoundError(
                f"Module '{module_id}' is not in the removable store."
            )
        size = _dir_size(store_path)
        shutil.rmtree(store_path)
        return {"removed": True, "reclaimedBytes": size}
    raise FileNotFoundError(f"Module '{module_id}' not found.")
