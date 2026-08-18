"""Query the mined dealer-tool indexes.

    python tools/dealer_index/dealerdb.py <command> [args]

Built for the questions these jobs actually ask, across all three corpora at once
where that makes sense:

    status                       what is indexed, how big, built when
    vehicles [pattern]           vehicle lines the indexes know about
    modules <vehicle>            modules on a vehicle, with CAN addressing
    module <name>                where a module appears and what it exposes
    pids <module> [--grep RE]    the PIDs a module exposes, with request bytes
    pid <name-or-id>             one PID or DID: scaling, units, states
    did <number>                 what a DID means (JLR SDD, and GDS2 by id)
    tests <module>               tests defined for a module
    test <key-or-name>           one test: its states and data bindings
    dtc <code>                   DTC text from either corpus
    shares <module>              which vehicles share a module
    routines <module>            JLR routine identifiers for a module
    screens <family>             DiagBox screens and actuator ids for a family
    option <code>                RPO / option pairs mentioning a code
    sql <corpus> <query>         escape hatch, read only

Corpus names are gds2, sdd and diagbox.
"""

import argparse
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths                                     # noqa: E402

CORPORA = ("gds2", "sdd", "diagbox")


def connect(name, required=True):
    p = paths.db_path(name)
    if not os.path.exists(p):
        if required:
            sys.stderr.write(
                "%s index not built. Run: python tools/dealer_index/build_%s.py\n"
                % (name, name))
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % str(p).replace("\\", "/"),
                           uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table(rows, headers, limit=None, width=None):
    rows = list(rows)
    if limit:
        rows = rows[:limit]
    if not rows:
        print("  (nothing)")
        return
    cols = len(headers)
    cells = [[fmt(r[i]) for i in range(cols)] for r in rows]
    widths = [max(len(headers[i]), max(len(c[i]) for c in cells))
              for i in range(cols)]
    if width:
        widths = [min(w, width) for w in widths]
    line = "  ".join(h[:widths[i]].ljust(widths[i]) for i, h in enumerate(headers))
    print("  " + line)
    print("  " + "  ".join("-" * w for w in widths))
    for c in cells:
        print("  " + "  ".join(c[i][:widths[i]].ljust(widths[i])
                               for i in range(cols)))
    print("  %d row(s)" % len(rows))


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return ("%g" % v)
    return str(v)


def like(s):
    return "%" + s + "%"


# --------------------------------------------------------------- commands ----
def cmd_status(args):
    for name in CORPORA:
        p = paths.db_path(name)
        if not os.path.exists(p):
            print("%-8s not built  (%s)" % (name, p))
            continue
        size = os.path.getsize(p) / 1e6
        conn = connect(name)
        print("%-8s %8.1f MB  %s" % (name, size, p))
        if name == "gds2":
            for r in conn.execute(
                    "SELECT label, version, size, built_at FROM deliverable"
                    " ORDER BY label"):
                print("           %-16s %-16s %6.0f MB  %s"
                      % (r["label"], r["version"], r["size"] / 1e6,
                         r["built_at"][:19]))
            for t, q in (("items", "SELECT COUNT(*) FROM item"),
                         ("objects", "SELECT COUNT(*) FROM object"),
                         ("pids", "SELECT COUNT(*) FROM pid"),
                         ("tests", "SELECT COUNT(*) FROM test"),
                         ("dtc rows", "SELECT COUNT(*) FROM dtc_row")):
                print("           %-10s %d" % (t, conn.execute(q).fetchone()[0]))
        elif name == "sdd":
            for t, q in (("platforms",
                          "SELECT COUNT(DISTINCT platform) FROM source_file"),
                         ("ECUs", "SELECT COUNT(*) FROM ecu"),
                         ("DIDs", "SELECT COUNT(*) FROM did"),
                         ("routines", "SELECT COUNT(*) FROM routine")):
                print("           %-10s %d" % (t, conn.execute(q).fetchone()[0]))
        elif name == "diagbox":
            for t, q in (("scripts",
                          "SELECT COUNT(*) FROM source_file WHERE kind='tree'"),
                         ("screens", "SELECT COUNT(*) FROM tree_screen"),
                         ("params", "SELECT COUNT(*) FROM fdb_param")):
                try:
                    print("           %-10s %d"
                          % (t, conn.execute(q).fetchone()[0]))
                except sqlite3.Error:
                    pass
        conn.close()


