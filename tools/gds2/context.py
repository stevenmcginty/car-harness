"""Given a test definition (type 17483), work out which module menu node owns
it and which module PID set that node binds.

The chain is:
    17483 test  <- list <- 17489 group <- list <- 17533 folder <- list(17533)
    that final list offset appears as the *value* of an item triple
    (machine-record, state-id 13, value) belonging to a "Module Menu" item;
    the same item's state-id 3 triple points at a list of type-17305 module
    PID sets, and its state-id 2 triple at the 17515/17466 connection record
    (which carries the CAN request id and bus name).

Triples are plain u32 (not doubles), so this searches for u32 references.

usage: context.py <vatbin> <17483-offset>
"""
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def u32refs(v, target):
    pat = struct.pack("<I", target)
    out = []
    pos = 0
    while True:
        i = v.buf.find(pat, pos)
        if i < 0:
            break
        if i % 4 == 0:
            out.append(i)
        pos = i + 1
    return out


def f64refs(v, target):
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


def list_owner(v, slot):
    for i in range(0, 65536):
        base = slot - 16 - i * 8
        if base < 0:
            return None
        if v.u32(base) == 6:
            c = v.u32(base + 12)
            if 0 < c < 1000000 and c > i:
                return base
            return None
    return None


def struct_owner(v, slot):
    for i in range(0, 512):
        base = slot - 16 - i * 12
        if base < 0:
            return None
        t = v.u32(base)
        if t > 6:
            c = v.u32(base + 8)
            if 0 < c < 4096 and c > i:
                return base
    return None


def up(v, off):
    """One step up the value-object graph. Both a struct header and a list
    header can plausibly precede a slot, so take whichever is nearer."""
    for s in f64refs(v, off):
        cands = [c for c in (list_owner(v, s), struct_owner(v, s)) if c]
        if cands:
            return max(cands)
    return None


def main():
    v = Vatbin(sys.argv[1])
    cur = int(sys.argv[2], 0)
    chain = [cur]
    for _ in range(6):
        n = up(v, cur)
        if n is None:
            break
        chain.append(n)
        cur = n
    top = chain[-1]
    print("value-object chain: %s" % " <- ".join("0x%X" % c for c in chain))

    # the top list appears as a triple value (u32)
    items = set()
    for s in u32refs(v, top):
        # triple = [machine u32][state id u32][value u32]; value is at s
        tstart = s - 8
        # find the item whose triple array base is tstart - 12*k
        for k in range(0, 64):
            base = tstart - 12 * k
            for r in u32refs(v, base):
                it = v.item(r - 20)
                if it["type"] > 0 and it["name"]:
                    items.add((it["off"], it["name"]))
            if items:
                break
        if items:
            break
    for off, name in sorted(items):
        it = v.item(off)
        print("module menu item 0x%X %r" % (off, name))
        for m, sid, val in it["triples"]:
            t = v.u32(val) if v.ok(val, 8) else -1
            extra = ""
            if t == 6 and v.u32(val + 8) == 17305:
                for i in range(v.u32(val + 12)):
                    mset = int(v.f64(val + 16 + i * 8))
                    extra += " PIDSET 0x%X %r" % (mset,
                                                  v.text(int(v.f64(mset + 12 + 2 * 12 + 4))))
            if t == 17515:
                lst = int(v.f64(val + 12 + 12 + 4))
                if v.ok(lst, 16) and v.u32(lst) == 6 and v.u32(lst + 12):
                    c = int(v.f64(lst + 16))
                    extra = (" CONN module=%r proto=%d reqid=%d(0x%X) bus=%r pidset=%r"
                             % (v.text(int(v.f64(c + 12 + 4))),
                                int(v.f64(c + 12 + 12 + 4)),
                                int(v.f64(c + 12 + 24 + 4)),
                                int(v.f64(c + 12 + 24 + 4)),
                                v.text(int(v.f64(c + 12 + 36 + 4))),
                                v.text(int(v.f64(c + 12 + 7 * 12 + 4)))))
            print("   triple state=%-3d -> 0x%X type=%d%s" % (sid, val, t, extra))
        break


if __name__ == "__main__":
    main()
