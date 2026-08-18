# GDS2 `vatbin.bin` reader and decoders

Static-analysis tools for the Vauxhall/Opel (and GM Global) GDS2 dealer-tool data file
`BinaryFile/vatbin.bin`, which ships inside every GDS2 "deliverable" package. The file is
not obfuscated — its strings are UTF-16LE, which is why plain-ASCII greps find nothing.

These scripts were written to recover the on-the-wire request bytes of the Astra-K T76
TCM "Transmission Adaptive Values Learn" routine. They are general, though: the same
walk gets you any GDS2 control function, PID or DTC list for any covered vehicle.

Nothing here talks to a car. The scripts are read-only over a local file.

## Requirements

Python 3 only, no dependencies. On Windows set `PYTHONIOENCODING=utf-8` before running —
the data is full of accented text and the console defaults to cp1252.

## The scripts

| script | what it does |
|---|---|
| `vatbin.py` | the reader class: header, string chains, menu items/containers, value objects, machines, data lists, function table. Run it directly for a header summary. |
| `walk.py <bin> <off>` | dump one menu item and its children |
| `dump_tree.py <bin>` | dump the reachable menu tree |
| `find_item.py <bin> <regex>` | breadth-first search of the menu tree by name, prints the menu path |
| `dump_obj.py <bin> <off> [depth]` | recursive pretty-printer for one value object, one line per node with its file offset |
| `scan_objects.py <bin> <type-id> [max] [--grep RE]` | scan the whole file for value objects of one type |
| `refs.py <bin> <target> [--owner]` | find every slot pointing at an object or string record (pointers are stored as IEEE-754 doubles) |
| `owner.py <bin> <off>` | given an offset inside an object's slots, find the object header |
| `parent.py <bin> <off> [levels]` | walk *up* the value-object graph |
| `context.py <bin> <test-off>` | given a test definition, resolve the module menu node that owns it, the module PID set it binds, and the module's CAN request id |
| `pids.py <bin> <pidset-off> [--grep RE]` | list a module's PIDs/CPIDs with decoded ids, lengths, types, byte patterns, conversions and enumerated states |
| `osc.py <bin> <oscsetup-off> [<pidset-off> ...]` | decode an Output State Control setup block all the way to literal request bytes |
| `machine.py <bin> [regex] [--states] [--data]` | dump a GDS2 state machine with VAT function names, transitions and attached data |

## Worked example

Recovering the Astra-K MY2017 transmission adaptive-values-learn command:

```sh
export PYTHONIOENCODING=utf-8
BIN=.../deliverable/BinaryFile/vatbin.bin

# 1. find the vehicle in the menu tree
python find_item.py "$BIN" "Astra-K"
python walk.py "$BIN" 0x5969B98      # 2017 Astra-K -> D2XX -> vehicle menu data
                                     # -> Module Diagnostics -> Transmission Control Module
                                     # -> "TCM - T76 - (Conti) - GME"

# 2. from the test definition, resolve its module context
python context.py "$BIN" 0x068A2AD4
#   -> module menu item 'Transmission Control Module$TCM - T76 - (Conti) - GME'
#      state 2 CONN reqid=2018(0x7E2) bus='GMLAN_HS_PRIMARY_500k_Pin6&14'
#      state 3 PIDSET 0x5AC7CAC 'Transmission Control Module-TCM-T76-Delphi Ref'

# 3. decode the control block to bytes
python osc.py "$BIN" 0x6912D28 0x5AC7CAC
#   -> AE 30 00 00 00 04 00     (start)
#   -> AE 00 00 00 00 00 00 ; 500 ms ; 20   (return to normal)

# 4. sanity-check the decode rule against a PID with known-good live data
python pids.py "$BIN" 0x5AC7CAC --grep "Fluid Temperature"
#   -> id=6464 (0x1940) type=0  ->  22 19 40, (X(0)*1)-40 degC
```

## Format notes worth keeping in mind

- Pointers inside **value objects** are stored as IEEE-754 doubles at unaligned offsets.
  Pointers in the **menu item** layer are plain u32. That is why a 32-bit scan for an
  object offset silently finds nothing.
- A **test definition** (object type 17483) does not contain a program. It contains
  `(objectId, state number, data object)` bindings, and the state numbers index into a
  shared state machine chosen by the test's type string — `Test Output Control` for
  "Output Control" tests, `Test Sequence Iterator` for "Sequence Iterator" tests. Use
  `machine.py` to see what each bound state actually does.
- Request bytes are assembled at run time from a per-protocol template table
  (`com/Mahle/VCI/c/n.java`'s `sB_` for GMLAN) plus the PID's own id, length, offset and
  byte-pattern fields. There are no service constants stored anywhere in the data file,
  which is why earlier grep-based attempts found nothing.
- GM Global A / GMW3110, not UDS: expect `$AE` RequestDeviceControl with a CPID, `$22`
  ReadDataByParameterIdentifier, `$20` ReturnToNormalMode, `$3E` TesterPresent, `$27`
  SecurityAccess, `$2C`/`$AA` for dynamic packet streaming.

The full decode — container format, object-type dictionary, and the recovered exchange —
is written up in the local-only notes under `captures/vauxhall/`.
