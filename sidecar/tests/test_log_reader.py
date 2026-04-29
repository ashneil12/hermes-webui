"""Unit tests for sidecar.log_reader.

These tests pin the wire contract:
  - cursor semantics (byte offset of next byte to read)
  - frame parsing (well-formed and malformed)
  - tail-and-stream behavior
  - SSE encoding

The contract is what the browser SW will reconnect against, so these
tests gate any change that would break a deployed client.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from sidecar.log_reader import (
    TERMINATOR_EVENTS,
    append_event_atomic,
    encode_sse_comment,
    encode_sse_frame,
    is_valid_stream_id,
    log_path,
    read_from_cursor,
    tail_events,
)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _write_jsonl(path: Path, *records: dict) -> int:
    """Write records as JSONL, return final file size in bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        for r in records:
            f.write((json.dumps(r) + "\n").encode("utf-8"))
    return path.stat().st_size


def _append_jsonl(path: Path, *records: dict) -> int:
    """Append records as JSONL, return final file size in bytes."""
    with open(path, "ab") as f:
        for r in records:
            f.write((json.dumps(r) + "\n").encode("utf-8"))
    return path.stat().st_size


def _frame(seq: int, event: str, data: dict | None = None) -> dict:
    return {"seq": seq, "event": event, "data": data or {}, "ts": 1.0}


# ──────────────────────────────────────────────────────────────────────────
# is_valid_stream_id
# ──────────────────────────────────────────────────────────────────────────

class TestStreamIdValidation:
    def test_accepts_uuid_hex(self):
        assert is_valid_stream_id("abcd1234ef567890abcd1234ef567890")

    def test_accepts_alphanumeric_with_dashes_underscores(self):
        assert is_valid_stream_id("stream-001_v2")

    def test_rejects_empty(self):
        assert not is_valid_stream_id("")

    def test_rejects_path_traversal(self):
        assert not is_valid_stream_id("../etc/passwd")
        assert not is_valid_stream_id("..")
        assert not is_valid_stream_id("a/b")

    def test_rejects_special_characters(self):
        for bad in ["a b", "a.b", "a:b", "a%b", "a\nb", "a\x00b"]:
            assert not is_valid_stream_id(bad), f"should reject {bad!r}"

    def test_rejects_too_long(self):
        assert not is_valid_stream_id("a" * 129)
        assert is_valid_stream_id("a" * 128)


# ──────────────────────────────────────────────────────────────────────────
# read_from_cursor
# ──────────────────────────────────────────────────────────────────────────

