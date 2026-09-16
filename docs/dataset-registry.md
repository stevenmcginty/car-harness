# Dataset registry

The map of every dealer-tool corpus on this machine: where it is, how big, what
format, how it was opened, what has been mined out of it, and what has not.

This document exists because working a format out costs a day and, before the
index, nothing queryable survived the session. The rule now is: raw corpora stay
where they are, the indexes are rebuildable from one command, and anything a
future job needs to know about a corpus is written here.

Everything below is static analysis of files already on this machine. Nothing in
the index touches a car.

## The indexes

Built databases live outside the repository at `C:\Users\steve\Desktop\car-harness-data\`
(override with the `CARHARNESS_DATA` environment variable). They are derived data:
large, useless in a diff, and one command from being rebuilt. Nothing there is in
git, and nothing there is a substitute for the raw corpus.

| Index | Built from | Size | Rebuild |
|---|---|---|---|
| `gds2.db` | 7 GDS2 `vatbin.bin` deliverables | 17.1 GB | `python tools/dealer_index/build_gds2.py` |
| `sdd.db` | JLR SDD 169 decrypted module database | 34 MB | `python tools/dealer_index/build_sdd.py` |
| `diagbox.db` | PSA DiagBox V09.233 trees and FDBs | 155 MB | `python tools/dealer_index/build_diagbox.py` |

Across the three: 8.05 M value objects, 1.37 M PIDs, 375 k DTC rows, 120 k tests
and 159 k menu items from GDS2; 9,132 DIDs, 8,144 routines and 2,256 ECU
addressing records from SDD; 503 k parameter rows, 5,366 screens and 1.19 M
script nodes from DiagBox. `python tools/dealer_index/selftest.py` checks 23 of
these against facts established independently.

Query them with `python tools/dealer_index/dealerdb.py` — see
`tools/dealer_index/README.md`.

`gds2.db` is big because it keeps every value object, every field and every
language of every string chain across all seven deliverables. Build a smaller one
with `--languages 0,1` (display text and English only) or by naming a single
deliverable.

---

## 1. GDS2 — Vauxhall / Opel and GM Global

**Location** `C:\Users\steve\Desktop\vauxhall\` — 3.3 GB, 35,813 files.

**What it is** The Global Diagnostic System 2 dealer application, in two lines:
the Opel/Vauxhall deliverables and the wider GM Global ones. Sourced as a
community repack ("fantomel"); see `captures/vauxhall/dealer-tool-source.md`.

**The deliverables that matter** — each is a `BinaryFile\vatbin.bin`:

| Label | Line | Size | Date |
|---|---|---|---|
| `_x_2023ov` | Opel/Vauxhall 2023.3 | 137.6 MB | Mar 2023 |
| `_x_deliv1022` | Opel/Vauxhall Oct 2022 | 137.6 MB | Oct 2022 |
| `d_ov44` | Opel/Vauxhall | 89.5 MB | Apr 2023 |
| `d_ov2406` | Opel/Vauxhall 2024.06 | 90.1 MB | Jun 2024 |
| `d_ovglobal233` | Opel/Vauxhall Global 23.3 | 148.8 MB | Mar 2023 |
| `d_gmglobal351` | GM Global 3.51 | 221.9 MB | May 2023 |
| `d_gg2406` | GM Global 2024.06 | 230.4 MB | Jun 2024 |

**Format** Not obfuscated, but not greppable either: strings are UTF-16LE records
and every pointer inside a value object is an absolute file offset stored as an
IEEE-754 double. The full format reference is
`captures/vauxhall/vatbin-decode-notes.md`; the reader is `tools/gds2/vatbin.py`;
the ground truth is the CFR-decompiled runtime at
`C:\Users\steve\Desktop\vauxhall\unpack\src\com\Mahle\`.

**How it was opened** Statically, with the decompiled reader classes as the
specification. No installer was run.

**Tooling** `tools/gds2/` in this repository. Those thirteen files were checked
byte-for-byte against the working copies in
`C:\Users\steve\Desktop\vauxhall\unpack\tools\`, which is outside version control
and is what was lost between earlier sessions; they are identical, and there is
nothing left in that directory that exists only there.

**What is mined** Everything reachable, into `gds2.db`:

- the whole menu-item tree with containers, attributes and state triples
- every value object and every field, with its file offset (720k objects on the
  2023.3 Opel file, 2.16 M on GM Global 2024.06)
- module nodes, connection records with their CAN request ids and bus names
- PID and CPID definitions with request parameters, reconstructed request bytes,
  conversions, ranges, units and enumerated states (87k PIDs on 2023.3 Opel,
  446k on GM Global 2024.06)
- test folders, groups, test definitions and their per-state data bindings
- every state machine, state, transition and attached data object
- the VAT function table, the DTC catalogues, the option pairs, the data-display
  lists, module timing, applicability, OSC setups and commands, named parameters
- every string chain any pointer reaches, in every language

**What is not mined** The rest of the GDS2 install: the Java runtime jars
(`unpack\s4`, `unpack\s6`, ~500 MB), the RTK and NA/Opel deliverables
(`na224`, `opel100`, `rtk224`), and the decompiled sources themselves. Those are
program code, not vehicle data. The `.dbdt` and `opengds` directories are empty
or trivial.

**What was built on it — the committed data packs** Two module nodes have been
exported out of `_x_2023ov` into JSON packs that live in the repository, each
row carrying the file offset it was read from:

- `backend/profiles/vauxhall/data/gds2_t76_tcm.json` — module node
  `Transmission Control Module$TCM - T76 - (Conti) - GME`, the 2017 Astra K
  gearbox: 189 parameters and 17 device controls with GM's names, scalings and
  state tables. Regenerated by `tools/gds2/export_module.py`. Read through
  `backend/profiles/vauxhall/dealer_data.py`. Its read decode is CONFIRMED on a
  car: the file's fluid-temperature parameter (`22 1940`, `(X*1)-40 °C`) is the
  one this harness already reads correctly from 0x7E2 on VIN
  W0LPF2EC8HG072884. Built on by `gearbox.py`, `tcm.py` and `procedures.py`.
- `backend/profiles/vauxhall/data/gds2_e39a_ecm.json` (2.9 MB) — module node
  `Engine Control Module$ECM - E39A (S3) - GME`, the node the Astra-J 2.0L
  (LHU) menu tree binds, which is the ECM in the 2016 Astra VXR: 970 parameters
  (900 reads on `$22`/`$1A` plus 70 `$AE` device controls), 61 named tests in 7
  folders, the node's own 28-row DTC override list, 6,223 rows of GM's master P
  and U dictionary, and the connection record (0x7E0/0x7E8,
  GMLAN_HS_PRIMARY_500k_Pin6&14). Regenerated by
  `tools/dealer_index/export_vauxhall_ecm.py`. Read through the `ecm_*` half of
  `dealer_data.py`; built on by `backend/profiles/vauxhall/engine.py`.

  **Nothing in the ECM pack is car-verified, and it carries four
  machine-readable `caveats` saying so.** The PID set the node binds is GM's
  SHARED "E39 4cyl" set rather than an A20NFT set, so every parameter is graded
  `applicability: UNVERIFIED` and NRC 31 is expected on a fair number of them.
  There is no test-to-frame join anywhere in the index (`test_binding` resolves
  for 0 of 467,860 rows), so the 61 tests are names only. There are no
  security-access rows for this ECM. And there are NO pass/fail thresholds: the
  `range` on a conversion is the limit GDS2 draws its gauge between, and some
  are plainly wrong — Brake Booster Pressure Sensor scales to kPa and carries
  hi = 1. Deliverable `d_ov44` carries the same node but its DTC text layer is
  corrupt and was not used.

**Corrections this corpus forced** (all now in `tools/dealer_index/README.md`):
the header's "data-list" table is the DTC catalogue table; the option-pair array
offset is read from `s+20` relative to `s`; the option pairs are
`(option category, option value)` name pairs, not bare RPO codes; and **value
objects are not always 4-byte aligned** — the Opel files are, the GM Global files
are 2-aligned.

---

## 2. JLR SDD 169

**Location** `C:\Users\steve\Desktop\sdd169\` — 7.2 GB, 22,309 files.

**What it is** Jaguar Land Rover's Symptom Driven Diagnostics, version
169.00.001. The SDD-era tool for the pre-Pathfinder platforms, which is the
generation the X260 AdBlue job and the L494 sweeps are on.

**Layout**

| Path | Size | Content |
|---|---|---|
| `SDD_169.00.001_FULL.exe` | 3.3 GB | the original InstallShield package |
| `extracted\Disk1\data2.cab` | 3.3 GB | the unpacked cabinet — **a duplicate of the payload above**, reclaimable once the groups below are out |
| `xcl\CURRENT_JLR_XCL_XML_DATA_XML\Xml` | 166 MB | **the diagnostic gold**: 54 platform directories, 5,722 files |
| `db\COMMON_SDD_DATA_DTC_HELP_LANG_EN` | — | plaintext DTC catalogue, 6,171 files |
| `db\COMMON_SDD_DATA_ODST_LANG_EN` | — | on-demand self-test definitions, 93 files |
| `db\CURRENT_PAG_MCP_TEXT_XML` | 29 MB | UI text resources, 7,164 files |
| `apps`, `core`, `tools`, `uniextract` | 316 MB | dealer service DLLs, `Environment.dll`, extraction tooling |

**How it was opened** `unshield_new.exe` reads the InstallShield cabinet directly,
but only into a short local path. The `.exml` module files are **3DES-ECB with
PKCS5 padding**; the 24-byte key sits nibble-swapped in `CCryptFile`'s constructor
in `Environment.dll` and unswaps to the ASCII string `YmZwZlQrQ3V4dVltNTArWE9s`.
Full account: `captures/jlr/sdd169-extraction.md`. The index decrypts in memory
and never writes plaintext back into the corpus.

**What is mined** into `sdd.db`, 34 MB:

- 54 platforms — L316, L319, L320, L322, L359, L405, L494, L538, L550, X150,
  X152, X250, X260, X350, X351, X356, X358, X404, X760, X761 and their model years
- 2,256 ECU addressing records: module type, CAN request and response identifiers,
  protocol properties, security operations, software-download parameters
- 1,790 MDX module-data files
- 9,132 data identifiers with 89,898 sub-fields — resolution, offset, units,
  ranges, data types, and the read/write/control service, session and security
  references
- 45,105 enumerated states
- 8,144 routine identifiers
- the plaintext DTC descriptions (5,962 rows), fault types (257) and ODST tests
- the decrypted XML of every file, zlib-compressed, so no later question needs the
  key or the cabinet again

**What is not mined** The dealer service-action DLLs in `apps\` (compiled code,
not data), the `CURRENT_PAG_MCP_TEXT_XML` resources beyond being catalogued as
files, the flash/calibration groups, and `LEGACY_JWDS_*` (present as empty
directories in `db\` — those groups were never extracted from the cabinet).

**Reclaimable** `extracted\` duplicates the installer payload: 3.3 GB.

---

## 3. PSA DiagBox V09.233

**Location** `C:\Users\steve\Desktop\diagbox\` — 3.2 GB, 5,858 files.

**What it is** The Peugeot/Citroen dealer tool, a "Maverick Diagnostics" community
repack. Relevant to the EP6/THP oil-pressure job on the 308 CC and the RCZ.

**Layout**

| Path | Size | Content |
|---|---|---|
| `DiagBox_setup_V09.233.exe` | 951 MB | NSIS SFX — unpack with 7-Zip, do not run |
| `extracted\AWRoot.zip` | 722 MB | app, label databases, the FDB databases |
| `extracted\Vehicle.zip` | 71 MB | per-model diagnostic trees |
| `extracted\vehicle\Vehicle\{A7,B6,B7,T7}` | — | 5,830 `.s` diagnostic scripts |
| `extracted\fdb\dtrd\comm\data\DSD.FDB` | 757 MB | per-ECU diagnostic screens and parameter tables |
| `extracted\fdb\dtrd\comm\data\GPC.FDB` | 566 MB | communication and parameter catalogue |
| `extracted\fdb\dtrd\database\*.FDB` | 32 MB | MEASURE, EOBD, JDD, REF_FAM_VEH, BASEMOTR |
| `extracted\comm\dtrd\comm\Cal*.dll` | — | per-ECU frame-generator interpreters (generic) |
| `extracted\fdb\dtrd\trans\POLUXDATAfr_FR.DU8` | 4 MB | French label dictionary, still undecoded |

Model codes: **T7 = 308**, **A7 = 308 CC**, **B7 = RCZ**, **B6 = RCZ-era second
platform**. Background: `captures/peugeot/diagbox-ep6-extraction.md`.

**How it was opened** 7-Zip on the SFX, then plain reads. The `.s` scripts are
SGML-ish (`<Start posc=1 posl=1>` — unquoted attributes), so an XML parser rejects
them; the index parses them leniently. The FDBs are Firebird databases and there
is no Firebird client here, so they are mined by record-pattern sweep.

**What is mined** into `diagbox.db`, ~150 MB:

- 5,830 diagnostic scripts as 1.19 M tagged nodes, with the model, function and
  ECU family each belongs to
- 5,366 screen references including actuator tests with their local identifier
  decoded from the screen name (e.g. `Screen_TA_ACT_POMPE_HUILE_2D` → LID `0x2D`,
  present on A7, B7 and T7 across six ECU families)
- 78,136 Polux label references
- **502,971 parameter rows** from `DSD.FDB` and `GPC.FDB` — 67,707 distinct
  parameter names and 566 distinct DIDs, each with its inline French label,
  numeric format and conversion. An earlier hand sweep of the same file recovered
  166 DIDs.
- 5,110 `$PV$ / $M$ / $C$` vehicle-engine-family records
- 63,879 cross references (messagerie XML names, `IOCBLID` actuator verbs)

**What is not mined — and this is the honest gap.** The Firebird page structure is
undecoded, so the FDB content is only what a byte-pattern sweep can see. Rows are
recovered without their table, their keys or their relationships; a parameter row
gives a name, label, DID, format and conversion but not which ECU variant it
belongs to. `fdb_sweep` in the database records exactly which patterns were run
against which file. `BASEMOTR`, `EOBD`, `JDD`, `MEASURE`, `MEASURESVEHICULE` and
`REF_FAM_VEH` yielded nothing to these patterns and remain effectively unread.
`POLUXDATAfr_FR.DU8` is catalogued but not decoded. The messagerie XML files are
named inside GPC but their content is not in this installer.

---

## 4. Mercedes XENTRY Diagnostics OpenShell 26.6.4

**Location** `C:\Users\steve\Desktop\mercedes\` — 18.7 GB zip (`XENTRY_Diagnostics_OpenShell_26.6.4.zip`,
downloaded 2026-08-22, CRC-verified) plus derived trees beside it:
`extracted\` (~19 GB, every payload MSI flattened as `MediaN__{guid}__name.msi`),
`xdos\antrieb\` and `xdos\caesar_{cbf,cff,ccf}\` (administratively-installed
payloads), `mined\` (parsed CBFs as JSON).

**What it is** The Mercedes dealer diagnostic application, full passenger-car
data set: XENTRY (XD-Daten-PKW), DAS legacy trees, SDFlash, Caesar CBF/CFF/CCF
containers. Relevant to the SLK R172 rough-running job
(`captures/mercedes/slk.md`).

**How it was opened** No installer was run. The zip's central directory was
listed directly; each GUID-named MSI identified by its `ProductName` property
via Windows Installer COM; the needed packages unpacked with
`msiexec /a ... /qn TARGETDIR=...`. Package→GUID map recoverable by rerunning
that COM sweep over `extracted\`.

**What is mined** Only the two engine ECUs the SLK job needs, from the Caesar
layer:

- `SIM271DE20.CBF` → `mined\SIM271DE20.json` (511 MB) — M271 EVO CGI turbo,
  SLK200/250. UDS, 500 kbit, 0x7E0/0x7E8.
- `MED177.CBF` → `mined\MED177.json` (1.1 GB) — M276 DE35 V6, SLK350 (CBF also
  carries M278 variants).

Parser: `docs/private/mercedes-data/OpenVehicleDiag/CBFParser` (Rust; built
with rustup 1.98). Curated rough-running channel list (misfire counters,
injection times/adaptations, mixture adaptations, fuel pressures, camshaft
positions — 85 DIDs with scaling) is in
`backend/profiles/mercedes/data/xentry_dids.json`, built by
`tools/dealer_index/build_xentry_slk.py`.

**What was built on it** `backend/profiles/mercedes/misfire.py` turns that
table into the SLK rough-running instrument (`captures/mercedes/slk.md`
steps 0/4/5): variant identify, the timestamped misfire drive-log capture
(decoded live, raw bytes kept beside every value) and the signature
assessment. CLI: `backend/tools/mercedes_misfire_session.py` (runs land in
`captures/mercedes/runs/`); report: `backend/tools/mercedes_misfire_report.py`
(self-contained HTML into `captures/reports/`). The online known-issue
research both engines are judged against lives in
`backend/profiles/mercedes/data/slk_rough_running.json` (each row carries its
sources; Mercedes LI07.70-P-059951 intercooler-condensation misfires on model
172 is in there).

**What is not mined — the honest gaps**

- The XENTRY `.gmf` guided-diagnosis trees (Istwerte/Pruefungen per ECU) are
  GMF2 **encrypted** binaries; decryption lives inside the XENTRY runtime and
  was not reversed. Everything above comes from the Caesar layer instead.
- Only 2 of ~70 CBFs are parsed. The other engine/transmission/body CBFs sit
  in `xdos\caesar_cbf\Xentry\MB_PKW\Caesar\cbf\` (incl. `SIM271KE20.CBF`,
  `SIM271KECNG.CBF` for older M271 Kompressor cars).
- DAS legacy trees, SDFlash flash data, Thesaurus, SMRX packages: untouched.
- Nothing is car-verified yet: every DID in `xentry_dids.json` is graded
  `candidate`; steps 0–2 of the SLK plan promote them.

---

## 5. Corpora referenced elsewhere but not indexed here

| Corpus | Where | Why not |
|---|---|---|
| BMW EDIABAS / SGBD | referenced by `backend/tools/bmw_live_extract.py` | already has its own generated pack (`backend/profiles/bmw/live_pack.json`), regenerated not committed |
| Autel APK teardown | `android/autel/` (gitignored) | third-party reference material, not ours to redistribute |
| EDC17 ZUZU captures | `captures/edc17-zuzu/` | live captures, not a dealer corpus |

---

## Adding a corpus

1. Leave the raw files where they are and record them here: path, size, format,
   how it opens, what is inside.
2. Write a builder in `tools/dealer_index/` that reads it in place and writes a
   SQLite database named after the corpus, with provenance on every row — which
   file, which offset or path, which object type.
3. Add the questions it can answer to `dealerdb.py`.
4. Say in this document what you did **not** mine. A gap that is written down is a
   starting point; a gap that is not is an ambush.
