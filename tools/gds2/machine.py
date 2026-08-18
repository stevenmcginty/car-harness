"""Dump a vatbin state machine ("chip program") with VAT function names.

Machine records live at header field o (0x30), stride 56; each has a state
array (stride 28) read by com/Mahle/b/a/f.java::j():

    +0  b  state number
    +4  c  VAT function id   (matches the id column of the function table)
    +8  g  default next state
    +12 h  (name/label pointer)
    +16 i  transition count
    +20 j  transitions, stride 12
    +24 k  attached data value-object

Field names come from com/Mahle/c/k.java's debug tree text:
"State Number: b; Function offset: c; Function Name: e; Default State: g".

The function table is header field u (0x4C) with count t (0x48), stride 12:
+0 id, +4 DLL-name pointer, +8 function-name pointer.

usage: machine.py <vatbin> [name-regex] [--states] [--data]
"""
import re
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def functable(v):
    out = {}
    for i in range(v.func_count):
        o = v.func_off + i * 12
        out[v.u32(o)] = (v.key(v.u32(o + 4)), v.key(v.u32(o + 8)))
    return out


def main():
    v = Vatbin(sys.argv[1])
    pat = re.compile(sys.argv[2], re.I) if len(sys.argv) > 2 else None
    ft = functable(v)
    for m in v.machines():
        if pat and not pat.search(m["name"]):
            continue
        print("machine 0x%X id=%d %r states=%d @0x%X"
              % (m["off"], m["id"], m["name"], m["state_count"], m["state_off"]))
        if "--states" not in sys.argv:
            continue
        for s in m["states"]:
            fn = ft.get(s["c"], ("?", "?"))
            data = ""
            if s["k"] and "--data" in sys.argv:
                t = v.u32(s["k"]) if v.ok(s["k"], 8) else -1
                data = " data=0x%X(type %d)" % (s["k"], t)
                if t > 6 and v.u32(s["k"] + 8) < 32:
                    bits = []
                    for i in range(v.u32(s["k"] + 8)):
                        eo = s["k"] + 12 + i * 12
                        ftp = v.u32(eo)
                        dv = v.f64(eo + 4)
                        bits.append(repr(v.text(int(dv)))[:40] if ftp == 5
                                    else ("<%d>" % ftp if ftp > 5 else str(int(dv))))
                    data += " [" + ", ".join(bits) + "]"
            trans = []
            for i in range(min(s["i"], 32)):
                to = s["j"] + 12 * i
                trans.append((v.u32(to), v.u32(to + 4), v.u32(to + 8)))
            print("  state %-4d fn=%-3d %-28s next=%-4d trans=%s%s"
                  % (s["b"], s["c"], fn[1], s["g"],
                     ",".join("%d->%d" % (a, b) for a, b, _ in trans), data))


if __name__ == "__main__":
    main()
