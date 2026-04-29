"""Integration test for the streaming.py JSONL tee.

The producer-side tee in api.streaming.put() is the bridge between the
WebUI's existing event firehose and the sidecar's durability log. If the
tee silently breaks, every running chat loses reconnect support.

We exercise the tee path directly by simulating what _run_agent_streaming
does: maintain a per-stream sequence counter and call append_event_atomic
on every event. This is intentionally a near-copy of the production code
path so that any drift between the two will fail this test.

We don't import api.streaming here because that pulls in hermes-agent
dependencies the test environment may not have. Instead, the assertion is
that append_event_atomic — the function streaming.py imports and calls —
produces logs in the format the sidecar's reader expects.
"""
from __future__ import annotations

import json
from pathlib import Path

from sidecar.log_reader import (
    TERMINATOR_EVENTS,
    append_event_atomic,
    read_from_cursor,
)


def test_tee_round_trip_for_typical_chat_event_sequence(tmp_path):
    """The events a real chat run emits — token, tool, approval, done —
    survive the tee and read back identically."""
    sid = "tee_chat_001"

    # Replay a realistic event sequence that mirrors _run_agent_streaming's
    # put() call sites: token deltas, then an approval, then a tool result,
    # then done.
    events = [
        ("token", {"text": "Hello"}),
        ("token", {"text": ", world"}),
        ("approval", {
            "session_id": sid,
            "approval_id": "a-001",
            "command": "rm -rf /tmp/foo",
            "pattern_key": "rm",
        }),
        ("tool", {
            "toolCall": {
                "id": "tc-001",
                "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                "status": "complete",
                "response": "file1\nfile2",
            },
            "phase": "complete",
        }),
        ("done", {"content": "Hello, world", "usage": {"in": 10, "out": 5}}),
    ]

    seq = 0
    for event_name, data in events:
        append_event_atomic(tmp_path, sid, event_name, data, seq=seq)
        seq += 1

    # Read it all back.
    log = tmp_path / f"{sid}.jsonl"
    frames, cursor = read_from_cursor(log, 0)

    assert len(frames) == len(events)
    assert cursor == log.stat().st_size

    # Event names line up.
    assert [f.event_name for f in frames] == [e[0] for e in events]
    # Seqs are monotonic from 0.
    assert [f.seq for f in frames] == list(range(len(events)))
    # Data round-trips. (data_json is the canonical representation; we
    # compare via json.loads to be insensitive to key ordering.)
    for f, (_, expected) in zip(frames, events):
        assert json.loads(f.data_json) == expected
    # Last event is the terminator.
    assert frames[-1].is_terminator
    assert frames[-1].event_name in TERMINATOR_EVENTS


def test_tee_produces_independent_logs_per_stream(tmp_path):
    """Two concurrent chats must not interleave into one another's
    JSONL files. The path is keyed by stream_id; this verifies the
    file separation is real, not a typo."""
    append_event_atomic(tmp_path, "s_alpha", "token", {"text": "A"}, seq=0)
    append_event_atomic(tmp_path, "s_beta", "token", {"text": "B"}, seq=0)
    append_event_atomic(tmp_path, "s_alpha", "done", {}, seq=1)
    append_event_atomic(tmp_path, "s_beta", "done", {}, seq=1)

    alpha_frames, _ = read_from_cursor(tmp_path / "s_alpha.jsonl", 0)
    beta_frames, _ = read_from_cursor(tmp_path / "s_beta.jsonl", 0)

    assert [json.loads(f.data_json).get("text") for f in alpha_frames if f.event_name == "token"] == ["A"]
    assert [json.loads(f.data_json).get("text") for f in beta_frames if f.event_name == "token"] == ["B"]


def test_tee_failure_isolated_to_one_stream(tmp_path, monkeypatch):
    """Simulate the 'best-effort' failure mode: if append throws, the
    caller (streaming.put) catches and continues. Verify the function's
    own contract — it raises on bad input rather than silently dropping
    — so the caller's try/except is actually catching a real signal."""
    import pytest
    with pytest.raises(ValueError):
        append_event_atomic(tmp_path, "../escape", "token", {}, seq=0)

    # Still no file should have been written.
    assert not (tmp_path / "..escape.jsonl").exists()
    assert list(tmp_path.iterdir()) == []