def cmd_vehicles(args):
    pat = args.pattern
    g = connect("gds2", required=False)
    if g:
        print("GDS2 vehicle lines")
        q = ("SELECT vehicle, COUNT(*) n, COUNT(DISTINCT label) rel"
             " FROM module_node JOIN deliverable d ON d.id=deliverable_id"
             " WHERE vehicle <> ''")
        p = []
        if pat:
            q += " AND vehicle LIKE ?"
            p.append(like(pat))
        q += " GROUP BY vehicle ORDER BY n DESC"
        table(g.execute(q, p), ("vehicle", "module nodes", "deliverables"),
              limit=args.limit)
        g.close()
    s = connect("sdd", required=False)
    if s:
        print("\nJLR SDD platforms")
        q = ("SELECT platform, COUNT(DISTINCT moduletype) m, COUNT(*) n"
             " FROM ecu WHERE 1=1")
        p = []
        if pat:
            q += " AND platform LIKE ?"
            p.append(like(pat))
        q += " GROUP BY platform ORDER BY platform"
        table(s.execute(q, p), ("platform", "module types", "ECU records"),
              limit=args.limit)
        s.close()
    d = connect("diagbox", required=False)
    if d:
        print("\nDiagBox models")
        q = ("SELECT model, COUNT(DISTINCT family) f, COUNT(*) n"
             " FROM source_file WHERE kind='tree' AND model IS NOT NULL")
        p = []
        if pat:
            q += " AND model LIKE ?"
            p.append(like(pat))
        q += " GROUP BY model ORDER BY model"
        table(d.execute(q, p), ("model", "ECU families", "scripts"),
              limit=args.limit)
        d.close()


def cmd_modules(args):
    v = args.vehicle
    g = connect("gds2", required=False)
    if g:
        print("GDS2 modules matching %r" % v)
        q = ("SELECT d.label, n.vehicle, n.label AS module, c.request_id,"
             "       c.bus, c.pidset_name, n.item_off"
             " FROM module_node n JOIN deliverable d ON d.id=n.deliverable_id"
             " LEFT JOIN module_conn_list cl"
             "        ON cl.deliverable_id=n.deliverable_id"
             "       AND cl.off=n.conn_obj"
             " LEFT JOIN module_conn c"
             "        ON c.deliverable_id=n.deliverable_id"
             "       AND c.container_off=n.conn_obj"
             " WHERE n.path LIKE ?"
             " GROUP BY n.deliverable_id, n.item_off"
             " ORDER BY d.label, n.path")
        table(g.execute(q, (like(v),)),
              ("deliverable", "vehicle", "module", "request", "bus", "pid set",
               "item off"), limit=args.limit, width=52)
        g.close()
    s = connect("sdd", required=False)
    if s:
        print("\nJLR SDD modules matching %r" % v)
        q = ("SELECT platform, moduletype, can_target AS request,"
             "       can_src AS response, model, year, COUNT(*) n"
             " FROM ecu WHERE platform LIKE ? OR model LIKE ?"
             " GROUP BY platform, moduletype, can_target"
             " ORDER BY platform, moduletype")
        table(s.execute(q, (like(v), like(v))),
              ("platform", "module", "request", "response", "model", "year",
               "variants"), limit=args.limit)
        s.close()


