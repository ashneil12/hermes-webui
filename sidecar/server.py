"""Hermes chat-jobs sidecar — HTTP server.

Endpoints
---------
GET /chat-jobs/health
    -> 200 {"ok": true, "version": "<webui_version>"}

GET /chat-jobs/<stream_id>/events?cursor=<offset>
    -> 200 SSE stream. Replays all complete events from `cursor` (or 0)
       to current EOF, then tails the file for new appends until a
       terminator event, idle timeout, or client disconnect.
    -> 404 if log file doesn't exist
    -> 400 if stream_id is invalid
    -> 416 if cursor exceeds current EOF

The server is intentionally tiny. It owns no chat lifecycle state and
issues no writes. The WebUI's streaming engine appends events to the
JSONL log; this process serves them as cursor-resumable SSE.

Usage
-----
    python -m sidecar.server

Env vars
--------
HERMES_SIDECAR_HOST    bind address (default 0.0.0.0)
HERMES_SIDECAR_PORT    listen port (default 8788)
HERMES_WEBUI_STATE_DIR base webui state dir (chat-jobs/ lives under it)
HERMES_CHAT_JOBS_DIR   override the chat-jobs/ location explicitly
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .log_reader import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    encode_sse_comment,
    encode_sse_frame,
    is_valid_stream_id,
    log_path,
    read_from_cursor,
    tail_events,
)

logger = logging.getLogger("hermes.sidecar")

DEFAULT_PORT = 8788


def resolve_chat_jobs_dir() -> Path:
    """Resolve the chat-jobs directory from env, with the same defaults
    the WebUI uses for STATE_DIR."""
    explicit = os.getenv("HERMES_CHAT_JOBS_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    state_dir = os.getenv("HERMES_WEBUI_STATE_DIR")
    if state_dir:
        return (Path(state_dir).expanduser() / "chat-jobs").resolve()
    home = Path.home()
    return (home / ".hermes" / "webui" / "chat-jobs").resolve()


def _resolve_version() -> str:
    """Reuse the WebUI's baked-in version string when available so the
    sidecar's /health response identifies the agent image generation."""
    try:
        from api._version import __version__ as v  # type: ignore
        return str(v)
    except Exception:
        return "unknown"


