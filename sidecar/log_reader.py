"""JSONL chat-event log reader with cursor-resumable tail.

Wire contract
-------------
- Each chat stream lives in a single append-only file:
  `<chat_jobs_dir>/<stream_id>.jsonl`.
- One event per line, terminated with `\n`. Each line is a JSON object:

      {"seq": 0, "event": "token", "data": {...}, "ts": 1714405200.123}

  Fields:
    seq:   monotonic int per stream, starting at 0.
    event: SSE event name. Stable set: token, reasoning, tool, approval,
           clarify, metering, title_status, done, error, cancel, timeout,
           close, stream_end.
    data:  arbitrary JSON-serializable dict; the SSE `data:` payload.
    ts:    producer wall-clock time (Unix seconds, float).

- Cursor is a byte offset: the next byte the consumer wants to read.
  Cursor 0 = start of file. Cursor == filesize = past the last byte
  (consumer is caught up, will tail).

- Terminator events end the live tail. Set:
  {"done", "error", "cancel", "timeout", "close", "stream_end"}.
  After a terminator, the file is final; future consumers can replay
  the whole thing from cursor=0.

The reader is intentionally read-only and stateless across requests.
The WebUI owns the writes; this reader doesn't know or care how the
file got there.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

TERMINATOR_EVENTS = frozenset({
    "done", "error", "cancel", "timeout", "close", "stream_end",
})

# Pattern that bounds stream_id to a safe shape — no path traversal,
# no surprises in URLs or file names. Matches the shape emitted by the
# WebUI's start_stream() (uuid4 hex, but more permissive).
STREAM_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

DEFAULT_POLL_INTERVAL = 0.1   # seconds between tail polls when caught up
DEFAULT_HEARTBEAT_INTERVAL = 15.0   # seconds of idle before sending an SSE comment
DEFAULT_IDLE_TIMEOUT = 300.0  # close the tail after this many seconds without events


def is_valid_stream_id(stream_id: str) -> bool:
    """Reject anything that could escape the chat_jobs_dir or break URLs."""
    return bool(STREAM_ID_RE.match(stream_id or ""))


def log_path(chat_jobs_dir: Path, stream_id: str) -> Path:
    """Resolve the on-disk JSONL path for a stream. Caller must have
    already validated stream_id with is_valid_stream_id."""
    return Path(chat_jobs_dir) / f"{stream_id}.jsonl"


@dataclass
class FrameRead:
    """One JSONL line read from the log.

    cursor_after is the byte offset *after* this frame — the value the
    client should send as `?cursor=` to resume from the next event.
    """
    raw_line: bytes      # the JSONL line as written, without trailing newline
    cursor_after: int    # byte offset of the byte after this line's terminating newline
    event_name: str      # parsed `event` field; may be "" if line is malformed
    data_json: str       # parsed `data` field re-serialized as JSON; "" if malformed
    seq: Optional[int]   # parsed `seq` field; None if malformed
    is_terminator: bool  # event_name is in TERMINATOR_EVENTS


def _parse_frame(raw_line: bytes, cursor_after: int) -> FrameRead:
    """Parse a single JSONL line. Tolerates malformed lines by returning
    an event_name of "" so the caller can decide whether to skip or
    pass through. The byte cursor still advances past the malformed line
    so the consumer doesn't get stuck."""
    try:
        obj = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return FrameRead(
            raw_line=raw_line,
            cursor_after=cursor_after,
            event_name="",
            data_json="",
            seq=None,
            is_terminator=False,
        )

    event_name = str(obj.get("event") or "")
    data_obj = obj.get("data")
    if data_obj is None:
        data_json = "{}"
    else:
        try:
            data_json = json.dumps(data_obj, ensure_ascii=False)
        except (TypeError, ValueError):
            data_json = "{}"

    seq_val = obj.get("seq")
    seq = int(seq_val) if isinstance(seq_val, int) else None

    return FrameRead(
        raw_line=raw_line,
        cursor_after=cursor_after,
        event_name=event_name,
        data_json=data_json,
        seq=seq,
        is_terminator=event_name in TERMINATOR_EVENTS,
    )


