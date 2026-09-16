"""Export the Astra J VXR engine controller out of the GDS2 index into JSON.

    python tools/dealer_index/export_vauxhall_ecm.py
    python tools/dealer_index/export_vauxhall_ecm.py --out somewhere.json

The 17 GB gds2.db is derived data and never enters git; this data pack does.
It is the engine-side twin of backend/profiles/vauxhall/data/gds2_t76_tcm.json
and carries the same ten top-level keys, so one reader serves both. The car is
a 2016 Vauxhall Astra VXR (Astra J VXR, A20NFT 2.0 turbo petrol), which GDS2
knows only as vehicle "Astra-J" with option pair Engine Identifier = 2.0L (LHU)
and module node "Engine Control Module$ECM - E39A (S3) - GME" -- the strings
VXR, OPC and A20NFT appear nowhere in the database.

Everything below is re-derived from the SQLite index, never from a written-up
table, and every entry keeps the deliverable, the table and the file offset it
came out of so any value can be re-checked against the binary.

Deliverable 1 (_x_2023ov) is the source. Deliverable 6 (d_ov44) carries the
same node but its dtc_row text layer decodes as UTF-16 garbage, so it is not
used here for anything.

Request-byte reconstruction is the index's own, written by build_gds2.py's
request_bytes() from com/Mahle/VCI/c/ad.java plus the GMLAN templates in
c/n.java. The command-value encoding for device controls follows
tools/gds2/export_module.py: the encoded value is OR-ed into the frame at
av + 1 + cd + p[5] = 2 + payloadOffset, and only equation forms with an
implemented rule are reconstructed -- anything else is emitted as a null
request with the reason attached rather than a guess.

Nothing here talks to a car. The output is a description of what the dealer
tool would send, not an instruction to send it.
"""

import argparse
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths                                    # noqa: E402

REPO = os.path.dirname(os.path.dirname(HERE))
DEFAULT_OUT = os.path.join(REPO, "backend", "profiles", "vauxhall", "data",
                           "gds2_e39a_ecm.json")

# The handles that select this module, all verified against the index. They are
# arguments rather than constants only in the sense that --node overrides one;
# the defaults are the 2016 Astra-J node, and 2014/15/17/18 bind the same child
# objects.
DELIVERABLE = "_x_2023ov"
NODE_OFF = 93245252

# Object types, as the generic layer records them.
T_CONN_RECORD = 17466
T_PID_SET = 17305
T_PID = 17297
T_PID_ITEM = 17283
T_PID_CONV = 17276
T_LOCAL_DTC_LIST = 17328
T_DTC_FOLDER = 17395
T_CONDITION = 17346
T_CONDITION_CHECK = 17337
T_COMPARE = 17333
T_WATCH = 16924
T_MESSAGE = 16881
T_TEST_FOLDER = 17533
T_TEST_GROUP = 17489
T_TEST = 17483

# pid_item.pid_type, from the schema comment plus what the frames prove.
SERVICE = {0: "$22", 2: "$1A", 3: "$AE", 4: "raw", 13: None}
PID_TYPE_NOTE = {
    0: "read, GMLAN ReadDataByIdentifier $22",
    2: "identification, GMLAN ReadDataByParameterIdentifier $1A",
    3: "DeviceControl $AE -- WRITES TO THE CAR",
    4: "raw frame, sent literally",
    13: "computed by the tool from other parameters; no wire request",
}
# 16881 result codes, as export_module.py reads them.
RESULT = {0: "pass", 1: "pending", 2: "timeout", 3: "fail"}

LINEAR_EQ = re.compile(r"^\(X\(0\)\*(-?[0-9.]+)\)([+-][0-9.]+)$")
SHIFT_EQ = re.compile(r"^X\(0\)<<([0-9]+)$")


