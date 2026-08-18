-- Schema for the GDS2 index (vatbin.bin, Vauxhall/Opel and GM Global).
--
-- Two layers.
--
-- The generic layer (object, object_field, item, container, string_record,
-- machine*, vat_function, dtc_*, option_pair) holds everything the file contains
-- that is reachable from the roots, with the file offset of every row. Nothing is
-- interpreted away; if a later question needs a field nobody has looked at, it is
-- already here.
--
-- The semantic layer (module_node, module_conn, pid_set, pid, pid_item, pid_conv,
-- pid_state, test_folder, test_group, test, test_binding, data_list, data_node,
-- module_timing, applicability, osc_setup, osc_command, named_param) is derived from
-- the same traversal and exists so the common questions are one indexed lookup.
--
-- Every row carries deliverable_id, and every row that came from a byte range
-- carries that offset, so provenance is always recoverable.

PRAGMA page_size = 8192;

CREATE TABLE IF NOT EXISTS deliverable (
    id          INTEGER PRIMARY KEY,
    label       TEXT UNIQUE,      -- deliverable directory, e.g. _x_2023ov, d_gg2406
    path        TEXT,             -- absolute path of the vatbin.bin that was read
    size        INTEGER,
    file_mtime  TEXT,
    sha1        TEXT,
    version     TEXT,             -- header version string, e.g. "Opel/Vauxhall"
    flags       INTEGER,
    lang_count  INTEGER,
    built_at    TEXT,
    builder     TEXT
);

