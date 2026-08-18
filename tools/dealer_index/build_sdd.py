"""Build the JLR SDD index from the decrypted module database.

    python tools/dealer_index/build_sdd.py
    python tools/dealer_index/build_sdd.py --platforms L494_201600,X260_201600

Reads the SDD corpus in place under C:\\Users\\steve\\Desktop\\sdd169 (override
with CARHARNESS_CORPUS_SDD). The .exml module files are decrypted in memory with
the key recovered from Environment.dll's CCryptFile constructor -- 3DES-ECB,
PKCS5 padding, key "YmZwZlQrQ3V4dVltNTArWE9s" (captures/jlr/sdd169-extraction.md).
Plaintext is never written back into the corpus; the decrypted XML is stored
zlib-compressed in the database instead, so every later question can be answered
from the index alone.

What comes out: per-platform ECU addressing (the CAN request/response ids), the
MDX data-identifier tables with their scaling, units and access rules, the
enumerated states, the routine identifiers, and the plaintext DTC and on-demand
self-test catalogues.
"""

import argparse
import datetime
import hashlib
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths                                     # noqa: E402

# 3DES key, stored nibble-swapped in the DLL constructor.
_KEY_BLOB = bytes([0x95, 0xd6, 0xa5, 0x77, 0xa5, 0xc6, 0x15, 0x27, 0x15, 0x33,
                   0x65, 0x43, 0x46, 0x65, 0xc6, 0x47, 0xe4, 0x45, 0x14, 0x27,
                   0x75, 0x54, 0x93, 0x37, 0x25, 0x13, 0x07, 0x24, 0x15, 0x45,
                   0x03, 0x93])
EXML_KEY = bytes(((b >> 4) | (b << 4)) & 0xFF for b in _KEY_BLOB)[:24]

XML_GROUP = "CURRENT_JLR_XCL_XML_DATA_XML"


def decrypt_exml(data):
    """3DES-ECB with PKCS5 padding. Imported lazily so that the plaintext parts
    of the corpus can still be indexed on a machine without pycryptodome."""
    from Crypto.Cipher import DES3
    pt = DES3.new(EXML_KEY, DES3.MODE_ECB).decrypt(data)
    if pt:
        pad = pt[-1]
        if 1 <= pad <= 8 and all(b == pad for b in pt[-pad:]):
            pt = pt[:-pad]
    return pt


def text_of(node, tag, default=None):
    e = node.find(tag)
    if e is None:
        return default
    return (e.text or "").strip()


def flatten(node, limit=4000):
    """Whole subtree as a compact string. Used for the sections that have no
    table of their own, so that nothing in a file is silently dropped."""
    parts = []
    for e in node.iter():
        bits = [e.tag]
        for k, val in sorted(e.attrib.items()):
            bits.append("%s=%s" % (k, val))
        txt = (e.text or "").strip()
        if txt:
            bits.append(repr(txt))
        if len(bits) > 1 or txt:
            parts.append(" ".join(bits))
    return " ; ".join(parts)[:limit]


def parse_int(s):
    if not s:
        return None
    s = s.strip()
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s, 10)
    except ValueError:
        m = re.search(r"[0-9A-Fa-f]{2,8}", s)
        if m:
            try:
                return int(m.group(0), 16)
            except ValueError:
                return None
    return None


