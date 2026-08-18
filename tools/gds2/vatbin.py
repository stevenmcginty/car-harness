"""vatbin.bin reader for GDS2 deliverables.

Format is taken directly from the CFR-decompiled GDS2 runtime:
  com/Mahle/b/a/c.java  -- header
  com/Mahle/b/a/f.java  -- DataProviderBinary (items, containers, strings, value objects)
  com/Mahle/b/a/i.java  -- item field layouts T (stride 40) and U (stride 44)

Nothing here is obfuscated: strings are UTF-16LE records, which is why ASCII
greps over the file come up empty.
"""

import mmap
import struct
import sys


class Vatbin:
    # com/Mahle/b/a/i.java
    LAYOUT_T = (0, 4, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40)
    LAYOUT_U = (0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44)

    def __init__(self, path):
        self.path = path
        self.fh = open(path, "rb")
        self.buf = mmap.mmap(self.fh.fileno(), 0, access=mmap.ACCESS_READ)
        self.size = len(self.buf)

        self.flags = self.u32(0)              # header field p
        self.ver_ptr = self.u32(20)           # r
        self.lang_count = self.u32(24)        # x
        self.lang_chain = self.u32(28)        # y
        self.root_item = self.u32(40)         # m
        self.machine_count = self.u32(44)     # n
        self.machine_off = self.u32(48)       # o
        self.datalist_count = self.u32(56)    # v
        self.datalist_off = self.u32(60)      # w
        self.func_count = self.u32(72)        # t
        self.func_off = self.u32(76)          # u
        self.option_off = self.u32(80)        # s

        self.layout = self.LAYOUT_U if self.flags & 1 else self.LAYOUT_T
        self.stride = self.layout[11]
        self.utf16 = bool(self.flags & 4)
        self._str_cache = {}

    # ---- primitives -----------------------------------------------------
    def u32(self, off):
        return struct.unpack_from("<I", self.buf, off)[0]

    def i32(self, off):
        return struct.unpack_from("<i", self.buf, off)[0]

    def u16(self, off):
        return struct.unpack_from("<H", self.buf, off)[0]

    def f64(self, off):
        return struct.unpack_from("<d", self.buf, off)[0]

    def ok(self, off, need=4):
        return 0 < off and off + need <= self.size

    # ---- strings --------------------------------------------------------
    def string_at(self, off):
        """One string record: [u32 lang][u16 flag][chars][NUL]. Returns
        (text, lang, flag, next_offset)."""
        if not self.ok(off, 8):
            return ("", -1, -1, 0)
        lang = self.u32(off)
        flag = self.u16(off + 4)
        pos = off + 6
        if self.utf16:
            end = self.buf.find(b"\x00\x00", pos)
            while end != -1 and (end - pos) % 2:
                end = self.buf.find(b"\x00\x00", end + 1)
            if end == -1:
                return ("", lang, flag, 0)
            text = self.buf[pos:end].decode("utf-16-le", "replace")
            nxt = end + 2
        else:
            end = self.buf.find(b"\x00", pos)
            text = self.buf[pos:end].decode("latin-1", "replace")
            nxt = end + 1
            if len(text) % 2 == 0:
                nxt += 1
        return (text, lang, flag, nxt)

    def key(self, off):
        """d() in f.java -- the symbolic key record, no language walk."""
        if off == 0:
            return " "
        if off in self._str_cache:
            return self._str_cache[off]
        text = self.string_at(off)[0]
        self._str_cache[off] = text
        return text

    def text(self, off, lang=0):
        """c() in f.java -- walk the chain for the wanted language id."""
        if off == 0:
            return " "
        first, g, h, nxt = self.string_at(off)
        if g == lang or h > 0:
            return first
        for _ in range(64):
            s, g, h, nxt = self.string_at(nxt)
            if g == lang:
                return s
            if h > 0 or nxt == 0:
                return first
        return first

    def chain(self, off, limit=40):
        """All records of a display-string chain, for inspection."""
        out = []
        nxt = off
        for _ in range(limit):
            s, g, h, nxt2 = self.string_at(nxt)
            if nxt2 == 0:
                break
            out.append((nxt, g, h, s))
            nxt = nxt2
        return out

    # ---- items ----------------------------------------------------------
    def item(self, off):
        a = self.layout
        it = {
            "off": off,
            "type": self.i32(off + a[0]),
            "name_ptr": self.u32(off + a[1]),
            "f3": self.u32(off + a[3]),
            "triples": [],
            "attrs": [],
            "child_container": self.u32(off + a[10]),
        }
        it["name"] = self.key(it["name_ptr"])
        it["label"] = self.text(it["name_ptr"])

        n = self.u32(off + a[4])
        base = self.u32(off + a[5])
        if n < 100000 and self.ok(base, 12 * n):
            for i in range(n):
                it["triples"].append((self.u32(base + i * 12),
                                      self.u32(base + i * 12 + 4),
                                      self.u32(base + i * 12 + 8)))
        n = self.u32(off + a[8])
        base = self.u32(off + a[9])
        if n < 100000 and self.ok(base, 8 * n):
            for i in range(n):
                it["attrs"].append((self.key(self.u32(base + i * 8)),
                                    self.key(self.u32(base + i * 8 + 4)),
                                    self.u32(base + i * 8 + 4)))
        return it

    def container(self, off):
        if not self.ok(off, 16):
            return None
        c = {
            "off": off,
            "key_ptr": self.u32(off),
            "b_ptr": self.u32(off + 4),
            "count": self.u32(off + 8),
            "items_off": self.u32(off + 12),
        }
        c["key"] = self.key(c["key_ptr"])
        c["label"] = self.text(c["key_ptr"])
        c["b"] = self.text(c["b_ptr"])
        return c

    def children(self, container_off):
        c = self.container(container_off)
        if not c or c["count"] > 100000:
            return []
        return [self.item(c["items_off"] + i * self.stride)
                for i in range(c["count"])]

    # ---- value objects ("ScrStruct") -----------------------------------
    def value(self, off, depth=0, seen=None):
        """f(int) in f.java. Returns a nested python structure."""
        if seen is None:
            seen = set()
        if not self.ok(off, 24) or depth > 24 or off in seen:
            return None
        seen = seen | {off}
        t = self.u32(off)
        d = self.f64(off + 16)
        if t <= 5:
            return self._scalar(t, d)
        if t == 6:
            elem_t = self.u32(off + 8)
            count = self.u32(off + 12)
            if count > 100000:
                return {"list_bad": (elem_t, count, off)}
            out = []
            for i in range(count):
                dv = self.f64(off + 16 + i * 8)
                if elem_t > 5:
                    out.append(self.value(int(dv), depth + 1, seen))
                else:
                    out.append(self._scalar(elem_t, dv))
            return {"list": out, "elem_type": elem_t, "off": off}
        count = self.u32(off + 8)
        if count > 100000:
            return {"struct_bad": (t, count, off)}
        out = []
        for i in range(count):
            ft = self.u32(off + 12 + i * 12)
            dv = self.f64(off + 12 + 4 + i * 12)
            if ft > 5:
                out.append(self.value(int(dv), depth + 1, seen) if dv else None)
            else:
                out.append(self._scalar(ft, dv))
        return {"struct": out, "type": t, "off": off}

    def _scalar(self, t, d):
        if t == 0:
            return ("byte", int(d) & 0xFF)
        if t == 1:
            return ("int", int(d))
        if t == 2:
            return ("short", int(d) & 0xFFFF)
        if t == 3:
            return ("long", int(d))
        if t == 4:
            return ("double", d)
        if t == 5:
            return ("str", self.text(int(d)), int(d))
        return ("?", d)

    # ---- tables ---------------------------------------------------------
    def functions(self):
        """VAT function table: 12-byte rows."""
        out = []
        for i in range(self.func_count):
            o = self.func_off + i * 12
            out.append({"off": o, "a": self.u32(o), "b": self.u32(o + 4),
                        "c": self.u32(o + 8)})
        return out

    def datalists(self):
        out = []
        for i in range(self.datalist_count):
            o = self.datalist_off + i * 12
            b = self.u32(o)
            out.append({"idx": i, "off": o, "name": self.key(b),
                        "b": b, "c": self.u32(o + 4), "d": self.u32(o + 8)})
        return out

    def machines(self):
        out = []
        for i in range(self.machine_count):
            o = self.machine_off + i * 56
            m = {"off": o, "id": self.u32(o), "name": self.text(self.u32(o + 4)),
                 "n8": self.u32(o + 8), "o12": self.u32(o + 12),
                 "attr_count": self.u32(o + 16), "attr_off": self.u32(o + 20),
                 "state_count": self.u32(o + 24), "state_off": self.u32(o + 28),
                 "states": []}
            for j in range(min(m["state_count"], 4096)):
                so = m["state_off"] + 28 * j
                m["states"].append({
                    "off": so, "b": self.u32(so), "c": self.u32(so + 4),
                    "g": self.u32(so + 8), "h": self.u32(so + 12),
                    "i": self.u32(so + 16), "j": self.u32(so + 20),
                    "k": self.u32(so + 24)})
            out.append(m)
        return out


