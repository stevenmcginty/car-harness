"""Given a file offset that lies inside a ScrStruct value object's entry array,
find the object header and print the decoded object.

usage: owner.py <vatbin> <hex-offset-of-double-slot> [depth]
"""
import pprint
import sys

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def find_owner(v, slot_off, max_index=4096):
    """slot_off points at the 8-byte double of a struct entry (entry+4)."""
    entry = slot_off - 4
    out = []
    for i in range(max_index):
        base = entry - 12 - i * 12
        if base < 0:
            break
        t = v.u32(base)
        if t <= 6:
            continue
        cnt = v.u32(base + 8)
        if cnt == i + 1 or (cnt > i and cnt < 4096):
            out.append((base, t, cnt, i))
    return out


def find_list_owner(v, slot_off, max_index=4096):
    """list objects hold doubles at base+16+i*8."""
    out = []
    for i in range(max_index):
        base = slot_off - 16 - i * 8
        if base < 0:
            break
        if v.u32(base) != 6:
            continue
        cnt = v.u32(base + 12)
        if cnt > i and cnt < 100000:
            out.append((base, 6, cnt, i))
    return out


if __name__ == "__main__":
    v = Vatbin(sys.argv[1])
    off = int(sys.argv[2], 0)
    depth = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    cands = find_owner(v, off, 512)
    lcands = find_list_owner(v, off, 512)
    print("struct-entry owners for 0x%X:" % off)
    for base, t, cnt, i in cands[:8]:
        print("  base=0x%X type=%d count=%d index=%d" % (base, t, cnt, i))
    print("list owners for 0x%X:" % off)
    for base, t, cnt, i in lcands[:8]:
        print("  base=0x%X type=%d count=%d index=%d" % (base, t, cnt, i))
    if cands:
        base = cands[0][0]
        print("--- decode 0x%X ---" % base)
        pprint.pprint(v.value(base), width=160, depth=depth)