def cmd_module(args):
    name = args.name
    g = connect("gds2", required=False)
    if g:
        print("GDS2 nodes for %r" % name)
        table(g.execute(
            "SELECT d.label, n.vehicle, n.label, n.item_off, n.path"
            " FROM module_node n JOIN deliverable d ON d.id=n.deliverable_id"
            " WHERE n.label LIKE ? ORDER BY d.label, n.path", (like(name),)),
            ("deliverable", "vehicle", "module", "item off", "path"),
            limit=args.limit, width=70)
        g.close()
    s = connect("sdd", required=False)
    if s:
        print("\nJLR SDD %r" % name)
        table(s.execute(
            "SELECT platform, moduletype, can_target, can_src, model, year"
            " FROM ecu WHERE moduletype LIKE ? ORDER BY platform",
            (like(name),)),
            ("platform", "module", "request", "response", "model", "year"),
            limit=args.limit)
        s.close()


def cmd_pids(args):
    g = connect("gds2")
    if not g:
        return
    q = ("SELECT d.label, s.module_name, p.ref_name, p.name, i.param_id,"
         "       i.request_hex, i.length, i.pid_type, p.off"
         " FROM pid p"
         " JOIN pid_set s ON s.off=p.set_off AND s.deliverable_id=p.deliverable_id"
         " JOIN deliverable d ON d.id=p.deliverable_id"
         " LEFT JOIN pid_item i ON i.pid_off=p.off"
         "        AND i.deliverable_id=p.deliverable_id AND i.idx=0"
         " WHERE s.ref_name LIKE ? OR s.module_name LIKE ?")
    params = [like(args.module), like(args.module)]
    if args.grep:
        q += " AND (p.name LIKE ? OR p.ref_name LIKE ?)"
        params += [like(args.grep), like(args.grep)]
    q += " GROUP BY d.label, p.off ORDER BY d.label, p.name"
    table(g.execute(q, params),
          ("deliverable", "module", "ref", "name", "param", "request", "len",
           "type", "off"), limit=args.limit, width=46)
    g.close()


def cmd_pid(args):
    key = args.name
    g = connect("gds2", required=False)
    if g:
        print("GDS2 PIDs matching %r" % key)
        q = ("SELECT DISTINCT p.off, p.ref_name, p.name, p.unit, s.ref_name AS set_name"
             " FROM pid p JOIN pid_set s"
             "   ON s.off=p.set_off AND s.deliverable_id=p.deliverable_id"
             " WHERE p.name LIKE ? OR p.ref_name LIKE ?"
             " ORDER BY p.name LIMIT ?")
        rows = list(g.execute(q, (like(key), like(key), args.limit or 20)))
        table(rows, ("off", "ref", "name", "unit", "pid set"), width=44)
        for r in rows[:args.detail]:
            print("\n  %s  (%s)  @0x%X" % (r["name"], r["ref_name"], r["off"]))
            table(g.execute(
                "SELECT DISTINCT param_id, request_hex, length, pid_type,"
                " byte_off, pattern FROM pid_item WHERE pid_off=?", (r["off"],)),
                ("param", "request", "len", "type", "offset", "pattern"))
            table(g.execute(
                "SELECT DISTINCT direction, equation, unit, lo, hi"
                " FROM pid_conv WHERE pid_off=?", (r["off"],)),
                ("dir", "equation", "unit", "low", "high"))
            st = list(g.execute(
                "SELECT DISTINCT value, text FROM pid_state WHERE pid_off=?"
                " ORDER BY value", (r["off"],)))
            if st:
                table(st, ("value", "state"))
        g.close()
    s = connect("sdd", required=False)
    if s:
        print("\nJLR SDD data identifiers matching %r" % key)
        table(s.execute(
            "SELECT m.platform, m.shortname, d.number, d.name, f.units,"
            "       f.resolution, f.offset, d.byte_size"
            " FROM did d JOIN mdx m ON m.id=d.mdx_id"
            " LEFT JOIN did_subfield f ON f.did_id=d.id AND f.idx=0"
            " WHERE d.name LIKE ? GROUP BY m.platform, m.shortname, d.number"
            " ORDER BY d.number", (like(key),)),
            ("platform", "module", "DID", "name", "units", "res", "offset",
             "bytes"), limit=args.limit, width=44)
        s.close()