def hx(off):
    return "0x%08X" % off


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #
class Index(object):
    """Read-only accessor over gds2.db, scoped to one deliverable."""

    def __init__(self, db, label):
        if not os.path.exists(db):
            sys.exit("gds2 index not built (%s). Run: "
                     "python tools/dealer_index/build_gds2.py" % db)
        self.db = db
        self.conn = sqlite3.connect(
            "file:%s?mode=ro" % db.replace(os.sep, "/"), uri=True)
        self.conn.row_factory = sqlite3.Row
        row = self.one("SELECT * FROM deliverable WHERE label = ?", label)
        if row is None:
            sys.exit("deliverable %s is not in %s" % (label, db))
        self.deliverable = dict(row)
        self.did = row["id"]

    def rows(self, sql, *args):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def one(self, sql, *args):
        cur = self.conn.execute(sql, args)
        return cur.fetchone()

    def fields(self, off):
        """Every decoded field of one value object, by index."""
        return self.rows(
            "SELECT idx, field_type, num, int_val, str_off, text, ref_off "
            "FROM object_field WHERE deliverable_id = ? AND obj_off = ? "
            "ORDER BY idx", self.did, off)

    def field(self, off, idx):
        for f in self.fields(off):
            if f["idx"] == idx:
                return f
        return None

    def text(self, off, idx):
        f = self.field(off, idx)
        return (f["text"] or "") if f else ""

    def num(self, off, idx):
        f = self.field(off, idx)
        return f["num"] if f else None

    def members(self, off, idx):
        """Struct field idx read as a list object -> its member offsets."""
        f = self.field(off, idx)
        if not f or not f["ref_off"]:
            return []
        return [g["ref_off"] for g in self.fields(f["ref_off"])
                if g["ref_off"]]

    def list_members(self, list_off):
        """A list object's member offsets, in file order."""
        if not list_off:
            return []
        return [g["ref_off"] for g in self.fields(list_off) if g["ref_off"]]


# --------------------------------------------------------------------------- #
# parameters
# --------------------------------------------------------------------------- #
def scaling(equation):
    """(factor, offset) for a linear read rule, or None when it is not one.

    Only the (X(0)*f)+o / (X(0)*f)-o form is a scaling. Bit-field extractions
    like (X(0)>>7)&0x1 are decode rules, not scalings, and are left as the
    equation alone rather than flattened into a factor nobody can apply.
    """
    m = LINEAR_EQ.match(equation or "")
    if not m:
        return None
    return {"factor": float(m.group(1)), "offset": float(m.group(2))}


def conversion(ix, pid_off, direction):
    """The first conversion of one direction, with its enumerated states.

    build_gds2.py inserts one pid_conv row per member of the conversion list in
    file order; export_module.py uses members[0], so the first row is the one
    the dealer tool applies.
    """
    rows = ix.rows(
        "SELECT rowid, off, conv_type, equation, unit, lo, hi, fmt "
        "FROM pid_conv WHERE deliverable_id = ? AND pid_off = ? "
        "AND direction = ? ORDER BY rowid", ix.did, pid_off, direction)
    if not rows:
        return None
    c = rows[0]
    out = {
        "offset": hx(c["off"]),
        "objectType": T_PID_CONV,
        "equation": c["equation"],
        "unit": c["unit"],
        "convType": c["conv_type"],
        "format": c["fmt"],
    }
    if c["lo"] != c["hi"]:
        out["range"] = [c["lo"], c["hi"]]
    states = ix.rows(
        "SELECT rowid, value, text FROM pid_state WHERE deliverable_id = ? "
        "AND pid_off = ? AND conv_off = ? ORDER BY rowid",
        ix.did, pid_off, c["off"])
    if states:
        out["states"] = [{"value": int(s["value"]), "text": s["text"]}
                         for s in states]
    return out


def encode_command(equation, value):
    """Apply a write conversion to one enumerated command value.

    Only the forms the index actually carries are implemented. Guessing here is
    what once turned a gear-select control's X(0)<<4 into X(0)<<0 and produced
    six wrong frames, so anything else returns None.
    """
    m = SHIFT_EQ.match(equation or "")
    if m:
        return value << int(m.group(1))
    m = LINEAR_EQ.match(equation or "")
    if m:
        return int(round(value * float(m.group(1)) + float(m.group(2))))
    return None


def parse_frame(request_hex):
    """A ' ' joined request back into a list of ints, XX kept as None."""
    if not request_hex:
        return None
    out = []
    for tok in request_hex.split():
        if tok.upper() == "XX":
            out.append(None)
        else:
            try:
                out.append(int(tok, 16))
            except ValueError:
                return None
    return out


