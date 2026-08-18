"""Build the GDS2 index: one full extraction of a vatbin.bin into SQLite.

    python tools/dealer_index/build_gds2.py            # every deliverable found
    python tools/dealer_index/build_gds2.py _x_2023ov  # one deliverable by label

The point of this program is that no future question about the Vauxhall/Opel or
GM Global dealer data should require another scan of the binary. It walks the
whole reachable structure once and writes it out with provenance on every row:

  * the header tables -- VAT functions, the DTC catalogues, the RPO option pairs
  * every state machine, every state, every transition, every machine attribute
  * the whole menu-item tree, its containers, attributes and state triples
  * every value object reachable from those roots, with every field decoded
  * semantic tables on top: module nodes, connection records with their CAN
    request ids, PID sets, PIDs with their request parameters, conversions and
    enumerated states, test folders/groups/tests and their state bindings,
    data-display lists, module timing, applicability, OSC setups and commands,
    named parameters and security blocks.

Format reference: captures/vauxhall/vatbin-decode-notes.md and the decompiled
runtime under C:\\Users\\steve\\Desktop\\vauxhall\\unpack\\src\\com\\Mahle\\b\\a\\.
"""

import argparse
import datetime
import hashlib
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "gds2"))

import paths                                    # noqa: E402
from vatbin import Vatbin                       # noqa: E402

BATCH = 20000

# Object types that get their own semantic table. The generic object/object_field
# tables hold these as well, so nothing is lost when a decode below is incomplete.
T_BINDING = 16823
T_DATA_NODE = 16924
T_OSC_COMMAND = 16982
T_OSC_SETUP = 17000
T_OSC_INIT = 17014
T_NAMED_PARAM = 17068
T_SECURITY = 17145
T_ENUM_STATE = 17169
T_ENUM_TABLE = 17172
T_CONVERSION = 17276
T_VCI_ITEM = 17283
T_PID = 17297
T_PID_SET = 17305
T_WATCH_SET = 17319
T_TIMING = 17346
T_DATA_LIST = 17355
T_APPLIC_COND = 17423
T_APPLIC_ITEM = 17426
T_CONN_RECORD = 17466
T_TEST = 17483
T_TEST_GROUP = 17489
T_CONN_CONTAINER = 17515
T_TEST_FOLDER = 17533

# Persistent-machine state numbers seen on module menu items.
STATE_CONNECTION = 2
STATE_PIDSETS = 3
STATE_DATALISTS = 8
STATE_APPLICABILITY = 9
STATE_DTC = 11
STATE_TIMING = 12
STATE_TESTS = 13


class Rows(object):
    """Batching insert helper. Keeps memory flat on a 137 MB input."""

    def __init__(self, conn):
        self.conn = conn
        self.buf = {}
        self.counts = {}

    def add(self, table, sql, row):
        b = self.buf.setdefault(table, [])
        b.append(row)
        self.counts[table] = self.counts.get(table, 0) + 1
        if len(b) >= BATCH:
            self.conn.executemany(sql, b)
            del b[:]
        self.buf[table] = b
        self._sql = getattr(self, "_sql", {})
        self._sql[table] = sql

    def flush(self):
        for table, b in self.buf.items():
            if b:
                self.conn.executemany(self._sql[table], b)
                del b[:]
        self.conn.commit()


