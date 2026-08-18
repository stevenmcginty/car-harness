"""Export one module's whole GDS2 data set out of a vatbin.bin into JSON.

Read-only over a local file. Nothing here talks to a car, and nothing it emits
is executable -- the output is a description of what the dealer tool would
send, not an instruction to send it.

Walks one module menu node and emits, with a file offset and object type on
every entry:

  * the module connection record  (CAN request id, bus, protocol)
  * every PID / CPID in the module's PID set, with the request bytes the GDS2
    runtime would build for it, its scaling equation, unit, range and any
    enumerated state table
  * every device control (PID type 3), with the literal $AE request and one
    frame per enumerated command value
  * the module's condition sets (the preconditions GDS2 checks before running
    a control), with their equations, the PIDs they read and the
    operator-facing message for each outcome
  * which DTC lists the node binds, the rows of its local override lists, and
    optionally GM's master-dictionary wording for a named set of codes
  * the module's test folder tree

Request-byte reconstruction follows com/Mahle/VCI/c/ad.java and the GMLAN
template table sB_ in com/Mahle/VCI/c/n.java, with the protocol constants of
class m (GMLAN) resolved through its parents a -> ad: av = 0, cc = 2, cd = 1,
ce = 1, cf = 1, cg = 1.

  PID type 0   read     template sB_[20] = 22 02 00, id written 2 bytes
                        big-endian at index 1        ->  22 <hi> <lo>
  PID type 2   read     template sB_[22] = 1A 00, id written 1 byte at
                        index 1 (ce = 1)             ->  1A <id>
  PID type 3   control  template sB_[26] = AE 00, id at index 1, byte
                        pattern D appended, the encoded value OR-ed in at
                        index av + 1 + cd + p[5]     ->  AE <cpid> <D...>
  PID type 4   raw      template sB_[25] = 00, id at index av = 0, D
                        appended                     ->  <id> <D...>

The encoded command value is applied through the PID's *write* conversion
before it is OR-ed in. Skipping that step is what once turned the gear-select
control's ``X(0)<<4`` into ``X(0)<<0`` and produced six wrong frames, so only
equation forms with an implemented rule are reconstructed; anything else is
emitted as a null request with the reason attached.

usage: export_module.py <vatbin> <module-node-offset> <out.json>
                        [--release NAME] [--dtc-names P0705,P0716,...]
"""

import json
import re
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin  # noqa: E402


# --------------------------------------------------------------------------- #
# value-object field helpers
# --------------------------------------------------------------------------- #
def fld(v, off, i):
    """Raw double of struct field i."""
    return v.f64(off + 12 + i * 12 + 4)


def sfld(v, off, i):
    """Struct field i as display text."""
    p = int(fld(v, off, i))
    return v.text(p) if p else ""


def ifld(v, off, i):
    return int(fld(v, off, i))


def lst(v, off, i):
    """Struct field i as a list of member offsets, or []."""
    p = int(fld(v, off, i))
    if not v.ok(p, 16) or v.u32(p) != 6:
        return []
    return [int(v.f64(p + 16 + k * 8)) for k in range(v.u32(p + 12))]


# --------------------------------------------------------------------------- #
# conversions and state tables
# --------------------------------------------------------------------------- #
def states(v, conv_off):
    """Enumerated states of a type-17276 conversion (field .6 -> 17172 -> 17169)."""
    out = []
    for sev in lst(v, conv_off, 6):
        for e in lst(v, sev, 2):
            # type 17169: field .0 is the value, field .1 the display text
            out.append({"value": ifld(v, e, 0), "text": sfld(v, e, 1)})
    return out


def conversion(v, pid_off, idx):
    members = lst(v, pid_off, idx)
    if not members:
        return None
    c = members[0]
    hi, lo = fld(v, c, 2), fld(v, c, 3)
    out = {
        "offset": "0x%08X" % c,
        "objectType": 17276,
        "equation": sfld(v, c, 1),
        "unit": sfld(v, c, 4),
    }
    if lo != hi:
        out["range"] = [lo, hi]
    st = states(v, c)
    if st:
        out["states"] = st
    return out


