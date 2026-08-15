"""Offline analysis of EDC17 flash dumps -- diff, map scan, hexdump, demo.

The stage-4 half of the X260 AdBlue job (branch jlr-x260-adblue): the car side
(read-only diagnostics) lives in backend/profiles/jlr/, this file works on the
FILE that comes off the ECU -- a Ktag boot-mode read of the Bosch EDC17CP55 on
the 306DT 3.0d. None of it talks to a car; it is pure binary forensics on a
dump you own.

What a "delete file" actually is: the SAME dump with a handful of bytes
changed -- dosing-quantity tables zeroed, monitor/flag bytes flipped,
checksums recomputed. That means one paid tuner file plus this diff answers
"where does the AdBlue logic live" precisely and honestly -- every changed
region is printed, nothing is guessed.

Commands (stdlib only, no car contact):
    python dump_diff.py demo                     walkthrough on synthetic data
    python dump_diff.py diff ORIGINAL MODIFIED   changed regions, classified
    python dump_diff.py scan DUMP                map-shaped structures
    python dump_diff.py hexdump DUMP --at 0x... --len N

Honest limits, stated up front:
- scan() uses shape heuristics (monotonic runs, physical-range u16 values),
  not a damos -- a hit is a CANDIDATE for a table, never a named map. Proof
  arrives from a diff against a known-good modified file, or from how a value
  moves against the car.
- diff() does NOT recompute EDC17 checksums. Region checksums live in the
  dump and must verify before anything is written back; that correction is
  done against a known-good reference file first, never invented here.
"""

import argparse
import random
import sys

# u16 big-endian is the EDC17 table word for most quantity/axis maps.
_U16 = "u16-be"


