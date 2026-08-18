"""Scan the whole vatbin for ScrStruct value objects of a given type id and
print a one-line summary of each (string fields resolved).

usage: scan_objects.py <vatbin> <type-id> [max] [--full] [--grep REGEX]
"""
import re
import struct
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def summarize(v, obj, depth=0):
    if obj is None:
        return "None"
    if isinstance(obj, tuple):
        if obj[0] == "str":
            return repr(obj[1])
        return "%s:%s" % (obj[0], obj[1])
    if "struct" in obj:
        if depth > 1:
            return "<struct %d n=%d @0x%X>" % (obj["type"], len(obj["struct"]), obj["off"])
        return "{%d| %s}" % (obj["type"],
                             ", ".join(summarize(v, f, depth + 1) for f in obj["struct"]))
    if "list" in obj:
        if depth > 1:
            return "<list %d n=%d>" % (obj["elem_type"], len(obj["list"]))
        return "[%d| %s]" % (obj["elem_type"],
                             ", ".join(summarize(v, f, depth + 1) for f in obj["list"]))
    return str(obj)


def main():
    v = Vatbin(sys.argv[1])
    tid = int(sys.argv[2], 0)
    limit = int(sys.argv[3]) if len(sys.argv) > 3 and not sys.argv[3].startswith("-") else 100000
    pat = None
    if "--grep" in sys.argv:
        pat = re.compile(sys.argv[sys.argv.index("--grep") + 1], re.I)

    pat_bytes = struct.pack("<I", tid)
    pos = 0
    found = 0
    while found < limit:
        i = v.buf.find(pat_bytes, pos)
        if i < 0:
            break
        pos = i + 1
        if i % 4:
            continue
        cnt = v.u32(i + 8)
        if cnt == 0 or cnt > 64:
            continue
        if not v.ok(i + 12 + cnt * 12):
            continue
        try:
            obj = v.value(i)
        except Exception:
            continue
        if obj is None or "struct" not in obj:
            continue
        line = "0x%08X %s" % (i, summarize(v, obj))
        if pat and not pat.search(line):
            continue
        found += 1
        print(line)
        sys.stdout.flush()
    print("# %d objects of type %d" % (found, tid))


if __name__ == "__main__":
    main()
