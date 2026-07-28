#!/usr/bin/env python3
"""Unit checks for enigma/skills.py (plain asserts; run directly)."""
import asyncio
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from enigma.skills import (cyclic, cyclic_find, parse_payload, parse_steps,  # noqa: E402
                           render_template, run_skill, skill_docs)  # noqa: E402


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


def test_toolbox_skill_dispatch():
    """ToolBox.run('skill', ...) reaches the registry via a faked docker exec."""
    import types

    from enigma.tools import ToolBox

    async def go():
        tb = ToolBox.__new__(ToolBox)  # bypass __init__ (needs cfg/http)
        tb._container = "fakecid"
        tb._workdir = "/workspace"
        tb._names = ("shell", "write", "read", "calc", "skill")
        tb.cfg = types.SimpleNamespace(tool_timeout_s=60, tool_result_chars=10000)
        tb.http = None

        async def fake_docker(*args, timeout=None, input_bytes=None):
            assert args[:2] == ("exec", "-i"), args
            if args[3] == "nm":
                return 0, "00000000004011d6 T win\n"
            if args[3] == "readelf":
                return 0, "  Type:  EXEC (Executable file)\n"
            return 1, "unexpected"

        tb._docker = fake_docker
        out = await tb.run("skill", "find_symbol /target/rung1 win")
        assert "0x4011d6" in out, out
        out = await tb.run("skill", "bogus x")
        assert "unknown skill" in out and "deliver_stdin" in out, out

    asyncio.run(go())


def test_parse_steps_shorthand():
    steps, hex8, err = parse_steps(r"main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9)")
    assert err is None, err
    assert steps == [("expect", r"main:\s*(0x[0-9a-f]+)"),
                     ("send", "A*72 + p64({leak}-0xb9)")], steps
    assert hex8 is False


def test_parse_steps_explicit_with_hex8():
    steps, hex8, err = parse_steps(
        r"expect:Choice: send:1 expect:main:\s*(0x[0-9a-f]+) A*72 + p64({leak}-0xb9) hex8")
    assert err is None, err
    assert steps == [("expect", "Choice:"), ("send", "1"),
                     ("expect", r"main:\s*(0x[0-9a-f]+)"),
                     ("send", "A*72 + p64({leak}-0xb9)")], steps
    assert hex8 is True


def test_parse_steps_errors():
    # missing template
    steps, _, err = parse_steps(r"main:\s*(0x[0-9a-f]+)")
    assert steps is None and err
    # no send step
    steps, _, err = parse_steps("expect:foo expect:bar")
    assert steps is None and "send" in err
    # step limit (9 steps)
    steps, _, err = parse_steps(" ".join(["send:x"] * 9))
    assert steps is None and "8" in err
    # expect regex with a space
    steps, _, err = parse_steps("expect:hello world send:x")
    assert steps is None and err
    # bad regex
    steps, _, err = parse_steps("expect:(unclosed send:x")
    assert steps is None and err


def test_render_template_leaks():
    leaks = {"leak": 0x5555555542c2, "leak1": 0x5555555542c2, "leak2": 0x1000}
    out = render_template("A*72 + p64({leak}-0xb9)", leaks)
    assert out == b"A" * 72 + struct.pack("<Q", 0x5555555542c2 - 0xb9)
    out = render_template("p64({leak2}+16)", leaks)
    assert out == struct.pack("<Q", 0x1010)
    out = render_template("p64({leak2+16})", leaks)  # offset inside braces
    assert out == struct.pack("<Q", 0x1010)
    out = render_template("p64({leak})", leaks)
    assert out == struct.pack("<Q", 0x5555555542c2)
    # literal text terms (menu answers) pass through as bytes
    assert render_template("1", {}) == b"1"
    assert render_template("A*2 + yes", {}) == b"AAyes"
    try:
        render_template("p64({leak3})", leaks)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "leak3" in str(e)


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
    test_toolbox_skill_dispatch()
    assert "discover_offset" in skill_docs() and "deliver_stdin" in skill_docs()
    test_parse_steps_shorthand()
    test_parse_steps_explicit_with_hex8()
    test_parse_steps_errors()
    test_render_template_leaks()
    print("test_skills OK")


if __name__ == "__main__":
    main()
