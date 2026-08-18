-- Schema for the JLR SDD index.
--
-- Source corpus: the InstallShield cabinet inside SDD_169.00.001_FULL.exe, whose
-- CURRENT_JLR_XCL_XML_DATA_XML group holds the per-platform module database. The
-- .exml files in it are 3DES-ECB encrypted (key YmZwZlQrQ3V4dVltNTArWE9s, see
-- captures/jlr/sdd169-extraction.md); the builder decrypts them in memory and
-- never writes plaintext back into the corpus.
--
-- Every row traces back to source_file, which carries the group, the platform
-- directory, the path relative to the SDD root and the SHA-1 of the file on disk.

PRAGMA page_size = 8192;

CREATE TABLE IF NOT EXISTS corpus (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE,
    root      TEXT,
    built_at  TEXT,
    builder   TEXT,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS source_file (
    id         INTEGER PRIMARY KEY,
    group_name TEXT,      -- installer file group, e.g. CURRENT_JLR_XCL_XML_DATA_XML
    platform   TEXT,      -- platform directory, e.g. L494_201600
    rel_path   TEXT UNIQUE,
    abs_path   TEXT,
    size       INTEGER,
    sha1       TEXT,
    encrypted  INTEGER,   -- 1 when the file on disk is .exml
    root_tag   TEXT,
    kind       TEXT,      -- 'module' | 'mdx' | 'gui' | 'dtc' | 'odst' | 'other'
    parse_error TEXT,
    xml        BLOB       -- zlib-compressed plaintext XML, so the corpus is
);                        -- answerable without re-decrypting anything

-- ------------------------------------------------------- module addressing ---
CREATE TABLE IF NOT EXISTS ecu (
    id             INTEGER PRIMARY KEY,
    source_file_id INTEGER,
    platform       TEXT,
    ecu_ref        TEXT,      -- ECU@id, e.g. PCM_SYSTEM_A_30
    moduletype     TEXT,      -- PCM, ABS, RDCM, ...
    ecu_type       TEXT,      -- ECU@type, e.g. WDS
    model          TEXT,      -- qualifier@model, e.g. L494
    year           TEXT,      -- qualifier@year, e.g. MY16
    can_src        TEXT,      -- response identifier
    can_target     TEXT,      -- request identifier
    addr_type      TEXT,
    addr_format    TEXT,
    bit_len        TEXT,
    accept_mask    TEXT,
    baud_rate      TEXT,
    spec_version   TEXT,
    device_no      TEXT
);

CREATE TABLE IF NOT EXISTS ecu_qual (
    ecu_id  INTEGER,
    type    TEXT,
    subtype TEXT,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS ecu_property (
    ecu_id  INTEGER,
    section TEXT,             -- 'properties' or 'swdl'
    prop_id TEXT,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS ecu_security (
    ecu_id  INTEGER,
    op_type TEXT,
    value   TEXT
);

-- ------------------------------------------------------ module data (MDX) ---
CREATE TABLE IF NOT EXISTS mdx (
    id             INTEGER PRIMARY KEY,
    source_file_id INTEGER,
    platform       TEXT,
    file_stem      TEXT,      -- MDX_PCM_D, MDX_PCM_D_3LT, ...
    ecu_name       TEXT,      -- ADMINISTRATION/ECU_NAME
    shortname      TEXT       -- ADMINISTRATION/SHORTNAME, e.g. PCM
);

CREATE TABLE IF NOT EXISTS did (
    id          INTEGER PRIMARY KEY,
    mdx_id      INTEGER,
    did_ref     TEXT,          -- DID@ID, e.g. did_0396
    number      TEXT,          -- as written, e.g. 0x0396
    number_int  INTEGER,       -- parsed, for range queries
    name        TEXT,
    description TEXT,
    byte_size   INTEGER,
    did_type    TEXT,
    read_services   TEXT,
    read_sessions   TEXT,
    read_security   TEXT,
    write_services  TEXT,
    write_sessions  TEXT,
    write_security  TEXT,
    control_services TEXT,
    control_sessions TEXT,
    control_iocp     TEXT
);

CREATE TABLE IF NOT EXISTS did_subfield (
    id          INTEGER PRIMARY KEY,
    did_id      INTEGER,
    sub_ref     TEXT,
    idx         INTEGER,
    name        TEXT,
    lsb         INTEGER,
    msb         INTEGER,
    data_type   TEXT,
    resolution  TEXT,
    offset      TEXT,
    units       TEXT,
    range_low   TEXT,
    range_high  TEXT,
    revert      TEXT
);

CREATE TABLE IF NOT EXISTS did_enum (
    subfield_id INTEGER,
    did_id      INTEGER,
    member_ref  TEXT,
    value       TEXT,
    name        TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS routine (
    id           INTEGER PRIMARY KEY,
    mdx_id       INTEGER,
    routine_ref  TEXT,
    number       TEXT,
    number_int   INTEGER,
    name         TEXT,
    description  TEXT,
    session_refs TEXT,
    max_run_time TEXT,
    restart      TEXT
);

CREATE TABLE IF NOT EXISTS routine_param (
    routine_id INTEGER,
    param_ref  TEXT,
    name       TEXT,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS memory_area (
    mdx_id        INTEGER,
    area_ref      TEXT,
    name          TEXT,
    start_address TEXT,
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS mdx_section (
    mdx_id  INTEGER,
    section TEXT,       -- SECURITY_DATA, SPECIAL_PROCEDURES, DEVIATIONS, ...
    detail  TEXT        -- flattened text, so nothing in the file is dropped
);

-- ------------------------------------------------- plaintext SDD catalogues ---
CREATE TABLE IF NOT EXISTS dtc_description (
    source_file_id INTEGER,
    code_id        TEXT,       -- dtc@id, e.g. 0x0001 or _12
    module         TEXT,       -- present in dtcModuleDescriptions.xml only
    description    TEXT
);

CREATE TABLE IF NOT EXISTS dtc_fault_type (
    source_file_id INTEGER,
    number         INTEGER,
    description    TEXT
);

CREATE TABLE IF NOT EXISTS odst_test (
    source_file_id INTEGER,
    module_type    TEXT,
    test_id        TEXT,
    help_id        TEXT,
    timeout        TEXT,
    time           TEXT,
    flag           TEXT
);

CREATE TABLE IF NOT EXISTS odst_qualifier (
    source_file_id INTEGER,
    module_type    TEXT,
    test_id        TEXT,
    model          TEXT,
    year           TEXT
);
