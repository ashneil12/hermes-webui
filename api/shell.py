"""PTY-backed terminal sessions for the WebUI terminal panel.

Endpoints (all auth-gated by the global check_auth before dispatch):
  POST /api/shell/new       → spawn a PTY shell, return shell_id
  GET  /api/shell/stream    → SSE stream of output (base64 chunks + seq)
  POST /api/shell/input     → write bytes to PTY master fd
  POST /api/shell/resize    → TIOCSWINSZ
  POST /api/shell/close     → SIGHUP child + close fd

Default-on for loopback hosts; opt-in via HERMES_WEBUI_ENABLE_SHELL=1 otherwise.
Each shell holds a small ring buffer so an SSE reconnect can resume from `?since=N`.
"""
import base64
import fcntl
import json as _json
import os
import pty
import select
import signal
import struct
import termios
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

from api.config import DEFAULT_WORKSPACE, HOST
from api.helpers import j

_SHELLS_LOCK = threading.RLock()
_SHELLS: dict = {}              # id -> Shell
_MAX_SHELLS = 8
_BUFFER_BYTES = 1_000_000       # 1 MB ring buffer per shell
_IDLE_TIMEOUT_S = 1800          # 30 min of inactivity → reaped


def shells_enabled() -> bool:
    val = os.environ.get("HERMES_WEBUI_ENABLE_SHELL")
    if val is not None:
        return val.strip().lower() in ("1", "true", "yes", "on")
    return HOST in ("127.0.0.1", "::1", "localhost")


def _resolve_cwd(requested: str | None) -> str:
    """Resolve cwd against DEFAULT_WORKSPACE; fall back to it on error."""
    base = Path(str(DEFAULT_WORKSPACE)).expanduser().resolve()
    if not requested:
        return str(base)
    try:
        p = Path(requested).expanduser().resolve()
    except Exception:
        return str(base)
    return str(p) if p.exists() and p.is_dir() else str(base)


