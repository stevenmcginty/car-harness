"""Breadth-first search of the vatbin item tree for items whose key or label
matches a regex. Prints the menu path to each hit.

usage: find_item.py <vatbin> <regex> [max_hits] [max_nodes]
"""
import re
import sys
from collections import deque

sys.path.insert(0, __file__.replace("\\", "/").rsplit("/", 1)[0])
from vatbin import Vatbin


def main():
    v = Vatbin(sys.argv[1])
    pat = re.compile(sys.argv[2], re.I)
    max_hits = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    max_nodes = int(sys.argv[4]) if len(sys.argv) > 4 else 400000

    root = v.item(v.root_item)
    q = deque([(root["child_container"], ())])
    seen = set()
    nodes = 0
    hits = 0
    while q and nodes < max_nodes and hits < max_hits:
        cont, path = q.popleft()
        if not cont or cont in seen:
            continue
        seen.add(cont)
        for ch in v.children(cont):
            nodes += 1
            label = ch["label"]
            name = ch["name"]
            npath = path + ("%s@0x%X" % (label, ch["off"]),)
            if pat.search(name) or pat.search(label):
                hits += 1
                print("HIT 0x%X type=%d key=%r label=%r" % (ch["off"], ch["type"], name, label))
                print("    path: " + " > ".join(npath))
                sys.stdout.flush()
                if hits >= max_hits:
                    break
            if ch["child_container"]:
                q.append((ch["child_container"], npath))
    print("scanned %d nodes, %d containers, %d hits" % (nodes, len(seen), hits))


if __name__ == "__main__":
    main()