def _load(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# diff: what changed between two dumps
# --------------------------------------------------------------------------

def diff_regions(a: bytes, b: bytes, gap: int = 8) -> list:
    """Group differing bytes into regions. Bytes closer than ``gap`` belong to
    the same region -- scattered single bytes read as flag flips, long
    contiguous runs read as tables."""
    if len(a) != len(b):
        print(f"WARNING: sizes differ ({len(a)} vs {len(b)} B) -- comparing the "
              f"common prefix. Two reads of one ECU always match; a size "
              "mismatch means these are not the same read.")
    n = min(len(a), len(b))
    regions = []
    cur = None
    for i in range(n):
        if a[i] != b[i]:
            if cur and i - cur["end"] <= gap:
                cur["end"] = i
            else:
                cur = {"start": i, "end": i}
                regions.append(cur)
        # fallthrough: equal byte closes nothing; regions close on distance
    for r in regions:
        r["bytes"] = [(i, a[i], b[i]) for i in range(r["start"], r["end"] + 1)
                      if a[i] != b[i]]
        r["width"] = r["end"] - r["start"] + 1
        r["kind"] = ("flag/value flip" if len(r["bytes"]) <= 4 and r["width"] <= 8
                     else "table/curve change" if r["width"] >= 8
                     else "small change")
    return regions


def print_diff(a: bytes, b: bytes) -> None:
    regions = diff_regions(a, b)
    changed = sum(len(r["bytes"]) for r in regions)
    print(f"{len(a)} B vs {len(b)} B -- {changed} byte(s) differ in "
          f"{len(regions)} region(s)\n")
    if not regions:
        print("Identical (in the compared range).")
        return
    for n, r in enumerate(regions, 1):
        print(f"region {n}  @0x{r['start']:06X}-0x{r['end']:06X} "
              f"({r['width']} B wide, {len(r['bytes'])} changed) -- {r['kind']}")
        for off, old, new in r["bytes"][:16]:
            print(f"    0x{off:06X}: {old:02X} -> {new:02X}")
        if len(r["bytes"]) > 16:
            print(f"    ... {len(r['bytes']) - 16} more")
    print("\nReading it: single bytes far from anything else are enable flags "
          "or thresholds; long runs are curves/tables (a zeroed run is a "
          "classic 'quantity off'). Size and shape FIRST, meaning after.")


# --------------------------------------------------------------------------
# scan: map-shaped structures in ONE dump
# --------------------------------------------------------------------------

def _u16s(buf: bytes, start: int, count: int) -> list:
    return [int.from_bytes(buf[start + 2 * i:start + 2 * i + 2], "big")
            for i in range(count)]


def scan_tables(buf: bytes, min_len: int = 8, max_val: int = 65535) -> list:
    """Find runs of u16-BE words that look like calibration tables: a
    monotonic ramp (axis: rpm, %, temp) or a band of values inside a physical
    range. Heuristic only -- every hit is a candidate offset, not a name."""
    hits = []
    i = 0
    n = len(buf) // 2
    words_all = _u16s(buf, 0, n)
    while i < n - min_len:
        j = i
        while j < n - 1:
            w0, w1 = words_all[j], words_all[j + 1]
            mono = (w0 < w1) or (w0 > w1 and w0 != 0xFFFF)
            in_range = w1 <= max_val and w0 <= max_val
            if not (mono and in_range) or w0 == 0 and w1 == 0:
                break
            j += 1
        run = j - i
        if run >= min_len:
            hits.append({"offset": 2 * i, "words": run,
                         "values": words_all[i:i + min(6, run)],
                         "shape": "monotonic"})
            i = j + 1
        else:
            i += 1
    return hits


def print_scan(buf: bytes) -> None:
    hits = scan_tables(buf)
    print(f"{len(hits)} monotonic-run candidate(s) >= 8 words in {len(buf)} B\n")
    for h in hits[:40]:
        vals = " ".join(f"{v:5d}" for v in h["values"])
        print(f"  @0x{h['offset']:06X}  {h['words']:3d} words  {h['shape']}  {vals}")
    if len(hits) > 40:
        print(f"  ... {len(hits) - 40} more (raise min_len to tighten)")
    print("\nCandidates, not maps -- a name arrives only from a diff against a "
          "known-good file or from how the value moves on the car.")


# --------------------------------------------------------------------------
# hexdump
# --------------------------------------------------------------------------

def print_hexdump(buf: bytes, at: int = 0, length: int = 256) -> None:
    end = min(len(buf), at + length)
    for off in range(at, end, 16):
        chunk = buf[off:off + 16]
        hexs = " ".join(f"{b:02X}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{off:08X}  {hexs:<47}  |{text}|")


# --------------------------------------------------------------------------
# demo: synthetic original vs synthetic "tuned" -- the anatomy walkthrough
# --------------------------------------------------------------------------

def _synth(seed: int = 42, size: int = 0x11000) -> bytes:
    """A clearly-labelled FAKE dump with real structure: code-ish noise, then
    three table regions -- a dosing curve, an NOx threshold axis, and an
    enable-flag area. Deterministic. Nothing here came from any ECU."""
    rng = random.Random(seed)
    buf = bytearray(rng.randbytes(size // 2))          # low half: noise
    buf += bytes(size - len(buf))                      # high half: cal area

    def put_words(start: int, words: list) -> None:
        for k, w in enumerate(words):
            buf[start + 2 * k:start + 2 * k + 2] = w.to_bytes(2, "big")

    put_words(0x10000, [2, 5, 9, 14, 20, 27, 35, 44, 54, 60, 63, 64])  # dosing
    put_words(0x10100, [100, 150, 220, 310, 420, 560, 730, 950, 1200])  # NOx axis
    put_words(0x10200, [int(x) for x in
                        (25, 30, 35, 41, 48, 56, 65, 75, 86, 98, 111, 125)])  # EGT
    buf[0x10400] = 0x01                                 # SCR monitor enable
    buf[0x10401] = 0x01                                 # dosing enable
    buf[0x10402] = 0x14                                 # countdown start (20)
    return bytes(buf)


def demo() -> None:
    original = _synth()
    tuned = bytearray(original)
    # The classic AdBlue-off shape, applied the way a tuner file would:
    tuned[0x10000:0x10018] = bytes(24)      # dosing curve -> all zero
    tuned[0x10400] = 0x00                   # SCR monitor flag off
    tuned[0x10401] = 0x00                   # dosing enable off
    tuned[0x10402] = 0xFF                   # countdown neutralised
    tuned = bytes(tuned)

    print("SYNTHETIC DEMO -- no bytes here came from any ECU. Shape only.\n")
    print("=== the dosing table, BEFORE (original dump) ===")
    print_hexdump(original, 0x10000, 32)
    print("\n=== same offset, AFTER (a 'delete' file) ===")
    print_hexdump(tuned, 0x10000, 32)
    print("\n=== diff -- every changed byte, the whole tuner file ===")
    print_diff(original, tuned)
    print("\n=== scan on the original -- candidates a map-hunt finds ===")
    print_scan(original)


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="walkthrough on synthetic data")
    d = sub.add_parser("diff", help="changed regions between two dumps")
    d.add_argument("original"); d.add_argument("modified")
    s = sub.add_parser("scan", help="map-shaped candidates in one dump")
    s.add_argument("dump")
    h = sub.add_parser("hexdump", help="classic hexdump")
    h.add_argument("dump")
    h.add_argument("--at", type=lambda x: int(x, 0), default=0)
    h.add_argument("--len", type=lambda x: int(x, 0), default=256)
    a = p.parse_args(argv)

    if a.cmd == "demo":
        demo()
    elif a.cmd == "diff":
        print_diff(_load(a.original), _load(a.modified))
    elif a.cmd == "scan":
        print_scan(_load(a.dump))
    elif a.cmd == "hexdump":
        print_hexdump(_load(a.dump), a.at, a.len)
    return 0


if __name__ == "__main__":
    sys.exit(main())
