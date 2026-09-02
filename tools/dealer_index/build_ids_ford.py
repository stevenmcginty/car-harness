"""Build backend/profiles/ford/data/mdx_dids.json from the decrypted Ford IDS
133.01 MDX tables for the Ranger T6 (program code P375).

Input (derived, outside git, under paths.corpus_root("ford")/ids-decrypted):
  XML/P375/MDX_PCM_D_MDX_PCM_D.xml   diesel PCM DIDs (service 22, sessions 01/03)
  XML/P375/MDX_PCM_MDX_PCM.xml       Bosch-style 0xDExx adjustment DIDs

The IDS data files are OpenSSL "Salted__" 3DES blobs; E:/car/ford/decrypt_ids.py
(2026-09-01) turned them into plain XML. Nothing here talks to a car: every
entry is a candidate until captures/ford/ranger-32-test-plan.md runs live.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths  # noqa: E402

REPO = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(REPO, "backend", "profiles", "ford", "data", "mdx_dids.json")

SOURCES = {
    "MDX_PCM_D": "XML/P375/MDX_PCM_D_MDX_PCM_D.xml",
    "MDX_PCM": "XML/P375/MDX_PCM_MDX_PCM.xml",
}

# The DIDs the Ranger test plan reads, grouped the way the plan reads them.
FAMILIES = {
    "identity": ["F190", "F188", "F189", "F195", "F197"],
    "delete_check": ["057B", "0380", "0508", "FD00"],
    "fuel_pressure": ["050D"],
    "adjustment": ["DE01", "DE03", "DE04", "DE0A", "DE0F"],
    "vehicle": ["F40D"],
}


def _text(node, tag, default=None):
    el = node.find(tag)
    if el is None or el.text is None:
        return default
    return el.text.strip()


def _num(text):
    if text is None:
        return None
    try:
        f = float(text)
    except ValueError:
        return text
    return int(f) if f.is_integer() else f


def parse_did(did, source):
    number = _text(did, "NUMBER", "")
    hexid = number.replace("0x", "").upper().zfill(4)
    access = did.find("ACCESS_PARAMETERS")
    readable = writeable = None
    if access is not None:
        r = access.find("READABLE")
        w = access.find("WRITEABLE")
        if r is not None:
            readable = {
                "service": r.get("SERVICE_REFS", "").replace("service_", ""),
                "sessions": [s.replace("session_", "") for s in r.get("SESSION_REFS", "").split()],
            }
        if w is not None:
            writeable = {
                "service": w.get("SERVICE_REFS", "").replace("service_", ""),
                "sessions": [s.replace("session_", "") for s in w.get("SESSION_REFS", "").split()],
            }
    subs = []
    for sf in did.findall("SUB_FIELD"):
        dd = sf.find("DATA_DEFINITION")
        np_ = dd.find("NUMERIC_PARAMETERS") if dd is not None else None
        sub = {
            "name": _text(sf, "NAME"),
            "lsb": _num(_text(sf, "LEAST_SIG_BIT")),
            "msb": _num(_text(sf, "MOST_SIG_BIT")),
            "data_type": _text(dd, "DATA_TYPE") if dd is not None else None,
        }
        if np_ is not None:
            sub.update({
                "resolution": _num(_text(np_, "RESOLUTION")),
                "offset": _num(_text(np_, "OFFSET")),
                "units": _text(np_, "UNITS"),
                "range": [_num(_text(np_, "RANGE_LOW")), _num(_text(np_, "RANGE_HIGH"))],
            })
        states = dd.find("STATE_ENCODED") if dd is not None else None
        if states is not None:
            sub["states"] = {
                s.get("VALUE", _text(s, "VALUE", "")): (_text(s, "NAME") or s.text or "").strip()
                for s in states
            }
        if any(v is not None for k, v in sub.items() if k != "name"):
            subs.append(sub)
    return hexid, {
        "name": _text(did, "NAME"),
        "byte_size": _num(_text(did, "BYTE_SIZE")),
        "did_type": _text(did, "DID_TYPE"),
        "readable": readable,
        "writeable": writeable,
        "subfields": subs,
        "source": source,
        "grade": "candidate",
    }


def build(root=None):
    root = root or (paths.corpus_root("ford") / "ids-decrypted")
    dids = {}
    counts = {}
    for label, rel in SOURCES.items():
        path = os.path.join(str(root), rel)
        tree = ET.parse(path)
        n = 0
        for did in tree.getroot().findall(".//DID"):
            hexid, entry = parse_did(did, label)
            if not hexid:
                continue
            # MDX_PCM_D is the hand-made diesel file; it wins on a clash.
            if hexid in dids and label != "MDX_PCM_D":
                continue
            dids[hexid] = entry
            n += 1
        counts[label] = n
    families = {}
    for fam, ids in FAMILIES.items():
        families[fam] = {
            "in_table": [i for i in ids if i in dids],
            "not_in_table": [i for i in ids if i not in dids],
        }
    return {
        "title": "Ford Ranger T6 P375 3.2 Duratorq - IDS 133.01 MDX PCM data identifiers",
        "provenance": {
            "package": "IDS-133.01_Full.exe (InstallShield, data2.cab) decrypted with E:/car/ford/decrypt_ids.py",
            "source_files": {k: v for k, v in SOURCES.items()},
            "counts": counts,
            "protocol": "HS-CAN 500 kbit, ISO 15765; PCM 0x7E0/0x7E8, TCM 0x7E1/0x7E9; UDS 0x22 read, 0x2E write in session 03",
        },
        "notes": [
            "Grading: every entry is 'candidate'. Nothing here has answered on a car yet.",
            "Bit offsets are relative to the positive-response payload AFTER the 62 <DID> header bytes.",
            "The truck is DPF-deleted and remapped: soot DIDs (057B, 0380, 0508) are delete evidence, not health data.",
            "F1xx identity DIDs and F40D are ISO-standard; they are listed in families even when the MDX tables omit them.",
        ],
        "families": families,
        "dids": dict(sorted(dids.items())),
    }


def main():
    data = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    print("wrote", OUT, "dids:", len(data["dids"]), data["provenance"]["counts"])
    for fam, v in data["families"].items():
        print(" ", fam, "in:", v["in_table"], "missing:", v["not_in_table"])


if __name__ == "__main__":
    main()
