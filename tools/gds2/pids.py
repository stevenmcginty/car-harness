"""List the PIDs of one module data set (a type-17305 object) with their
decoded request parameters.

A type-17297 object is one PID/CPID definition. Its field .1 is a list of
type-17283 "VCIDataItem" records; the runtime (com/Mahle/VCI/a/q.java, method
t() plus the aB[] permutation {0,3,1,4,2,5,6}) maps those eight fields onto
p[0..6] plus the byte-pattern string D:

    field .0 -> p[0]  parameter / CPID number      (the wire id)
    field .1 -> p[2]  flags
    field .2 -> p[4]
    field .3 -> p[1]  data length in bytes
    field .4 -> p[3]  PID type  (0 = ReadDataByParameterIdentifier $22,
                                 3 = device control $AE, 4 = raw, ...)
    field .5 -> p[5]  byte offset of the value inside the payload
    field .6 -> p[6]
    field .7 -> D     comma separated hex byte pattern, "XX" = wildcard

usage: pids.py <vatbin> <17305-offset> [--grep REGEX] [--all]
"""
import re
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def states(v, conv_off):
    """Enumerated state values of a conversion (type 17276 field .6)."""
    out = []
    if not conv_off:
        return out
    lst = int(v.f64(conv_off + 12 + 6 * 12 + 4))
    if not v.ok(lst, 16) or v.u32(lst) != 6:
        return out
    for i in range(v.u32(lst + 12)):
        sev = int(v.f64(lst + 16 + i * 8))
        inner = int(v.f64(sev + 12 + 2 * 12 + 4))
        if not v.ok(inner, 16) or v.u32(inner) != 6:
            continue
        for j in range(v.u32(inner + 12)):
            e = int(v.f64(inner + 16 + j * 8))
            out.append((int(v.f64(e + 12 + 4)), v.text(int(v.f64(e + 12 + 12 + 4)))))
    return out


def conv(v, pid_off, idx):
    lst = int(v.f64(pid_off + 12 + idx * 12 + 4))
    if not v.ok(lst, 16) or v.u32(lst) != 6 or v.u32(lst + 12) == 0:
        return None
    return int(v.f64(lst + 16))


def show_pid(v, off):
    ref = v.text(int(v.f64(off + 12 + 4)))
    name = v.text(int(v.f64(off + 12 + 5 * 12 + 4)))
    key = v.text(int(v.f64(off + 12 + 6 * 12 + 4)))
    lines = ["0x%08X %-18s %-45s key=%s" % (off, ref, name, key)]
    items = int(v.f64(off + 12 + 1 * 12 + 4))
    if v.ok(items, 16) and v.u32(items) == 6:
        for i in range(v.u32(items + 12)):
            it = int(v.f64(items + 16 + i * 8))
            f = [int(v.f64(it + 12 + k * 12 + 4)) for k in range(7)]
            d = v.text(int(v.f64(it + 12 + 7 * 12 + 4)))
            p = [f[0], f[3], f[1], f[4], f[2], f[5], f[6]]
            lines.append("    item[%d] @0x%X  id=%d(0x%X) len=%d type=%d flags=0x%X "
                         "off=%d p6=%d  D=%r"
                         % (i, it, p[0], p[0], p[1], p[3], p[2], p[5], p[6], d))
    for idx, tag in ((2, "read "), (4, "write")):
        c = conv(v, off, idx)
        if c is None:
            continue
        eq = v.text(int(v.f64(c + 12 + 1 * 12 + 4)))
        unit = v.text(int(v.f64(c + 12 + 4 * 12 + 4)))
        hi = v.f64(c + 12 + 2 * 12 + 4)
        lo = v.f64(c + 12 + 3 * 12 + 4)
        st = states(v, c)
        lines.append("    %s conv @0x%X eq=%r unit=%r range=[%g,%g] states=%s"
                     % (tag, c, eq, unit, lo, hi, st))
    return "\n".join(lines)


if __name__ == "__main__":
    v = Vatbin(sys.argv[1])
    off = int(sys.argv[2], 0)
    pat = None
    if "--grep" in sys.argv:
        pat = re.compile(sys.argv[sys.argv.index("--grep") + 1], re.I)
    print("module set 0x%08X ref=%r name=%r"
          % (off, v.text(int(v.f64(off + 12 + 2 * 12 + 4))),
             v.text(int(v.f64(off + 12 + 3 * 12 + 4)))))
    lst = int(v.f64(off + 12 + 4))
    for i in range(v.u32(lst + 12)):
        p = int(v.f64(lst + 16 + i * 8))
        block = show_pid(v, p)
        if pat and not pat.search(block):
            continue
        print(block)