def frame_hex(frame):
    return " ".join("XX" if b is None else "%02X" % b for b in frame)


def control_frames(item, conv):
    """One $AE frame per enumerated command value of a device control.

    ad.java::e(q) ORs the encoded value into the frame at 2 + payloadOffset,
    one byte per element.
    """
    states = (conv or {}).get("states")
    if not states:
        return None
    frame = parse_frame(item["request_hex"])
    if frame is None:
        return None
    idx = 2 + item["byte_off"]
    equation = (conv or {}).get("equation")
    out = []
    for s in states:
        row = {"value": s["value"], "text": s["text"]}
        enc = encode_command(equation, s["value"])
        if idx >= len(frame):
            row["request"] = None
            row["note"] = ("payload offset %d lies past the %d-byte frame"
                           % (item["byte_off"], len(frame)))
        elif enc is None:
            row["request"] = None
            row["note"] = ("write equation %r has no reconstruction rule in "
                           "this exporter" % equation)
        elif item["length"] != 1:
            row["request"] = None
            row["note"] = ("command is %d bytes wide; only single-byte "
                           "commands are reconstructed here" % item["length"])
        elif not 0 <= enc <= 0xFF:
            row["request"] = None
            row["note"] = "encoded value 0x%X does not fit one byte" % enc
        else:
            f = list(frame)
            f[idx] = ((f[idx] or 0) | enc) & 0xFF
            row["encoded"] = "0x%02X" % enc
            row["request"] = frame_hex(f)
        out.append(row)
    return out


def identification_request(item):
    """The $1A request for a type-2 item, which the index does not reconstruct.

    build_gds2.py's request_bytes() implements types 0, 3 and 4 only, so
    pid_item.request_hex is NULL for identification. The rule is not invented
    here: com/Mahle/VCI/c/ad.java writes the identifier as one byte at index 1
    of GMLAN template sB_[22] = 1A 00, which is what tools/gds2/export_module.py
    already emits for the same items. It is flagged in the entry so nobody
    mistakes a reconstruction for something the database stated.
    """
    if item["pid_type"] != 2 or item["param_id"] > 0xFF:
        return None
    return "1A %02X" % item["param_id"]


def pid_entry(ix, pid, item, set_off):
    """One PID or CPID, in the vocabulary gds2_t76_tcm.json already uses.

    The extra fields the engine pack adds -- service, requestHex,
    responseLength, scaling, unit, source -- exist so a consumer can build the
    request and decode the reply from this entry alone, without the database.
    """
    ptype = item["pid_type"]
    request = item["request_hex"] or identification_request(item)
    read = conversion(ix, pid["off"], "read")
    write = conversion(ix, pid["off"], "write")
    conv = write if ptype == 3 and write else (read or write)
    entry = {
        "ref": pid["ref_name"],
        "name": pid["name"],
        "key": pid["key"],
        "offset": hx(pid["off"]),
        "objectType": T_PID,
        "item": {
            "offset": hx(item["off"]),
            "objectType": T_PID_ITEM,
            "id": item["param_id"],
            "idHex": "0x%X" % item["param_id"],
            "length": item["length"],
            "flags": "0x%X" % item["flags"],
            "pidType": ptype,
            "payloadOffset": item["byte_off"],
            "bytePattern": item["pattern"],
        },
        "service": SERVICE.get(ptype),
        "serviceNote": PID_TYPE_NOTE.get(
            ptype, "PID type %d has no documented rule" % ptype),
        "request": request,
        "requestHex": request,
        "requestSource": ("pid_item.request_hex" if item["request_hex"]
                          else ("reconstructed by this exporter from GMLAN "
                                "template sB_[22] = 1A 00 (ad.java); the index "
                                "reconstructs no request for PID type 2 -- "
                                "UNVERIFIED against a car"
                                if request else None)),
        "responseLength": item["length"],
        "responsePattern": item["pattern"],
        "unit": (conv or {}).get("unit"),
        "scaling": scaling((conv or {}).get("equation")),
        "applicability": "UNVERIFIED",
        "pidSet": hx(set_off),
        "source": {
            "deliverable": ix.deliverable["label"],
            "deliverableId": ix.did,
            "tables": ["pid", "pid_item", "pid_conv", "pid_state"],
            "pidOffset": hx(pid["off"]),
            "itemOffset": hx(item["off"]),
            "pidSetOffset": hx(set_off),
        },
    }
    if read:
        entry["read"] = read
    if write:
        entry["write"] = write
    if entry["scaling"] is None and (conv or {}).get("equation"):
        entry["scalingNote"] = (
            "equation %r is a decode rule, not a linear scaling; apply the "
            "equation as written" % conv["equation"])
    if request is None:
        entry["requestNote"] = (
            "no request frame: PID type %d is %s"
            % (ptype, PID_TYPE_NOTE.get(ptype, "undocumented")))
    return entry


