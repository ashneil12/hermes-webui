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
    """Ensure sensitive files in HERMES_HOME have safe permissions.

    Respects:
      - HERMES_SKIP_CHMOD=1  → bypass entirely
      - HERMES_HOME_MODE     → group bits are allowed if set by the operator,
                               only world-readable/world-writable files are fixed
    """
    if os.environ.get('HERMES_SKIP_CHMOD', '').strip() in ('1', 'true'):
        return

    # Parse operator-declared mode to know if group bits are intentional
    declared_mode = None
    raw_mode = os.environ.get('HERMES_HOME_MODE', '').strip()
    if raw_mode:
        try:
            declared_mode = int(raw_mode, 8)
        except ValueError:
            pass

    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    if not hermes_home.is_dir():
        return
    for name in _SENSITIVE_FILES:
        fpath = hermes_home / name
        if not fpath.exists():
            continue
        try:
            current = stat.S_IMODE(fpath.stat().st_mode)
            # If operator declared a mode, allow group bits but still fix world bits
            if declared_mode is not None:
                if current & 0o007:  # other bits set (world-readable/writable)
                    fpath.chmod(current & ~0o007)
                    print(f'  [security] removed world bits on {fpath.name} ({oct(current)} -> {oct(current & ~0o007)})', flush=True)
            else:
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


def seed_bundled_skills_on_startup() -> dict | None:
    """Seed the upstream Hermes Agent's bundled skills into ``~/.hermes/skills/``.

    The vanilla-hermes-agent package ships ~89 SKILL.md files in
    ``hermes-agent/skills/`` and another ~60 in ``hermes-agent/optional-skills/``.
    Its CLI seeds them via ``hermes_cli.profiles.seed_profile_skills()``, but
    that path only fires from interactive ``hermes`` commands — when the agent
    boots under hermes-webui, the seed never runs and tenants end up with
    only the handful of user-authored skills they create themselves.

    This helper closes that gap by calling ``tools.skills_sync.sync_skills``
    with ``HERMES_BUNDLED_SKILLS`` pointed at the agent's ``skills/`` dir.
    Bankr's blockchain skills (Solana, Base) live under
    ``optional-skills/blockchain/`` upstream — we stage them into the bundle
    dir before sync so they ride the same flow without modifying upstream.

    Idempotent: ``sync_skills`` short-circuits skills already in the
    on-disk manifest with matching hashes. Safe to run on every boot.

    Failures are logged but do not raise — startup must not be blocked by
    a problem in skill seeding.

    Honors:
      - ``HERMES_SKIP_SKILLS_SEED=1``  bypass entirely
      - ``HERMES_WEBUI_AGENT_DIR``     override agent dir (defaults to
                                        ``$HERMES_HOME/hermes-agent``)

    Returns the dict from ``sync_skills`` (with keys ``copied``,
    ``updated``, ``skipped``, ``user_modified``, ``cleaned``,
    ``total_bundled``) on success, or ``None`` if seeding was skipped.
    """
    if os.environ.get("HERMES_SKIP_SKILLS_SEED", "").strip() in ("1", "true", "yes"):
        print("[skills] seed skipped (HERMES_SKIP_SKILLS_SEED set)", flush=True)
        return None

    agent_dir = _agent_dir()
    if agent_dir is None:
        print("[skills] seed skipped: agent dir not found", flush=True)
        return None

    bundled_dir = agent_dir / "skills"
    if not bundled_dir.is_dir():
        print(f"[skills] seed skipped: no bundled skills dir at {bundled_dir}", flush=True)
        return None

    # Stage selected ``optional-skills/`` subdirs into the bundle so the
    # existing sync flow picks them up alongside the rest. Each entry is
    # a one-shot copy: if the destination already exists we leave it
    # alone so any updates the user (or a later vanilla release) made
    # are not clobbered. ``shutil.copytree`` refuses an existing
    # destination, which is exactly what we want here.
    #
    # What we stage and why:
    #   - blockchain/  Nous-bundled Solana + Base skills.
    #   - bankr/       BankrBot suite (~37 skills) vendored upstream so
    #                  every agent ships with the Bankr stack pre-
    #                  installed instead of routing through the dashboard
    #                  catalog one skill at a time.
    #
    # NOT staged: the rest of optional-skills/ (research/, security/,
    # email/, communication/, etc.) — those remain opt-in via the
    # dashboard's catalog "Install" flow because we don't want to
    # auto-install ~60 skills the user didn't ask for.
    import shutil
    for staged_name in ("blockchain", "bankr"):
        src = agent_dir / "optional-skills" / staged_name
        if not src.is_dir():
            continue
        dest = bundled_dir / staged_name
        if dest.exists():
            continue
        try:
            shutil.copytree(src, dest)
            count = sum(1 for _ in dest.rglob("SKILL.md"))
            print(
                f"[skills] staged optional-skills/{staged_name} ({count} skills) into {dest}",
                flush=True,
            )
        except OSError as e:
            # Non-fatal — sync_skills will still pick up the rest
            print(f"[!!] failed to stage optional-skills/{staged_name}: {e}", flush=True)

    # Point the upstream sync at the (now-augmented) bundle dir. The env
    # var is read by tools.skills_sync._get_bundled_dir, so we don't have
    # to plumb it through any other way.
    os.environ["HERMES_BUNDLED_SKILLS"] = str(bundled_dir)

    try:
        # Imported lazily so a missing/broken vanilla-hermes-agent install
        # surfaces here as a logged warning rather than a top-level
        # ImportError that would tank the entire startup module.
        from tools.skills_sync import sync_skills  # type: ignore[import-not-found]
    except ImportError as e:
        print(f"[!!] skills seed unavailable: {e}", flush=True)
        return None

    try:
        result = sync_skills(quiet=True)
    except Exception as e:  # noqa: BLE001 - never block startup on this
        print(f"[!!] skills seed failed: {e}", flush=True)
        return None

    copied = len(result.get("copied", []) or [])
    updated = len(result.get("updated", []) or [])
    user_modified = len(result.get("user_modified", []) or [])
    total = result.get("total_bundled", 0)
    if copied or updated or user_modified:
        print(
            f"[skills] seed: copied={copied} updated={updated} "
            f"user_modified={user_modified} total_bundled={total}",
            flush=True,
        )
    else:
        # Quiet path on the common case (every boot after the first).
        # Verbose enough for grep but not noisy in scrollback.
        print(f"[skills] seed: up to date ({total} bundled)", flush=True)
    return result


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
