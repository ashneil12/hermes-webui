"""Hermes Web UI -- startup helpers."""
from __future__ import annotations
import json
import logging
import os, stat, subprocess, sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Credential files that should never be world-readable
_SENSITIVE_FILES = (
    '.env',
    'google_token.json',
    'google_client_secret.json',
    '.signing_key',
    'auth.json',
)


def fix_credential_permissions() -> None:
    """Ensure sensitive files in HERMES_HOME are chmod 600 (owner-only)."""
    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    if not hermes_home.is_dir():
        return
    for name in _SENSITIVE_FILES:
        fpath = hermes_home / name
        if not fpath.exists():
            continue
        try:
            current = stat.S_IMODE(fpath.stat().st_mode)
            if current & 0o077:  # group or other bits set
                fpath.chmod(0o600)
                print(f'  [security] fixed permissions on {fpath.name} ({oct(current)} -> 0600)', flush=True)
        except OSError:
            pass  # best-effort; don't abort startup


def _agent_dir() -> Path | None:
    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    for raw in [os.environ.get('HERMES_WEBUI_AGENT_DIR', '').strip(), str(hermes_home / 'hermes-agent')]:
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.is_dir():
            return p.resolve()
    return None

def _trusted_agent_dir(agent_dir: Path) -> bool:
    """Return True if agent_dir passes ownership and permission checks.

    Validates that the directory is not world- or group-writable and,
    on POSIX systems, is owned by the current process user.

    Intentionally does NOT enforce a canonical path (i.e. does not require
    the dir to be ~/.hermes/hermes-agent), so custom HERMES_WEBUI_AGENT_DIR
    paths work correctly when HERMES_WEBUI_AUTO_INSTALL=1 is set.
    """
    try:
        st = agent_dir.stat()
        if stat.S_IMODE(st.st_mode) & 0o022:
            # World- or group-writable — untrusted
            return False
        if hasattr(os, 'getuid') and st.st_uid != os.getuid():
            # Not owned by current user (POSIX only; Windows fallback skips)
            return False
        return True
    except OSError:
        return False


def auto_install_agent_deps() -> bool:
    enabled = os.environ.get('HERMES_WEBUI_AUTO_INSTALL', '').strip().lower() in ('1', 'true', 'yes')
    if not enabled:
        print('[!!] Auto-install disabled. Set HERMES_WEBUI_AUTO_INSTALL=1 to enable.', flush=True)
        return False
    agent_dir = _agent_dir()
    if agent_dir is None:
        print('[!!] Auto-install skipped: agent directory not found.', flush=True)
        return False
    if not _trusted_agent_dir(agent_dir):
        print('[!!] Auto-install skipped: agent directory failed trust check (check ownership/permissions).', flush=True)
        return False
    req_file = agent_dir / 'requirements.txt'
    pyproject = agent_dir / 'pyproject.toml'
    if req_file.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', '-r', str(req_file)]
        print(f'     Installing from {req_file} ...', flush=True)
    elif pyproject.exists():
        install_args = [sys.executable, '-m', 'pip', 'install', '--quiet', str(agent_dir)]
        print(f'     Installing from {agent_dir} (pyproject.toml) ...', flush=True)
    else:
        print('[!!] Auto-install skipped: no requirements.txt or pyproject.toml in agent dir.', flush=True)
        return False
    try:
        result = subprocess.run(install_args, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f'[!!] pip install failed (exit {result.returncode}):', flush=True)
            for line in (result.stderr or '').splitlines()[-10:]:
                print(f'     {line}', flush=True)
            return False
        print('[ok] pip install completed.', flush=True)
        return True
    except subprocess.TimeoutExpired:
        print('[!!] Auto-install timed out after 120s.', flush=True)
        return False
    except Exception as e:
        print(f'[!!] Auto-install error: {e}', flush=True)
        return False


def sweep_stale_inflight_state() -> int:
    """Clear stale in-flight bookkeeping from all on-disk sessions at boot.

    A server restart or crash during a streaming turn leaves
    ``active_stream_id`` set on the session JSON, since STREAMS lives in
    memory only. The frontend reads that field on page load and renders a
    stuck "THINKING..." indicator that survives refresh.

    At server boot ``STREAMS`` is empty by definition, so any on-disk
    ``active_stream_id`` is stale. We use the session index as a fast
    pre-filter so we only load+save the (small) set of sessions that
    actually need cleaning.

    Returns the number of sessions cleaned up.
    """
    try:
        from api.models import (
            SESSION_DIR,
            SESSION_INDEX_FILE,
            Session,
            clear_stale_inflight_state,
        )
    except Exception:
        # Models module not importable (e.g. during certain test setups);
        # skip silently — this is a best-effort housekeeping pass.
        logger.debug("sweep_stale_inflight_state: models import failed", exc_info=True)
        return 0

    if not SESSION_DIR.exists():
        return 0

    candidates: list[str] = []
    if SESSION_INDEX_FILE.exists():
        try:
            index = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
            if isinstance(index, list):
                candidates = [
                    e['session_id']
                    for e in index
                    if isinstance(e, dict)
                    and e.get('active_stream_id')
                    and e.get('session_id')
                ]
        except Exception:
            # Index missing/corrupt — fall through to full scan
            candidates = []

    if not candidates:
        # Index didn't help — scan only files that have active_stream_id set.
        for p in SESSION_DIR.glob('*.json'):
            if p.name.startswith('_'):
                continue
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                continue
            if isinstance(data, dict) and data.get('active_stream_id'):
                candidates.append(p.stem)

    cleaned = 0
    empty: set = set()  # STREAMS is empty at boot — any active_stream_id is stale
    for sid in candidates:
        try:
            s = Session.load(sid)
            if s and clear_stale_inflight_state(s, active_stream_ids=empty):
                cleaned += 1
        except Exception:
            logger.debug(
                "sweep_stale_inflight_state: failed for session=%s", sid, exc_info=True,
            )
            continue

    if cleaned:
        print(f'  [cleanup] cleared stale streaming state on {cleaned} session(s)', flush=True)

    return cleaned
