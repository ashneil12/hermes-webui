"""Regression tests for the stuck "THINKING..." indicator after server restart.

Background:
  When the server is restarted (redeploy, crash, OOM) during a streaming
  turn, the session JSON on disk still has ``active_stream_id`` set, but
  the in-memory STREAMS dict (which actually holds the live stream) is
  empty. The frontend reads ``active_stream_id`` from /api/session on
  page load and renders a "THINKING..." indicator and "STOP GENERATING"
  button that no refresh can clear.

These tests cover:
  - clear_stale_inflight_state() detects and clears stale state.
  - clear_stale_inflight_state() is a no-op when the stream is alive.
  - sweep_stale_inflight_state() cleans up all on-disk sessions at boot.
  - session_status reports agent_running=False for stale sessions.
"""
import json
import threading

import pytest

import api.models as models
from api.models import Session, clear_stale_inflight_state


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect session storage and STREAMS to test-local state."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()

    stream_map: dict = {}
    stream_lock = threading.Lock()
    monkeypatch.setattr(models, "STREAMS", stream_map)
    monkeypatch.setattr(models, "STREAMS_LOCK", stream_lock)

    yield session_dir, index_file, stream_map

    models.SESSIONS.clear()


def _make_session(sid, stream_id=None, pending_msg=None):
    s = Session(
        session_id=sid,
        title=sid,
        messages=[{"role": "user", "content": f"seed-{sid}"}],
    )
    s.active_stream_id = stream_id
    s.pending_user_message = pending_msg
    s.pending_attachments = ["a.txt"] if pending_msg else []
    s.pending_started_at = 1700000000.0 if pending_msg else None
    return s


class TestClearStaleInflightState:
    def test_clears_when_stream_id_not_in_streams(self, _isolate):
        """The bug case: active_stream_id set but stream is gone (server restart)."""
        s = _make_session("s1", stream_id="ghost-stream", pending_msg="hello")
        s.save()

        was_stale = clear_stale_inflight_state(s)

        assert was_stale is True
        assert s.active_stream_id is None
        assert s.pending_user_message is None
        assert s.pending_attachments == []
        assert s.pending_started_at is None

        # Persisted to disk so subsequent reads are clean
        loaded = Session.load("s1")
        assert loaded.active_stream_id is None
        assert loaded.pending_user_message is None

    def test_noop_when_stream_alive(self, _isolate):
        """A live, healthy stream must not be wiped out."""
        _, _, streams = _isolate
        streams["live-stream"] = object()

        s = _make_session("s2", stream_id="live-stream", pending_msg="hello")
        s.save()

        was_stale = clear_stale_inflight_state(s)

        assert was_stale is False
        assert s.active_stream_id == "live-stream"
        assert s.pending_user_message == "hello"

    def test_noop_when_no_active_stream_id(self, _isolate):
        """Session with no in-flight state should be untouched."""
        s = _make_session("s3", stream_id=None)
        s.save()

        was_stale = clear_stale_inflight_state(s)

        assert was_stale is False
        assert s.active_stream_id is None

    def test_persist_false_does_not_save(self, _isolate, tmp_path):
        """persist=False clears in-memory state but leaves disk untouched."""
        s = _make_session("s4", stream_id="ghost", pending_msg="hi")
        s.save()  # Save with stale state on disk

        was_stale = clear_stale_inflight_state(s, persist=False)

        assert was_stale is True
        assert s.active_stream_id is None
        # Disk still has the stale state since persist=False
        loaded = Session.load("s4")
        assert loaded.active_stream_id == "ghost"


class TestSweepStaleInflightState:
    def test_sweep_clears_all_stale_sessions(self, _isolate, monkeypatch):
        from api.startup import sweep_stale_inflight_state

        # Three sessions: two stale, one clean
        stale_a = _make_session("stale_a", stream_id="dead-1", pending_msg="msg-a")
        stale_b = _make_session("stale_b", stream_id="dead-2", pending_msg="msg-b")
        clean = _make_session("clean", stream_id=None)
        for s in (stale_a, stale_b, clean):
            s.save()

        cleaned = sweep_stale_inflight_state()

        assert cleaned == 2

        for sid in ("stale_a", "stale_b"):
            loaded = Session.load(sid)
            assert loaded.active_stream_id is None
            assert loaded.pending_user_message is None
            assert loaded.pending_attachments == []
            assert loaded.pending_started_at is None

        # Untouched
        loaded_clean = Session.load("clean")
        assert loaded_clean.active_stream_id is None  # was already None

    def test_sweep_noop_on_empty_session_dir(self, _isolate):
        from api.startup import sweep_stale_inflight_state
        # No sessions written
        assert sweep_stale_inflight_state() == 0

    def test_sweep_uses_index_when_available(self, _isolate):
        """Index pre-filter should keep sweep cheap on large session stores."""
        from api.startup import sweep_stale_inflight_state

        stale = _make_session("only_one", stream_id="dead", pending_msg="x")
        stale.save()  # save() also writes the index

        cleaned = sweep_stale_inflight_state()

        assert cleaned == 1
        loaded = Session.load("only_one")
        assert loaded.active_stream_id is None

    def test_sweep_falls_back_to_full_scan_when_index_corrupt(
        self, _isolate, tmp_path
    ):
        from api.startup import sweep_stale_inflight_state

        stale = _make_session("scan_me", stream_id="dead", pending_msg="x")
        stale.save()

        # Corrupt the index
        _, index_file, _ = _isolate
        index_file.write_text("not valid json {", encoding="utf-8")

        cleaned = sweep_stale_inflight_state()

        assert cleaned == 1
        loaded = Session.load("scan_me")
        assert loaded.active_stream_id is None


class TestSessionStatusAgentRunning:
    """session_status() must report agent_running based on live STREAMS, not
    the persisted active_stream_id field."""

    def test_agent_running_false_for_stale_session(self, _isolate):
        from api.session_ops import session_status

        s = _make_session("stat_1", stream_id="ghost", pending_msg="hi")
        s.save()

        status = session_status("stat_1")

        assert status["agent_running"] is False
        # Self-heal also clears the stale state on disk
        loaded = Session.load("stat_1")
        assert loaded.active_stream_id is None

    def test_agent_running_true_for_live_session(self, _isolate):
        from api.session_ops import session_status

        _, _, streams = _isolate
        streams["live"] = object()
        s = _make_session("stat_2", stream_id="live", pending_msg="hi")
        s.save()

        status = session_status("stat_2")

        assert status["agent_running"] is True
        # Live session preserved
        loaded = Session.load("stat_2")
        assert loaded.active_stream_id == "live"