class Builder(object):
    def __init__(self, conn, vat, deliverable_id, verbose=True, langs=None):
        self.conn = conn
        self.v = vat
        self.did = deliverable_id
        self.rows = Rows(conn)
        self.verbose = verbose
        # None keeps every language record of every chain; a set of language ids
        # keeps only those (plus the first record, which carries the key).
        self.langs = langs
        self.text_cache = {}
        self.string_heads = set()
        # Visited bitmap, one bit per file byte. Object offsets are NOT always
        # 4-byte aligned: the Opel/Vauxhall deliverables happen to be, the GM
        # Global ones are 2-aligned, and nothing in the format promises either.
        self.seen = bytearray((vat.size // 8) + 2)
        self.func_by_id = {}
        self.machine_state_func = {}   # (machine_off, state_no) -> func name
        self.typed = {}                # type id -> set of offsets, for the semantic pass
        self.n_objects = 0

    # ------------------------------------------------------------ helpers ---
    def log(self, msg):
        if self.verbose:
            sys.stderr.write("  %s\n" % msg)
            sys.stderr.flush()

    def txt(self, off):
        if not off:
            return ""
        t = self.text_cache.get(off)
        if t is None:
            t = self.v.text(off)
            self.text_cache[off] = t
        if off:
            self.string_heads.add(off)
        return t

    def mark(self, off):
        """True the first time this offset is visited."""
        byte, bit = off // 8, 1 << (off % 8)
        if self.seen[byte] & bit:
            return False
        self.seen[byte] |= bit
        return True

    def sfield(self, off, idx):
        """One struct field: (field_type, raw double)."""
        base = off + 12 + idx * 12
        return self.v.u32(base), self.v.f64(base + 4)

    def sstr(self, off, idx):
        ft, dv = self.sfield(off, idx)
        return self.txt(int(dv)) if dv else ""

    def snum(self, off, idx):
        return self.sfield(off, idx)[1]

    def sref(self, off, idx):
        ft, dv = self.sfield(off, idx)
        return int(dv) if dv else 0

    def elems(self, off):
        """Elements of a type-6 list object as raw doubles."""
        v = self.v
        if not v.ok(off, 16) or v.u32(off) != 6:
            return []
        n = v.u32(off + 12)
        if n > 200000 or not v.ok(off + 16 + 8 * n):
            return []
        return [v.f64(off + 16 + i * 8) for i in range(n)]

    def refs(self, off):
        return [int(d) for d in self.elems(off) if d]

    # ------------------------------------------------------- header tables ---
    def build_functions(self):
        v = self.v
        sql = ("INSERT INTO vat_function VALUES (?,?,?,?,?,?)")
        for i in range(v.func_count):
            o = v.func_off + i * 12
            fid = v.u32(o)
            desc = self.txt(v.u32(o + 4))
            name = self.txt(v.u32(o + 8))
            self.func_by_id[fid] = name
            self.rows.add("vat_function", sql, (self.did, i, o, fid, name, desc))
        self.rows.flush()
        self.log("VAT functions: %d" % v.func_count)

    def build_dtc(self):
        """The header's 'data-list' table is the DTC catalogue table.

        f.java::l() reads rows of [name ptr][count][table offset]; e.a() then
        reads DTC rows of 24 bytes, 28 when header bit 7 is set; d.java gives the
        field layout.
        """
        v = self.v
        stride = 28 if (v.flags & 0x80) else 24
        list_sql = "INSERT INTO dtc_list VALUES (?,?,?,?,?,?,?)"
        row_sql = "INSERT INTO dtc_row VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        total = 0
        for i in range(v.datalist_count):
            o = v.datalist_off + i * 12
            name_ptr = v.u32(o)
            count = v.u32(o + 4)
            base = v.u32(o + 8)
            name = self.txt(name_ptr)
            self.rows.add("dtc_list", list_sql,
                          (self.did, i, o, name, count, base, stride))
            if count > 500000 or not v.ok(base, stride * max(count, 1)):
                continue
            for j in range(count):
                r = base + j * stride
                self.rows.add("dtc_row", row_sql, (
                    self.did, name, j, r,
                    v.i32(r), self.txt(v.u32(r + 4)), self.txt(v.u32(r + 8)),
                    self.txt(v.u32(r + 12)), self.txt(v.u32(r + 16)),
                    self.txt(v.u32(r + 20)),
                    self.txt(v.u32(r + 24)) if stride == 28 else None))
                total += 1
        self.rows.flush()
        self.log("DTC catalogues: %d lists, %d rows" % (v.datalist_count, total))

    def build_options(self):
        """RPO option pairs. f.java::i(): count at s+16, array offset at s+20
        relative to s, entries are two key-string pointers."""
        v = self.v
        s = v.option_off
        if not v.ok(s, 24) or v.u32(s + 4) < 2:
            self.log("option pairs: table absent")
            return
        count = v.u32(s + 16)
        rel = v.u32(s + 20)
        sql = "INSERT INTO option_pair VALUES (?,?,?,?)"
        n = 0
        if count < 5000000 and v.ok(s + rel, 8 * count):
            for i in range(count):
                a = v.key(v.u32(s + rel + 8 * i))
                b = v.key(v.u32(s + rel + 8 * i + 4))
                self.rows.add("option_pair", sql, (self.did, i, a, b))
                n += 1
        self.rows.flush()
        self.log("option pairs: %d (base 0x%X + 0x%X)" % (n, s, rel))

    def build_machines(self):
        v = self.v
        m_sql = "INSERT INTO machine VALUES (?,?,?,?,?,?,?,?)"
        a_sql = "INSERT INTO machine_attr VALUES (?,?,?,?,?)"
        s_sql = "INSERT INTO machine_state VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        t_sql = "INSERT INTO machine_transition VALUES (?,?,?,?,?,?,?)"
        n_states = 0
        self.machine_data = []
        for i in range(v.machine_count):
            o = v.machine_off + i * 56
            mid = v.u32(o)
            name = self.txt(v.u32(o + 4))
            n8, o12 = v.u32(o + 8), v.u32(o + 12)
            acount, aoff = v.u32(o + 16), v.u32(o + 20)
            scount, soff = v.u32(o + 24), v.u32(o + 28)
            self.rows.add("machine", m_sql,
                          (self.did, o, mid, name, n8, o12, scount, soff))
            if acount < 4096 and v.ok(aoff, 8 * acount):
                for j in range(acount):
                    self.rows.add("machine_attr", a_sql, (
                        self.did, o, j,
                        self.txt(v.u32(aoff + j * 8)),
                        self.txt(v.u32(aoff + j * 8 + 4))))
            if scount > 100000 or not v.ok(soff, 28 * max(scount, 1)):
                continue
            for j in range(scount):
                so = soff + 28 * j
                state_no = v.u32(so)
                fid = v.u32(so + 4)
                fname = self.func_by_id.get(fid, "")
                nxt = v.u32(so + 8)
                label = self.txt(v.u32(so + 12))
                tcount, toff = v.u32(so + 16), v.u32(so + 20)
                data = v.u32(so + 24)
                self.machine_state_func[(o, state_no)] = fname
                self.rows.add("machine_state", s_sql, (
                    self.did, o, so, state_no, fid, fname, nxt, label,
                    tcount, toff, data))
                if data:
                    self.machine_data.append(data)
                if tcount < 4096 and v.ok(toff, 12 * tcount):
                    for k in range(tcount):
                        to = toff + k * 12
                        self.rows.add("machine_transition", t_sql, (
                            self.did, o, state_no, k,
                            v.u32(to), v.u32(to + 4), v.u32(to + 8)))
                n_states += 1
        self.rows.flush()
        self.log("machines: %d, states: %d" % (v.machine_count, n_states))

    # ----------------------------------------------------------- menu tree ---
    def build_items(self):
        v = self.v
        i_sql = "INSERT OR IGNORE INTO item VALUES (?,?,?,?,?,?,?,?,?,?)"
        c_sql = "INSERT OR IGNORE INTO container VALUES (?,?,?,?,?,?,?)"
        e_sql = "INSERT INTO item_edge VALUES (?,?,?,?,?)"
        a_sql = "INSERT INTO item_attr VALUES (?,?,?,?,?)"
        tr_sql = "INSERT INTO item_triple VALUES (?,?,?,?,?,?)"

        self.triple_targets = []
        self.item_paths = {}
        queue = [(v.root_item, None, 0, "")]
        seen_items = set()
        seen_containers = set()
        n = 0
        while queue:
            off, parent, depth, ppath = queue.pop()
            if off in seen_items or not v.ok(off, v.stride):
                continue
            seen_items.add(off)
            try:
                it = v.item(off)
            except Exception:
                continue
            label = (it["label"] if it["label"].strip() else it["name"]).strip()
            path = (ppath + " / " + label) if ppath else label
            path = path.lstrip(" /")
            self.item_paths[off] = path
            self.string_heads.add(it["name_ptr"])
            self.rows.add("item", i_sql, (
                self.did, off, it["type"], it["name_ptr"], it["name"],
                it["label"], it["f3"], it["child_container"], depth, path))
            n += 1
            for j, (an, av, _p) in enumerate(it["attrs"]):
                self.rows.add("item_attr", a_sql, (self.did, off, j, an, av))
            for j, (m, sid, val) in enumerate(it["triples"]):
                self.rows.add("item_triple", tr_sql,
                              (self.did, off, j, m, sid, val))
                if val:
                    self.triple_targets.append((off, m, sid, val))
            cc = it["child_container"]
            if cc and v.ok(cc, 16):
                c = v.container(cc)
                if c and c["count"] < 100000:
                    if cc not in seen_containers:
                        seen_containers.add(cc)
                        self.rows.add("container", c_sql, (
                            self.did, cc, c["key"], c["label"], c["b"],
                            c["count"], c["items_off"]))
                    for k in range(c["count"]):
                        child = c["items_off"] + k * v.stride
                        self.rows.add("item_edge", e_sql,
                                      (self.did, off, cc, child, k))
                        queue.append((child, off, depth + 1, path))
        self.rows.flush()
        self.log("menu items: %d, containers: %d, triples: %d"
                 % (n, len(seen_containers), len(self.triple_targets)))

    # ------------------------------------------------- value-object graph ---
    def build_objects(self):
        """Breadth-first over the whole reachable value-object graph.

        Seeds are every item triple target and every state's attached data
        object. Each object is written once with all of its fields decoded; every
        reference found is queued, so the closure is complete.
        """
        v = self.v
        o_sql = "INSERT OR IGNORE INTO object VALUES (?,?,?,?,?,?,?)"
        f_sql = "INSERT INTO object_field VALUES (?,?,?,?,?,?,?,?,?)"
        queue = [t[3] for t in self.triple_targets]
        queue.extend(self.machine_data)
        head = 0
        t0 = time.time()
        while head < len(queue):
            off = queue[head]
            head += 1
            if off <= 0 or not v.ok(off, 24):
                continue
            if not self.mark(off):
                continue
            t = v.u32(off)
            if t == 6:
                elem_t = v.u32(off + 8)
                count = v.u32(off + 12)
                if count > 200000 or not v.ok(off + 16 + 8 * count):
                    continue
                parts = []
                self.rows.add("object", o_sql,
                              (self.did, off, 6, "list", elem_t, count, None))
                for i in range(count):
                    dv = v.f64(off + 16 + i * 8)
                    iv = int(dv) if abs(dv) < 9e15 else None
                    so = txt = ref = None
                    if elem_t == 5 and dv:
                        so = int(dv)
                        txt = self.txt(so)
                    elif elem_t > 6 and dv:
                        ref = int(dv)
                        queue.append(ref)
                    self.rows.add("object_field", f_sql, (
                        self.did, off, i, elem_t, dv, iv, so, txt, ref))
                    if len(parts) < 6 and txt:
                        parts.append(txt)
                if parts:
                    self.conn.execute(
                        "UPDATE object SET summary=? WHERE deliverable_id=? "
                        "AND off=?", (" | ".join(parts), self.did, off))
                self.n_objects += 1
                continue
            if t <= 5:
                dv = v.f64(off + 16)
                self.rows.add("object", o_sql,
                              (self.did, off, t, "scalar", None, 0,
                               self.txt(int(dv)) if t == 5 and dv else None))
                self.n_objects += 1
                continue
            count = v.u32(off + 8)
            if count == 0 or count > 4096 or not v.ok(off + 12 + count * 12):
                continue
            parts = []
            for i in range(count):
                ft = v.u32(off + 12 + i * 12)
                dv = v.f64(off + 12 + i * 12 + 4)
                iv = int(dv) if abs(dv) < 9e15 else None
                so = txt = ref = None
                if ft == 5 and dv:
                    so = int(dv)
                    txt = self.txt(so)
                    parts.append(txt)
                elif ft > 6 and dv:
                    ref = int(dv)
                    queue.append(ref)
                    parts.append("@0x%X" % ref)
                elif ft == 6 and dv:
                    ref = int(dv)
                    queue.append(ref)
                    parts.append("[0x%X]" % ref)
                else:
                    parts.append("%g" % dv)
                self.rows.add("object_field", f_sql, (
                    self.did, off, i, ft, dv, iv, so, txt, ref))
            self.rows.add("object", o_sql, (
                self.did, off, t, "struct", None, count,
                ", ".join(parts)[:400]))
            self.typed.setdefault(t, []).append(off)
            self.n_objects += 1
            if self.n_objects % 200000 == 0:
                self.rows.flush()
                self.log("  objects %d, queue %d, %.0fs"
                         % (self.n_objects, len(queue) - head, time.time() - t0))
        self.rows.flush()
        self.log("value objects: %d (%.0fs)" % (self.n_objects, time.time() - t0))

    # -------------------------------------------------------- string chains ---
    def build_strings(self):
        v = self.v
        h_sql = "INSERT OR IGNORE INTO string_head VALUES (?,?,?,?,?,?)"
        r_sql = "INSERT INTO string_record VALUES (?,?,?,?,?,?,?)"
        n = 0
        for off in self.string_heads:
            if not off or not v.ok(off, 8):
                continue
            recs = v.chain(off)
            if not recs:
                continue
            first = recs[0]
            self.rows.add("string_head", h_sql, (
                self.did, off, first[3], self.text_cache.get(off, first[3]),
                first[2], len(recs)))
            for seq, (ro, lang, flag, text) in enumerate(recs):
                if self.langs is not None and seq and lang not in self.langs:
                    continue
                self.rows.add("string_record", r_sql,
                              (self.did, off, seq, ro, lang, flag, text))
                n += 1
        self.rows.flush()
        self.log("string chains: %d heads, %d records"
                 % (len(self.string_heads), n))

    # -------------------------------------------------------- semantic pass ---
    def build_semantics(self):
        self._modules()
        self._pids()
        self._tests()
        self._misc()

    def _modules(self):
        v = self.v
        n_sql = "INSERT INTO module_node VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        c_sql = "INSERT OR IGNORE INTO module_conn VALUES (?,?,?,?,?,?,?,?)"
        l_sql = "INSERT INTO module_conn_list VALUES (?,?,?,?)"
        by_item = {}
        for item_off, m, sid, val in self.triple_targets:
            by_item.setdefault(item_off, {})[sid] = val
        n = 0
        for item_off, states in by_item.items():
            if STATE_CONNECTION not in states and STATE_PIDSETS not in states:
                continue
            it = v.item(item_off)
            path = self.item_paths.get(item_off, "")
            parts = path.split(" / ")
            vehicle = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
            self.rows.add("module_node", n_sql, (
                self.did, item_off, it["name"], it["label"], path, vehicle,
                states.get(STATE_CONNECTION), states.get(STATE_PIDSETS),
                states.get(STATE_DATALISTS), states.get(STATE_APPLICABILITY),
                states.get(STATE_DTC), states.get(STATE_TIMING),
                states.get(STATE_TESTS)))
            n += 1
        # connection containers and records
        for off in self.typed.get(T_CONN_CONTAINER, []):
            lst = self.sref(off, 1)
            for i in range(2, min(self.v.u32(off + 8), 8)):
                name = self.sstr(off, i)
                if name and name.strip():
                    self.rows.add("module_conn_list", l_sql,
                                  (self.did, off, i - 2, name))
            for rec in self.refs(lst):
                if not v.ok(rec, 24):
                    continue
                cnt = v.u32(rec + 8)
                if cnt == 0 or cnt > 64:
                    continue
                fields = []
                for i in range(cnt):
                    ft, dv = self.sfield(rec, i)
                    fields.append(self.txt(int(dv)) if ft == 5 and dv
                                  else ("%g" % dv))
                req = int(self.snum(rec, 2))
                self.rows.add("module_conn", c_sql, (
                    self.did, rec, off, self.sstr(rec, 0),
                    "0x%X" % req if req else "",
                    self.sstr(rec, 3), self.sstr(rec, 7),
                    " | ".join(fields)[:800]))
        self.rows.flush()
        self.log("module nodes: %d, connection records: %d"
                 % (n, len(self.typed.get(T_CONN_RECORD, []))))

    def _pids(self):
        v = self.v
        s_sql = "INSERT OR IGNORE INTO pid_set VALUES (?,?,?,?,?)"
        p_sql = "INSERT OR IGNORE INTO pid VALUES (?,?,?,?,?,?,?)"
        i_sql = "INSERT INTO pid_item VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        c_sql = "INSERT INTO pid_conv VALUES (?,?,?,?,?,?,?,?,?,?)"
        e_sql = "INSERT INTO pid_state VALUES (?,?,?,?,?)"
        n_pids = 0
        detailed = set()          # a PID object shared by several sets is
        for sof in self.typed.get(T_PID_SET, []):   # detailed only once
            pid_list = self.sref(sof, 0)
            pid_offs = self.refs(pid_list)
            self.rows.add("pid_set", s_sql, (
                self.did, sof, self.sstr(sof, 2), self.sstr(sof, 3),
                len(pid_offs)))
            for pof in pid_offs:
                if not v.ok(pof, 24) or v.u32(pof) != T_PID:
                    continue
                self.rows.add("pid", p_sql, (
                    self.did, pof, sof, self.sstr(pof, 0), self.sstr(pof, 5),
                    self.sstr(pof, 6), self.sstr(pof, 7)))
                n_pids += 1
                if pof in detailed:
                    continue
                detailed.add(pof)
                for idx, iof in enumerate(self.refs(self.sref(pof, 1))):
                    if not v.ok(iof, 24):
                        continue
                    f = [self.snum(iof, k) for k in range(7)]
                    pattern = self.sstr(iof, 7)
                    p = [int(f[0]), int(f[3]), int(f[1]), int(f[4]),
                         int(f[2]), int(f[5]), int(f[6])]
                    self.rows.add("pid_item", i_sql, (
                        self.did, pof, idx, iof, p[0], p[1], p[2], p[3],
                        p[4], p[5], p[6], pattern,
                        request_bytes(p[3], p[0], pattern)))
                for direction, fidx in (("read", 2), ("write", 4)):
                    for cof in self.refs(self.sref(pof, fidx)):
                        if not v.ok(cof, 24):
                            continue
                        self.rows.add("pid_conv", c_sql, (
                            self.did, pof, direction, cof,
                            self.sstr(cof, 0), self.sstr(cof, 1),
                            self.sstr(cof, 4), self.snum(cof, 3),
                            self.snum(cof, 2), self.sstr(cof, 5)))
                        for tof in self.refs(self.sref(cof, 6)):
                            for eof in self.refs(self.sref(tof, 2)):
                                if not v.ok(eof, 24):
                                    continue
                                self.rows.add("pid_state", e_sql, (
                                    self.did, pof, cof, self.snum(eof, 0),
                                    self.sstr(eof, 1)))
        self.rows.flush()
        self.log("PID sets: %d, PIDs: %d"
                 % (len(self.typed.get(T_PID_SET, [])), n_pids))

    def _tests(self):
        v = self.v
        f_sql = "INSERT INTO test_folder VALUES (?,?,?,?)"
        g_sql = "INSERT INTO test_group VALUES (?,?,?,?,?,?)"
        t_sql = "INSERT INTO test VALUES (?,?,?,?,?,?,?,?)"
        b_sql = "INSERT INTO test_binding VALUES (?,?,?,?,?,?,?,?,?)"
        group_parent = {}
        for fof in self.typed.get(T_TEST_FOLDER, []):
            groups = self.refs(self.sref(fof, 2))
            self.rows.add("test_folder", f_sql,
                          (self.did, fof, self.sstr(fof, 1), len(groups)))
            for g in groups:
                group_parent[g] = fof
        n_tests = 0
        test_parent = {}
        for gof in self.typed.get(T_TEST_GROUP, []):
            tests = self.refs(self.sref(gof, 3))
            self.rows.add("test_group", g_sql, (
                self.did, gof, group_parent.get(gof), self.sstr(gof, 1),
                self.sstr(gof, 2), len(tests)))
            for t in tests:
                test_parent[t] = gof
        for tof in self.typed.get(T_TEST, []):
            bindings = self.refs(self.sref(tof, 5))
            self.rows.add("test", t_sql, (
                self.did, tof, test_parent.get(tof), self.sstr(tof, 1),
                self.sstr(tof, 2), self.sstr(tof, 3), self.sstr(tof, 7),
                len(bindings)))
            n_tests += 1
            for idx, bof in enumerate(bindings):
                if not v.ok(bof, 24):
                    continue
                obj_id = int(self.snum(bof, 0))
                state_no = int(self.snum(bof, 1))
                data = self.sref(bof, 2)
                dtype = v.u32(data) if data and v.ok(data, 4) else None
                self.rows.add("test_binding", b_sql, (
                    self.did, tof, idx, bof, obj_id, state_no, data, dtype,
                    self.func_for_state(obj_id, state_no)))
        self.rows.flush()
        self.log("test folders: %d, groups: %d, tests: %d"
                 % (len(self.typed.get(T_TEST_FOLDER, [])),
                    len(self.typed.get(T_TEST_GROUP, [])), n_tests))

    def func_for_state(self, machine_id, state_no):
        """A binding names its machine by id, the state table by record offset."""
        if not hasattr(self, "_mach_by_id"):
            self._mach_by_id = {}
            for r in self.conn.execute(
                    "SELECT machine_id, off FROM machine WHERE deliverable_id=?",
                    (self.did,)):
                self._mach_by_id[r[0]] = r[1]
        moff = self._mach_by_id.get(machine_id)
        if moff is None:
            return ""
        return self.machine_state_func.get((moff, state_no), "")

    def _misc(self):
        dl_sql = "INSERT INTO data_list VALUES (?,?,?,?)"
        dn_sql = "INSERT INTO data_node VALUES (?,?,?,?,?,?)"
        tm_sql = "INSERT INTO module_timing VALUES (?,?,?,?)"
        ap_sql = "INSERT INTO applicability VALUES (?,?,?,?,?)"
        os_sql = "INSERT INTO osc_setup VALUES (?,?,?,?)"
        oc_sql = "INSERT INTO osc_command VALUES (?,?,?,?)"
        np_sql = "INSERT INTO named_param VALUES (?,?,?,?,?,?,?)"
        sc_sql = "INSERT INTO security_block VALUES (?,?,?,?,?,?,?,?)"

        node_owner = {}
        for off in self.typed.get(T_DATA_LIST, []):
            nodes = self.refs(self.sref(off, 2))
            self.rows.add("data_list", dl_sql,
                          (self.did, off, self.sstr(off, 1), len(nodes)))
            for n in nodes:
                node_owner[n] = off
        for off in self.typed.get(T_WATCH_SET, []):
            for n in self.refs(self.sref(off, 1)) + self.refs(self.sref(off, 0)):
                node_owner.setdefault(n, off)
        for off in self.typed.get(T_DATA_NODE, []):
            self.rows.add("data_node", dn_sql, (
                self.did, off, node_owner.get(off), self.sstr(off, 1),
                self.sstr(off, 2), self.sstr(off, 3)))
        for off in self.typed.get(T_TIMING, []):
            self.rows.add("module_timing", tm_sql,
                          (self.did, off, None, self.fields_text(off)))
        for off in self.typed.get(T_APPLIC_ITEM, []):
            conds = []
            for c in self.refs(self.sref(off, 2)):
                conds.append(self.fields_text(c))
            self.rows.add("applicability", ap_sql, (
                self.did, off, None, self.sstr(off, 1), " ; ".join(conds)[:2000]))
        setup_owner = {}
        for off in self.typed.get(T_OSC_INIT, []):
            for s in self.refs(self.sref(off, 1)):
                setup_owner[s] = off
        for off in self.typed.get(T_OSC_SETUP, []):
            self.rows.add("osc_setup", os_sql,
                          (self.did, off, setup_owner.get(off),
                           self.fields_text(off)))
        for off in self.typed.get(T_OSC_COMMAND, []):
            self.rows.add("osc_command", oc_sql,
                          (self.did, off, None, self.fields_text(off)))
        for off in self.typed.get(T_NAMED_PARAM, []):
            self.rows.add("named_param", np_sql, (
                self.did, off, self.sstr(off, 1), self.sstr(off, 2),
                self.sstr(off, 3), self.sstr(off, 4), self.sstr(off, 5)))
        for off in self.typed.get(T_SECURITY, []):
            self.rows.add("security_block", sc_sql, (
                self.did, off, self.sstr(off, 0), self.sstr(off, 1),
                self.sstr(off, 2), self.sstr(off, 3), self.sstr(off, 4),
                self.fields_text(off)))
        self.rows.flush()
        self.log("data lists: %d, data nodes: %d, OSC setups: %d, params: %d"
                 % (len(self.typed.get(T_DATA_LIST, [])),
                    len(self.typed.get(T_DATA_NODE, [])),
                    len(self.typed.get(T_OSC_SETUP, [])),
                    len(self.typed.get(T_NAMED_PARAM, []))))

    def fields_text(self, off):
        v = self.v
        if not v.ok(off, 24):
            return ""
        cnt = v.u32(off + 8)
        if cnt > 256:
            return ""
        out = []
        for i in range(cnt):
            ft, dv = self.sfield(off, i)
            if ft == 5 and dv:
                out.append(self.txt(int(dv)))
            elif ft > 5 and dv:
                out.append("@0x%X" % int(dv))
            else:
                out.append("%g" % dv)
        return " | ".join(out)[:2000]


def request_bytes(pid_type, param_id, pattern):
    """Reconstruct the literal request for one VCIDataItem where the rule is
    known (com/Mahle/VCI/c/ad.java plus the GMLAN templates in c/n.java).

    p[3] == 0  read              22 <hi> <lo>
    p[3] == 3  device control    AE <p0> <pattern...>
    p[3] == 4  raw               <p0> <pattern...>
    """
    extra = []
    if pattern:
        for tok in pattern.replace(" ", "").split(","):
            if tok in ("", "*", "0", "_"):
                continue
            extra.append(tok.upper() if tok.upper() != "XX" else "XX")
    if pid_type == 0:
        return "22 %02X %02X" % ((param_id >> 8) & 0xFF, param_id & 0xFF)
    if pid_type == 3:
        return " ".join(["AE", "%02X" % (param_id & 0xFF)] + extra)
    if pid_type == 4:
        return " ".join(["%02X" % (param_id & 0xFF)] + extra)
    return None


def sha1_of(path, limit=64 * 1024 * 1024):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        done = 0
        while done < limit:
            b = fh.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            done += len(b)
    return h.hexdigest()


def open_db(path, schema_path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-200000")
    conn.execute("PRAGMA temp_store=MEMORY")
    with open(schema_path, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_str_head_text ON string_head(text)",
    "CREATE INDEX IF NOT EXISTS ix_str_head_key ON string_head(key)",
    "CREATE INDEX IF NOT EXISTS ix_item_name ON item(name)",
    "CREATE INDEX IF NOT EXISTS ix_item_label ON item(label)",
    "CREATE INDEX IF NOT EXISTS ix_item_edge_child ON item_edge(child_off)",
    "CREATE INDEX IF NOT EXISTS ix_item_triple_item ON item_triple(item_off)",
    "CREATE INDEX IF NOT EXISTS ix_item_triple_obj ON item_triple(obj_off)",
    "CREATE INDEX IF NOT EXISTS ix_objfield_obj ON object_field(obj_off)",
    "CREATE INDEX IF NOT EXISTS ix_objfield_ref ON object_field(ref_off)",
    "CREATE INDEX IF NOT EXISTS ix_objfield_text ON object_field(text)",
    "CREATE INDEX IF NOT EXISTS ix_object_type ON object(type)",
    "CREATE INDEX IF NOT EXISTS ix_mstate ON machine_state(machine_off, state_no)",
    "CREATE INDEX IF NOT EXISTS ix_mstate_func ON machine_state(func_name)",
    "CREATE INDEX IF NOT EXISTS ix_dtc_code ON dtc_row(code)",
    "CREATE INDEX IF NOT EXISTS ix_dtc_list ON dtc_row(list_name)",
    "CREATE INDEX IF NOT EXISTS ix_optpair_a ON option_pair(a)",
    "CREATE INDEX IF NOT EXISTS ix_optpair_b ON option_pair(b)",
    "CREATE INDEX IF NOT EXISTS ix_modnode_label ON module_node(label)",
    "CREATE INDEX IF NOT EXISTS ix_modnode_path ON module_node(path)",
    "CREATE INDEX IF NOT EXISTS ix_modconn_module ON module_conn(module)",
    "CREATE INDEX IF NOT EXISTS ix_modconn_pidset ON module_conn(pidset_name)",
    "CREATE INDEX IF NOT EXISTS ix_pidset_ref ON pid_set(ref_name)",
    "CREATE INDEX IF NOT EXISTS ix_pid_name ON pid(name)",
    "CREATE INDEX IF NOT EXISTS ix_pid_ref ON pid(ref_name)",
    "CREATE INDEX IF NOT EXISTS ix_pid_set ON pid(set_off)",
    "CREATE INDEX IF NOT EXISTS ix_piditem_pid ON pid_item(pid_off)",
    "CREATE INDEX IF NOT EXISTS ix_piditem_param ON pid_item(param_id)",
    "CREATE INDEX IF NOT EXISTS ix_pidconv_pid ON pid_conv(pid_off)",
    "CREATE INDEX IF NOT EXISTS ix_pidstate_pid ON pid_state(pid_off)",
    "CREATE INDEX IF NOT EXISTS ix_test_key ON test(key)",
    "CREATE INDEX IF NOT EXISTS ix_test_name ON test(name)",
    "CREATE INDEX IF NOT EXISTS ix_test_group ON test(group_off)",
    "CREATE INDEX IF NOT EXISTS ix_binding_test ON test_binding(test_off)",
    "CREATE INDEX IF NOT EXISTS ix_datanode_pid ON data_node(pid_ref)",
    "CREATE INDEX IF NOT EXISTS ix_namedparam ON named_param(name)",
]


def build_one(conn, label, path, verbose=True, langs=None):
    st = os.stat(path)
    v = Vatbin(str(path))
    cur = conn.execute("SELECT id FROM deliverable WHERE label=?", (label,))
    row = cur.fetchone()
    if row:
        did = row[0]
        for t in ("string_head", "string_record", "container", "item",
                  "item_edge", "item_attr", "item_triple", "object",
                  "object_field", "machine", "machine_attr", "machine_state",
                  "machine_transition", "vat_function", "dtc_list", "dtc_row",
                  "option_pair", "module_node", "module_conn",
                  "module_conn_list", "pid_set", "pid", "pid_item", "pid_conv",
                  "pid_state", "test_folder", "test_group", "test",
                  "test_binding", "data_list", "data_node", "module_timing",
                  "applicability", "osc_setup", "osc_command", "named_param",
                  "security_block"):
            conn.execute("DELETE FROM %s WHERE deliverable_id=?" % t, (did,))
        conn.execute("DELETE FROM deliverable WHERE id=?", (did,))
    conn.execute(
        "INSERT INTO deliverable (label, path, size, file_mtime, sha1, version,"
        " flags, lang_count, built_at, builder) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (label, str(path), st.st_size,
         datetime.datetime.fromtimestamp(
             st.st_mtime, datetime.timezone.utc).isoformat(),
         sha1_of(path), v.text(v.ver_ptr), v.flags, v.lang_count,
         datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "tools/dealer_index/build_gds2.py"))
    conn.commit()
    did = conn.execute("SELECT id FROM deliverable WHERE label=?",
                       (label,)).fetchone()[0]

    b = Builder(conn, v, did, verbose, langs)
    t0 = time.time()
    b.build_functions()
    b.build_dtc()
    b.build_options()
    b.build_machines()
    b.build_items()
    b.build_objects()
    b.build_semantics()
    b.build_strings()
    conn.commit()
    sys.stderr.write("  %s done in %.0fs\n" % (label, time.time() - t0))
    return b


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("labels", nargs="*",
                    help="deliverable labels to build; default is all found")
    ap.add_argument("--db", default=None, help="output database path")
    ap.add_argument("--corpus", default=None, help="root of the GDS2 corpus")
    ap.add_argument("--list", action="store_true",
                    help="list the deliverables found and exit")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--languages", default="all",
                    help="'all' (default) keeps every language record of every "
                         "string chain; otherwise a comma separated list of "
                         "language ids, e.g. '0,1' for the display text and "
                         "English only")
    args = ap.parse_args()
    langs = None
    if args.languages != "all":
        langs = set(int(x) for x in args.languages.split(",") if x.strip())

    found = paths.find_vatbins(args.corpus)
    if args.list:
        for label, p in found:
            print("%-20s %10.1f MB  %s" % (label, os.path.getsize(p) / 1e6, p))
        return
    if not found:
        sys.exit("no vatbin.bin found under %s" % paths.corpus_root("gds2"))
    if args.labels:
        wanted = set(args.labels)
        found = [(l, p) for l, p in found if l in wanted]
        if not found:
            sys.exit("no deliverable matched %s" % ", ".join(args.labels))

    db = args.db or str(paths.db_path("gds2"))
    conn = open_db(db, os.path.join(HERE, "schema_gds2.sql"))
    for label, p in found:
        sys.stderr.write("building %s from %s\n" % (label, p))
        build_one(conn, label, p, verbose=not args.quiet, langs=langs)
    sys.stderr.write("creating indexes\n")
    for sql in INDEXES:
        conn.execute(sql)
    conn.commit()
    conn.execute("PRAGMA optimize")
    conn.close()
    sys.stderr.write("database: %s (%.1f MB)\n"
                     % (db, os.path.getsize(db) / 1e6))


if __name__ == "__main__":
    main()