-- ---------------------------------------------------------------- strings ----
CREATE TABLE IF NOT EXISTS string_head (
    deliverable_id INTEGER,
    off            INTEGER,       -- head record offset, the value a pointer holds
    key            TEXT,          -- first record: the symbolic key when flag = 1
    text           TEXT,          -- display text for the default language
    flag           INTEGER,
    n_records      INTEGER,
    PRIMARY KEY (deliverable_id, off)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS string_record (
    deliverable_id INTEGER,
    head_off       INTEGER,
    seq            INTEGER,
    off            INTEGER,
    lang           INTEGER,
    flag           INTEGER,
    text           TEXT
);

-- ------------------------------------------------------------- menu tree ----
CREATE TABLE IF NOT EXISTS container (
    deliverable_id INTEGER,
    off            INTEGER,
    key            TEXT,
    label          TEXT,
    question       TEXT,
    count          INTEGER,
    items_off      INTEGER,
    PRIMARY KEY (deliverable_id, off)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS item (
    deliverable_id  INTEGER,
    off             INTEGER,
    type            INTEGER,
    name_ptr        INTEGER,
    name            TEXT,          -- symbolic key
    label           TEXT,          -- display text
    f3              INTEGER,
    child_container INTEGER,
    depth           INTEGER,
    path            TEXT,          -- first path found from the root, ' / ' joined
    PRIMARY KEY (deliverable_id, off)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS item_edge (
    deliverable_id INTEGER,
    parent_off     INTEGER,
    container_off  INTEGER,
    child_off      INTEGER,
    idx            INTEGER
);

CREATE TABLE IF NOT EXISTS item_attr (
    deliverable_id INTEGER,
    item_off       INTEGER,
    idx            INTEGER,
    name           TEXT,
    value          TEXT
);

-- The join between the menu tree and the value-object graph: one value object
-- attached to one numbered state of one state machine.
CREATE TABLE IF NOT EXISTS item_triple (
    deliverable_id INTEGER,
    item_off       INTEGER,
    idx            INTEGER,
    machine_off    INTEGER,
    state_no       INTEGER,
    obj_off        INTEGER
);

-- ---------------------------------------------------- value-object graph ----
CREATE TABLE IF NOT EXISTS object (
    deliverable_id INTEGER,
    off            INTEGER,
    type           INTEGER,       -- ScrStruct type id; 6 = list, <= 5 = scalar
    shape          TEXT,          -- 'struct' | 'list' | 'scalar'
    elem_type      INTEGER,       -- lists only
    n_fields       INTEGER,
    summary        TEXT,          -- one-line human summary, strings resolved
    PRIMARY KEY (deliverable_id, off)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS object_field (
    deliverable_id INTEGER,
    obj_off        INTEGER,
    idx            INTEGER,
    field_type     INTEGER,
    num            REAL,          -- raw double as stored
    int_val        INTEGER,       -- integer view where the field type is integral
    str_off        INTEGER,       -- string pointer when field_type = 5
    text           TEXT,          -- resolved display text for that pointer
    ref_off        INTEGER        -- referenced object offset when field_type > 6
);

-- -------------------------------------------------------- state machines ----
CREATE TABLE IF NOT EXISTS machine (
    deliverable_id INTEGER,
    off            INTEGER,
    machine_id     INTEGER,
    name           TEXT,
    n8             INTEGER,
    o12            INTEGER,
    state_count    INTEGER,
    state_off      INTEGER,
    PRIMARY KEY (deliverable_id, off)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS machine_attr (
    deliverable_id INTEGER,
    machine_off    INTEGER,
    idx            INTEGER,
    name           TEXT,
    value          TEXT
);

CREATE TABLE IF NOT EXISTS machine_state (
    deliverable_id INTEGER,
    machine_off    INTEGER,
    off            INTEGER,
    state_no       INTEGER,
    func_id        INTEGER,
    func_name      TEXT,
    default_next   INTEGER,
    label          TEXT,
    trans_count    INTEGER,
    trans_off      INTEGER,
    data_obj       INTEGER
);

CREATE TABLE IF NOT EXISTS machine_transition (
    deliverable_id INTEGER,
    machine_off    INTEGER,
    state_no       INTEGER,
    idx            INTEGER,
    result         INTEGER,
    next_state     INTEGER,
    extra          INTEGER
);

-- ---------------------------------------------------------- header tables ----
CREATE TABLE IF NOT EXISTS vat_function (
    deliverable_id INTEGER,
    idx            INTEGER,
    off            INTEGER,
    func_id        INTEGER,
    name           TEXT,          -- the key used in the state table
    description    TEXT
);

CREATE TABLE IF NOT EXISTS dtc_list (
    deliverable_id INTEGER,
    idx            INTEGER,
    off            INTEGER,
    name           TEXT,          -- master-list name a module connection points at
    row_count      INTEGER,
    rows_off       INTEGER,
    row_stride     INTEGER
);

CREATE TABLE IF NOT EXISTS dtc_row (
    deliverable_id INTEGER,
    list_name      TEXT,
    idx            INTEGER,
    off            INTEGER,
    code_int       INTEGER,       -- numeric code at +0
    code           TEXT,          -- code string at +4
    description    TEXT,          -- +8
    field_12       TEXT,
    field_16       TEXT,
    field_20       TEXT,
    field_24       TEXT           -- only when header flag bit 7 is set
);

CREATE TABLE IF NOT EXISTS option_pair (
    deliverable_id INTEGER,
    idx            INTEGER,
    a              TEXT,          -- RPO / option code
    b              TEXT           -- the code it is bound to
);

-- -------------------------------------------------------- semantic layer ----
CREATE TABLE IF NOT EXISTS module_node (
    deliverable_id INTEGER,
    item_off       INTEGER,
    name           TEXT,
    label          TEXT,
    path           TEXT,
    vehicle        TEXT,          -- best-effort vehicle/line from the item path
    conn_obj       INTEGER,       -- state 2  container of connection records
    pidset_obj     INTEGER,       -- state 3
    datalist_obj   INTEGER,       -- state 8
    applic_obj     INTEGER,       -- state 9
    dtc_obj        INTEGER,       -- state 11
    timing_obj     INTEGER,       -- state 12
    test_obj       INTEGER        -- state 13
);

CREATE TABLE IF NOT EXISTS module_conn (
    deliverable_id INTEGER,
    off            INTEGER,
    container_off  INTEGER,
    module         TEXT,
    request_id     TEXT,          -- CAN request identifier as the file states it
    bus            TEXT,
    pidset_name    TEXT,
    fields         TEXT,          -- whole record, ' | ' joined, nothing discarded
    PRIMARY KEY (deliverable_id, off)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS module_conn_list (
    deliverable_id INTEGER,
    off            INTEGER,       -- the 17515 container
    idx            INTEGER,
    master_list    TEXT           -- the four master-list names (DTC catalogues)
);

CREATE TABLE IF NOT EXISTS pid_set (
    deliverable_id INTEGER,
    off            INTEGER,
    ref_name       TEXT,
    module_name    TEXT,
    n_pids         INTEGER,
    PRIMARY KEY (deliverable_id, off)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pid (
    deliverable_id INTEGER,
    off            INTEGER,
    set_off        INTEGER,
    ref_name       TEXT,
    name           TEXT,
    key            TEXT,
    unit           TEXT,
    PRIMARY KEY (deliverable_id, off, set_off)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pid_item (
    deliverable_id INTEGER,
    pid_off        INTEGER,
    idx            INTEGER,
    off            INTEGER,
    param_id       INTEGER,       -- p[0], the wire id
    length         INTEGER,       -- p[1]
    flags          INTEGER,       -- p[2]
    pid_type       INTEGER,       -- p[3]: 0 read $22, 3 device control $AE, 4 raw
    p4             INTEGER,
    byte_off       INTEGER,       -- p[5]
    p6             INTEGER,
    pattern        TEXT,          -- D, comma separated hex, XX = wildcard
    request_hex    TEXT           -- reconstructed request bytes where the rule is known
);

CREATE TABLE IF NOT EXISTS pid_conv (
    deliverable_id INTEGER,
    pid_off        INTEGER,
    direction      TEXT,          -- 'read' | 'write'
    off            INTEGER,
    conv_type      TEXT,
    equation       TEXT,
    unit           TEXT,
    lo             REAL,
    hi             REAL,
    fmt            TEXT
);

CREATE TABLE IF NOT EXISTS pid_state (
    deliverable_id INTEGER,
    pid_off        INTEGER,
    conv_off       INTEGER,
    value          REAL,
    text           TEXT
);

CREATE TABLE IF NOT EXISTS test_folder (
    deliverable_id INTEGER,
    off            INTEGER,
    name           TEXT,
    n_groups       INTEGER
);

CREATE TABLE IF NOT EXISTS test_group (
    deliverable_id INTEGER,
    off            INTEGER,
    folder_off     INTEGER,
    name           TEXT,
    key            TEXT,
    n_tests        INTEGER
);

CREATE TABLE IF NOT EXISTS test (
    deliverable_id INTEGER,
    off            INTEGER,
    group_off      INTEGER,
    key            TEXT,          -- the symbolic key, e.g. 110282_001
    name           TEXT,
    test_type      TEXT,
    description    TEXT,
    n_bindings     INTEGER
);

CREATE TABLE IF NOT EXISTS test_binding (
    deliverable_id INTEGER,
    test_off       INTEGER,
    idx            INTEGER,
    off            INTEGER,
    object_id      INTEGER,
    state_no       INTEGER,
    data_off       INTEGER,
    data_type      INTEGER,
    func_name      TEXT           -- VAT function of that state, when resolvable
);

CREATE TABLE IF NOT EXISTS data_list (
    deliverable_id INTEGER,
    off            INTEGER,
    key            TEXT,
    n_nodes        INTEGER
);

CREATE TABLE IF NOT EXISTS data_node (
    deliverable_id INTEGER,
    off            INTEGER,
    owner_off      INTEGER,       -- the 17355 list or 17319 watch set it sits in
    pid_ref        TEXT,
    module         TEXT,
    label          TEXT
);

CREATE TABLE IF NOT EXISTS module_timing (
    deliverable_id INTEGER,
    off            INTEGER,
    owner_off      INTEGER,
    fields         TEXT
);

CREATE TABLE IF NOT EXISTS applicability (
    deliverable_id INTEGER,
    off            INTEGER,
    owner_off      INTEGER,
    key            TEXT,
    conditions     TEXT           -- the BCD condition strings, ' ; ' joined
);

CREATE TABLE IF NOT EXISTS osc_setup (
    deliverable_id INTEGER,
    off            INTEGER,
    owner_off      INTEGER,
    fields         TEXT
);

CREATE TABLE IF NOT EXISTS osc_command (
    deliverable_id INTEGER,
    off            INTEGER,
    setup_off      INTEGER,
    fields         TEXT
);

CREATE TABLE IF NOT EXISTS named_param (
    deliverable_id INTEGER,
    off            INTEGER,
    name           TEXT,
    value          TEXT,
    short          TEXT,
    description    TEXT,
    unit           TEXT
);

CREATE TABLE IF NOT EXISTS security_block (
    deliverable_id INTEGER,
    off            INTEGER,
    module         TEXT,
    equation       TEXT,
    level          TEXT,
    calc_type      TEXT,
    flags          TEXT,
    seed_bytes     TEXT
);
