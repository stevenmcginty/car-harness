"""Build the PSA DiagBox index from the already-extracted installer trees.

    python tools/dealer_index/build_diagbox.py

Reads C:\\Users\\steve\\Desktop\\diagbox\\extracted (override with
CARHARNESS_CORPUS_DIAGBOX). Nothing is extracted or installed here; the installer
was unpacked in an earlier session and this only reads what is on disk.

Two sources, handled differently.

*Diagnostic trees* (`vehicle\\Vehicle\\<MODEL>\\<FUNCTION>\\<FAMILY>\\*.s`) are
SGML-ish: `<Start posc=1 posl=1 ident=19>` has unquoted attributes, so an XML
parser rejects them. They are parsed leniently, tag by tag, and every screen,
actuator test and Polux label reference is indexed with the model, function and
ECU family it belongs to.

*FDB files* are Firebird databases whose page structure has not been decoded. They
are mined by record-pattern sweep, which recovers the parameter rows (name, inline
French label, DID, format, conversion) and the `$PV$ $M$ $C$` family records with
their byte offsets. What each sweep covered is recorded in `fdb_sweep`; the rest
of those files remains unread, and the registry says so.
"""

import argparse
import datetime
import hashlib
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths                                     # noqa: E402

TAG_RE = re.compile(r"<\s*(/?)([A-Za-z_][\w.-]*)([^>]*)>")
ATTR_RE = re.compile(r'([\w.:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|[^\s>]+)')
POLUX_RE = re.compile(r"@?(P\d+)-POLUXDATA")
LOCAL_ID_RE = re.compile(r"_([0-9A-Fa-f]{2})$")

# The inline labels are French in latin-1, so accented bytes have to count as
# printable or every label is truncated at its first accent.
PRINTABLE = re.compile(rb"[\x20-\x7E\xA0-\xFF]{3,}")
MP_RE = re.compile(rb"\b(MP[A-Z]?_[A-Z0-9_]{2,})")
FAMILY_RE = re.compile(
    rb"\$PV\$(\S+)\s+\$M\$(\S+)\s+\$C\$(\S+?)(?:\s+\$PD\$(\S+))?[\x00-\x1F]")
MESSAGERIE_RE = re.compile(rb"\b(INJ_[A-Z0-9_]+_\d+_[A-Z0-9]+\.xml)")
IOCB_RE = re.compile(rb"\b(IOCBLID[A-Z]*_[A-Z0-9_]+)")
DID_TOKEN = re.compile(r"^[0-9A-F]{4}$")
EDGE_NOISE = re.compile(r"^[^\x20-\x7E]+|[^\x20-\x7E]+$")
FORMATS = {"UNM", "SNM", "ENUM", "ENUM_TEXT", "ASCII", "HEXA", "BOOL", "TEXT",
           "BIT", "BITS"}
CONVERSIONS = {"FLOAT", "INT", "LINEAR", "TABLE", "DIRECT", "HEXA", "ASCII",
               "BOOL"}


def sha1_of(path, limit=32 * 1024 * 1024):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        done = 0
        while done < limit:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
    return h.hexdigest()


def parse_attrs(blob):
    out = []
    for m in ATTR_RE.finditer(blob):
        val = m.group(3) if m.group(3) is not None else (
            m.group(4) if m.group(4) is not None else m.group(2))
        out.append((m.group(1), val))
    return out