# --------------------------------------------------------------------------- #
# request-byte reconstruction
# --------------------------------------------------------------------------- #
def pattern_bytes(d):
    """The type-17283 byte pattern D as a list of ints, or None for a wildcard.

    '*', '0' and '_' mean "no pattern"; 'XX' inside a pattern is a wildcard
    byte, which is preserved as None so it is never silently turned into 0x00.
    """
    if not d or d.strip() in ("*", "0", "_", ""):
        return []
    out = []
    for tok in d.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.upper() == "XX":
            out.append(None)
        else:
            try:
                out.append(int(tok, 16))
            except ValueError:
                return None
    return out


def hexs(frame):
    return " ".join("XX" if b is None else "%02X" % b for b in frame)


def build_request(p, d):
    """The literal request frame, or (None, reason) when it is not derivable."""
    pid_type, ident, off = p[3], p[0], p[5]
    pat = pattern_bytes(d)
    if pat is None:
        return None, "byte pattern %r did not parse" % d

    if pid_type == 0:                                   # sB_[20] = 22 02 00
        if ident > 0xFFFF:
            return None, "parameter id 0x%X does not fit the 2-byte field" % ident
        return [0x22, (ident >> 8) & 0xFF, ident & 0xFF], None

    if pid_type == 2:                                   # sB_[22] = 1A 00
        if ident > 0xFF:
            return None, "identifier 0x%X does not fit the 1-byte field" % ident
        return [0x1A, ident], None

    if pid_type == 3:                                   # sB_[26] = AE 00
        if ident > 0xFF:
            return None, "CPID 0x%X does not fit the 1-byte field" % ident
        frame = [0xAE, ident] + list(pat)
        # the encoded command value is OR-ed in at av + 1 + cd + p[5] = 2 + off
        idx = 2 + off
        if idx >= len(frame):
            return None, ("payload offset %d lies past the %d-byte pattern"
                          % (off, len(pat)))
        return frame, None

    if pid_type == 4:                                   # sB_[25] = 00
        if ident > 0xFF:
            return None, "identifier 0x%X does not fit the 1-byte field" % ident
        return [ident] + list(pat), None

    return None, "PID type %d has no reconstruction rule in this exporter" % pid_type


SHIFT_EQ = re.compile(r"^X\(0\)<<(\d+)$")
LINEAR_EQ = re.compile(r"^\(X\(0\)\*(-?[\d.]+)\)\+(-?[\d.]+)$")


def encode_command(equation, value):
    """Apply a write conversion to one enumerated command value.

    Only the two equation forms the T76 controls actually use are implemented,
    and anything else returns None rather than a guess. Getting this wrong is
    how ``X(0)<<4`` became ``X(0)<<0`` in an earlier write-up and turned gear
    selection 1-6 into the wrong nibble.
    """
    m = SHIFT_EQ.match(equation or "")
    if m:
        return value << int(m.group(1))
    m = LINEAR_EQ.match(equation or "")
    if m:
        return int(round(value * float(m.group(1)) + float(m.group(2))))
    return None