CONTROL_WARNING = (
    "WRITES TO THE CAR. This $AE frame commands an actuator or starts a learn. "
    "Release it with the Return To Normal (20) and Null CPID (AE 00) frames in "
    "this same list; GDS2 ends every output control with them.")
RAW_WARNING = (
    "WRITES TO THE CAR. This is a literal frame the tool sends, not a value it "
    "reads. Return To Normal (20) and Null CPID (AE 00) are how an output "
    "control is released; Communication Bus Test Status (3E) is the "
    "tester-present keepalive.")


def device_control(entry, item):
    """Stamp a PID entry as a frame that writes to the car."""
    entry["writesToCar"] = True
    entry["frameClass"] = "cpid" if item["pid_type"] == 3 else "raw"
    entry["warning"] = (CONTROL_WARNING if item["pid_type"] == 3
                        else RAW_WARNING)
    frames = control_frames(item, entry.get("write") or entry.get("read"))
    if frames:
        entry["commands"] = frames
    return entry


# --------------------------------------------------------------------------- #
# conditions (17346 -> 17337 -> 17333 -> 16924, messages 16881)
# --------------------------------------------------------------------------- #
def condition_entry(ix, off):
    entry = {
        "name": ix.text(off, 2),
        "offset": hx(off),
        "objectType": T_CONDITION,
        "checks": [],
    }
    for c in ix.members(off, 5):
        chk = {
            "offset": hx(c),
            "objectType": T_CONDITION_CHECK,
            "signal": ix.text(c, 2),
            "comparisons": [],
            "messages": [],
        }
        for cmp_off in ix.members(c, 3):
            block = {
                "offset": hx(cmp_off),
                "objectType": T_COMPARE,
                "equation": ix.text(cmp_off, 1),
                "watches": [],
            }
            for ref in ix.members(cmp_off, 0):
                block["watches"].append({
                    "offset": hx(ref),
                    "objectType": T_WATCH,
                    "pidRef": ix.text(ref, 1),
                    "module": ix.text(ref, 2),
                    "name": ix.text(ref, 3),
                })
            chk["comparisons"].append(block)
        for m in ix.members(c, 6):
            code = int(ix.num(m, 1) or 0)
            chk["messages"].append({
                "resultCode": code,
                "result": RESULT.get(code, "code %d" % code),
                "text": ix.text(m, 2),
            })
        entry["checks"].append(chk)
    return entry


# --------------------------------------------------------------------------- #
# DTC
# --------------------------------------------------------------------------- #
def dtc_rows(ix, list_name):
    """Every row of one catalogue, in file order.

    dtc_row stores the record fields by their byte offset: +8 is the failure
    type letter and +12 the text. gds2_t76_tcm.json names those failureType and
    name, so they keep those names here.
    """
    return [{
        "offset": hx(r["off"]),
        "code": r["code"],
        "failureType": r["description"],
        "name": r["field_12"],
    } for r in ix.rows(
        "SELECT off, code, description, field_12 FROM dtc_row "
        "WHERE deliverable_id = ? AND list_name = ? ORDER BY idx",
        ix.did, list_name)]