class Shell:
    def __init__(self, cwd: str | None = None, cols: int = 120, rows: int = 30) -> None:
        self.id = uuid.uuid4().hex[:16]
        self.cols = max(20, min(500, int(cols or 120)))
        self.rows = max(5, min(200, int(rows or 30)))
        self.created = time.time()
        self.last_active = self.created
        self.closed = False
        self.buf = bytearray()
        self.seq = 0
        self.buf_start_seq = 0
        self.cv = threading.Condition()

        resolved_cwd = _resolve_cwd(cwd)
        shell_path = os.environ.get("SHELL") or "/bin/bash"
        merged_env = os.environ.copy()
        merged_env["TERM"] = "xterm-256color"
        merged_env["COLORTERM"] = "truecolor"
        merged_env.pop("PYTHONHOME", None)
        merged_env.pop("PYTHONPATH", None)

        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # child
            try:
                os.chdir(resolved_cwd)
            except Exception:
                pass
            try:
                os.execvpe(shell_path, [shell_path, "-l"], merged_env)
            except Exception:
                os._exit(127)
        # parent
        self._set_size(self.rows, self.cols)
        self.cwd = resolved_cwd
        self._reader_thread = threading.Thread(target=self._reader, daemon=True, name=f"shell-{self.id}")
        self._reader_thread.start()

    # ── private ────────────────────────────────────────────────────────────
    def _set_size(self, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def _reader(self) -> None:
        while not self.closed:
            try:
                r, _, _ = select.select([self.fd], [], [], 1.0)
                if not r:
                    continue
                data = os.read(self.fd, 65536)
                if not data:
                    self.close()
                    return
                with self.cv:
                    self.buf.extend(data)
                    self.seq += len(data)
                    if len(self.buf) > _BUFFER_BYTES:
                        drop = len(self.buf) - _BUFFER_BYTES
                        del self.buf[:drop]
                        self.buf_start_seq += drop
                    self.last_active = time.time()
                    self.cv.notify_all()
            except OSError:
                self.close()
                return
            except Exception:
                continue

    # ── public ─────────────────────────────────────────────────────────────
    def write(self, data: bytes) -> None:
        if self.closed:
            return
        try:
            os.write(self.fd, data)
            self.last_active = time.time()
        except OSError:
            self.close()

    def resize(self, rows: int, cols: int) -> None:
        self.rows = max(5, min(200, int(rows)))
        self.cols = max(20, min(500, int(cols)))
        self._set_size(self.rows, self.cols)

    def read_since(self, since_seq: int, timeout: float = 15.0) -> tuple[bytes, int]:
        """Block until new data past `since_seq` or timeout. Returns (chunk, new_seq)."""
        deadline = time.time() + timeout
        with self.cv:
            while not self.closed:
                if since_seq < self.buf_start_seq:
                    since_seq = self.buf_start_seq
                if since_seq < self.seq:
                    start = since_seq - self.buf_start_seq
                    chunk = bytes(self.buf[start:])
                    return chunk, self.seq
                left = deadline - time.time()
                if left <= 0:
                    return b"", self.seq
                self.cv.wait(timeout=left)
            return b"", self.seq

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        with self.cv:
            self.cv.notify_all()


# ── garbage collection ────────────────────────────────────────────────────
def _gc() -> None:
    now = time.time()
    with _SHELLS_LOCK:
        dead = [k for k, s in _SHELLS.items() if s.closed or (now - s.last_active > _IDLE_TIMEOUT_S)]
        for k in dead:
            try:
                _SHELLS[k].close()
            except Exception:
                pass
            _SHELLS.pop(k, None)


# ── HTTP handlers ─────────────────────────────────────────────────────────
def handle_new(handler, body: dict):
    if not shells_enabled():
        return j(handler, {"error": "Shell disabled. Set HERMES_WEBUI_ENABLE_SHELL=1."}, status=403)
    _gc()
    cwd = body.get("cwd") if isinstance(body, dict) else None
    cols = (body or {}).get("cols", 120)
    rows = (body or {}).get("rows", 30)
    with _SHELLS_LOCK:
        if len(_SHELLS) >= _MAX_SHELLS:
            return j(handler, {"error": "Too many shells open"}, status=429)
        try:
            sh = Shell(cwd=cwd, cols=cols, rows=rows)
        except Exception as e:
            return j(handler, {"error": f"Failed to spawn shell: {e}"}, status=500)
        _SHELLS[sh.id] = sh
    return j(handler, {"shell_id": sh.id, "cwd": sh.cwd, "rows": sh.rows, "cols": sh.cols})


def handle_stream(handler, parsed) -> bool:
    if not shells_enabled():
        return j(handler, {"error": "Shell disabled"}, status=403)
    qs = parse_qs(parsed.query)
    sid = (qs.get("id") or [""])[0]
    since = int((qs.get("since") or ["0"])[0])
    with _SHELLS_LOCK:
        sh = _SHELLS.get(sid)
    if not sh:
        return j(handler, {"error": "shell not found"}, status=404)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-store")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    try:
        handler.wfile.write(b"event: hello\ndata: {}\n\n")
        handler.wfile.flush()
        cur = since
        while True:
            data, new_seq = sh.read_since(cur, timeout=12.0)
            if sh.closed and not data:
                handler.wfile.write(b"event: closed\ndata: {}\n\n")
                handler.wfile.flush()
                return True
            if data:
                b64 = base64.b64encode(data).decode("ascii")
                payload = _json.dumps({"seq": new_seq, "b64": b64})
                handler.wfile.write(f"event: data\ndata: {payload}\n\n".encode("utf-8"))
                handler.wfile.flush()
                cur = new_seq
            else:
                handler.wfile.write(b": ping\n\n")
                handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        return True
    except Exception:
        return True


def handle_input(handler, body: dict):
    if not shells_enabled():
        return j(handler, {"error": "Shell disabled"}, status=403)
    sid = (body or {}).get("id") or ""
    data = (body or {}).get("data") or ""
    with _SHELLS_LOCK:
        sh = _SHELLS.get(sid)
    if not sh:
        return j(handler, {"error": "shell not found"}, status=404)
    if isinstance(data, str):
        sh.write(data.encode("utf-8"))
    elif isinstance(data, list):
        # base64-encoded bytes (e.g. binary keys)
        try:
            sh.write(base64.b64decode("".join(data)))
        except Exception:
            return j(handler, {"error": "bad data"}, status=400)
    return j(handler, {"ok": True})


def handle_resize(handler, body: dict):
    if not shells_enabled():
        return j(handler, {"error": "Shell disabled"}, status=403)
    sid = (body or {}).get("id") or ""
    rows = int((body or {}).get("rows") or 30)
    cols = int((body or {}).get("cols") or 120)
    with _SHELLS_LOCK:
        sh = _SHELLS.get(sid)
    if not sh:
        return j(handler, {"error": "shell not found"}, status=404)
    sh.resize(rows, cols)
    return j(handler, {"ok": True})


def handle_close(handler, body: dict):
    sid = (body or {}).get("id") or ""
    with _SHELLS_LOCK:
        sh = _SHELLS.pop(sid, None)
    if sh:
        sh.close()
    return j(handler, {"ok": True})


def handle_status(handler):
    """GET /api/shell/status → enabled flag + count, used by the UI."""
    with _SHELLS_LOCK:
        n = len(_SHELLS)
    return j(handler, {"enabled": shells_enabled(), "count": n, "max": _MAX_SHELLS})
