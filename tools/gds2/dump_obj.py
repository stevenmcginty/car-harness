"""Recursively pretty-print a vatbin ScrStruct value object.

usage: dump_obj.py <vatbin> <hex-offset> [max-depth]

Unlike scan_objects.py's one-line summariser this walks the whole tree and
prints one line per node with its file offset, so every byte reported can be
traced back to an offset in the file.
"""
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def show(v, off, depth=0, maxdepth=12, seen=None, label=""):
    pad = "  " * depth
    if seen is None:
        seen = set()
    if not v.ok(off, 24):
        print("%s%s<bad off 0x%X>" % (pad, label, off))
        return
    if off in seen:
        print("%s%s<repeat 0x%X>" % (pad, label, off))
        return
    seen.add(off)
    t = v.u32(off)
    if t <= 5:
        print("%s%s%s @0x%X" % (pad, label, v._scalar(t, v.f64(off + 16)), off))
        return
    if t == 6:
        et = v.u32(off + 8)
        n = v.u32(off + 12)
        print("%s%sLIST elem=%d n=%d @0x%X" % (pad, label, et, n, off))
        if n > 4096:
            return
        for i in range(n):
            dv = v.f64(off + 16 + i * 8)
            if et > 5:
                if depth + 1 > maxdepth:
                    print("%s  [%d] -> 0x%X (cut)" % (pad, i, int(dv)))
                else:
                    show(v, int(dv), depth + 1, maxdepth, seen, "[%d] " % i)
            else:
                print("%s  [%d] %s" % (pad, i, v._scalar(et, dv)))
        return
    n = v.u32(off + 8)
    print("%s%sSTRUCT type=%d n=%d @0x%X" % (pad, label, t, n, off))
    if n > 4096:
        return
    for i in range(n):
        eo = off + 12 + i * 12
        ft = v.u32(eo)
        dv = v.f64(eo + 4)
        if ft > 5:
            if not dv:
                print("%s  .%d (t%d) NULL" % (pad, i, ft))
            elif depth + 1 > maxdepth:
                print("%s  .%d (t%d) -> 0x%X (cut)" % (pad, i, ft, int(dv)))
            else:
                show(v, int(dv), depth + 1, maxdepth, seen, ".%d " % i)
        else:
            print("%s  .%d %s   [slot @0x%X]" % (pad, i, v._scalar(ft, dv), eo + 4))


if __name__ == "__main__":
    v = Vatbin(sys.argv[1])
    off = int(sys.argv[2], 0)
    md = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    show(v, off, 0, md)
