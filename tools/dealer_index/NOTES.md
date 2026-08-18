# Working notes -- dealer-data index

Running log for the build of the mined-data layer. Written as the work happens so
that a lost session is a re-read, not a re-crack.

## Decisions

**Branch and checkout.** Work happens in a git worktree at
`C:\Users\steve\Desktop\car-harness-index` on branch `dealer-data-index`, cut from
`master`. The main checkout stays on `vauxhall-gm-global-a` untouched because
another agent is working there. `tools/gds2/` was taken by `git show cef7121:...`
rather than by checking that branch out.

**Where the databases live.** Outside the repository, at
`C:\Users\steve\Desktop\car-harness-data\` (override with the `CARHARNESS_DATA`
environment variable). Reasons:

- They are large (hundreds of MB to a few GB) and are pure derived data. A
  gitignored directory inside the repo would still be duplicated per worktree and
  would sit one `git add -A -f` away from being committed.
- A single store shared by every checkout and worktree means the CLI finds the same
  database no matter which copy of the tree it is run from.
- Losing the store costs one rebuild command, never a re-crack.

Raw installer blobs are not copied. Every builder reads the corpus in place from
`C:\Users\steve\Desktop\{vauxhall,sdd169,diagbox}\`.

## Corpus 1 -- GDS2 `vatbin.bin`

Format reference: `captures/vauxhall/vatbin-decode-notes.md`, plus the decompiled
runtime at `C:\Users\steve\Desktop\vauxhall\unpack\src\com\Mahle\b\a\`.

Facts confirmed while writing the builder, beyond what the notes already said:

- The header's "data-list" table (count `0x38`, table `0x3C`, stride 12) is really
  the **DTC catalogue table**. `f.java::l()` builds a `com.Mahle.b.a.e` per row:
  `[name string ptr][row count][row table offset]`, and `e.a()` reads rows of 24
  bytes (28 when header flag bit 7 is set; it is clear here). `d.java` gives the row
  layout: `+0` numeric code, `+4` code string, `+8` / `+12` / `+16` / `+20` four more
  string pointers. Those name strings are what a module connection record points at
  through its "master list" fields.
- The VAT function table rows (count `0x48`, table `0x4C`, stride 12) are
  `[id][string ptr -> description][string ptr -> name]` per `a.java` and `f.java::m()`.
- The RPO option-pair array offset is **read from `s+20` as a value relative to `s`**,
  not hard-coded at `s+24` (`f.java::i()`); the count is at `s+16` and the guard is
  `s+4 >= 2`. On 2023.3 OV `s+20` happens to be 24, which is where the earlier note's
  constant came from.
- Machine attribute tables are pairs of string pointers, stride 8, at `+20` with the
  count at `+16` (`f.java::e(int)`). The attribute named `Persistent Machine` is what
  marks the persistent machine; `Test Edit States`, `Application Data States` and
  `Shutdown State` are comma-separated state number lists.

Build design: one generic layer that stores **every reachable value object and every
one of its fields** with its file offset, plus semantic tables (modules, PIDs,
conversions, tests, bindings, DTC rows, machines) built on top of the same traversal.
The generic layer is the "extract everything" guarantee; the semantic tables are the
fast path for the questions jobs actually ask.

## Corpus 2 -- JLR SDD 169

Decrypted `.exml` module database. Key facts in `captures/jlr/sdd169-extraction.md`.
3DES-ECB, PKCS5, key `YmZwZlQrQ3V4dVltNTArWE9s`.

## Corpus 3 -- PSA DiagBox V09.233

Partially mined; see `captures/peugeot/diagbox-ep6-extraction.md`.

## Progress log

- Worktree created, `tools/gds2/` restored from `cef7121`.
- Header/table semantics cross-checked against the decompiled reader (above).
