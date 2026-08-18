"""Decrypt JLR SDD ``.exml`` module-definition files.

SDD ships its per-module diagnostic definitions (DIDs, routines, CAN addressing)
as encrypted ``.exml`` files inside the InstallShield cabinet
``Disk1/data2.cab`` (file-group ``CURRENT_JLR_XCL_XML_DATA_XML``).

The cipher was recovered by reversing ``CCryptFile::Decrypt`` in SDD's
``Environment.dll`` (CryptImportKey PLAINTEXTKEYBLOB, alg ``CALG_3DES`` 0x6603,
``CryptSetKeyParam`` KP_MODE = ECB): **3DES / 168-bit / ECB / PKCS5**. The
24-byte key is stored nibble-swapped in the ctor at RVA 0x1000; swapped it is
ASCII ``YmZwZlQrQ3V4dVltNTArWE9s``.

This is a research/one-off tool (needs ``pycryptodome``); it is not imported by
the running harness. See ``captures/jlr/sdd169-extraction.md`` for the full
provenance and what was extracted.

Usage:
    python -m backend.tools.jlr_sdd_decrypt IN.exml [OUT.xml]
"""

from __future__ import annotations

import sys

# The embedded SDD key, de-obfuscated (each byte nibble-swapped), first 24 bytes.
_OBFUSCATED = bytes([
    0x95, 0xd6, 0xa5, 0x77, 0xa5, 0xc6, 0x15, 0x27, 0x15, 0x33, 0x65, 0x43,
    0x46, 0x65, 0xc6, 0x47, 0xe4, 0x45, 0x14, 0x27, 0x75, 0x54, 0x93, 0x37,
    0x25, 0x13, 0x07, 0x24, 0x15, 0x45, 0x03, 0x93,
])
KEY = bytes(((b >> 4) | (b << 4)) & 0xFF for b in _OBFUSCATED)[:24]  # b"YmZwZlQrQ3V4dVltNTArWE9s"


def decrypt(data: bytes) -> bytes:
    """3DES-ECB decrypt an ``.exml`` blob and strip PKCS5 padding."""
    from Crypto.Cipher import DES3  # local import: research-only dependency

    pt = DES3.new(KEY, DES3.MODE_ECB).decrypt(data)
    pad = pt[-1]
    if 1 <= pad <= 8 and all(b == pad for b in pt[-pad:]):
        pt = pt[:-pad]
    return pt


def decrypt_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return decrypt(fh.read())


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    out = decrypt_file(argv[0])
    if len(argv) > 1:
        with open(argv[1], "wb") as fh:
            fh.write(out)
    else:
        sys.stdout.buffer.write(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