def dtc_catalogue(ix, list_name):
    row = ix.one("SELECT idx, off, row_count, rows_off, row_stride "
                 "FROM dtc_list WHERE deliverable_id = ? AND name = ?",
                 ix.did, list_name)
    if row is None:
        return {"name": list_name, "note": "no dtc_list row for this name"}
    return {
        "name": list_name,
        "dataListIndex": row["idx"],
        "rowCount": row["row_count"],
        "dataOffset": hx(row["rows_off"]),
        "rowStride": row["row_stride"],
    }


def dtc_section(ix, node):
    """Which DTC catalogues the node binds, and their rows.

    The distinction is kept explicit: a local override list belongs to this
    module, whereas the master list is a corpus-wide text dictionary. A code
    being in the master list says GM assigned that code somewhere -- never that
    this ECM supports it.
    """
    dtc_obj = node["dtc_obj"]
    master_name = ix.text(dtc_obj, 2)
    out = {
        "folder": {
            "offset": hx(dtc_obj),
            "objectType": T_DTC_FOLDER,
            "masterFolder": ix.text(dtc_obj, 1),
        },
        "note": ("The node binds one corpus-wide master text dictionary plus "
                 "one local override list of its own. Only the override list "
                 "is module-specific; membership of the master dictionary is "
                 "not evidence that this ECM supports a code."),
        "localLists": [],
    }
    master = dtc_catalogue(ix, master_name)
    rows = [r for r in dtc_rows(ix, master_name)
            if r["code"] and r["code"][0] in ("P", "U")]
    master["filter"] = ("P and U codes only. The ECM is a powertrain module "
                        "and reports P and U codes; the B and C rows of the "
                        "master dictionary belong to body and chassis modules "
                        "and are left out to keep this pack small. Any B or C "
                        "code the node itself names is carried in full in "
                        "localLists.")
    master["rowsIncluded"] = len(rows)
    master["rows"] = rows
    out["masterList"] = master

    for lo in ix.members(dtc_obj, 0):
        name = ix.text(lo, 1)
        entry = dtc_catalogue(ix, name)
        entry["offset"] = hx(lo)
        entry["objectType"] = T_LOCAL_DTC_LIST
        entry["rows"] = dtc_rows(ix, name)
        out["localLists"].append(entry)
    return out


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_tree(ix, node):
    folders = []
    for fo in ix.list_members(node["test_obj"]):
        frow = ix.one("SELECT name FROM test_folder WHERE deliverable_id = ? "
                      "AND off = ?", ix.did, fo)
        folder = {
            "name": frow["name"] if frow else ix.text(fo, 1),
            "offset": hx(fo),
            "objectType": T_TEST_FOLDER,
            "groups": [],
        }
        for g in ix.rows("SELECT off, name, key FROM test_group WHERE "
                         "deliverable_id = ? AND folder_off = ? ORDER BY off",
                         ix.did, fo):
            group = {
                "name": g["name"],
                "key": g["key"],
                "offset": hx(g["off"]),
                "objectType": T_TEST_GROUP,
                "tests": [],
            }
            for t in ix.rows("SELECT off, key, name, test_type, n_bindings "
                             "FROM test WHERE deliverable_id = ? AND "
                             "group_off = ? ORDER BY off", ix.did, g["off"]):
                group["tests"].append({
                    "key": t["key"],
                    "name": t["name"],
                    "testType": t["test_type"],
                    "offset": hx(t["off"]),
                    "objectType": T_TEST,
                    "bindingCount": t["n_bindings"],
                    "frameBinding": "UNRESOLVED",
                })
            folder["groups"].append(group)
        folders.append(folder)
    return folders