def cmd_did(args):
    num = args.number.upper().replace("0X", "")
    s = connect("sdd", required=False)
    if s:
        print("JLR SDD DID %s" % num)
        table(s.execute(
            "SELECT m.platform, m.file_stem, m.shortname, d.number, d.name,"
            "       d.byte_size, d.read_services, d.read_sessions,"
            "       d.write_services"
            " FROM did d JOIN mdx m ON m.id=d.mdx_id"
            " WHERE REPLACE(UPPER(d.number),'0X','') = ?"
            " ORDER BY m.platform, m.file_stem", (num,)),
            ("platform", "file", "module", "DID", "name", "bytes", "read svc",
             "sessions", "write svc"), limit=args.limit, width=44)
        rows = list(s.execute(
            "SELECT f.name, f.lsb, f.msb, f.data_type, f.resolution, f.offset,"
            "       f.units, f.range_low, f.range_high"
            " FROM did d JOIN did_subfield f ON f.did_id=d.id"
            " WHERE REPLACE(UPPER(d.number),'0X','') = ?"
            " GROUP BY f.name, f.lsb, f.msb, f.resolution, f.offset, f.units",
            (num,)))
        if rows:
            print("\n  sub-fields")
            table(rows, ("name", "lsb", "msb", "type", "resolution", "offset",
                         "units", "low", "high"), width=40)
        enums = list(s.execute(
            "SELECT DISTINCT e.value, e.name, e.description"
            " FROM did d JOIN did_enum e ON e.did_id=d.id"
            " WHERE REPLACE(UPPER(d.number),'0X','') = ? ORDER BY e.value",
            (num,)))
        if enums:
            print("\n  enumerated states")
            table(enums, ("value", "name", "description"), width=50)
        s.close()
    try:
        pid_id = int(num, 16)
    except ValueError:
        pid_id = None
    g = connect("gds2", required=False)
    if g and pid_id is not None:
        print("\nGDS2 PIDs with parameter id 0x%X" % pid_id)
        table(g.execute(
            "SELECT DISTINCT d.label, s.module_name, p.name, i.request_hex,"
            "       i.length, i.pid_type"
            " FROM pid_item i JOIN pid p ON p.off=i.pid_off"
            "   AND p.deliverable_id=i.deliverable_id"
            " JOIN pid_set s ON s.off=p.set_off"
            "   AND s.deliverable_id=p.deliverable_id"
            " JOIN deliverable d ON d.id=p.deliverable_id"
            " WHERE i.param_id=? ORDER BY d.label, p.name", (pid_id,)),
            ("deliverable", "module", "name", "request", "len", "type"),
            limit=args.limit, width=48)
        g.close()


def cmd_tests(args):
    g = connect("gds2")
    if not g:
        return
    q = ("SELECT d.label, n.vehicle, n.label AS module, t.key, t.name,"
         "       t.test_type, t.off"
         " FROM module_node n"
         " JOIN deliverable d ON d.id=n.deliverable_id"
         " JOIN object o ON o.deliverable_id=n.deliverable_id"
         "              AND o.off=n.test_obj"
         " JOIN object_field f ON f.deliverable_id=n.deliverable_id"
         "                   AND f.obj_off=n.test_obj"
         " JOIN test_folder fo ON fo.deliverable_id=n.deliverable_id"
         "                    AND fo.off=f.ref_off"
         " JOIN test_group gr ON gr.deliverable_id=n.deliverable_id"
         "                   AND gr.folder_off=fo.off"
         " JOIN test t ON t.deliverable_id=n.deliverable_id"
         "            AND t.group_off=gr.off"
         " WHERE n.label LIKE ? OR n.path LIKE ?"
         " GROUP BY d.label, n.item_off, t.off ORDER BY d.label, t.name")
    table(g.execute(q, (like(args.module), like(args.module))),
          ("deliverable", "vehicle", "module", "key", "test", "type", "off"),
          limit=args.limit, width=46)
    g.close()


