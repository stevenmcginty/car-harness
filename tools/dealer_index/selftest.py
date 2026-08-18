"""Sanity-check the built indexes against facts that were established elsewhere.

    python tools/dealer_index/selftest.py

This is not a unit test of the builders; it is a check that the index agrees with
the things the project already believes. Every check names its independent source.
A failure here is a finding, not a formality: either the index is wrong or the
source it is checked against is.
"""

import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths                                     # noqa: E402

FAILURES = []
CHECKS = [0]


def check(name, ok, detail=""):
    CHECKS[0] += 1
    if ok:
        print("  pass  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILURES.append((name, detail))


def ro(name):
    p = paths.db_path(name)
    if not os.path.exists(p):
        return None
    return sqlite3.connect("file:%s?mode=ro" % str(p).replace("\\", "/"),
                           uri=True)


def gds2_checks(conn):
    print("\nGDS2 -- checked against captures/vauxhall/vatbin-decode-notes.md")

    # The Astra-K TCM node, named in the decode notes as
    # "Transmission Control Module$TCM - T76 - (Conti) - GME".
    rows = list(conn.execute(
        "SELECT DISTINCT n.label, n.path FROM module_node n"
        " WHERE n.path LIKE '%Astra-K%' AND n.label LIKE '%T76%'"))
    check("Astra-K T76 TCM node exists", bool(rows),
          "no module_node matched")
    if rows:
        check("Astra-K TCM node is the Conti GME T76",
              any("(Conti)" in r[0] for r in rows),
              "labels: %s" % sorted({r[0] for r in rows}))

    # Test 110282_001, the Transmission Adaptive Values Learn routine.
    rows = list(conn.execute(
        "SELECT key, name, test_type, n_bindings FROM test"
        " WHERE key = '110282_001'"))
    check("test 110282_001 is indexed", bool(rows))
    if rows:
        check("110282_001 is Transmission Adaptive Values Learn",
              all("Adaptive Values Learn" in r[1] for r in rows),
              "names: %s" % sorted({r[1] for r in rows}))
        # The decode notes describe four Output Control copies on the 2023.3
        # Opel file. Across all seven deliverables the same key also appears as
        # a Sequence Iterator test -- the second machine the notes name (id
        # 162871), which has its own PERFORM_SECURITY compare at state 584.
        types = sorted({r[2] for r in rows})
        check("110282_001 runs on the Output Control machine",
              "Output Control" in types, "types: %s" % types)
        print("        110282_001 copies: %d, test types %s"
              % (len(rows), types))

    # The ground-truth PID: transmission fluid temperature reads 22 19 40 and
    # scales (X*1)-40 degC, which is what the harness reads live from the TCM.
    rows = list(conn.execute(
        "SELECT DISTINCT i.param_id, i.request_hex FROM pid p"
        " JOIN pid_item i ON i.pid_off=p.off"
        "   AND i.deliverable_id=p.deliverable_id"
        " WHERE p.ref_name='8442_6736'"))
    check("PID 8442_6736 parameter id is 0x1940",
          any(r[0] == 0x1940 for r in rows), "got %s" % rows[:4])
    check("PID 8442_6736 request is 22 19 40",
          any(r[1] == "22 19 40" for r in rows), "got %s" % rows[:4])
    rows = list(conn.execute(
        "SELECT DISTINCT v.equation, v.unit FROM pid p"
        " JOIN pid_conv v ON v.pid_off=p.off"
        "   AND v.deliverable_id=p.deliverable_id"
        " WHERE p.ref_name='8442_6736' AND v.direction='read'"))
    check("PID 8442_6736 scales (X(0)*1)-40 in degC",
          any(r[0] == "(X(0)*1)-40" and "C" in (r[1] or "") for r in rows),
          "got %s" % rows[:4])

    # The AF40-versus-T76 question, settled by join rather than by memory.
    t76 = {r[0] for r in conn.execute(
        "SELECT DISTINCT b FROM option_pair WHERE b LIKE '%T76%'"
        " AND a LIKE '%Transmission%Type%'")}
    af40 = {r[0] for r in conn.execute(
        "SELECT DISTINCT b FROM option_pair WHERE b LIKE '%AF40%'"
        " OR b LIKE '%AF-40%'")}
    check("option pairs bind MH8 to T76",
          any("MH8" in v for v in t76), "T76 option values: %s" % sorted(t76)[:6])
    check("option pairs bind AF40 to M36/MDK/0A01/0A02",
          any(any(code in v for code in ("M36", "MDK", "0A01", "0A02"))
              for v in af40),
          "AF40 option values: %s" % sorted(af40)[:6])
    check("the T76 and AF40 option sets are disjoint",
          not (t76 & af40))

    # Deliverable coverage: every deliverable must have a populated object graph.
    rows = list(conn.execute(
        "SELECT d.label, (SELECT COUNT(*) FROM object o"
        "                  WHERE o.deliverable_id=d.id) n"
        " FROM deliverable d ORDER BY d.label"))
    empty = [r[0] for r in rows if r[1] == 0]
    check("every deliverable has a value-object graph", not empty,
          "empty: %s" % empty)
    for label, n in rows:
        print("        %-16s %d objects" % (label, n))


def sdd_checks(conn, jlr_json):
    print("\nJLR SDD -- checked against backend/profiles/jlr/data/sdd_dids.json")

    rows = list(conn.execute(
        "SELECT DISTINCT moduletype FROM ecu"
        " WHERE platform='L494_201600' AND can_target='0x7E6'"))
    check("0x7E6 on L494 MY16 is ABS, not an SCR controller",
          [r[0] for r in rows] == ["ABS"], "got %s" % rows)

    rows = list(conn.execute(
        "SELECT DISTINCT moduletype FROM ecu"
        " WHERE platform='L494_201600' AND can_target='0x7E0'"))
    check("0x7E0 on L494 MY16 is the PCM", "PCM" in [r[0] for r in rows],
          "got %s" % rows)

    if not jlr_json:
        print("  skip  sdd_dids.json not found; DID cross-check skipped")
        return

    # module_address_map_L494_MY16 is keyed by CAN request address, valued by
    # module name.
    addr = jlr_json.get("module_address_map_L494_MY16") or {}
    mismatches = []
    for address, module in addr.items():
        if not isinstance(module, str):
            module = module.get("module", "") if isinstance(module, dict) else ""
        want = address.upper().replace("0X", "")
        got = [(r[0] or "").upper().replace("0X", "") for r in conn.execute(
            "SELECT DISTINCT can_target FROM ecu"
            " WHERE platform='L494_201600' AND moduletype=?", (module,))]
        if got and want not in got:
            mismatches.append((address, module, got))
    check("every module address in sdd_dids.json matches the index",
          not mismatches, "mismatches: %s" % mismatches[:6])

    missing, renamed = [], []
    for entry in jlr_json.get("pcm_diesel_dids") or []:
        num = str(entry.get("did", "")).upper().replace("0X", "")
        # sdd_dids.json kept the raw <tm id="..."> markup where SDD wraps a name;
        # the index stores the inner text, so compare on the text.
        want_name = re.sub(r"<[^>]+>", " ", entry.get("name") or "")
        want_name = " ".join(want_name.split())
        got = [r[0] for r in conn.execute(
            "SELECT DISTINCT d.name FROM did d JOIN mdx m ON m.id=d.mdx_id"
            " WHERE m.platform='L494_201600' AND m.shortname='PCM'"
            "   AND REPLACE(UPPER(d.number),'0X','')=?", (num,))]
        if not got:
            missing.append(num)
        elif want_name and not any(
                want_name.lower()[:24] in g.lower() for g in got):
            renamed.append((num, want_name, got))
    check("every sdd_dids.json diesel-PCM DID is present in the index",
          not missing, "missing: %s" % missing[:8])
    check("DID names agree with sdd_dids.json", not renamed,
          "differing: %s" % renamed[:4])

    missing = []
    for entry in jlr_json.get("pcm_diesel_routines") or []:
        num = str(entry.get("routine", "")).upper().replace("0X", "")
        got = [r[0] for r in conn.execute(
            "SELECT DISTINCT r.name FROM routine r JOIN mdx m ON m.id=r.mdx_id"
            " WHERE m.platform='L494_201600' AND m.shortname='PCM'"
            "   AND REPLACE(UPPER(r.number),'0X','')=?", (num,))]
        if not got:
            missing.append(num)
    check("every sdd_dids.json PCM routine is present in the index",
          not missing, "missing: %s" % missing[:8])

    rows = list(conn.execute(
        "SELECT DISTINCT r.number, r.name, m.shortname FROM routine r"
        " JOIN mdx m ON m.id=r.mdx_id"
        " WHERE m.platform='L494_201600' AND r.number IN"
        " ('0x4076','0x4094','0x4095','0x4096')"))
    check("the four SCR routines are indexed as PCM routines",
          len(rows) == 4 and all(r[2] == "PCM" for r in rows),
          "got %s" % rows)

    x260 = {r[0] for r in conn.execute(
        "SELECT DISTINCT d.number FROM did d JOIN mdx m ON m.id=d.mdx_id"
        " WHERE m.platform='X260_201600' AND m.file_stem LIKE 'MDX_PCM_D%'")}
    l494 = {r[0] for r in conn.execute(
        "SELECT DISTINCT d.number FROM did d JOIN mdx m ON m.id=d.mdx_id"
        " WHERE m.platform='L494_201600' AND m.file_stem LIKE 'MDX_PCM_D%'")}
    shared = len(x260 & l494)
    print("        X260 and L494 diesel-PCM DIDs: %d shared, %d X260-only,"
          " %d L494-only" % (shared, len(x260 - l494), len(l494 - x260)))
    check("the X260 diesel PCM map transfers from the L494", shared > 30,
          "only %d shared" % shared)


def diagbox_checks(conn):
    print("\nDiagBox -- checked against captures/peugeot/diagbox-ep6-extraction.md")
    want = {
        "D429": "MP_PRESSION_HUILE_MOTEUR_BRUTE",
        "D464": "MP_CONSI_PRES_HUILE_MOT",
        "D43B": "MP_CMD_EV_POMPE_HUILE",
        "D4D6": "MP_TEMP_HUILE_MOT_ESTIM",
        "D41E": "MP_TENSION_HUILE_BRUTE",
        "D472": "MP_CONSI_POMPE_BASSE_PRE",
    }
    bad = []
    for did, name in want.items():
        got = [r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM fdb_param WHERE did=?", (did,))]
        if name not in got:
            bad.append((did, name, got[:3]))
    check("the oil-circuit DIDs carry the names the notes recorded", not bad,
          "wrong: %s" % bad)

    rows = list(conn.execute(
        "SELECT DISTINCT local_id FROM tree_screen"
        " WHERE screen='Screen_TA_ACT_POMPE_HUILE_2D'"))
    check("the oil-pump actuator screen decodes to local id 2D",
          [r[0] for r in rows] == ["2D"], "got %s" % rows)

    rows = list(conn.execute(
        "SELECT DISTINCT family FROM fdb_family WHERE engines LIKE '%5FV%'"))
    fams = {r[0] for r in rows}
    check("engine 5FV maps to the MED17 families",
          any(f.startswith("MED17_4") for f in fams),
          "families: %s" % sorted(fams)[:8])


def main():
    print("dealer-data index self-test")
    g = ro("gds2")
    if g:
        gds2_checks(g)
        g.close()
    else:
        print("\nGDS2 index not built, skipped")

    s = ro("sdd")
    if s:
        jlr_json = None
        for cand in (
                os.path.join(os.path.dirname(os.path.dirname(HERE)),
                             "backend", "profiles", "jlr", "data",
                             "sdd_dids.json"),
                os.path.expanduser(
                    "~/Desktop/car-harness-jlr/backend/profiles/jlr/data/"
                    "sdd_dids.json"),
                os.path.expanduser(
                    "~/Desktop/car-harness/backend/profiles/jlr/data/"
                    "sdd_dids.json")):
            if os.path.exists(cand):
                with open(cand, "r", encoding="utf-8") as fh:
                    jlr_json = json.load(fh)
                print("\n(cross-checking against %s)" % cand)
                break
        sdd_checks(s, jlr_json)
        s.close()
    else:
        print("\nSDD index not built, skipped")

    d = ro("diagbox")
    if d:
        diagbox_checks(d)
        d.close()
    else:
        print("\nDiagBox index not built, skipped")

    print("\n%d checks, %d failures" % (CHECKS[0], len(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