class SddBuilder(object):
    def __init__(self, conn, root, verbose=True):
        self.conn = conn
        self.root = root
        self.verbose = verbose
        self.counts = {}

    def log(self, msg):
        if self.verbose:
            sys.stderr.write("  %s\n" % msg)
            sys.stderr.flush()

    def bump(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    # ------------------------------------------------------------- files ----
    def add_file(self, abs_path, group, platform, kind, xml_bytes, root_tag,
                 err=None):
        rel = os.path.relpath(abs_path, self.root)
        st = os.stat(abs_path)
        h = hashlib.sha1()
        with open(abs_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        blob = zlib.compress(xml_bytes, 6) if xml_bytes else None
        cur = self.conn.execute(
            "INSERT OR REPLACE INTO source_file (group_name, platform, rel_path,"
            " abs_path, size, sha1, encrypted, root_tag, kind, parse_error, xml)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (group, platform, rel, abs_path, st.st_size, h.hexdigest(),
             1 if abs_path.lower().endswith(".exml") else 0, root_tag, kind,
             err, blob))
        self.bump("files")
        return cur.lastrowid

    # ------------------------------------------------------ module files ----
    def do_ecus(self, sfid, platform, root):
        for ecu in root.iter("ECU"):
            qual = ecu.find("qualifier")
            verona = ecu.find("VERONA")
            can = ecu.find(".//CAN")
            addressing = ecu.find(".//addressing")
            cur = self.conn.execute(
                "INSERT INTO ecu (source_file_id, platform, ecu_ref, moduletype,"
                " ecu_type, model, year, can_src, can_target, addr_type,"
                " addr_format, bit_len, accept_mask, baud_rate, spec_version,"
                " device_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sfid, platform, ecu.get("id"), ecu.get("moduletype"),
                 ecu.get("type"),
                 qual.get("model") if qual is not None else None,
                 qual.get("year") if qual is not None else None,
                 can.get("srcId") if can is not None else None,
                 can.get("targetId") if can is not None else None,
                 addressing.get("type") if addressing is not None else None,
                 addressing.get("format") if addressing is not None else None,
                 verona.get("bitLen") if verona is not None else None,
                 verona.get("acceptMask") if verona is not None else None,
                 verona.get("baudRate") if verona is not None else None,
                 verona.get("specVersion") if verona is not None else None,
                 verona.get("deviceNo") if verona is not None else None))
            eid = cur.lastrowid
            self.bump("ecus")
            if qual is not None:
                for q in qual.findall("qual"):
                    self.conn.execute(
                        "INSERT INTO ecu_qual VALUES (?,?,?,?)",
                        (eid, q.get("type"), q.get("subtype"), q.get("value")))
            for section in ("properties", "swdl"):
                for holder in ecu.iter(section):
                    for p in holder.findall("property"):
                        self.conn.execute(
                            "INSERT INTO ecu_property VALUES (?,?,?,?)",
                            (eid, section, p.get("id"), p.get("value")))
                        self.bump("ecu_properties")
            for op in ecu.iter("operation"):
                self.conn.execute("INSERT INTO ecu_security VALUES (?,?,?)",
                                  (eid, op.get("type"), op.get("value")))

    # --------------------------------------------------------- MDX files ----
    def do_mdx(self, sfid, platform, stem, root):
        admin = root.find("ADMINISTRATION")
        cur = self.conn.execute(
            "INSERT INTO mdx (source_file_id, platform, file_stem, ecu_name,"
            " shortname) VALUES (?,?,?,?,?)",
            (sfid, platform, stem,
             text_of(admin, "ECU_NAME") if admin is not None else None,
             text_of(admin, "SHORTNAME") if admin is not None else None))
        mdx_id = cur.lastrowid
        self.bump("mdx")

        for did in root.iter("DID"):
            self.do_did(mdx_id, did)
        for rout in root.iter("ROUTINE"):
            self.do_routine(mdx_id, rout)
        for area in root.iter("MEMORY_AREA"):
            self.conn.execute(
                "INSERT INTO memory_area VALUES (?,?,?,?,?)",
                (mdx_id, area.get("ID"), text_of(area, "NAME"),
                 text_of(area, "START_ADDRESS"), flatten(area)))
            self.bump("memory_areas")
        for section in ("SECURITY_DATA", "SPECIAL_PROCEDURES", "DEVIATIONS",
                        "PROTOCOL", "DIAGNOSTIC_TROUBLE_CODES"):
            for node in root.iter(section):
                detail = flatten(node)
                if detail and detail != section:
                    self.conn.execute("INSERT INTO mdx_section VALUES (?,?,?)",
                                      (mdx_id, section, detail))

    def do_did(self, mdx_id, did):
        access = did.find("ACCESS_PARAMETERS")
        readable = access.find("READABLE") if access is not None else None
        writeable = access.find("WRITEABLE") if access is not None else None
        control = access.find("CONTROLLABLE") if access is not None else None
        number = text_of(did, "NUMBER")
        cur = self.conn.execute(
            "INSERT INTO did (mdx_id, did_ref, number, number_int, name,"
            " description, byte_size, did_type, read_services, read_sessions,"
            " read_security, write_services, write_sessions, write_security,"
            " control_services, control_sessions, control_iocp)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mdx_id, did.get("ID"), number, parse_int(number),
             text_of(did, "NAME"), text_of(did, "DESCRIPTION"),
             parse_int(text_of(did, "BYTE_SIZE")), text_of(did, "DID_TYPE"),
             readable.get("SERVICE_REFS") if readable is not None else None,
             readable.get("SESSION_REFS") if readable is not None else None,
             readable.get("SECURITY_REFS") if readable is not None else None,
             writeable.get("SERVICE_REFS") if writeable is not None else None,
             writeable.get("SESSION_REFS") if writeable is not None else None,
             writeable.get("SECURITY_REFS") if writeable is not None else None,
             control.get("SERVICE_REFS") if control is not None else None,
             control.get("SESSION_REFS") if control is not None else None,
             control.get("IOCP_REFS") if control is not None else None))
        did_id = cur.lastrowid
        self.bump("dids")

        for idx, sub in enumerate(did.findall("SUB_FIELD")):
            defn = sub.find("DATA_DEFINITION")
            num = defn.find("NUMERIC_PARAMETERS") if defn is not None else None
            cur = self.conn.execute(
                "INSERT INTO did_subfield (did_id, sub_ref, idx, name, lsb, msb,"
                " data_type, resolution, offset, units, range_low, range_high,"
                " revert) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did_id, sub.get("ID"), idx, text_of(sub, "NAME"),
                 parse_int(text_of(sub, "LEAST_SIG_BIT")),
                 parse_int(text_of(sub, "MOST_SIG_BIT")),
                 text_of(defn, "DATA_TYPE") if defn is not None else None,
                 text_of(num, "RESOLUTION") if num is not None else None,
                 text_of(num, "OFFSET") if num is not None else None,
                 text_of(num, "UNITS") if num is not None else None,
                 text_of(num, "RANGE_LOW") if num is not None else None,
                 text_of(num, "RANGE_HIGH") if num is not None else None,
                 text_of(defn, "DATA_REVERT") if defn is not None else None))
            sub_id = cur.lastrowid
            self.bump("subfields")
            if defn is None:
                continue
            for member in defn.iter("ENUM_MEMBER"):
                self.conn.execute(
                    "INSERT INTO did_enum VALUES (?,?,?,?,?,?)",
                    (sub_id, did_id, member.get("ID"),
                     text_of(member, "ENUM_VALUE"), text_of(member, "NAME"),
                     text_of(member, "DESCRIPTION")))
                self.bump("enums")

    def do_routine(self, mdx_id, rout):
        number = text_of(rout, "NUMBER")
        cur = self.conn.execute(
            "INSERT INTO routine (mdx_id, routine_ref, number, number_int, name,"
            " description, session_refs, max_run_time, restart)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (mdx_id, rout.get("ID"), number, parse_int(number),
             text_of(rout, "NAME"), text_of(rout, "DESCRIPTION"),
             rout.get("SESSION_REFS"), text_of(rout, "MAX_ROUTINE_RUN_TIME"),
             text_of(rout, "RESTART_WHILE_RUNNING")))
        rid = cur.lastrowid
        self.bump("routines")
        for p in rout.iter("ROUTINE_PARAM"):
            self.conn.execute("INSERT INTO routine_param VALUES (?,?,?,?)",
                              (rid, p.get("ID"), text_of(p, "NAME"),
                               flatten(p, 1000)))

    # ------------------------------------------------ plaintext catalogues ---
    def do_dtc_catalogue(self, sfid, root):
        for dtc in root.iter("dtc"):
            self.conn.execute(
                "INSERT INTO dtc_description VALUES (?,?,?,?)",
                (sfid, dtc.get("id"), dtc.get("module"),
                 (dtc.text or "").strip()))
            self.bump("dtc_descriptions")
        for ft in root.iter("faultType"):
            self.conn.execute(
                "INSERT INTO dtc_fault_type VALUES (?,?,?)",
                (sfid, parse_int(ft.get("number")), (ft.text or "").strip()))
            self.bump("fault_types")

    def do_odst(self, sfid, root):
        module = root.get("moduleType")
        for sel in root.iter("odstTestSelection"):
            self.conn.execute(
                "INSERT INTO odst_test VALUES (?,?,?,?,?,?,?)",
                (sfid, module, sel.get("testID"),
                 sel.get("helpScreenDataNameId"), sel.get("timeout"),
                 sel.get("time"), sel.get("flag")))
            self.bump("odst_tests")
            for q in sel.findall("odstTestQualifier"):
                self.conn.execute(
                    "INSERT INTO odst_qualifier VALUES (?,?,?,?,?)",
                    (sfid, module, sel.get("testID"), q.get("model"),
                     q.get("year")))

    # ------------------------------------------------------------- driver ---
    def run(self, platforms=None, store_xml=True):
        t0 = time.time()
        xml_root = None
        for cand in (os.path.join(self.root, "xcl", XML_GROUP, "Xml"),
                     os.path.join(self.root, "db", XML_GROUP, "Xml")):
            if os.path.isdir(cand):
                xml_root = cand
                break
        if xml_root:
            self.walk_modules(xml_root, platforms, store_xml)
        else:
            self.log("module database not found under %s" % self.root)
        self.walk_plaintext(store_xml)
        self.conn.commit()
        self.log("done in %.0fs" % (time.time() - t0))
        for k in sorted(self.counts):
            self.log("%-18s %d" % (k, self.counts[k]))

    def walk_modules(self, xml_root, platforms, store_xml):
        names = sorted(d for d in os.listdir(xml_root)
                       if os.path.isdir(os.path.join(xml_root, d)))
        if platforms:
            names = [n for n in names if n in platforms]
        for platform in names:
            d = os.path.join(xml_root, platform)
            for fn in sorted(os.listdir(d)):
                p = os.path.join(d, fn)
                if not os.path.isfile(p):
                    continue
                low = fn.lower()
                if not (low.endswith(".exml") or low.endswith(".xml")):
                    continue
                try:
                    raw = open(p, "rb").read()
                    data = decrypt_exml(raw) if low.endswith(".exml") else raw
                except Exception as exc:
                    self.add_file(p, XML_GROUP, platform, "other", None, None,
                                  "read/decrypt: %s" % exc)
                    continue
                try:
                    root = ET.fromstring(data)
                except Exception as exc:
                    self.add_file(p, XML_GROUP, platform, "other",
                                  data if store_xml else None, None,
                                  "parse: %s" % exc)
                    continue
                stem = os.path.splitext(fn)[0]
                kind = {"ECUs": "module", "ECU": "module", "MDX": "mdx"}.get(
                    root.tag, "gui" if root.tag == "gui_class" else "other")
                sfid = self.add_file(p, XML_GROUP, platform, kind,
                                     data if store_xml else None, root.tag)
                if root.tag in ("ECUs", "ECU"):
                    self.do_ecus(sfid, platform, root)
                elif root.tag == "MDX":
                    self.do_mdx(sfid, platform, stem, root)
            self.conn.commit()
            self.log("platform %s" % platform)

    def walk_plaintext(self, store_xml):
        db = os.path.join(self.root, "db")
        if not os.path.isdir(db):
            return
        for group in sorted(os.listdir(db)):
            gdir = os.path.join(db, group)
            if not os.path.isdir(gdir) or group == XML_GROUP:
                continue
            for dirpath, _dirs, files in os.walk(gdir):
                for fn in sorted(files):
                    if not fn.lower().endswith(".xml"):
                        continue
                    p = os.path.join(dirpath, fn)
                    try:
                        data = open(p, "rb").read()
                        root = ET.fromstring(data)
                    except Exception as exc:
                        self.add_file(p, group, None, "other", None, None,
                                      "parse: %s" % exc)
                        continue
                    kind = "other"
                    if root.tag in ("dtcDescriptionList", "faultTypeList"):
                        kind = "dtc"
                    elif root.tag == "odstInfo":
                        kind = "odst"
                    keep = data if (store_xml and kind != "other") else None
                    sfid = self.add_file(p, group, None, kind, keep, root.tag)
                    if kind == "dtc":
                        self.do_dtc_catalogue(sfid, root)
                    elif kind == "odst":
                        self.do_odst(sfid, root)
            self.conn.commit()
            self.log("group %s" % group)


INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_ecu_module ON ecu(moduletype)",
    "CREATE INDEX IF NOT EXISTS ix_ecu_platform ON ecu(platform)",
    "CREATE INDEX IF NOT EXISTS ix_ecu_target ON ecu(can_target)",
    "CREATE INDEX IF NOT EXISTS ix_ecu_model ON ecu(model, year)",
    "CREATE INDEX IF NOT EXISTS ix_mdx_short ON mdx(shortname)",
    "CREATE INDEX IF NOT EXISTS ix_mdx_platform ON mdx(platform)",
    "CREATE INDEX IF NOT EXISTS ix_did_number ON did(number)",
    "CREATE INDEX IF NOT EXISTS ix_did_number_int ON did(number_int)",
    "CREATE INDEX IF NOT EXISTS ix_did_name ON did(name)",
    "CREATE INDEX IF NOT EXISTS ix_did_mdx ON did(mdx_id)",
    "CREATE INDEX IF NOT EXISTS ix_sub_did ON did_subfield(did_id)",
    "CREATE INDEX IF NOT EXISTS ix_enum_did ON did_enum(did_id)",
    "CREATE INDEX IF NOT EXISTS ix_routine_number ON routine(number)",
    "CREATE INDEX IF NOT EXISTS ix_routine_name ON routine(name)",
    "CREATE INDEX IF NOT EXISTS ix_routine_mdx ON routine(mdx_id)",
    "CREATE INDEX IF NOT EXISTS ix_dtcdesc_code ON dtc_description(code_id)",
    "CREATE INDEX IF NOT EXISTS ix_dtcdesc_module ON dtc_description(module)",
    "CREATE INDEX IF NOT EXISTS ix_odst_module ON odst_test(module_type)",
    "CREATE INDEX IF NOT EXISTS ix_srcfile_kind ON source_file(kind)",
    "CREATE INDEX IF NOT EXISTS ix_srcfile_platform ON source_file(platform)",
]