def main():
    v = Vatbin(sys.argv[1])
    print("file      %s" % v.path)
    print("size      0x%X" % v.size)
    print("flags     0x%X  layout=%s stride=%d utf16=%s"
          % (v.flags, "U" if v.flags & 1 else "T", v.stride, v.utf16))
    print("version   %r" % v.text(v.ver_ptr))
    print("root item 0x%X" % v.root_item)
    print("machines  %d @0x%X" % (v.machine_count, v.machine_off))
    print("datalists %d @0x%X" % (v.datalist_count, v.datalist_off))
    print("functions %d @0x%X" % (v.func_count, v.func_off))
    print("options   @0x%X" % v.option_off)
    root = v.item(v.root_item)
    print("root      name=%r label=%r triples=%d attrs=%r child=0x%X"
          % (root["name"], root["label"], len(root["triples"]),
             root["attrs"], root["child_container"]))
    c = v.container(root["child_container"])
    if c:
        print("root container key=%r label=%r count=%d"
              % (c["key"], c["label"], c["count"]))
        for it in v.children(root["child_container"])[:40]:
            print("   0x%X type=%-6d %r | %r child=0x%X"
                  % (it["off"], it["type"], it["name"], it["label"],
                     it["child_container"]))


if __name__ == "__main__":
    main()