def control_frames(p, d, equation, st):
    """For a device control, the frame per enumerated command value.

    ad.java::e(q) ORs the *encoded* value into the frame at
    av + 1 + cd + p[5] = 2 + p[5], one byte per element of the encoded array.
    """
    frame, _why = build_request(p, d)
    if frame is None:
        return None
    idx = 2 + p[5]
    out = []
    for s in st or []:
        val = s["value"]
        enc = encode_command(equation, val)
        row = {"value": val, "text": s["text"]}
        if enc is None:
            row["request"] = None
            row["note"] = ("write equation %r has no reconstruction rule in "
                           "this exporter" % equation)
        elif p[1] != 1:
            row["request"] = None
            row["note"] = ("command is %d bytes wide; only single-byte "
                           "commands are reconstructed here" % p[1])
        elif not 0 <= enc <= 0xFF:
            row["request"] = None
            row["note"] = ("encoded value 0x%X does not fit one byte" % enc)
        else:
            f = list(frame)
            base = f[idx] or 0
            f[idx] = (base | enc) & 0xFF
            row["encoded"] = "0x%02X" % enc
            row["request"] = hexs(f)
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# one PID / CPID definition (type 17297)
# --------------------------------------------------------------------------- #
def pid_entry(v, off):
    ref = sfld(v, off, 0)
    entry = {
        "ref": ref,
        "name": sfld(v, off, 5),
        "key": sfld(v, off, 6),
        "offset": "0x%08X" % off,
        "objectType": 17297,
    }
    items = lst(v, off, 1)
    if not items:
        entry["note"] = "no VCIDataItem bound"
        return entry
    it = items[0]
    f = [ifld(v, it, k) for k in range(7)]
    d = sfld(v, it, 7)
    # com/Mahle/VCI/a/q.java t() with the permutation aB = {0,3,1,4,2,5,6}
    p = [f[0], f[3], f[1], f[4], f[2], f[5], f[6]]
    entry["item"] = {
        "offset": "0x%08X" % it,
        "objectType": 17283,
        "id": p[0],
        "idHex": "0x%X" % p[0],
        "length": p[1],
        "flags": "0x%X" % p[2],
        "pidType": p[3],
        "payloadOffset": p[5],
        "bytePattern": d,
    }
    if len(items) > 1:
        entry["item"]["siblingItems"] = len(items) - 1

    read = conversion(v, off, 2)
    write = conversion(v, off, 4)
    if read:
        entry["read"] = read
    if write:
        entry["write"] = write

    frame, why = build_request(p, d)
    if frame is None:
        entry["request"] = None
        entry["requestNote"] = why
    else:
        entry["request"] = hexs(frame)
    if p[3] == 3:
        conv = write or read or {}
        cf = control_frames(p, d, conv.get("equation"), conv.get("states"))
        if cf:
            entry["commands"] = cf
    return entry


# --------------------------------------------------------------------------- #
# condition sets (type 17346 -> 17337)
# --------------------------------------------------------------------------- #
def condition_entry(v, off):
    """One type-17346 condition set: name, the PID it watches, its pass
    equation and the operator-facing text for each outcome."""
    entry = {
        "name": sfld(v, off, 2),
        "offset": "0x%08X" % off,
        "objectType": 17346,
        "checks": [],
    }
    # 16881 result codes, read off the four messages every check carries.
    RESULT = {0: "pass", 1: "pending", 2: "timeout", 3: "fail"}
    for c in lst(v, off, 5):
        chk = {
            "offset": "0x%08X" % c,
            "objectType": 17337,
            "signal": sfld(v, c, 2),
            "comparisons": [],
            "messages": [],
        }
        # field .3: list of 17333 InstCompare blocks -- .0 the data-node refs
        # the equation reads, .1 the equation itself.
        for cmp_off in lst(v, c, 3):
            block = {
                "offset": "0x%08X" % cmp_off,
                "objectType": 17333,
                "equation": sfld(v, cmp_off, 1),
                "watches": [],
            }
            for ref in lst(v, cmp_off, 0):
                block["watches"].append({
                    "offset": "0x%08X" % ref,
                    "objectType": 16924,
                    "pidRef": sfld(v, ref, 1),
                    "module": sfld(v, ref, 2),
                    "name": sfld(v, ref, 3),
                })
            chk["comparisons"].append(block)
        # field .6: list of 16881 outcome messages -- .1 result code, .2 text.
        for m in lst(v, c, 6):
            code = ifld(v, m, 1)
            chk["messages"].append({
                "resultCode": code,
                "result": RESULT.get(code, "code %d" % code),
                "text": sfld(v, m, 2),
            })
        entry["checks"].append(chk)
    return entry


# --------------------------------------------------------------------------- #
# DTC data lists
# --------------------------------------------------------------------------- #
# com/Mahle/b/a/e.java: n3 = 24, and +4 only when header flag bit 0x80 is set.
# That bit is clear on the Opel/Vauxhall files (flags = 0x7F), so the stride is
# 24. com/Mahle/b/a/d.java reads fields at +0 code, +4, +8, +12, +16, +20.
DTC_STRIDE = 24