class SidecarHandler(BaseHTTPRequestHandler):
    """Handles GET /chat-jobs/* requests.

    Pulled out as its own class so tests can mount it on a dynamic-port
    server without going through the WebUI's auth/profile machinery.
    """

    server_version = "HermesSidecar/" + _resolve_version()
    chat_jobs_dir: Path = Path("/tmp/hermes-sidecar-unset")
    poll_interval: float = DEFAULT_POLL_INTERVAL
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT

    def log_message(self, fmt, *args):
        # Match webui style — quiet by default, structured one-line per request.
        pass

    def log_request(self, code: str = "-", size: str = "-") -> None:
        duration_ms = round((time.time() - getattr(self, "_t0", time.time())) * 1000, 1)
        record = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": self.command or "-",
            "path": self.path or "-",
            "status": int(code) if str(code).isdigit() else code,
            "ms": duration_ms,
        })
        print(f"[sidecar] {record}", flush=True)

    # ──────────────────────────────────────────────────────────────────
    # Routing
    # ──────────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802 (stdlib name)
        self._t0 = time.time()
        try:
            parsed = urlparse(self.path or "")
            path = parsed.path

            # The dashboard mints signed URLs at /api/chat-jobs/...; the
            # bare /chat-jobs/... form is supported for direct probes and
            # tests that bypass the dashboard's URL convention. Strip the
            # /api prefix here so a single match arm handles both.
            if path.startswith("/api/chat-jobs/"):
                path = path[len("/api"):]

            if path == "/chat-jobs/health":
                return self._handle_health()

            # /chat-jobs/<stream_id>/events
            if path.startswith("/chat-jobs/") and path.endswith("/events"):
                stream_id = path[len("/chat-jobs/"):-len("/events")]
                return self._handle_events(stream_id, parsed.query)

            return self._send_json(404, {"error": "not found"})
        except Exception:
            logger.exception("sidecar: unhandled error for %s", self.path)
            try:
                return self._send_json(500, {"error": "internal error"})
            except Exception:
                return

    def do_HEAD(self) -> None:  # noqa: N802
        # Some health-check probes use HEAD; route it through GET.
        self.do_GET()

    # ──────────────────────────────────────────────────────────────────
    # Endpoint handlers
    # ──────────────────────────────────────────────────────────────────
    def _handle_health(self) -> None:
        self._send_json(200, {
            "ok": True,
            "version": _resolve_version(),
            "chat_jobs_dir": str(self.chat_jobs_dir),
        })

    def _handle_events(self, stream_id: str, query: str) -> None:
        if not is_valid_stream_id(stream_id):
            return self._send_json(400, {"error": "invalid stream_id"})

        qs = parse_qs(query)
        cursor_str = (qs.get("cursor") or ["0"])[0]
        try:
            cursor = int(cursor_str)
        except ValueError:
            return self._send_json(400, {"error": "invalid cursor"})

        path = log_path(self.chat_jobs_dir, stream_id)
        if not path.exists():
            return self._send_json(404, {"error": "stream not found"})

        # Validate cursor against current size up front so a confused client
        # gets a clean 416 instead of a hung connection. tail_events() will
        # also catch this on its first read, but a synchronous reply is
        # better when we already know the answer.
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return self._send_json(404, {"error": "stream not found"})
        if cursor < 0 or cursor > size:
            return self._send_json(
                416,
                {"error": "cursor out of range", "size": size, "cursor": cursor},
            )

        # Begin SSE response. Tell the connection to close on EOF so that
        # urllib (and any HTTP/1.1 client without explicit framing) sees
        # the stream end as a clean socket close instead of waiting for
        # more bytes on a kept-alive connection.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        last_write = time.monotonic()
        try:
            for frame in tail_events(
                path,
                start_cursor=cursor,
                poll_interval=self.poll_interval,
                idle_timeout=self.idle_timeout,
            ):
                self.wfile.write(encode_sse_frame(frame))
                self.wfile.flush()
                last_write = time.monotonic()

                # Inject heartbeat comments between events when idle.
                # tail_events() handles its own polling but doesn't know
                # about heartbeats; we emit one if the gap since the last
                # write exceeded the heartbeat interval.
                if time.monotonic() - last_write > self.heartbeat_interval:
                    self.wfile.write(encode_sse_comment("heartbeat"))
                    self.wfile.flush()
                    last_write = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────
    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class QuietSidecarServer(ThreadingHTTPServer):
    """Threaded HTTP server that swallows the noisy disconnect errors
    typical of long-lived SSE clients."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc_type, exc_value, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        if exc_type is socket.error and getattr(exc_value, "errno", None) in (54, 104, 32):
            return
        super().handle_error(request, client_address)


def make_handler(
    chat_jobs_dir: Path,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
) -> type[SidecarHandler]:
    """Build a Handler subclass with config bound as class attributes
    (BaseHTTPRequestHandler is instantiated per request, so config has
    to live on the class)."""

    class _Bound(SidecarHandler):
        pass

    _Bound.chat_jobs_dir = Path(chat_jobs_dir)
    _Bound.poll_interval = poll_interval
    _Bound.heartbeat_interval = heartbeat_interval
    _Bound.idle_timeout = idle_timeout
    return _Bound


def run(
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    chat_jobs_dir: Path | None = None,
) -> None:
    if chat_jobs_dir is None:
        chat_jobs_dir = resolve_chat_jobs_dir()

    # Best-effort directory creation. The sidecar runs read-only against
    # this directory; the WebUI's tee in streaming.py does the actual
    # writes (and creates the directory on first append). On a cold-start
    # where the WebUI's STATE_DIR isn't yet provisioned (e.g. fresh
    # container, sidecar starts ahead of webui), mkdir may fail with
    # PermissionError. Don't crash — webui will materialize the path
    # and the sidecar will start serving as soon as a stream exists.
    try:
        chat_jobs_dir.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileNotFoundError) as e:
        print(
            f"[sidecar] note: chat_jobs_dir not yet writable ({e}); "
            f"continuing — WebUI will create it on first event",
            flush=True,
        )

    handler = make_handler(chat_jobs_dir)
    server = QuietSidecarServer((host, port), handler)
    print(
        f"[sidecar] listening on {host}:{port} chat_jobs_dir={chat_jobs_dir}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[sidecar] shutting down", flush=True)
    finally:
        server.server_close()


def main() -> None:
    host = os.getenv("HERMES_SIDECAR_HOST", "0.0.0.0")
    port = int(os.getenv("HERMES_SIDECAR_PORT", str(DEFAULT_PORT)))
    run(host=host, port=port)


if __name__ == "__main__":
    main()