def read_from_cursor(
    log_file: Path,
    cursor: int,
) -> tuple[list[FrameRead], int]:
    """Read all complete JSONL lines from `cursor` to current EOF.

    Returns (frames, new_cursor). new_cursor is the byte offset of the
    byte after the last complete line read (i.e. the next cursor value).
    Partial trailing lines (no `\n` yet) are not returned and not
    advanced past — they'll be picked up on the next read once the
    writer flushes the newline.

    Raises:
        FileNotFoundError if the log doesn't exist.
        ValueError if cursor is negative or exceeds current file size.
    """
    if cursor < 0:
        raise ValueError(f"negative cursor: {cursor}")

    size = log_file.stat().st_size
    if cursor > size:
        raise ValueError(f"cursor {cursor} past EOF {size}")

    frames: list[FrameRead] = []
    if cursor == size:
        return frames, cursor

    with open(log_file, "rb") as f:
        f.seek(cursor)
        # Read everything available, but only emit complete lines.
        buf = f.read(size - cursor)

    pos = 0
    new_cursor = cursor
    while pos < len(buf):
        nl = buf.find(b"\n", pos)
        if nl < 0:
            # Partial trailing line — leave it for the next read.
            break
        raw_line = buf[pos:nl]
        line_cursor_after = cursor + nl + 1
        frames.append(_parse_frame(raw_line, line_cursor_after))
        pos = nl + 1
        new_cursor = line_cursor_after

    return frames, new_cursor


def tail_events(
    log_file: Path,
    start_cursor: int = 0,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    cancel: Optional[callable] = None,
    monotonic: callable = time.monotonic,
    sleep: callable = time.sleep,
) -> Iterator[FrameRead]:
    """Yield events from log_file starting at `start_cursor`.

    Reads any already-written events first, then polls for appends.
    Stops when:
      - A terminator event is encountered (yielded, then stop).
      - `idle_timeout` seconds elapse with no new events.
      - `cancel()` returns truthy.

    The caller is responsible for catching FileNotFoundError if the
    file doesn't exist when this generator starts (we let it propagate
    on the first read so the HTTP layer can return 404).

    `monotonic` and `sleep` are injected for testability.
    """
    cursor = start_cursor
    last_event_at = monotonic()

    while True:
        if cancel is not None and cancel():
            return

        try:
            frames, new_cursor = read_from_cursor(log_file, cursor)
        except FileNotFoundError:
            # The file was deleted out from under us. Treat as terminal.
            return

        if frames:
            for f in frames:
                yield f
                if f.is_terminator:
                    return
            cursor = new_cursor
            last_event_at = monotonic()
            continue

        # No new events. Idle timeout?
        if monotonic() - last_event_at > idle_timeout:
            return

        sleep(poll_interval)


def encode_sse_frame(frame: FrameRead) -> bytes:
    """Format one parsed frame as a wire-ready SSE chunk.

    Layout:
        id: <cursor_after>\n
        event: <event_name>\n
        data: <data_json>\n
        \n

    The cursor_after value is sent as the SSE `id:` so EventSource's
    Last-Event-Id header carries it on reconnect — but the client also
    has it in `data` for explicit cursor tracking.

    Lines without a known event_name (malformed) are emitted as event
    'log_skip' with the cursor advance so consumers can still keep
    their cursor in sync. Producers shouldn't emit malformed lines;
    this is defense in depth.
    """
    if not frame.event_name:
        return (
            f"id: {frame.cursor_after}\n"
            f"event: log_skip\n"
            f"data: {{\"reason\":\"malformed\"}}\n\n"
        ).encode("utf-8")

    return (
        f"id: {frame.cursor_after}\n"
        f"event: {frame.event_name}\n"
        f"data: {frame.data_json}\n\n"
    ).encode("utf-8")


def encode_sse_comment(text: str) -> bytes:
    """SSE comment line — used for heartbeats. Browsers ignore these
    but they keep the TCP connection alive through proxies."""
    safe = text.replace("\n", " ").replace("\r", " ")
    return f": {safe}\n\n".encode("utf-8")


def append_event_atomic(
    chat_jobs_dir: Path,
    stream_id: str,
    event: str,
    data: dict,
    *,
    seq: int,
    ts: Optional[float] = None,
) -> int:
    """Append one event to the JSONL log for stream_id, atomically.

    Returns the new file size (== cursor that points past this event).

    Producer-side helper. The sidecar itself never writes; this lives
    here so the WebUI can tee through one well-tested function and
    tests can construct logs with the same code path the producer uses.

    Atomicity: a single os.write() of <BUFSIZE bytes is atomic on
    POSIX. We assume sub-PIPE_BUF (4096) line size — if a future event
    exceeds that, the line is still complete because we use O_APPEND
    semantics and a single write() call. POSIX guarantees O_APPEND
    seeks-to-end-and-writes as one operation.
    """
    if not is_valid_stream_id(stream_id):
        raise ValueError(f"invalid stream_id: {stream_id!r}")

    chat_jobs_dir = Path(chat_jobs_dir)
    chat_jobs_dir.mkdir(parents=True, exist_ok=True)
    path = chat_jobs_dir / f"{stream_id}.jsonl"

    record = {
        "seq": seq,
        "event": event,
        "data": data,
        "ts": ts if ts is not None else time.time(),
    }
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)

    return path.stat().st_size
