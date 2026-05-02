"""HTTP integration tests for the sidecar server.

Spins up SidecarHandler on a free port in a background thread, hits it
with urllib, and asserts the wire response. No subprocess, no webui
dependency.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import pytest

from sidecar.log_reader import append_event_atomic
from sidecar.server import QuietSidecarServer, make_handler


def _free_port() -> int:
    """Bind 0 and immediately close to claim a free port. Race-prone
    in theory; in practice fine for a single-test fixture."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sidecar_server(tmp_path):
    """Yield (base_url, chat_jobs_dir). Server runs in a daemon thread
    and is stopped at fixture teardown."""
    port = _free_port()
    chat_jobs = tmp_path / "chat-jobs"
    chat_jobs.mkdir()

    handler = make_handler(
        chat_jobs,
        poll_interval=0.01,
        heartbeat_interval=0.5,
        idle_timeout=1.0,
    )
    server = QuietSidecarServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"

    # Give the server a moment to come up. Health endpoint is the gate.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/chat-jobs/health", timeout=0.5) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.05)

    try:
        yield base, chat_jobs
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _parse_sse_stream(raw: bytes) -> list[dict]:
    """Tiny SSE parser for tests. Returns one dict per event with the
    fields we care about: id (cursor), event, data (parsed JSON)."""
    text = raw.decode("utf-8")
    events: list[dict] = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        ev: dict = {}
        for line in chunk.split("\n"):
            if line.startswith(":"):
                ev.setdefault("comments", []).append(line[1:].lstrip())
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            val = val.lstrip()
            if key == "data":
                try:
                    ev["data"] = json.loads(val)
                except json.JSONDecodeError:
                    ev["data"] = val
            elif key == "id":
                ev["id"] = int(val) if val.isdigit() else val
            elif key == "event":
                ev["event"] = val
        if "event" in ev or "data" in ev:
            events.append(ev)
    return events


# ──────────────────────────────────────────────────────────────────────────
# Health & routing
# ──────────────────────────────────────────────────────────────────────────

def test_health_endpoint(sidecar_server):
    base, _ = sidecar_server
    with urllib.request.urlopen(base + "/chat-jobs/health", timeout=2) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert body["ok"] is True
    assert "version" in body
    assert "chat_jobs_dir" in body


def test_api_prefix_aliases_chat_jobs(sidecar_server):
    """The dashboard mints URLs at /api/chat-jobs/... (matching the public
    URL convention); requests at that prefix must reach the same handlers
    as the bare /chat-jobs/... form."""
    base, chat_jobs = sidecar_server
    sid = "alias001"
    append_event_atomic(chat_jobs, sid, "token", {"text": "hi"}, seq=0)
    append_event_atomic(chat_jobs, sid, "done", {}, seq=1)

    # Health under the /api prefix
    with urllib.request.urlopen(base + "/api/chat-jobs/health", timeout=2) as r:
        assert r.status == 200
        assert json.loads(r.read())["ok"] is True

    # Events under the /api prefix
    with urllib.request.urlopen(
        base + f"/api/chat-jobs/{sid}/events?cursor=0",
        timeout=3,
    ) as r:
        assert r.status == 200
        events = _parse_sse_stream(r.read())
    assert [e["event"] for e in events] == ["token", "done"]


def test_unknown_path_returns_404(sidecar_server):
    base, _ = sidecar_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/some/random/path", timeout=2)
    assert exc.value.code == 404


def test_invalid_stream_id_returns_400(sidecar_server):
    base, _ = sidecar_server
    # A stream_id with a slash falls through routing entirely (the path
    # /chat-jobs/.. /events doesn't match), so we test invalid chars
    # that still match the route shape but get rejected by validation.
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            base + "/chat-jobs/has%20space/events",
            timeout=2,
        )
    assert exc.value.code == 400


def test_missing_stream_returns_404(sidecar_server):
    base, _ = sidecar_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(base + "/chat-jobs/nope-doesnt-exist/events", timeout=2)
    assert exc.value.code == 404