class TestReadFromCursor:
    def test_empty_file_returns_no_frames(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_bytes(b"")
        frames, cursor = read_from_cursor(path, 0)
        assert frames == []
        assert cursor == 0

    def test_full_replay_from_zero(self, tmp_path):
        path = tmp_path / "s.jsonl"
        size = _write_jsonl(
            path,
            _frame(0, "token", {"text": "hi"}),
            _frame(1, "token", {"text": " there"}),
            _frame(2, "done", {}),
        )
        frames, cursor = read_from_cursor(path, 0)
        assert [f.event_name for f in frames] == ["token", "token", "done"]
        assert [f.seq for f in frames] == [0, 1, 2]
        assert cursor == size
        assert frames[-1].is_terminator
        # Each frame's cursor_after matches the cumulative file size up to
        # and including its terminating newline.
        assert frames[2].cursor_after == size

    def test_resume_from_middle(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(
            path,
            _frame(0, "token", {"text": "hi"}),
            _frame(1, "token", {"text": " there"}),
            _frame(2, "done", {}),
        )
        # Find the cursor_after of the first frame to resume from there.
        first_pass, _ = read_from_cursor(path, 0)
        resume_at = first_pass[0].cursor_after

        frames, cursor = read_from_cursor(path, resume_at)
        assert [f.seq for f in frames] == [1, 2]
        assert cursor == path.stat().st_size

    def test_resume_at_eof_returns_empty(self, tmp_path):
        path = tmp_path / "s.jsonl"
        size = _write_jsonl(path, _frame(0, "done", {}))
        frames, cursor = read_from_cursor(path, size)
        assert frames == []
        assert cursor == size

    def test_partial_trailing_line_not_yielded(self, tmp_path):
        """A writer in the middle of flushing a line shouldn't trigger a
        partial-frame read. Cursor must stay at the start of the
        incomplete line so the next read finds the full line."""
        path = tmp_path / "s.jsonl"
        complete_record = (json.dumps(_frame(0, "token", {"text": "ok"})) + "\n").encode()
        partial_record = (json.dumps(_frame(1, "token", {"text": "part"})))[:10].encode()
        path.write_bytes(complete_record + partial_record)

        frames, cursor = read_from_cursor(path, 0)
        assert len(frames) == 1
        assert frames[0].seq == 0
        assert cursor == len(complete_record)

    def test_cursor_past_eof_raises(self, tmp_path):
        path = tmp_path / "s.jsonl"
        size = _write_jsonl(path, _frame(0, "done", {}))
        with pytest.raises(ValueError, match="past EOF"):
            read_from_cursor(path, size + 1)

    def test_negative_cursor_raises(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_bytes(b"")
        with pytest.raises(ValueError, match="negative"):
            read_from_cursor(path, -1)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_from_cursor(tmp_path / "nope.jsonl", 0)

    def test_malformed_line_yields_skip_frame(self, tmp_path):
        """A non-JSON line shouldn't crash the reader. The frame is
        emitted with event_name='' so the consumer can pass through
        a 'skip' marker and keep its cursor in sync."""
        path = tmp_path / "s.jsonl"
        path.write_bytes(b"not valid json\n" + (json.dumps(_frame(1, "done")) + "\n").encode())
        frames, _ = read_from_cursor(path, 0)
        assert len(frames) == 2
        assert frames[0].event_name == ""
        assert frames[0].seq is None
        assert frames[1].event_name == "done"

    def test_terminator_classification(self, tmp_path):
        path = tmp_path / "s.jsonl"
        records = [_frame(i, ev) for i, ev in enumerate(["token", "tool", "done"])]
        _write_jsonl(path, *records)
        frames, _ = read_from_cursor(path, 0)
        assert [f.is_terminator for f in frames] == [False, False, True]

    def test_terminator_set_includes_all_documented_events(self):
        # If this set drifts from the comment in log_reader.py, the wire
        # contract documentation drifts too. Update both together.
        assert TERMINATOR_EVENTS == frozenset(
            {"done", "error", "cancel", "timeout", "close", "stream_end"}
        )


# ──────────────────────────────────────────────────────────────────────────
# tail_events
# ──────────────────────────────────────────────────────────────────────────

class TestTailEvents:
    def test_terminator_stops_generator(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(
            path,
            _frame(0, "token", {"text": "hi"}),
            _frame(1, "done", {}),
        )
        frames = list(tail_events(path, idle_timeout=0.5, poll_interval=0.01))
        assert [f.event_name for f in frames] == ["token", "done"]

    def test_idle_timeout_stops_when_no_events(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_bytes(b"")
        t0 = time.monotonic()
        frames = list(tail_events(path, idle_timeout=0.1, poll_interval=0.01))
        elapsed = time.monotonic() - t0
        assert frames == []
        assert elapsed < 1.0   # should bail out near 0.1s, not hang

    def test_picks_up_appends_during_tail(self, tmp_path):
        """Producer appends after the tail starts. Consumer should see
        the new events without restart."""
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, _frame(0, "token", {"text": "first"}))

        # Schedule appends shortly after the tail starts.
        def _appender():
            time.sleep(0.05)
            _append_jsonl(path, _frame(1, "token", {"text": "second"}))
            time.sleep(0.05)
            _append_jsonl(path, _frame(2, "done", {}))

        threading.Thread(target=_appender, daemon=True).start()

        frames = list(tail_events(path, idle_timeout=2.0, poll_interval=0.01))
        assert [f.seq for f in frames] == [0, 1, 2]
        assert frames[-1].is_terminator

    def test_resume_from_cursor_skips_already_seen(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(
            path,
            _frame(0, "token", {"text": "a"}),
            _frame(1, "token", {"text": "b"}),
            _frame(2, "done", {}),
        )
        # Pretend the consumer already read frame 0; resume from there.
        first_pass, _ = read_from_cursor(path, 0)
        resume = first_pass[0].cursor_after

        frames = list(tail_events(path, start_cursor=resume,
                                  idle_timeout=0.5, poll_interval=0.01))
        assert [f.seq for f in frames] == [1, 2]

    def test_cancel_callback_stops_generator(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, _frame(0, "token", {"text": "a"}))

        seen = []

        def _cancel():
            return len(seen) >= 1   # stop after first event

        for f in tail_events(path, idle_timeout=2.0, poll_interval=0.01, cancel=_cancel):
            seen.append(f)

        assert len(seen) == 1   # cancel was honored before the tail kept polling

    def test_missing_file_returns_empty(self, tmp_path):
        """If the file vanishes mid-tail (deleted, rotated), the
        generator returns cleanly rather than crashing."""
        path = tmp_path / "missing.jsonl"
        # never created
        frames = list(tail_events(path, idle_timeout=0.1, poll_interval=0.01))
        assert frames == []


# ──────────────────────────────────────────────────────────────────────────
# encode_sse_frame / encode_sse_comment
# ──────────────────────────────────────────────────────────────────────────

class TestSSEEncoding:
    def test_well_formed_frame(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, _frame(0, "token", {"text": "hello"}))
        frames, _ = read_from_cursor(path, 0)
        out = encode_sse_frame(frames[0]).decode("utf-8")

        # SSE frame must end with a blank line.
        assert out.endswith("\n\n")
        # id: line carries the cursor_after for client-side tracking.
        assert "id: " in out
        assert f"id: {frames[0].cursor_after}\n" in out
        assert "event: token\n" in out
        assert '"text": "hello"' in out or '"text":"hello"' in out

    def test_malformed_frame_becomes_log_skip(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_bytes(b"not json\n")
        frames, _ = read_from_cursor(path, 0)
        out = encode_sse_frame(frames[0]).decode("utf-8")
        assert "event: log_skip\n" in out
        assert "data: " in out
        assert out.endswith("\n\n")

    def test_comment_format(self):
        out = encode_sse_comment("heartbeat").decode("utf-8")
        assert out == ": heartbeat\n\n"

    def test_comment_strips_newlines(self):
        out = encode_sse_comment("hi\nthere").decode("utf-8")
        assert out == ": hi there\n\n"
        assert "\n\n" in out
        assert out.count("\n\n") == 1   # exactly one terminator


# ──────────────────────────────────────────────────────────────────────────
# append_event_atomic — round-trip with read_from_cursor
# ──────────────────────────────────────────────────────────────────────────

class TestAppendRoundtrip:
    def test_appended_events_read_back_in_order(self, tmp_path):
        sid = "rt001"
        size_after_a = append_event_atomic(
            tmp_path, sid, "token", {"text": "a"}, seq=0, ts=1.0,
        )
        size_after_b = append_event_atomic(
            tmp_path, sid, "token", {"text": "b"}, seq=1, ts=2.0,
        )
        size_after_done = append_event_atomic(
            tmp_path, sid, "done", {}, seq=2, ts=3.0,
        )

        path = log_path(tmp_path, sid)
        assert path.stat().st_size == size_after_done
        assert size_after_a < size_after_b < size_after_done

        frames, cursor = read_from_cursor(path, 0)
        assert [f.seq for f in frames] == [0, 1, 2]
        assert [f.event_name for f in frames] == ["token", "token", "done"]
        assert cursor == size_after_done
        # cursor_after of frame 0 == size after appending only frame 0
        assert frames[0].cursor_after == size_after_a

    def test_invalid_stream_id_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="invalid stream_id"):
            append_event_atomic(tmp_path, "../bad", "token", {}, seq=0)

    def test_unicode_text_round_trips(self, tmp_path):
        sid = "rt002"
        text = "héllo 世界 🌍"
        append_event_atomic(tmp_path, sid, "token", {"text": text}, seq=0)
        path = log_path(tmp_path, sid)
        frames, _ = read_from_cursor(path, 0)
        assert json.loads(frames[0].data_json) == {"text": text}

    def test_concurrent_appends_dont_corrupt(self, tmp_path):
        """O_APPEND guarantees every write() call atomically seeks to
        end and writes — even with concurrent writers, lines never
        interleave. Pin that invariant."""
        sid = "rt003"
        N_THREADS = 8
        EVENTS_PER_THREAD = 25

        def _producer(tid: int):
            for i in range(EVENTS_PER_THREAD):
                append_event_atomic(
                    tmp_path, sid, "token",
                    {"tid": tid, "i": i},
                    seq=tid * 1000 + i,
                )

        threads = [threading.Thread(target=_producer, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        path = log_path(tmp_path, sid)
        frames, _ = read_from_cursor(path, 0)
        # All events accounted for, all parseable (no torn lines).
        assert len(frames) == N_THREADS * EVENTS_PER_THREAD
        assert all(f.event_name == "token" for f in frames)
        assert all(f.seq is not None for f in frames)
