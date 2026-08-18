-- Schema for the PSA DiagBox index.
--
-- Two very different kinds of source sit in this corpus.
--
-- The diagnostic trees (Vehicle\<MODEL>\<FUNCTION>\<ECU family>\*.s) are SGML-ish
-- scripts -- readable, but with unquoted attributes, so they are parsed leniently
-- rather than with an XML parser. They name every screen, actuator test and label
-- the dealer tool can reach for a given model and ECU family.
--
-- The FDB files are Firebird databases. There is no Firebird client on this
-- machine and the page structure has not been decoded, so they are mined by
-- record-pattern sweep: the parameter rows and the family/engine records are
-- recovered with their byte offsets, and everything else in those files remains
-- unread. That limitation is recorded here rather than hidden: see the fdb_sweep
-- table for what each sweep covered.

PRAGMA page_size = 8192;

CREATE TABLE IF NOT EXISTS corpus (
    id       INTEGER PRIMARY KEY,
    name     TEXT UNIQUE,
    root     TEXT,
    built_at TEXT,
    builder  TEXT,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS source_file (
    id       INTEGER PRIMARY KEY,
    rel_path TEXT UNIQUE,
    abs_path TEXT,
    size     INTEGER,
    sha1     TEXT,
    kind     TEXT,     -- 'tree' | 'fdb' | 'ini' | 'xml' | 'curve' | 'other'
    model    TEXT,     -- A7, B6, B7, T7 for tree files
    function TEXT,     -- INJ, BOITEVIT, ... the function directory
    family   TEXT      -- MED17_4_2, EDC17C10, ... the ECU family directory
);

-- ------------------------------------------------------- diagnostic trees ---
CREATE TABLE IF NOT EXISTS tree_node (
    source_file_id INTEGER,
    seq            INTEGER,
    depth          INTEGER,
    tag            TEXT,
    name           TEXT,      -- Name= or ScreenName= or FileName name=
    attrs          TEXT       -- every attribute, key=value joined with ' '
);

CREATE TABLE IF NOT EXISTS tree_screen (
    source_file_id INTEGER,
    tag            TEXT,      -- ActuatorTestScr, ReadECUDataScr, ...
    screen         TEXT,
    local_id       TEXT,      -- trailing _XX of an actuator screen name, as hex
    model          TEXT,
    function       TEXT,
    family         TEXT
);

CREATE TABLE IF NOT EXISTS tree_label (
    source_file_id INTEGER,
    polux_id       TEXT,      -- P1772 in @P1772-POLUXDATA
    context        TEXT
);

-- --------------------------------------------------------------- FDB mine ---
CREATE TABLE IF NOT EXISTS fdb_sweep (
    source_file_id INTEGER,
    sweep          TEXT,      -- which pattern was run
    pattern        TEXT,
    hits           INTEGER,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS fdb_param (
    source_file_id INTEGER,
    off            INTEGER,   -- byte offset of the parameter name in the file
    name           TEXT,      -- MP_PRESSION_HUILE_MOTEUR_BRUTE
    label          TEXT,      -- inline French label
    did            TEXT,      -- D429, when the row carries one
    format         TEXT,      -- UNM / SNM / ENUM / ...
    conversion     TEXT       -- FLOAT / ...
);

CREATE TABLE IF NOT EXISTS fdb_family (
    source_file_id INTEGER,
    off            INTEGER,
    vehicle        TEXT,      -- $PV$ model code, e.g. T7
    engines        TEXT,      -- $M$ engine codes, e.g. 5FV|5FN
    family         TEXT,      -- $C$ ECU family, e.g. MED17_4_2
    pd             TEXT       -- $PD$ field where present
);

CREATE TABLE IF NOT EXISTS fdb_reference (
    source_file_id INTEGER,
    off            INTEGER,
    kind           TEXT,      -- 'messagerie' | 'variant' | 'screen' | 'iocblid'
    value          TEXT
);
