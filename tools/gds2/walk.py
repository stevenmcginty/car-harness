"""Interactive-ish tree walker over a vatbin item tree."""
import sys
sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from vatbin import Vatbin


def show(v, off, indent=0):
    it = v.item(off)
    print("%s0x%X type=%d name=%r label=%r" % ("  " * indent, off, it["type"],
                                               it["name"], it["label"]))
    for k, val, ptr in it["attrs"]:
        print("%s  attr %s = %r" % ("  " * indent, k, val))
    for t in it["triples"]:
        print("%s  triple (%d, %d, 0x%X)" % ("  " * indent, t[0], t[1], t[2]))
    print("%s  child container 0x%X" % ("  " * indent, it["child_container"]))
    c = v.container(it["child_container"])
    if c:
        print("%s  container key=%r label=%r b=%r count=%d"
              % ("  " * indent, c["key"], c["label"], c["b"], c["count"]))
        for ch in v.children(it["child_container"]):
            print("%s    0x%X type=%-8d %r | %r  child=0x%X attrs=%s"
                  % ("  " * indent, ch["off"], ch["type"], ch["name"],
                     ch["label"], ch["child_container"],
                     [(a[0], a[1]) for a in ch["attrs"]]))


if __name__ == "__main__":
    v = Vatbin(sys.argv[1])
    off = int(sys.argv[2], 0) if len(sys.argv) > 2 else v.root_item
    show(v, off)
