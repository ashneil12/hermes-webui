"""Tests for ``api.startup.seed_bundled_skills_on_startup``.

Covers the three ways the helper can exit:
1. Skipped via ``HERMES_SKIP_SKILLS_SEED`` env var.
2. Skipped because no agent dir / bundled skills dir exists.
3. Successful seed: bundled skills (incl. blockchain optional skills) end up
   under ``$HERMES_HOME/skills/`` and the manifest reflects them.

The third test is the one that matters in production — it locks in the
behaviour that fleets booting after this PR will get the upstream-bundled
skill set installed automatically, which is the whole point.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_skill(skill_dir: Path, name: str, body: str = "Body text.\n") -> None:
    """Write a minimal SKILL.md so it's discoverable by skills_sync."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}.\n---\n\n{body}",
        encoding="utf-8",
    )


def test_seed_skipped_by_env_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SKIP_SKILLS_SEED", "1")

    from api.startup import seed_bundled_skills_on_startup
    result = seed_bundled_skills_on_startup()

    assert result is None
    out = capsys.readouterr().out
    assert "skipped" in out.lower()


def test_seed_skipped_when_agent_dir_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    """No ``hermes-agent/skills/`` dir → skip and log, do not raise."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_WEBUI_AGENT_DIR", raising=False)
    monkeypatch.delenv("HERMES_SKIP_SKILLS_SEED", raising=False)

    from api.startup import seed_bundled_skills_on_startup
    result = seed_bundled_skills_on_startup()

    assert result is None
    out = capsys.readouterr().out
    assert "agent dir not found" in out or "no bundled skills dir" in out


def test_seed_copies_bundled_and_blockchain_skills(tmp_path: Path, monkeypatch, capsys) -> None:
    """Realistic-shaped agent dir → seed lands in ``~/.hermes/skills/`` with
    a populated manifest, and the optional blockchain skills get staged into
    the bundle (so Bankr's Solana + Base ride the same flow)."""
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    agent_dir = hermes_home / "hermes-agent"
    bundled = agent_dir / "skills"
    optional_blockchain = agent_dir / "optional-skills" / "blockchain"

    # Two upstream-bundled skills under different categories
    _make_skill(bundled / "devops" / "commit", "commit")
    _make_skill(bundled / "productivity" / "notion", "notion")

    # Two optional blockchain skills (the Bankr suite shape)
    _make_skill(optional_blockchain / "solana", "solana")
    _make_skill(optional_blockchain / "base", "base")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(agent_dir))
    monkeypatch.delenv("HERMES_SKIP_SKILLS_SEED", raising=False)

    # The fixture doesn't have vanilla-hermes-agent on sys.path; bring along
    # the project's vendored copy if present, otherwise stub sync_skills with
    # a minimal implementation so we still exercise the pre-staging logic.
    from api import startup

    sync_calls: list[dict] = []

    def fake_sync_skills(quiet: bool = True) -> dict:
        # Mirror the real sync's return shape so the wrapper's logging works.
        # Walk bundle and copy missing skills → exercise the same pre-stage
        # path the production code relies on.
        import shutil
        bundled_root = Path(os.environ["HERMES_BUNDLED_SKILLS"])
        skills_dir = Path(os.environ["HERMES_HOME"]) / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        total = 0
        for skill_md in bundled_root.rglob("SKILL.md"):
            total += 1
            rel = skill_md.parent.relative_to(bundled_root)
            dest = skills_dir / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(skill_md.parent, dest)
                copied.append(skill_md.parent.name)
        # Write a v1 manifest (one name per line)
        names = sorted({d.name for d in skills_dir.rglob("*") if (d / "SKILL.md").exists()})
        (skills_dir / ".bundled_manifest").write_text(
            "\n".join(names) + "\n", encoding="utf-8"
        )
        record = {
            "copied": copied,
            "updated": [],
            "skipped": 0,
            "user_modified": [],
            "cleaned": [],
            "total_bundled": total,
        }
        sync_calls.append(record)
        return record

    # Patch the lazy import inside seed_bundled_skills_on_startup
    import types
    fake_module = types.ModuleType("tools.skills_sync")
    fake_module.sync_skills = fake_sync_skills  # type: ignore[attr-defined]

    # tools package may not exist either — provide a stub
    fake_tools_pkg = types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.skills_sync", fake_module)

    result = startup.seed_bundled_skills_on_startup()

    # Sync was called with our fake
    assert result is not None
    assert len(sync_calls) == 1
    assert result["total_bundled"] == 4  # 2 bundled + 2 blockchain (after staging)

    # Bundle was augmented with blockchain (production code did the cp)
    assert (bundled / "blockchain" / "solana" / "SKILL.md").exists()
    assert (bundled / "blockchain" / "base" / "SKILL.md").exists()

    # Skills dir got everything copied
    skills_dir = hermes_home / "skills"
    assert (skills_dir / "devops" / "commit" / "SKILL.md").exists()
    assert (skills_dir / "productivity" / "notion" / "SKILL.md").exists()
    assert (skills_dir / "blockchain" / "solana" / "SKILL.md").exists()
    assert (skills_dir / "blockchain" / "base" / "SKILL.md").exists()

    # Manifest got written
    manifest = (skills_dir / ".bundled_manifest").read_text(encoding="utf-8")
    assert "commit" in manifest
    assert "notion" in manifest
    assert "solana" in manifest
    assert "base" in manifest


def test_seed_idempotent_when_blockchain_already_staged(tmp_path: Path, monkeypatch) -> None:
    """If ``hermes-agent/skills/blockchain`` already exists, the helper must
    not error or clobber it — shutil.copytree refuses an existing dst, and
    our wrapper short-circuits before calling it."""
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()
    agent_dir = hermes_home / "hermes-agent"
    bundled = agent_dir / "skills"
    bundled.mkdir(parents=True)
    optional = agent_dir / "optional-skills" / "blockchain"
    _make_skill(optional / "solana", "solana")

    # Pre-create blockchain in the bundle dir with a sentinel file so we
    # can verify it isn't overwritten.
    bundled_blockchain = bundled / "blockchain"
    bundled_blockchain.mkdir()
    (bundled_blockchain / "SENTINEL").write_text("preexisting", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_WEBUI_AGENT_DIR", str(agent_dir))
    monkeypatch.delenv("HERMES_SKIP_SKILLS_SEED", raising=False)

    # Stub sync_skills so we don't depend on vanilla-hermes-agent
    import types
    fake_module = types.ModuleType("tools.skills_sync")
    fake_module.sync_skills = lambda quiet=True: {  # type: ignore[attr-defined]
        "copied": [], "updated": [], "skipped": 0,
        "user_modified": [], "cleaned": [], "total_bundled": 0,
    }
    fake_tools_pkg = types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", fake_tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.skills_sync", fake_module)

    from api.startup import seed_bundled_skills_on_startup
    seed_bundled_skills_on_startup()  # Must not raise

    # Sentinel preserved
    assert (bundled_blockchain / "SENTINEL").read_text() == "preexisting"
