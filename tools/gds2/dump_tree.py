"""Dump the reachable vatbin item tree (container children + triple-borne
child items) with paths.

usage: dump_tree.py <vatbin> [max_nodes]
"""
import sys
from collections import deque

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def main():
    v = Vatbin(sys.argv[1])
    max_nodes = int(sys.argv[2]) if len(sys.argv) > 2 else 2000000

    root = v.item(v.root_item)
    q = deque([(root["child_container"], 0)])
    seen_cont = set()
    seen_item = set()
    nodes = 0
    while q and nodes < max_nodes:
        cont, depth = q.popleft()
        if not cont or cont in seen_cont:
            continue
        seen_cont.add(cont)
        c = v.container(cont)
        if not c:
            continue
        print("%sCONT 0x%X key=%r label=%r count=%d"
              % ("  " * depth, cont, c["key"], c["label"], c["count"]))
        for ch in v.children(cont):
            if ch["off"] in seen_item:
                continue
            seen_item.add(ch["off"])
            nodes += 1
            print("%s  ITEM 0x%X type=%d key=%r label=%r"
                  % ("  " * depth, ch["off"], ch["type"], ch["name"], ch["label"]))
            for k, val, ptr in ch["attrs"]:
                print("%s       attr %s=%r" % ("  " * depth, k, val))
            for t in ch["triples"]:
                if t[0] == 0 and t[1] == 0:
                    sub = v.item(t[2])
                    print("%s       SUBITEM 0x%X type=%d key=%r label=%r child=0x%X"
                          % ("  " * depth, t[2], sub["type"], sub["name"],
                             sub["label"], sub["child_container"]))
                    if sub["child_container"]:
                        q.append((sub["child_container"], depth + 2))
                else:
                    print("%s       trip m=0x%X s=%d v=0x%X" % ("  " * depth, t[0], t[1], t[2]))
            if ch["child_container"]:
                q.append((ch["child_container"], depth + 1))
    print("nodes=%d containers=%d" % (nodes, len(seen_cont)))


if __name__ == "__main__":
    main()
