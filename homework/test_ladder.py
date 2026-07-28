#!/usr/bin/env python3
"""Unit checks for ladder.py helpers (plain asserts; run directly)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ladder import _transcript_stats


def main():
    # synthetic transcript: 3 tool steps (one pivot, one block), then done
    recs = [
        {"step": 1, "action": "tool", "tool": "shell", "arg": "ls", "result": "ok"},
        {"step": 2, "action": "tool", "tool": "shell", "arg": "gdb x",
         "result": "[harness strategy pivot] proposes:\nstop that\n\nrest"},
        {"step": 3, "action": "tool", "tool": "read", "arg": "/a",
         "result": "[blocked by harness] NOT executed"},
        {"step": 4, "action": "done", "summary": "flag written"},
    ]
    path = os.path.join(HERE, "out", "_test_synth.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")

    stats = _transcript_stats(path)
    assert stats["steps"] == 3, stats          # tool steps only
    assert stats["pivots"] == 1, stats
    assert stats["blocked"] == 1, stats
    assert stats["solved"] is True, stats      # action == "done"
    os.unlink(path)
    print("test_ladder OK")


if __name__ == "__main__":
    main()