def cmd_test(args):
    g = connect("gds2")
    if not g:
        return
    rows = list(g.execute(
        "SELECT t.deliverable_id, t.off, t.key, t.name, t.test_type,"
        "       t.n_bindings, d.label"
        " FROM test t JOIN deliverable d ON d.id=t.deliverable_id"
        " WHERE t.key LIKE ? OR t.name LIKE ? ORDER BY d.label LIMIT ?",
        (like(args.key), like(args.key), args.limit or 20)))
    table(rows, ("deliverable id", "off", "key", "name", "type", "bindings",
                 "deliverable"), width=44)
    for r in rows[:args.detail]:
        print("\n  %s  %s  @0x%X (%s)"
              % (r["key"], r["name"], r["off"], r["label"]))
        table(g.execute(
            "SELECT idx, state_no, func_name, data_type, data_off"
            " FROM test_binding WHERE deliverable_id=? AND test_off=?"
            " ORDER BY idx", (r["deliverable_id"], r["off"])),
            ("idx", "state", "VAT function", "data type", "data off"))
    g.close()


def cmd_dtc(args):
    code = args.code
    g = connect("gds2", required=False)
    if g:
        print("GDS2 DTC rows matching %r" % code)
        table(g.execute(
            "SELECT d.label, r.list_name, r.code, r.description, r.field_12,"
            "       r.field_16"
            " FROM dtc_row r JOIN deliverable d ON d.id=r.deliverable_id"
            " WHERE r.code LIKE ? ORDER BY d.label, r.list_name",
            (like(code),)),
            ("deliverable", "list", "code", "description", "field 12",
             "field 16"), limit=args.limit, width=46)
        g.close()
    s = connect("sdd", required=False)
    if s:
        print("\nJLR SDD DTC descriptions matching %r" % code)
        table(s.execute(
            "SELECT code_id, module, description FROM dtc_description"
            " WHERE code_id LIKE ? ORDER BY module", (like(code),)),
            ("code", "module", "description"), limit=args.limit, width=70)
        s.close()


def cmd_shares(args):
    name = args.module
    g = connect("gds2", required=False)
    if g:
        print("GDS2 vehicles carrying a module like %r" % name)
        table(g.execute(
            "SELECT d.label, n.vehicle, n.label AS module, COUNT(*) nodes"
            " FROM module_node n JOIN deliverable d ON d.id=n.deliverable_id"
            " WHERE n.label LIKE ?"
            " GROUP BY d.label, n.vehicle, n.label"
            " ORDER BY n.vehicle, d.label", (like(name),)),
            ("deliverable", "vehicle", "module", "nodes"),
            limit=args.limit, width=50)
        print("\nGDS2 option pairs mentioning %r" % name)
        table(g.execute(
            "SELECT DISTINCT d.label, o.a, o.b FROM option_pair o"
            " JOIN deliverable d ON d.id=o.deliverable_id"
            " WHERE o.a LIKE ? OR o.b LIKE ? ORDER BY o.a, o.b",
            (like(name), like(name))),
            ("deliverable", "option category", "option value"),
            limit=args.limit, width=60)
        g.close()
    s = connect("sdd", required=False)
    if s:
        print("\nJLR platforms carrying module %r" % name)
        table(s.execute(
            "SELECT moduletype, GROUP_CONCAT(DISTINCT platform) platforms"
            " FROM ecu WHERE moduletype LIKE ? GROUP BY moduletype",
            (like(name),)),
            ("module", "platforms"), limit=args.limit, width=110)
        s.close()