def test_pretouched_empty_stream_returns_200_and_tails(sidecar_server):
    """A pre-touched empty JSONL file must produce 200 SSE, not 404.

    `_handle_chat_start` in api/routes.py touches the per-stream JSONL
    synchronously before returning the stream_id, so the browser's SW
    can fire the events GET sub-second later without losing the race
    against the agent thread's first put(). This test pins that the
    sidecar serves an empty file the same way it serves a populated
    one — opening the SSE connection and tailing for events. Without
    the pre-touch, the browser would see the historical 404 "stream
    not found" race that this whole change is designed to eliminate.
    """
    base, chat_jobs = sidecar_server
    sid = "pretouch01"

    # Producer-side pre-touch: same shape as the chat-start change.
    (chat_jobs / f"{sid}.jsonl").touch()

    # Append one event from a background thread *after* the GET is
    # already in flight, mimicking the real agent-thread timing.
    def _append_after_delay() -> None:
        time.sleep(0.1)
        append_event_atomic(chat_jobs, sid, "done", {}, seq=0)

    appender = threading.Thread(target=_append_after_delay, daemon=True)
    appender.start()

    with urllib.request.urlopen(
        base + f"/chat-jobs/{sid}/events?cursor=0",
        timeout=3,
    ) as r:
        assert r.status == 200
        assert "text/event-stream" in r.headers.get("Content-Type", "")
        events = _parse_sse_stream(r.read())

    appender.join(timeout=2.0)
    assert [e["event"] for e in events] == ["done"]


def test_cursor_past_eof_returns_416(sidecar_server):
    base, chat_jobs = sidecar_server
    sid = "fixt001"
    append_event_atomic(chat_jobs, sid, "done", {}, seq=0)

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            base + f"/chat-jobs/{sid}/events?cursor=999999",
            timeout=2,
        )
    assert exc.value.code == 416
    body = json.loads(exc.value.read())
    assert "size" in body
    assert body["cursor"] == 999999


def test_negative_cursor_returns_416(sidecar_server):
    base, chat_jobs = sidecar_server
    sid = "fixt002"
    append_event_atomic(chat_jobs, sid, "done", {}, seq=0)

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            base + f"/chat-jobs/{sid}/events?cursor=-1",
            timeout=2,
        )
    assert exc.value.code == 416


def test_non_integer_cursor_returns_400(sidecar_server):
    base, chat_jobs = sidecar_server
    sid = "fixt003"
    append_event_atomic(chat_jobs, sid, "done", {}, seq=0)

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            base + f"/chat-jobs/{sid}/events?cursor=abc",
            timeout=2,
        )
    assert exc.value.code == 400


# ──────────────────────────────────────────────────────────────────────────
# Replay & resume
# ──────────────────────────────────────────────────────────────────────────

def test_full_replay_from_cursor_zero(sidecar_server):
    base, chat_jobs = sidecar_server
    sid = "rep001"
    append_event_atomic(chat_jobs, sid, "token", {"text": "hi"}, seq=0)
    append_event_atomic(chat_jobs, sid, "token", {"text": " there"}, seq=1)
    append_event_atomic(chat_jobs, sid, "done", {}, seq=2)

    with urllib.request.urlopen(
        base + f"/chat-jobs/{sid}/events?cursor=0",
        timeout=3,
    ) as r:
        assert r.status == 200
        assert "text/event-stream" in r.headers["Content-Type"]
        # tail_events stops on the 'done' terminator, so the response
        # body completes naturally without us needing to break.
        raw = r.read()

    events = _parse_sse_stream(raw)
    assert [e["event"] for e in events] == ["token", "token", "done"]
    assert events[0]["data"] == {"text": "hi"}
    assert events[2]["data"] == {}
    # id values must be strictly increasing — they're file byte offsets.
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_resume_from_middle(sidecar_server):
    """Client says 'I last saw cursor N'. Server returns events strictly
    after N. This is the core reconnect contract."""
    base, chat_jobs = sidecar_server
    sid = "rep002"
    append_event_atomic(chat_jobs, sid, "token", {"text": "first"}, seq=0)
    append_event_atomic(chat_jobs, sid, "token", {"text": "second"}, seq=1)
    append_event_atomic(chat_jobs, sid, "done", {}, seq=2)

    # Full read first to learn the cursor of the second event.
    with urllib.request.urlopen(
        base + f"/chat-jobs/{sid}/events?cursor=0",
        timeout=3,
    ) as r:
        events_full = _parse_sse_stream(r.read())
    cursor_after_first = events_full[0]["id"]

    # Resume from the cursor after the first event — should see only the
    # remaining two events.
    with urllib.request.urlopen(
        base + f"/chat-jobs/{sid}/events?cursor={cursor_after_first}",
        timeout=3,
    ) as r:
        events_resume = _parse_sse_stream(r.read())

    assert [e["event"] for e in events_resume] == ["token", "done"]
    assert events_resume[0]["data"] == {"text": "second"}


