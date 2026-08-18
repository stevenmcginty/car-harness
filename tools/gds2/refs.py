"""Find every 8-byte IEEE-754 double in the vatbin whose value equals a given
integer, i.e. every value-object slot that points at that offset.

Pointers in ScrStruct value objects are stored as doubles (getDouble + d2i in
com/Mahle/b/a/f.java), so this is how you find who references an object or a
string record.

usage: refs.py <vatbin> <hex-or-dec-target> [--owner]
"""
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def find_refs(v, target):
    pat = struct.pack("<d", float(target))
    out = []
    pos = 0
    while True:
        i = v.buf.find(pat, pos)
        if i < 0:
            break
        out.append(i)
        pos = i + 1
    return out


def owner_of(v, slot):
    """slot = offset of the 8-byte double. Try struct-entry then list-elem."""
    # struct entry: base+12+i*12+4 == slot  ->  (slot-16) % 12 == 0 relative base
    for i in range(0, 4096):
        base = slot - 4 - 12 - i * 12
        if base < 0:
            break
        t = v.u32(base)
        if t > 6 and v.u32(base + 8) == i + 1 or (t > 6 and 0 < v.u32(base + 8) < 4096 and v.u32(base + 8) > i):
            return (base, t, v.u32(base + 8), i, "struct")
    for i in range(0, 65536):
        base = slot - 16 - i * 8
        if base < 0:
            break
        if v.u32(base) == 6 and 0 < v.u32(base + 12) < 100000 and v.u32(base + 12) > i:
            return (base, 6, v.u32(base + 12), i, "list")
    return None


if __name__ == "__main__":
    v = Vatbin(sys.argv[1])
    tgt = int(sys.argv[2], 0)
    hits = find_refs(v, tgt)
    print("# %d slots hold 0x%X (%d)" % (len(hits), tgt, tgt))
    for h in hits:
        if "--owner" in sys.argv:
            o = owner_of(v, h)
            print("  slot 0x%08X  owner=%s" % (h, ("0x%08X type=%d n=%d idx=%d %s" % o) if o else "?"))
        else:
            print("  slot 0x%08X" % h)
