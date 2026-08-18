"""Decode an InstInitOSCSetup block (type 17014) into the request bytes it
will put on the wire, and resolve every CPID against a module PID set.

Field mapping comes from the decompiled runtime, not from guesswork:

  17014  = the data of VAT function InstInitOSCSetup
           (com/Mahle/VATFunctions/VCI/g/d.java: .1 = list of setups)
  17000  = one OSC setup (g/s.java)
           .0 B flags   .1 index into am[]/an[] -> control type C
           .2 F reference name (what InstPerformOSCControlByName looks up)
           .3 G          .4 list of commands
  16982  = one command (g/n.java)
           .0 H flags   .1 J   .2 O value equation   .3 P post-delay ms
           .8 list -> the CPID data node (16924)   .9 monitor PID
           .13 bs = name of the UI control parameter when H & 2

  16924  = a data-node reference: .1 PID ref name, .2 module name, .3 label

The wire message is then built by com/Mahle/VCI/c/ad.java from the PID's own
VCIDataItem (see pids.py) using the GMLAN request templates in
com/Mahle/VCI/c/n.java (sB_):

  PID type 3 -> template sB_[26] = AE 00 :
      k[0]=0xAE, k[1..cd]=p[0] (cd=1), then the D bytes appended,
      then the encoded value OR'ed in at k[1+cd+p[5]]
  PID type 4 -> template sB_[25] = 00 :
      k[0]=p[0], then the D bytes appended

usage: osc.py <vatbin> <17014-offset> [<17305 module-set offset> ...]
"""
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin

CD = 1  # com/Mahle/VCI/c/ad.java: protected int cd = 1 (GM/GMLAN never overrides it)


def sfield(v, off, i):
    return v.text(int(v.f64(off + 12 + i * 12 + 4)))


def ifield(v, off, i):
    return int(v.f64(off + 12 + i * 12 + 4))


def lst(v, off, i):
    o = int(v.f64(off + 12 + i * 12 + 4))
    if not v.ok(o, 16) or v.u32(o) != 6:
        return []
    return [int(v.f64(o + 16 + k * 8)) for k in range(v.u32(o + 12))]


def index_module(v, mod_off):
    """ref name -> 17297 offset, for one type-17305 module PID set."""
    out = {}
    l = int(v.f64(mod_off + 12 + 4))
    for i in range(v.u32(l + 12)):
        p = int(v.f64(l + 16 + i * 8))
        out[sfield(v, p, 0)] = p
    return out


def pid_wire(v, pid_off, value=None):
    """Return (description, message bytes) for one PID definition."""
    items = lst(v, pid_off, 1)
    if not items:
        return ("no VCIDataItem", None)
    it = items[0]
    f = [int(v.f64(it + 12 + k * 12 + 4)) for k in range(7)]
    d = v.text(int(v.f64(it + 12 + 7 * 12 + 4)))
    p0, p2, p4, p1, p3, p5, p6 = f[0], f[1], f[2], f[3], f[4], f[5], f[6]
    # D -> K[]
    K = []
    if d and d[0] not in "_*" and d != "0":
        for tok in d.split(","):
            try:
                K.append(int(tok, 16))
            except ValueError:
                K.append(0)
    if p3 == 3:
        msg = [0xAE] + [(p0 >> (8 * (CD - 1 - i)) & 0xFF) for i in range(CD)] + K
        if value is not None:
            idx = 1 + CD + p5
            if idx < len(msg):
                msg[idx] |= value & 0xFF
        return ("type3 device-control CPID 0x%02X off=%d" % (p0, p5), msg)
    if p3 == 4:
        return ("type4 raw service 0x%02X" % p0, [p0 & 0xFF] + K)
    if p3 == 0:
        w = 2 if p0 > 0xFF else 1
        return ("type0 read PID 0x%X len=%d" % (p0, p1),
                [0x22] + [(p0 >> (8 * (w - 1 - i)) & 0xFF) for i in range(w)] + [0x00])
    return ("type%d id=0x%X (message build not modelled)" % (p3, p0), None)


def states(v, pid_off):
    """write-conversion enumerated states, (value, text)."""
    convs = lst(v, pid_off, 4)
    out = []
    for c in convs:
        for sev in lst(v, c, 6):
            for e in lst(v, sev, 2):
                out.append((int(v.f64(e + 12 + 4)), sfield(v, e, 1)))
    return out


def main():
    v = Vatbin(sys.argv[1])
    off = int(sys.argv[2], 0)
    mods = {}
    for m in sys.argv[3:]:
        mods.update(index_module(v, int(m, 0)))
    for s in lst(v, off, 1):
        print("SETUP @0x%08X  B=%d ctrlIdx=%d  ref=%r  G=%r"
              % (s, ifield(v, s, 0), ifield(v, s, 1), sfield(v, s, 2), sfield(v, s, 3)))
        for c in lst(v, s, 4):
            H = ifield(v, c, 0)
            J = ifield(v, c, 1)
            O = sfield(v, c, 2)
            P = sfield(v, c, 3)
            bs = sfield(v, c, 13)
            nodes = lst(v, c, 8)
            mon = lst(v, c, 9)
            print("  CMD @0x%08X H=0x%X J=%d value=%r delay=%rms uiParam=%r"
                  % (c, H, J, O, P, bs))
            for nd in nodes:
                ref = sfield(v, nd, 1)
                print("    CPID ref=%-16s module=%-28r label=%r"
                      % (ref, sfield(v, nd, 2), sfield(v, nd, 3)))
                pid = mods.get(ref)
                if pid is None:
                    print("      (ref not in the supplied module set)")
                    continue
                st = states(v, pid)
                vals = [None]
                if H & 2:
                    vals = [x[0] for x in st] or [None]
                elif O.isdigit():
                    vals = [int(O)]
                for val in vals:
                    desc, msg = pid_wire(v, pid, val)
                    lbl = ""
                    for sv, stx in st:
                        if sv == val:
                            lbl = " (%s)" % stx
                    print("      pid 0x%08X %s  value=%s%s -> %s"
                          % (pid, desc, val, lbl,
                             " ".join("%02X" % b for b in msg) if msg else "?"))
                if st:
                    print("      states: %s" % st)
            for nd in mon:
                print("    MONITOR ref=%s label=%r" % (sfield(v, nd, 1), sfield(v, nd, 3)))


if __name__ == "__main__":
    main()
