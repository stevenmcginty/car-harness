# Dealer-data index

A local, queryable index of the dealer-tool data that has been cracked open for
this project. It exists because working a format out costs a day and, until now,
nothing queryable survived the session, so the next job started the archaeology
again — and a wrong claim (AF40 versus T76 gearbox families) survived three
sessions because there was nothing to join against.

Everything here is static analysis of files already on this machine. Nothing in
this directory talks to a car.

## Where things live

| What | Where |
|---|---|
| Builders, readers, query tool | `tools/dealer_index/`, `tools/gds2/` (in git) |
| Built databases | `C:\Users\steve\Desktop\car-harness-data\` (**not** in git) |
| Raw corpora | left where they are; see `docs/dataset-registry.md` |

The databases are derived data — large, rebuildable, and useless in a diff. They
live outside the repository so no worktree can accidentally commit them and every
checkout finds the same store. Override the location with the `CARHARNESS_DATA`
environment variable.

## Rebuilding

Losing the store must cost a rebuild, never a re-crack:

```
python tools/dealer_index/build_gds2.py            # every GDS2 deliverable found
python tools/dealer_index/build_gds2.py _x_2023ov  # just one, by label
python tools/dealer_index/build_gds2.py --list     # what is available
python tools/dealer_index/build_sdd.py             # the decrypted JLR SDD corpus
python tools/dealer_index/build_diagbox.py         # PSA DiagBox
```

Each builder is idempotent per deliverable: re-running one deletes and replaces
just that deliverable's rows.

Build times on this machine: GDS2 about 25 minutes for all seven deliverables
plus index creation, SDD about 90 seconds, DiagBox about 4 minutes.

Do not query a database while it is being built — the builders run with
`journal_mode=OFF` for speed, so a reader takes a lock the writer then waits on.
They set a two-minute busy timeout so a passing reader no longer kills the run,
and `build_gds2.py --index-only` recreates the indexes without re-extracting if
one ever does.

## Checking

```
python tools/dealer_index/selftest.py
```

23 checks that the index agrees with facts established independently elsewhere:
the Astra-K T76 TCM node and test `110282_001` from the decode notes, PID
`8442_6736` decoding to `22 19 40` with `(X(0)*1)-40 degC` as read live from the
car, the MH8/T76 versus AF40/M36 option split, the L494 module address map and
diesel-PCM DIDs and routines against `backend/profiles/jlr/data/sdd_dids.json`,
and the DiagBox oil-circuit DIDs and the `0x2D` oil-pump actuator against the
DiagBox extraction notes. A failure here is a finding: either the index is wrong
or the thing it is checked against is.

## Querying

```
python tools/dealer_index/dealerdb.py <command> [args]
```

See `dealerdb.py --help`. The questions it is built to answer are the ones these
jobs actually ask: what modules does this vehicle have, what PIDs does this module
expose, what does this DID mean, what tests exist for this module, which vehicles
share this module.

## GDS2 index (`gds2.db`)

Built from `vatbin.bin`. Two layers.

**Generic layer** — everything reachable, nothing interpreted away. `object` and
`object_field` hold every value object and every field with its file offset;
`item`, `container`, `item_edge`, `item_attr`, `item_triple` hold the whole menu
tree; `machine`, `machine_state`, `machine_transition`, `machine_attr` hold the
state machines; `string_head`/`string_record` hold every string chain that any
pointer in the file reaches, in every language; `vat_function`, `dtc_list`,
`dtc_row`, `option_pair` hold the header tables.

**Semantic layer** — derived from the same traversal, indexed for the hot queries:
`module_node`, `module_conn`, `module_conn_list`, `pid_set`, `pid`, `pid_item`,
`pid_conv`, `pid_state`, `test_folder`, `test_group`, `test`, `test_binding`,
`data_list`, `data_node`, `module_timing`, `applicability`, `osc_setup`,
`osc_command`, `named_param`, `security_block`.

`pid_item.request_hex` is the literal request reconstructed from the PID type and
parameter id (`22 hi lo` for reads, `AE ..` for device control, raw otherwise).

Every row carries `deliverable_id`, and every row that came from a byte range
carries that offset, so any answer can be taken back to the file it came from.

## Format corrections found while building

- The header table at `0x38`/`0x3C` (count/offset, stride 12) is **not** a
  "data-list" table; it is the **DTC catalogue table**. Each row is
  `[name ptr][row count][row table offset]` (`f.java::l()`), and the rows are DTC
  entries of 24 bytes — 28 when header bit 7 is set, which it is not here
  (`e.a()`, layout in `d.java`: `+0` numeric code, `+4` code string, `+8`, `+12`,
  `+16`, `+20` further strings). The names are what a module connection record's
  master-list fields point at.
- The RPO option-pair array offset is read from `s+20` **relative to `s`**, not
  fixed at `s+24` (`f.java::i()`); the count is at `s+16`, and the table is only
  valid when `s+4 >= 2`.
- The option pairs are not bare RPO codes. They are `(option category, option
  value)` name pairs — `Automatic Transmission Type` →
  `Automatic Transmission T76 Delphi (MH8)`, `AF40 Transmission Control Module
  Version` → `Automatic Transmission AF40 (M36 _ 0A01)`. The RPO code sits in
  parentheses inside the value text. This is the table that settles the
  AF40-versus-T76 question by query rather than by memory.
- Machine attributes are string-pointer pairs at `+20` with the count at `+16`
  (`f.java::e(int)`); `Persistent Machine`, `Test Edit States`,
  `Application Data States` and `Shutdown State` are the meaningful names.