def open_db(path, schema_path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-200000")
    with open(schema_path, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None)
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--platforms", default=None,
                    help="comma separated platform directories to index")
    ap.add_argument("--no-xml", action="store_true",
                    help="do not store the decrypted XML in the database")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = args.corpus or str(paths.corpus_root("sdd"))
    if not os.path.isdir(root):
        sys.exit("SDD corpus not found at %s" % root)
    db = args.db or str(paths.db_path("sdd"))
    if os.path.exists(db):
        os.remove(db)
    conn = open_db(db, os.path.join(HERE, "schema_sdd.sql"))
    conn.execute(
        "INSERT OR REPLACE INTO corpus (name, root, built_at, builder, note)"
        " VALUES (?,?,?,?,?)",
        ("sdd169", root,
         datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "tools/dealer_index/build_sdd.py",
         "SDD 169.00.001; .exml decrypted in memory, 3DES-ECB PKCS5"))
    platforms = set(args.platforms.split(",")) if args.platforms else None
    b = SddBuilder(conn, root, verbose=not args.quiet)
    b.run(platforms, store_xml=not args.no_xml)
    sys.stderr.write("creating indexes\n")
    for sql in INDEXES:
        conn.execute(sql)
    conn.commit()
    conn.close()
    sys.stderr.write("database: %s (%.1f MB)\n"
                     % (db, os.path.getsize(db) / 1e6))


if __name__ == "__main__":
    main()
