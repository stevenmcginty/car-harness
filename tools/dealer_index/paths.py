"""Where the mined-data layer keeps its databases and where it reads its corpora.

The databases are derived data. They are large, they are rebuildable from one
command, and they must never enter git, so they live outside the repository in a
single shared store. Override the location with the CARHARNESS_DATA environment
variable; everything else in the tool asks this module rather than hard-coding a
path.
"""

import os
import pathlib

DEFAULT_DATA_ROOT = pathlib.Path.home() / "Desktop" / "car-harness-data"

# Corpora, as they sit on this machine. These are read-only inputs; nothing in the
# tool ever writes into them. They all live under one movable root laid out per
# docs/SSD-MIGRATION-PLAN.md: the external SSD (E:\car) when it is plugged in,
# otherwise the Desktop staging copy. Override the root with CARHARNESS_CORPUS_ROOT,
# or a single corpus with CARHARNESS_CORPUS_<NAME>.
SSD_CORPUS_ROOT = pathlib.Path("E:/car")
STAGING_CORPUS_ROOT = pathlib.Path.home() / "Desktop" / "car-data-ssd"

CORPUS_SUBDIRS = {
    "gds2": pathlib.Path("vauxhall-opel") / "gds2",
    "sdd": pathlib.Path("jlr") / "sdd169",
    "diagbox": pathlib.Path("vauxhall-opel") / "diagbox",
    "mercedes": pathlib.Path("mercedes"),
    "ford": pathlib.Path("ford"),
}


def corpus_base():
    """Root directory holding all raw corpora (SSD first, staging fallback)."""
    env = os.environ.get("CARHARNESS_CORPUS_ROOT")
    if env:
        return pathlib.Path(env)
    if SSD_CORPUS_ROOT.exists():
        return SSD_CORPUS_ROOT
    return STAGING_CORPUS_ROOT


def data_root():
    """Directory holding the built databases. Created on demand."""
    env = os.environ.get("CARHARNESS_DATA")
    root = pathlib.Path(env) if env else DEFAULT_DATA_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path(name):
    """Path of one corpus database, e.g. db_path('gds2') -> .../gds2.db"""
    return data_root() / ("%s.db" % name)


def corpus_root(name):
    env = os.environ.get("CARHARNESS_CORPUS_%s" % name.upper())
    if env:
        return pathlib.Path(env)
    return corpus_base() / CORPUS_SUBDIRS[name]


def find_vatbins(root=None):
    """Every vatbin.bin under the Vauxhall corpus, newest deliverable first.

    Returns a list of (label, path). The label is the deliverable directory the
    file came out of, which is what gets recorded as provenance.
    """
    root = pathlib.Path(root) if root else corpus_root("gds2")
    out = []
    if not root.exists():
        return out
    generic = {"binaryfile", "unpacked", "databackups", "data", "bin"}
    for p in root.rglob("vatbin.bin"):
        # The deliverable is the first directory above the file whose name is not
        # one of the generic container directories the installers use.
        label = p.parent.name
        for parent in p.parents:
            if parent == root:
                break
            if parent.name.lower() not in generic:
                label = parent.name
                break
        out.append((label, p))
    out.sort(key=lambda lp: lp[0])
    return out