# --------------------------------------------------------------------------- #
# the pack
# --------------------------------------------------------------------------- #
CAVEATS = [
    {
        "id": "shared-pid-set",
        "severity": "high",
        "applies": "parameters",
        "text": ("The PID set this node binds is GM's shared "
                 "'E39 4cyl LAF - 10 Equinox/Terrian' set, not an A20NFT set. "
                 "Only 14 applicability rows in the whole database mention "
                 "LHU, so the database cannot say which of these parameters "
                 "the A20NFT actually answers. Every parameter is therefore "
                 "marked applicability = UNVERIFIED, and NRC-31 on a real car "
                 "is expected for a fair number of them."),
    },
    {
        "id": "test-bindings-dead",
        "severity": "high",
        "applies": "tests",
        "text": ("test_binding is dead in this index: 0 of 467,860 rows "
                 "resolve to a data list, data node or object, and func_name "
                 "is empty in every row. There is no test -> frame and no "
                 "test -> watch-list join anywhere. Pairing a test with a "
                 "device control can only be done by name, and any such "
                 "pairing is UNVERIFIED."),
    },
    {
        "id": "no-security-access",
        "severity": "medium",
        "applies": "connection",
        "text": ("There are no security-access or seed/key rows for this ECM. "
                 "security_block has no row tying to the E39A node, so nothing "
                 "here unlocks a programming session."),
    },
    {
        "id": "no-thresholds",
        "severity": "medium",
        "applies": "parameters",
        "text": ("There are no pass/fail thresholds. The lo/hi values in each "
                 "conversion are the display limits GDS2 draws with, not a "
                 "specification, and some are plainly wrong -- Brake Booster "
                 "Pressure Sensor scales to kPa but carries hi = 1. Never "
                 "treat a range in this pack as a pass/fail limit."),
    },
]


def build(ix, node_off):
    node = ix.one("SELECT * FROM module_node WHERE deliverable_id = ? AND "
                  "item_off = ?", ix.did, node_off)
    if node is None:
        sys.exit("no module node at %d in deliverable %s"
                 % (node_off, ix.deliverable["label"]))
    node = dict(node)
    d = ix.deliverable

    doc = {
        "schema": 1,
        "caveats": CAVEATS,
        "deliverable": {
            "product": d["version"],
            "release": d["label"],
            "file": "BinaryFile/vatbin.bin",
            "path": d["path"],
            "size": d["size"],
            "sha1": d["sha1"],
            "fileMtime": d["file_mtime"],
            "headerFlags": "0x%X" % (d["flags"] or 0),
            "deliverableId": ix.did,
            "note": ("The index stores no marketing release string, so "
                     "release is the deliverable directory name. Deliverable "
                     "d_ov44 carries the same node but its DTC text layer is "
                     "corrupt, and is not used here."),
        },
        "moduleNode": {
            "name": node["name"],
            "offset": hx(node["item_off"]),
            "vehicle": node["vehicle"],
            "path": node["path"],
            "engineSelector": "Engine Identifier = 2.0L (LHU)",
            "note": ("The 2014, 2015, 2017 and 2018 Astra-J nodes bind the "
                     "same connection, PID set, DTC and test objects. The "
                     "strings VXR, OPC and A20NFT appear nowhere in the "
                     "database; Astra-J plus 2.0L (LHU) plus E39A (S3) are "
                     "the only handles."),
        },
    }

    # -- connection ---------------------------------------------------------
    conn = ix.one("SELECT * FROM module_conn WHERE deliverable_id = ? AND "
                  "container_off = ? ORDER BY off LIMIT 1",
                  ix.did, node["conn_obj"])
    if conn is not None:
        req = int(conn["request_id"], 16) if conn["request_id"] else 0
        doc["connection"] = {
            "offset": hx(conn["off"]),
            "objectType": T_CONN_RECORD,
            "module": conn["module"],
            "protocol": 0,
            "canRequest": "0x%03X" % req,
            "canResponse": "0x%03X" % (req + 8 if req >= 2016 else req),
            "bus": conn["bus"],
            "pidSetName": conn["pidset_name"],
            "responseRule": ("com/Mahle/VCI/c/m.java: request id >= 2016 "
                             "gives response filter = request + 8"),
            "masterLists": [r["master_list"] for r in ix.rows(
                "SELECT master_list FROM module_conn_list WHERE "
                "deliverable_id = ? AND off = ? ORDER BY idx",
                ix.did, node["conn_obj"])],
        }

    # -- PID sets, parameters, device controls ------------------------------
    doc["pidSets"] = []
    parameters, controls = [], []
    for set_off in ix.list_members(node["pidset_obj"]):
        s = ix.one("SELECT ref_name, module_name, n_pids FROM pid_set WHERE "
                   "deliverable_id = ? AND off = ?", ix.did, set_off)
        if s is None:
            continue
        doc["pidSets"].append({
            "offset": hx(set_off),
            "objectType": T_PID_SET,
            "ref": s["ref_name"],
            "module": s["module_name"],
            "pidCount": s["n_pids"],
        })
        for pid in ix.rows("SELECT off, ref_name, name, key FROM pid WHERE "
                           "deliverable_id = ? AND set_off = ? ORDER BY off",
                           ix.did, set_off):
            item = ix.one("SELECT * FROM pid_item WHERE deliverable_id = ? "
                          "AND pid_off = ? AND idx = 0", ix.did, pid["off"])
            if item is None:
                continue
            item = dict(item)
            entry = pid_entry(ix, pid, item, set_off)
            # Types 3 and 4 are both frames the tool sends, so both belong with
            # the writes. Return To Normal (20) and Null CPID (AE 00) are type
            # 4 and are how GDS2 ends every output control -- a caller needs
            # them beside the controls, not buried among the readable values.
            if item["pid_type"] in (3, 4):
                controls.append(device_control(entry, item))
            else:
                parameters.append(entry)

    parameters.sort(key=lambda e: (e["item"]["pidType"], e["item"]["id"],
                                   e["ref"]))
    controls.sort(key=lambda e: (e["item"]["pidType"], e["item"]["id"],
                                 e["name"], e["ref"]))
    doc["parameters"] = parameters
    doc["deviceControls"] = controls

    # -- DTC, conditions, tests --------------------------------------------
    doc["dtc"] = dtc_section(ix, node)
    doc["conditions"] = [condition_entry(ix, off)
                         for off in ix.list_members(node["timing_obj"])]
    doc["tests"] = test_tree(ix, node)
    return doc


