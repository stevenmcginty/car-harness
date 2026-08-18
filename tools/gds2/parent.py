"""Walk UP the vatbin value-object graph.

Given an object offset, find the slot(s) that point at it, work out which
object owns each slot, and repeat -- so you can find out which module data
list / vehicle context a PID or test definition belongs to.

usage: parent.py <vatbin> <hex-offset> [levels]
"""
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def slots_pointing_at(v, target):
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


def owner(v, slot):
    """Return (base, type, count, index, kind) for the object owning this slot.

    A struct entry lives at base+12+i*12+4; a list element at base+16+i*8.
    We prefer the *nearest* plausible header, which in practice is unambiguous
    because element/entry counts have to bracket the index.
    """
    best = None
    for i in range(0, 512):
        base = slot - 16 - i * 12
        if base < 0:
            break
        t = v.u32(base)
        if t > 6:
            c = v.u32(base + 8)
            if 0 < c < 4096 and c > i:
                best = (base, t, c, i, "struct")
                break
    for i in range(0, 65536):
        base = slot - 16 - i * 8
        if base < 0:
            break
        if v.u32(base) == 6:
            c = v.u32(base + 12)
            if 0 < c < 1000000 and c > i:
                cand = (base, 6, c, i, "list")
                if best is None or cand[0] > best[0]:
                    best = cand
                break
    return best


def label(v, base, t):
    """One short line describing an object, string fields resolved."""
    if t == 6:
        return "LIST elem=%d n=%d" % (v.u32(base + 8), v.u32(base + 12))
    n = v.u32(base + 8)
    bits = []
    for i in range(min(n, 8)):
        eo = base + 12 + i * 12
        ft = v.u32(eo)
        dv = v.f64(eo + 4)
        if ft == 5:
            bits.append(repr(v.text(int(dv)))[:70])
        elif ft <= 5:
            bits.append(str(int(dv)))
        else:
            bits.append("<%d>" % ft)
    return "STRUCT type=%d n=%d [%s]" % (t, n, ", ".join(bits))


if __name__ == "__main__":
    v = Vatbin(sys.argv[1])
    cur = int(sys.argv[2], 0)
    levels = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    for lvl in range(levels):
        print("0x%08X  %s" % (cur, label(v, cur, v.u32(cur))))
        slots = slots_pointing_at(v, cur)
        if not slots:
            print("  (no referrer)")
            break
        ups = []
        for s in slots:
            o = owner(v, s)
            if o:
                ups.append(o)
        if not ups:
            print("  (referrer slots %s but no owner found)" % [hex(s) for s in slots])
            break
        if len(ups) > 1:
            for o in ups:
                print("  ^ 0x%08X %s idx=%d" % (o[0], label(v, o[0], o[1])[:110], o[3]))
        cur = ups[0][0]
