"""Regression tests for get_effective_default_model priority order.

The WebUI's BASE LLM OVERRIDE flow writes the user's selection to
config.yaml's `model.default` via /api/default-model. Earlier this
function let HERMES_MODEL / OPENAI_MODEL / LLM_MODEL env vars override
config.yaml — but the env vars are set ONCE at provision time (e.g. by
HermesOS deploys baking HERMES_MODEL=deepseek-v4-pro into the container
.env), so any later UI selection silently never took effect. The agent
kept running with the provision-time model regardless of what the user
picked, sessions stamped s.model = provision-time-model at creation,
and chats hit whatever provider was rate-limited that day.

The fix: config.yaml WINS over env. Env is only the fallback for fresh
installs (no config.yaml yet) and for one-shot CLI invocations where
the user explicitly sets HERMES_MODEL=... on the command line.

This file pins that priority order so a future refactor can't quietly
revert it. See HermesOS handoff 2026-04-29.
"""
import pytest
from api import config as cfg


@pytest.fixture
def clear_model_env(monkeypatch):
    """Strip every model-override env var so each test sees a clean slate."""
    for key in ("HERMES_MODEL", "OPENAI_MODEL", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_config_wins_over_hermes_model_env(monkeypatch, clear_model_env):
    """config.yaml model.default beats HERMES_MODEL env var.

    This is the headline case — HermesOS sets HERMES_MODEL at provision
    time, then the user picks something else in BASE LLM OVERRIDE which
    persists to config.yaml. The user's later pick must win.
    """
    monkeypatch.setenv("HERMES_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(cfg, "DEFAULT_MODEL", "deepseek-v4-pro")
    cfg_data = {"model": {"default": "claude-opus-4-7"}}
    assert cfg.get_effective_default_model(cfg_data) == "claude-opus-4-7"


def test_config_wins_over_openai_model_env(monkeypatch, clear_model_env):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-3.5-turbo")
    cfg_data = {"model": {"default": "claude-opus-4-7"}}
    assert cfg.get_effective_default_model(cfg_data) == "claude-opus-4-7"


def test_config_wins_over_llm_model_env(monkeypatch, clear_model_env):
    monkeypatch.setenv("LLM_MODEL", "some-other-model")
    cfg_data = {"model": {"default": "claude-opus-4-7"}}
    assert cfg.get_effective_default_model(cfg_data) == "claude-opus-4-7"


def test_string_form_model_cfg_also_wins_over_env(monkeypatch, clear_model_env):
    """Older config.yaml may have `model: <name>` as a bare string instead
    of a dict. That form must also beat env vars."""
    monkeypatch.setenv("HERMES_MODEL", "deepseek-v4-pro")
    cfg_data = {"model": "claude-opus-4-7"}
    assert cfg.get_effective_default_model(cfg_data) == "claude-opus-4-7"


def test_env_used_when_config_has_no_model(monkeypatch, clear_model_env):
    """Fresh install path — config.yaml has no model section at all, so
    HERMES_MODEL is the legitimate fallback."""
    monkeypatch.setenv("HERMES_MODEL", "fallback-model")
    monkeypatch.setattr(cfg, "DEFAULT_MODEL", "")
    assert cfg.get_effective_default_model({}) == "fallback-model"


def test_env_used_when_config_default_is_empty_string(monkeypatch, clear_model_env):
    """Config has model section but default is blank — env still wins
    over the (also-blank) HERMES_WEBUI_DEFAULT_MODEL."""
    monkeypatch.setenv("HERMES_MODEL", "fallback-model")
    monkeypatch.setattr(cfg, "DEFAULT_MODEL", "")
    cfg_data = {"model": {"default": ""}}
    assert cfg.get_effective_default_model(cfg_data) == "fallback-model"


def test_env_priority_among_themselves(monkeypatch, clear_model_env):
    """When all three legacy env vars are set, HERMES_MODEL wins (the
    `or` chain checks HERMES_MODEL first). Pinning this so a future
    refactor doesn't silently rearrange the order."""
    monkeypatch.setenv("HERMES_MODEL", "first")
    monkeypatch.setenv("OPENAI_MODEL", "second")
    monkeypatch.setenv("LLM_MODEL", "third")
    monkeypatch.setattr(cfg, "DEFAULT_MODEL", "")
    assert cfg.get_effective_default_model({}) == "first"


def test_default_model_constant_used_when_nothing_else_set(monkeypatch, clear_model_env):
    """Last-resort fallback: HERMES_WEBUI_DEFAULT_MODEL (= DEFAULT_MODEL)."""
    monkeypatch.setattr(cfg, "DEFAULT_MODEL", "baked-in-fallback")
    assert cfg.get_effective_default_model({}) == "baked-in-fallback"


def test_returns_empty_string_when_truly_no_default(monkeypatch, clear_model_env):
    """If nothing is set anywhere, return empty string rather than crashing."""
    monkeypatch.setattr(cfg, "DEFAULT_MODEL", "")
    assert cfg.get_effective_default_model({}) == ""


def test_default_to_global_cfg_when_no_arg_passed(monkeypatch, clear_model_env):
    """Calling with no config_data arg must read from the module-level cfg
    cache — that's how /api/settings populates the response."""
    monkeypatch.setattr(cfg, "DEFAULT_MODEL", "from-default-constant")
    saved = dict(cfg.cfg)
    try:
        cfg.cfg.clear()
        cfg.cfg.update({"model": {"default": "from-cached-cfg"}})
        assert cfg.get_effective_default_model() == "from-cached-cfg"
    finally:
        cfg.cfg.clear()
        cfg.cfg.update(saved)