def summarise(doc, out):
    by_type = {}
    for e in doc["parameters"] + doc["deviceControls"]:
        by_type[e["item"]["pidType"]] = by_type.get(e["item"]["pidType"], 0) + 1
    groups = sum(len(f["groups"]) for f in doc["tests"])
    tests = sum(len(g["tests"]) for f in doc["tests"] for g in f["groups"])
    print("wrote %s (%.1f MB)" % (out, os.path.getsize(out) / 1e6))
    print("  deliverable     %s, node %s"
          % (doc["deliverable"]["release"], doc["moduleNode"]["offset"]))
    print("  module          %s" % doc["moduleNode"]["name"])
    print("  connection      %s on %s"
          % (doc["connection"]["canRequest"], doc["connection"]["bus"]))
    print("  parameters      %d  (read $22 %d, identification $1A %d, "
          "derived %d)"
          % (len(doc["parameters"]), by_type.get(0, 0), by_type.get(2, 0),
             by_type.get(13, 0)))
    print("  device controls %d  (CPID $AE %d, raw frames %d) -- every one "
          "writes to the car"
          % (len(doc["deviceControls"]), by_type.get(3, 0), by_type.get(4, 0)))
    print("  DTC             %d local override rows, %d of %d master rows "
          "kept (%s)"
          % (sum(len(l.get("rows", [])) for l in doc["dtc"]["localLists"]),
             doc["dtc"]["masterList"]["rowsIncluded"],
             doc["dtc"]["masterList"]["rowCount"],
             "P and U codes only; B and C belong to body and chassis modules"))
    print("  conditions      %d" % len(doc["conditions"]))
    print("  tests           %d in %d groups in %d folders"
          % (tests, groups, len(doc["tests"])))
    print("  caveats         %d, and every parameter is applicability="
          "UNVERIFIED for the A20NFT" % len(doc["caveats"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=None, help="gds2 index path")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output JSON path")
    ap.add_argument("--deliverable", default=DELIVERABLE,
                    help="deliverable label to read (default %s)" % DELIVERABLE)
    ap.add_argument("--node", type=lambda s: int(s, 0), default=NODE_OFF,
                    help="module node item_off (default %d, the 2016 Astra-J "
                         "E39A ECM)" % NODE_OFF)
    args = ap.parse_args()

    db = args.db or str(paths.db_path("gds2"))
    ix = Index(db, args.deliverable)
    doc = build(ix, args.node)

    out = os.path.abspath(args.out)
    d = os.path.dirname(out)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    summarise(doc, out)


if __name__ == "__main__":
    main()
