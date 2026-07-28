#!/usr/bin/env python3
"""Unit checks for enigma/skills.py (plain asserts; run directly)."""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from enigma.skills import cyclic, cyclic_find, parse_payload  # noqa: E402


def main():
    # cyclic round-trip: every 4-byte fragment locates itself
    pat = cyclic(256)
    assert len(pat) == 256
    for off in (0, 4, 72, 128, 252):
        assert cyclic_find(pat, pat[off:off + 4]) == off, off

    # parse_payload: repeats, packed addrs, mixed, whitespace tolerant
    assert parse_payload("A*8") == b"AAAAAAAA"
    assert parse_payload("A*4 + p64(0x4011d6)") == b"AAAA" + struct.pack("<Q", 0x4011d6)
    assert parse_payload("B*2+p32(16)") == b"BB" + struct.pack("<I", 16)
    try:
        parse_payload("nonsense")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("test_skills OK")


if __name__ == "__main__":
    main()