def dtc_code(raw):
    """The u32 code as GM prints it: two bits pick the letter, 14 bits the number."""
    return "%s%04X" % ("PCBU"[(raw >> 14) & 3], raw & 0x3FFF)


def dtc_rows(v, dl):
    out = []
    for i in range(dl["c"]):
        o = dl["d"] + i * DTC_STRIDE
        raw = struct.unpack_from("<I", v.buf, o)[0]
        p = [struct.unpack_from("<I", v.buf, o + k)[0] for k in (4, 8, 12)]
        out.append({
            "offset": "0x%08X" % o,
            "code": dtc_code(raw),
            "failureType": v.text(p[1]) if p[1] else "",
            "name": v.text(p[2]) if p[2] else "",
        })
    return out


def dtc_section(v, folder, name_lookup):
    """Which DTC lists the node binds, and GM names for a requested code set.

    The distinction matters and is kept explicit in the output: a *local
    override* list belongs to this module, whereas the master list is a
    corpus-wide text dictionary. A code appearing in the master list says only
    that GM has assigned that code somewhere -- never that this module
    supports it.
    """
    lists = {dl["name"]: dl for dl in v.datalists()}
    out = {
        "masterList": {"name": folder.get("masterList", "")},
        "localLists": [],
        "note": ("This deliverable carries no full DTC list for the T76 TCM. "
                 "The node binds a corpus-wide master text dictionary plus a "
                 "short local override list; membership of the master "
                 "dictionary is not evidence that this module supports a code."),
    }
    dl = lists.get(out["masterList"]["name"])
    if dl:
        out["masterList"].update({
            "dataListIndex": dl["idx"],
            "rowCount": dl["c"],
            "dataOffset": "0x%08X" % dl["d"],
            "rowStride": DTC_STRIDE,
        })
    for loc in folder.get("localLists", []):
        dl = lists.get(loc["name"])
        entry = dict(loc)
        if dl:
            entry.update({
                "dataListIndex": dl["idx"],
                "rowCount": dl["c"],
                "dataOffset": "0x%08X" % dl["d"],
                "rows": dtc_rows(v, dl),
            })
        out["localLists"].append(entry)

    if name_lookup and dl is not None:
        master = lists.get(out["masterList"]["name"])
        if master:
            want = set(c.strip().upper() for c in name_lookup if c.strip())
            found = {}
            for row in dtc_rows(v, master):
                if row["code"] in want:
                    found[row["code"]] = row
            out["masterNames"] = {
                "note": ("GM names for codes the profile already carries, read "
                         "out of the master text dictionary. Provenance for "
                         "the wording only -- not a module support list."),
                "dataListIndex": out["masterList"].get("dataListIndex"),
                "codes": [found[c] for c in sorted(found)],
                "notFound": sorted(want - set(found)),
            }
    return out


# --------------------------------------------------------------------------- #
# test folder tree (17533 -> 17489 -> 17483)
# --------------------------------------------------------------------------- #
def test_tree(v, list_off):
    folders = []
    if not v.ok(list_off, 16) or v.u32(list_off) != 6:
        return folders
    for i in range(v.u32(list_off + 12)):
        fo = int(v.f64(list_off + 16 + i * 8))
        folder = {"name": sfld(v, fo, 1), "offset": "0x%08X" % fo,
                  "objectType": 17533, "groups": []}
        for go in lst(v, fo, 2):
            group = {"name": sfld(v, go, 1), "key": sfld(v, go, 2),
                     "offset": "0x%08X" % go, "objectType": 17489, "tests": []}
            for to in lst(v, go, 3):
                group["tests"].append({
                    "key": sfld(v, to, 1),
                    "name": sfld(v, to, 2),
                    "testType": sfld(v, to, 3),
                    "offset": "0x%08X" % to,
                    "objectType": 17483,
                })
            folder["groups"].append(group)
        folders.append(folder)
    return folders