class DiagboxBuilder(object):
    def __init__(self, conn, root, verbose=True):
        self.conn = conn
        self.root = root
        self.verbose = verbose
        self.counts = {}

    def log(self, msg):
        if self.verbose:
            sys.stderr.write("  %s\n" % msg)
            sys.stderr.flush()

    def bump(self, k, n=1):
        self.counts[k] = self.counts.get(k, 0) + n

    def add_file(self, abs_path, kind, model=None, function=None, family=None):
        rel = os.path.relpath(abs_path, self.root)
        cur = self.conn.execute(
            "INSERT OR REPLACE INTO source_file (rel_path, abs_path, size, sha1,"
            " kind, model, function, family) VALUES (?,?,?,?,?,?,?,?)",
            (rel, abs_path, os.path.getsize(abs_path), sha1_of(abs_path), kind,
             model, function, family))
        self.bump("files")
        return cur.lastrowid

    # ------------------------------------------------------ diagnostic trees --
    def do_trees(self):
        base = None
        for cand in (os.path.join(self.root, "vehicle", "Vehicle"),
                     os.path.join(self.root, "Vehicle")):
            if os.path.isdir(cand):
                base = cand
                break
        if not base:
            self.log("no diagnostic trees found")
            return
        n = 0
        for model in sorted(os.listdir(base)):
            mdir = os.path.join(base, model)
            if not os.path.isdir(mdir):
                continue
            for dirpath, _dirs, files in os.walk(mdir):
                rel = os.path.relpath(dirpath, mdir).split(os.sep)
                function = rel[0] if rel and rel[0] != "." else None
                family = rel[1] if len(rel) > 1 else None
                for fn in sorted(files):
                    if not fn.lower().endswith(".s"):
                        continue
                    self.do_tree_file(os.path.join(dirpath, fn), model,
                                      function, family)
                    n += 1
            self.conn.commit()
            self.log("model %s (%d scripts so far)" % (model, n))

    def do_tree_file(self, path, model, function, family):
        try:
            text = open(path, "r", encoding="latin-1").read()
        except Exception:
            return
        sfid = self.add_file(path, "tree", model, function, family)
        depth = 0
        rows = []
        screens = []
        for seq, m in enumerate(TAG_RE.finditer(text)):
            closing, tag, blob = m.group(1), m.group(2), m.group(3)
            if closing:
                depth = max(depth - 1, 0)
                continue
            attrs = parse_attrs(blob)
            adict = dict(attrs)
            name = (adict.get("Name") or adict.get("ScreenName")
                    or adict.get("name"))
            rows.append((sfid, seq, depth, tag,
                         name, " ".join("%s=%s" % kv for kv in attrs)[:1000]))
            if name and ("Scr" in tag or "Screen" in tag):
                lid = LOCAL_ID_RE.search(name)
                screens.append((sfid, tag, name,
                                lid.group(1).upper() if lid else None,
                                model, function, family))
            if not blob.endswith("/"):
                depth += 1
        self.conn.executemany(
            "INSERT INTO tree_node VALUES (?,?,?,?,?,?)", rows)
        self.bump("tree_nodes", len(rows))
        if screens:
            self.conn.executemany(
                "INSERT INTO tree_screen VALUES (?,?,?,?,?,?,?)", screens)
            self.bump("tree_screens", len(screens))
        labels = []
        for m in POLUX_RE.finditer(text):
            start = max(m.start() - 60, 0)
            labels.append((sfid, m.group(1), text[start:m.end() + 20]))
        if labels:
            self.conn.executemany("INSERT INTO tree_label VALUES (?,?,?)",
                                  labels)
            self.bump("tree_labels", len(labels))

    # -------------------------------------------------------------- FDB mine --
    def do_fdbs(self):
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in sorted(files):
                if not fn.lower().endswith(".fdb"):
                    continue
                self.do_fdb(os.path.join(dirpath, fn))
                self.conn.commit()

    def do_fdb(self, path):
        sfid = self.add_file(path, "fdb")
        t0 = time.time()
        with open(path, "rb") as fh:
            data = fh.read()
        n_param = self.sweep_params(sfid, data)
        n_family = self.sweep_families(sfid, data)
        n_ref = self.sweep_references(sfid, data)
        self.conn.executemany(
            "INSERT INTO fdb_sweep VALUES (?,?,?,?,?)",
            [(sfid, "parameters", MP_RE.pattern.decode("latin-1"), n_param,
              "parameter name, inline label, DID, format and conversion"),
             (sfid, "families", FAMILY_RE.pattern.decode("latin-1"), n_family,
              "$PV$ vehicle / $M$ engines / $C$ ECU family records"),
             (sfid, "references", "messagerie xml, IOCBLID verbs", n_ref,
              "cross references named inside the database")])
        self.log("%s: %d params, %d families, %d refs (%.0fs)"
                 % (os.path.basename(path), n_param, n_family, n_ref,
                    time.time() - t0))

    def sweep_params(self, sfid, data):
        """One parameter row is a name, then its inline label, then a trailer
        carrying the DID, the numeric format and the conversion. The trailer sits
        after the name, which is why an earlier hand read of the file paired the
        wrong DID with the wrong name if it looked backwards."""
        rows = []
        starts = [m.start() for m in MP_RE.finditer(data)]
        for i, m in enumerate(MP_RE.finditer(data)):
            name = m.group(1).decode("latin-1")
            end = m.end()
            stop = starts[i + 1] if i + 1 < len(starts) else end + 400
            window = data[end:min(stop, end + 400)]
            toks = [t.decode("latin-1") for t in PRINTABLE.findall(window)]
            label = did = fmt = conv = None
            for t in toks:
                t = t.strip()
                if not t:
                    continue
                # Accented bytes count as printable so that French labels
                # survive, but that also glues the surrounding record bytes onto
                # short tokens ("UNM\xfb"). Strip non-ASCII from the ends before
                # classifying; keep the raw run for the label.
                core = EDGE_NOISE.sub("", t)
                if did is None and DID_TOKEN.match(core):
                    did = core
                elif fmt is None and core in FORMATS:
                    fmt = core
                elif conv is None and core in CONVERSIONS:
                    conv = core
                elif label is None and len(t) > 3 and not core.startswith("MP_"):
                    if not DID_TOKEN.match(core) and core not in FORMATS \
                            and core not in CONVERSIONS:
                        label = t
            rows.append((sfid, m.start(), name, label, did, fmt, conv))
            if len(rows) >= 20000:
                self.conn.executemany(
                    "INSERT INTO fdb_param VALUES (?,?,?,?,?,?,?)", rows)
                del rows[:]
        if rows:
            self.conn.executemany(
                "INSERT INTO fdb_param VALUES (?,?,?,?,?,?,?)", rows)
        n = len(starts)
        self.bump("fdb_params", n)
        return n

    def sweep_families(self, sfid, data):
        rows = []
        for m in FAMILY_RE.finditer(data):
            rows.append((sfid, m.start(),
                         m.group(1).decode("latin-1"),
                         m.group(2).decode("latin-1"),
                         m.group(3).decode("latin-1"),
                         m.group(4).decode("latin-1") if m.group(4) else None))
        if rows:
            self.conn.executemany(
                "INSERT INTO fdb_family VALUES (?,?,?,?,?,?)", rows)
        self.bump("fdb_families", len(rows))
        return len(rows)

    def sweep_references(self, sfid, data):
        rows = []
        for m in MESSAGERIE_RE.finditer(data):
            rows.append((sfid, m.start(), "messagerie",
                         m.group(1).decode("latin-1")))
        for m in IOCB_RE.finditer(data):
            rows.append((sfid, m.start(), "iocblid",
                         m.group(1).decode("latin-1")))
        if rows:
            self.conn.executemany(
                "INSERT INTO fdb_reference VALUES (?,?,?,?)", rows)
        self.bump("fdb_references", len(rows))
        return len(rows)

    # ------------------------------------------------------------- leftovers --
    def do_other(self):
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in sorted(files):
                low = fn.lower()
                kind = None
                if low.endswith(".ini"):
                    kind = "ini"
                elif low.endswith(".xml"):
                    kind = "xml"
                elif low.endswith((".asc", ".crv")):
                    kind = "curve"
                elif low.endswith(".du8"):
                    kind = "labels"
                elif low.endswith(".csv"):
                    kind = "derived"
                if kind:
                    self.add_file(os.path.join(dirpath, fn), kind)
        self.conn.commit()

    def run(self):
        t0 = time.time()
        self.do_trees()
        self.do_fdbs()
        self.do_other()
        self.conn.commit()
        self.log("done in %.0fs" % (time.time() - t0))
        for k in sorted(self.counts):
            self.log("%-18s %d" % (k, self.counts[k]))


INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_dbx_screen ON tree_screen(screen)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_screen_fam ON tree_screen(family)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_screen_model ON tree_screen(model, function)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_node_name ON tree_node(name)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_node_file ON tree_node(source_file_id)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_label ON tree_label(polux_id)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_param_name ON fdb_param(name)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_param_did ON fdb_param(did)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_family ON fdb_family(family)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_family_engine ON fdb_family(engines)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_ref ON fdb_reference(value)",
    "CREATE INDEX IF NOT EXISTS ix_dbx_srcfile ON source_file(kind, model)",
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
    ap.add_argument("--skip-fdb", action="store_true",
                    help="index only the diagnostic trees")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = args.corpus or os.path.join(str(paths.corpus_root("diagbox")),
                                       "extracted")
    if not os.path.isdir(root):
        sys.exit("DiagBox corpus not found at %s" % root)
    db = args.db or str(paths.db_path("diagbox"))
    if os.path.exists(db):
        os.remove(db)
    conn = open_db(db, os.path.join(HERE, "schema_diagbox.sql"))
    conn.execute(
        "INSERT OR REPLACE INTO corpus (name, root, built_at, builder, note)"
        " VALUES (?,?,?,?,?)",
        ("diagbox-V09.233", root,
         datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "tools/dealer_index/build_diagbox.py",
         "Firebird FDB page structure not decoded; FDB content is mined by "
         "record-pattern sweep only"))
    b = DiagboxBuilder(conn, root, verbose=not args.quiet)
    if args.skip_fdb:
        b.do_trees()
        b.do_other()
    else:
        b.run()
    sys.stderr.write("creating indexes\n")
    for sql in INDEXES:
        conn.execute(sql)
    conn.commit()
    conn.close()
    sys.stderr.write("database: %s (%.1f MB)\n"
                     % (db, os.path.getsize(db) / 1e6))


if __name__ == "__main__":
    main()
