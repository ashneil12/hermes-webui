from pathlib import Path
from types import SimpleNamespace

import pytest

from api import routes


def test_terminal_start_uses_existing_session_workspace(monkeypatch, tmp_path):
    chat_workspace = tmp_path / "chat-workspace"
    chat_workspace.mkdir()

    def fake_get_session(session_id):
        assert session_id == "chat-session"
        return SimpleNamespace(workspace=str(chat_workspace))

    resolved = []

    def fake_resolve_trusted_workspace(raw_path):
        resolved.append(raw_path)
        return Path(raw_path)

    monkeypatch.setattr(routes, "get_session", fake_get_session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve_trusted_workspace)

    session_id, workspace = routes._terminal_session_and_workspace({
        "session_id": "chat-session",
    })

    assert session_id == "chat-session"
    assert workspace == chat_workspace
    assert resolved == [str(chat_workspace)]


def test_terminal_start_uses_explicit_workspace_when_session_missing(monkeypatch, tmp_path):
    explicit_workspace = tmp_path / "dashboard-workspace"
    explicit_workspace.mkdir()

    def fake_get_session(session_id):
        assert session_id == "dashboard-terminal"
        raise KeyError("Session not found")

    resolved = []

    def fake_resolve_trusted_workspace(raw_path):
        resolved.append(raw_path)
        return Path(raw_path)

    monkeypatch.setattr(routes, "get_session", fake_get_session)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", fake_resolve_trusted_workspace)

    session_id, workspace = routes._terminal_session_and_workspace({
        "session_id": "dashboard-terminal",
        "workspace": str(explicit_workspace),
    })

    assert session_id == "dashboard-terminal"
    assert workspace == explicit_workspace
    assert resolved == [str(explicit_workspace)]