def test_cursor_at_eof_with_terminator_returns_quickly(sidecar_server):
    """If a client asks for ?cursor=<eof> on a finalized stream, we
    can't deliver anything new (the terminator already passed). The
    tail_events loop times out via idle_timeout. Test that the
    connection closes within a reasonable bound."""
    base, chat_jobs = sidecar_server
    sid = "rep003"
    size = append_event_atomic(chat_jobs, sid, "done", {}, seq=0)

    t0 = time.monotonic()
    with urllib.request.urlopen(
        base + f"/chat-jobs/{sid}/events?cursor={size}",
        timeout=3,
    ) as r:
        raw = r.read()
    elapsed = time.monotonic() - t0

    events = _parse_sse_stream(raw)
    assert events == []
    # Should have closed roughly when idle_timeout (1.0s in the fixture)
    # elapsed, well within the urlopen timeout. If we hung, this would
    # be near the urlopen timeout (3s) — guarding against that regression.
    assert elapsed < 2.5


# ──────────────────────────────────────────────────────────────────────────
# Mid-tail appends — the durability invariant
# ──────────────────────────────────────────────────────────────────────────

def test_mid_tail_appends_delivered(sidecar_server):
    """The tab-close-and-reopen scenario: client connects with cursor=0
    on a stream that's still being written. Producer appends events
    after the connection is established. Client must receive them."""
    base, chat_jobs = sidecar_server
    sid = "tail001"
    # Seed with one event so the file exists at request time.
    append_event_atomic(chat_jobs, sid, "token", {"text": "seed"}, seq=0)

    def _producer():
        time.sleep(0.05)
        append_event_atomic(chat_jobs, sid, "token", {"text": "mid"}, seq=1)
        time.sleep(0.05)
        append_event_atomic(chat_jobs, sid, "tool", {"name": "bash"}, seq=2)
        time.sleep(0.05)
        append_event_atomic(chat_jobs, sid, "done", {}, seq=3)

    threading.Thread(target=_producer, daemon=True).start()

    with urllib.request.urlopen(
        base + f"/chat-jobs/{sid}/events?cursor=0",
        timeout=5,
    ) as r:
        raw = r.read()

    events = _parse_sse_stream(raw)
    assert [e["event"] for e in events] == ["token", "token", "tool", "done"]
    seqs = [e["data"].get("text") or e["data"].get("name") for e in events]
    assert seqs == ["seed", "mid", "bash", None]


def test_resume_then_tail(sidecar_server):
    """Browser SW reconnect path end-to-end:
       1. Existing cursor at byte N (from a previous connection).
       2. Two more events were written after disconnect.
       3. Reconnect at cursor=N. Replay 2 buffered events, then tail.
       4. Producer appends one more. That arrives live. Then terminator."""
    base, chat_jobs = sidecar_server
    sid = "tail002"
    size_after_seed = append_event_atomic(chat_jobs, sid, "token", {"text": "before"}, seq=0)
    # Two events written before reconnect.
    append_event_atomic(chat_jobs, sid, "token", {"text": "buffered1"}, seq=1)
    append_event_atomic(chat_jobs, sid, "token", {"text": "buffered2"}, seq=2)

    def _late_producer():
        time.sleep(0.1)
        append_event_atomic(chat_jobs, sid, "token", {"text": "live"}, seq=3)
        time.sleep(0.05)
        append_event_atomic(chat_jobs, sid, "done", {}, seq=4)

    threading.Thread(target=_late_producer, daemon=True).start()

    # Reconnect at the cursor right after the seed event.
    with urllib.request.urlopen(
        base + f"/chat-jobs/{sid}/events?cursor={size_after_seed}",
        timeout=5,
    ) as r:
        raw = r.read()

    events = _parse_sse_stream(raw)
    texts = [e["data"].get("text") for e in events if e["event"] == "token"]
    assert texts == ["buffered1", "buffered2", "live"]
    assert events[-1]["event"] == "done"
