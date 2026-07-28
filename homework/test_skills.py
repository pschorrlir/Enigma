#!/usr/bin/env python3
"""Unit checks for enigma/skills.py (plain asserts; run directly)."""
import asyncio
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from enigma.skills import cyclic, cyclic_find, parse_payload, run_skill, skill_docs  # noqa: E402


def _fake_cexec(canned):
    """canned: list of (match_fn, (code, out)). Returns an async cexec fake."""
    calls = []

    async def cexec(*argv, input_bytes=None, timeout=None):
        calls.append((argv, input_bytes))
        for match, resp in canned:
            if match(argv):
                return resp
        return 1, "unexpected call: %r" % (argv,)

    cexec.calls = calls
    return cexec


def test_find_symbol_nonpie():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "nm", (0, "00000000004011d6 T win\n0000000000401130 T main\n")),
            (lambda a: a[0] == "readelf", (0, "ELF Header:\n  Type:  EXEC (Executable file)\n")),
        ])
        out = await run_skill("find_symbol", "/target/rung1 win", cexec)
        assert "0x4011d6" in out and "absolute" in out, out
    asyncio.run(go())


def test_find_symbol_pie():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "nm", (0, "00000000000011d6 T win\n")),
            (lambda a: a[0] == "readelf", (0, "  Type:  DYN (Position-Independent Executable file)\n")),
        ])
        out = await run_skill("find_symbol", "/target/rung2 win", cexec)
        assert "0x11d6" in out and "PIE" in out and "offset" in out, out
    asyncio.run(go())


def test_discover_offset():
    pat = cyclic(256)
    rip = struct.unpack("<Q", pat[72:80])[0]

    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "bash", (0, "")),
            (lambda a: a[0] == "gdb", (0, "rip            0x%x\n" % rip)),
        ])
        out = await run_skill("discover_offset", "/target/rung1", cexec)
        assert "72" in out, out
    asyncio.run(go())


def test_discover_offset_fallback():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "bash", (0, "")),
            (lambda a: a[0] == "gdb", (1, "gdb: command not found")),
        ])
        out = await run_skill("discover_offset", "/target/rung1", cexec)
        assert "72" in out and "gdb: command not found" in out, out
    asyncio.run(go())


def test_deliver_stdin():
    async def go():
        cexec = _fake_cexec([
            (lambda a: a[0] == "/target/rung1", (0, "flag{test}\n")),
        ])
        out = await run_skill("deliver_stdin", "/target/rung1 A*72 + p64(0x4011d6)", cexec)
        assert "flag{test}" in out, out
        argv, payload = cexec.calls[0]
        assert payload == b"A" * 72 + struct.pack("<Q", 0x4011d6), payload
    asyncio.run(go())


def test_unknown_skill_lists_available():
    async def go():
        out = await run_skill("nope", "", None)
        assert "discover_offset" in out and "deliver_stdin" in out, out
    asyncio.run(go())


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
    test_find_symbol_nonpie()
    test_find_symbol_pie()
    test_discover_offset()
    test_discover_offset_fallback()
    test_deliver_stdin()
    test_unknown_skill_lists_available()
    assert "discover_offset" in skill_docs() and "deliver_stdin" in skill_docs()
    print("test_skills OK")


if __name__ == "__main__":
    main()