# --------------------------------------------------------------------------- #
def main():
    binpath, node_off, outpath = sys.argv[1], int(sys.argv[2], 0), sys.argv[3]
    name_lookup = []
    if "--dtc-names" in sys.argv:
        name_lookup = sys.argv[sys.argv.index("--dtc-names") + 1].split(",")
    release = "unknown"
    if "--release" in sys.argv:
        release = sys.argv[sys.argv.index("--release") + 1]
    v = Vatbin(binpath)
    node = v.item(node_off)

    doc = {
        "schema": 1,
        "deliverable": {
            "product": v.text(v.ver_ptr),
            "release": release,
            "file": "BinaryFile/vatbin.bin",
            "size": v.size,
            "headerFlags": "0x%X" % v.flags,
        },
        "moduleNode": {
            "name": node["name"],
            "offset": "0x%08X" % node["off"],
        },
    }

    triples = {sid: val for _m, sid, val in node["triples"]}

    # -- state 2: connection record (17515 -> 17466) ------------------------
    conn_off = triples.get(2)
    if conn_off:
        members = lst(v, conn_off, 1)
        if members:
            c = members[0]
            req = ifld(v, c, 2)
            doc["connection"] = {
                "offset": "0x%08X" % c,
                "objectType": 17466,
                "module": sfld(v, c, 0),
                "protocol": ifld(v, c, 1),
                "canRequest": "0x%03X" % req,
                "canResponse": "0x%03X" % (req + 8 if req >= 2016 else req),
                "bus": sfld(v, c, 3),
                "pidSetName": sfld(v, c, 7),
                "responseRule": ("com/Mahle/VCI/c/m.java: request id >= 2016 "
                                 "gives response filter = request + 8"),
            }

    # -- state 3: PID sets (17305) -----------------------------------------
    pids, controls = [], []
    setoff = triples.get(3)
    sets = []
    if setoff and v.ok(setoff, 16) and v.u32(setoff) == 6:
        for i in range(v.u32(setoff + 12)):
            sets.append(int(v.f64(setoff + 16 + i * 8)))
    for so in sets:
        doc.setdefault("pidSets", []).append({
            "offset": "0x%08X" % so,
            "objectType": 17305,
            "ref": sfld(v, so, 2),
            "module": sfld(v, so, 3),
        })
        for po in lst(v, so, 0):
            e = pid_entry(v, po)
            e["pidSet"] = "0x%08X" % so
            if e.get("item", {}).get("pidType") == 3:
                controls.append(e)
            else:
                pids.append(e)

    pids.sort(key=lambda e: (e.get("item", {}).get("pidType", 99),
                             e.get("item", {}).get("id", 0)))
    controls.sort(key=lambda e: (e.get("item", {}).get("id", 0), e["name"]))
    doc["parameters"] = pids
    doc["deviceControls"] = controls

    # -- state 11: DTC folder (17395) --------------------------------------
    dt = triples.get(11)
    if dt:
        folder = {"offset": "0x%08X" % dt, "objectType": 17395,
                  "masterFolder": sfld(v, dt, 1), "masterList": sfld(v, dt, 2),
                  "localLists": []}
        for lo in lst(v, dt, 0):
            folder["localLists"].append({"offset": "0x%08X" % lo,
                                         "objectType": 17328,
                                         "name": sfld(v, lo, 1)})
        section = dtc_section(v, folder, name_lookup)
        section["folder"] = {"offset": folder["offset"], "objectType": 17395,
                             "masterFolder": folder["masterFolder"]}
        doc["dtc"] = section

    # -- state 12: condition sets (17346) ----------------------------------
    co = triples.get(12)
    if co and v.ok(co, 16) and v.u32(co) == 6:
        doc["conditions"] = [
            condition_entry(v, int(v.f64(co + 16 + i * 8)))
            for i in range(v.u32(co + 12))
        ]

    # -- state 13: test folder tree ----------------------------------------
    to = triples.get(13)
    if to:
        doc["tests"] = test_tree(v, to)

    with open(outpath, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    print("wrote %s" % outpath)
    print("  parameters      %d" % len(doc.get("parameters", [])))
    print("  device controls %d" % len(doc.get("deviceControls", [])))
    print("  conditions      %d" % len(doc.get("conditions", [])))
    print("  test folders    %d" % len(doc.get("tests", [])))


if __name__ == "__main__":
    main()