def cmd_routines(args):
    s = connect("sdd")
    if not s:
        return
    q = ("SELECT m.platform, m.file_stem, m.shortname, r.number, r.name,"
         "       r.session_refs"
         " FROM routine r JOIN mdx m ON m.id=r.mdx_id"
         " WHERE m.shortname LIKE ? OR m.file_stem LIKE ? OR m.platform LIKE ?")
    params = [like(args.module)] * 3
    if args.grep:
        q += " AND r.name LIKE ?"
        params.append(like(args.grep))
    q += " ORDER BY m.platform, r.number"
    table(s.execute(q, params),
          ("platform", "file", "module", "routine", "name", "sessions"),
          limit=args.limit, width=48)
    s.close()


def cmd_screens(args):
    d = connect("diagbox")
    if not d:
        return
    q = ("SELECT model, function, family, tag, screen, local_id"
         " FROM tree_screen WHERE family LIKE ? OR model LIKE ?")
    params = [like(args.family), like(args.family)]
    if args.grep:
        q += " AND screen LIKE ?"
        params.append(like(args.grep))
    q += " GROUP BY model, family, screen ORDER BY model, family, screen"
    table(d.execute(q, params),
          ("model", "function", "family", "tag", "screen", "local id"),
          limit=args.limit, width=56)
    d.close()


def cmd_option(args):
    g = connect("gds2")
    if not g:
        return
    table(g.execute(
        "SELECT DISTINCT d.label, o.a, o.b FROM option_pair o"
        " JOIN deliverable d ON d.id=o.deliverable_id"
        " WHERE o.a LIKE ? OR o.b LIKE ? ORDER BY o.a, o.b",
        (like(args.code), like(args.code))),
        ("deliverable", "option category", "option value"),
        limit=args.limit, width=60)
    g.close()


def cmd_sql(args):
    if not re.match(r"^\s*(select|with|pragma|explain)\b", args.query, re.I):
        sys.exit("read-only: only SELECT / WITH / PRAGMA / EXPLAIN are allowed")
    conn = connect(args.corpus)
    if not conn:
        return
    cur = conn.execute(args.query)
    headers = [c[0] for c in cur.description]
    table(cur, headers, limit=args.limit, width=60)
    conn.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--detail", type=int, default=1,
                    help="how many matches to expand in detail")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    p = sub.add_parser("vehicles")
    p.add_argument("pattern", nargs="?")
    p.set_defaults(fn=cmd_vehicles)

    p = sub.add_parser("modules")
    p.add_argument("vehicle")
    p.set_defaults(fn=cmd_modules)

    p = sub.add_parser("module")
    p.add_argument("name")
    p.set_defaults(fn=cmd_module)

    p = sub.add_parser("pids")
    p.add_argument("module")
    p.add_argument("--grep")
    p.set_defaults(fn=cmd_pids)

    p = sub.add_parser("pid")
    p.add_argument("name")
    p.set_defaults(fn=cmd_pid)

    p = sub.add_parser("did")
    p.add_argument("number")
    p.set_defaults(fn=cmd_did)

    p = sub.add_parser("tests")
    p.add_argument("module")
    p.set_defaults(fn=cmd_tests)

    p = sub.add_parser("test")
    p.add_argument("key")
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("dtc")
    p.add_argument("code")
    p.set_defaults(fn=cmd_dtc)

    p = sub.add_parser("shares")
    p.add_argument("module")
    p.set_defaults(fn=cmd_shares)

    p = sub.add_parser("routines")
    p.add_argument("module")
    p.add_argument("--grep")
    p.set_defaults(fn=cmd_routines)

    p = sub.add_parser("screens")
    p.add_argument("family")
    p.add_argument("--grep")
    p.set_defaults(fn=cmd_screens)

    p = sub.add_parser("option")
    p.add_argument("code")
    p.set_defaults(fn=cmd_option)

    p = sub.add_parser("sql")
    p.add_argument("corpus", choices=CORPORA)
    p.add_argument("query")
    p.set_defaults(fn=cmd_sql)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
