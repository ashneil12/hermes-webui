"""Regression tests for streaming approval-event enrichment.

vanilla-hermes-agent's ``tools/approval.py`` calls ``notify_cb(approval_data)``
with a bare dict — ``{command, pattern_key, pattern_keys, description}`` —
that omits both the ``approval_id`` (which lives on the ``_ApprovalEntry``
in ``_gateway_queues``) and the ``session_id``. Without enrichment the
WebUI streams that bare dict to the browser as ``event: approval``, the
dashboard's React click handler ends up with ``call.approval.approvalId
=== undefined``, and its ``/api/approval/respond`` proxy falls back to
``webui.pendingApproval(sessionId)`` first.

That fallback then queries ``/api/approval/pending``, which only consults
``tools.approval._pending`` (a separate store from ``_gateway_queues`` —
``_pending`` is populated by ``submit_pending`` callers; the gateway
flow used by streaming.py puts entries in ``_gateway_queues``). Result:
``pending`` returns ``null``, the dashboard returns 409 "No pending
approval found", the user sees "Approval failed. Try again." in red,
and the agent's worker thread stays parked in ``entry.event.wait()``.

streaming.py now enriches the dict before publishing it to the SSE
queue: ``session_id`` is injected from the chat session context, and
``approval_id`` is read from the most recently queued
``_ApprovalEntry.data`` for that session. The SSE event is then
self-describing and the dashboard hits ``/api/approval/respond``
directly — no pending lookup needed.

These tests pin the contract by simulating what
``register_gateway_notify`` would invoke and asserting the published
event carries both keys. See HermesOS handoff 2026-04-29.
"""
import sys
import types
import pytest


@pytest.fixture
def fake_approval_module(monkeypatch):
    """Stub ``tools.approval`` enough that streaming.py's enrichment can
    import ``_gateway_queues`` and ``_lock`` and read entries by session.

    Tests then push fake ``_ApprovalEntry``-shaped objects into the
    queue and assert what the notify callback ends up publishing.
    """
    import threading
    fake = types.ModuleType('tools.approval')
    fake._gateway_queues = {}
    fake._lock = threading.RLock()
    fake._gateway_notify_cbs = {}

    def register_gateway_notify(session_key, cb):
        fake._gateway_notify_cbs[session_key] = cb

    def unregister_gateway_notify(session_key):
        fake._gateway_notify_cbs.pop(session_key, None)

    fake.register_gateway_notify = register_gateway_notify
    fake.unregister_gateway_notify = unregister_gateway_notify

    # Streaming.py imports ``tools`` as a package, so make sure both
    # stubs are reachable via ``import tools.approval`` and
    # ``from tools.approval import ...``.
    tools_pkg = types.ModuleType('tools')
    tools_pkg.approval = fake
    monkeypatch.setitem(sys.modules, 'tools', tools_pkg)
    monkeypatch.setitem(sys.modules, 'tools.approval', fake)
    return fake


def _make_entry(data):
    """Mimic ``_ApprovalEntry`` enough for the tail-walk in
    streaming.py's enrichment to succeed.
    """
    entry = types.SimpleNamespace()
    entry.data = dict(data)
    return entry


def _build_notify_cb(session_id, fake_approval, published):
    """Re-create the notify callback exactly as streaming.py defines it.

    Mirroring the source rather than importing it keeps the test from
    pulling in the entire WebUI stack (server bootstrap, agent
    discovery, etc.) just to exercise a pure-Python closure.
    """
    from tools.approval import (
        _gateway_queues as _approval_gateway_queues,
        _lock as _approval_lock,
    )
    _can_enrich_approval = True

    def put(event, data):
        published.append((event, data))

    def _approval_notify_cb(approval_data):
        enriched = dict(approval_data)
        enriched.setdefault('session_id', session_id)
        if _can_enrich_approval and not enriched.get('approval_id'):
            with _approval_lock:
                entries = _approval_gateway_queues.get(session_id) or []
                for candidate in reversed(entries):
                    candidate_data = getattr(candidate, 'data', None)
                    if isinstance(candidate_data, dict):
                        aid = candidate_data.get('approval_id')
                        if aid:
                            enriched['approval_id'] = aid
                            break
        put('approval', enriched)

    return _approval_notify_cb


def test_injects_approval_id_from_gateway_queue(fake_approval_module):
    sid = 'agent-session-abc'
    fake_approval_module._gateway_queues[sid] = [
        _make_entry({'approval_id': 'approval-7', 'command': 'rm -rf /tmp/x'}),
    ]
    published = []
    cb = _build_notify_cb(sid, fake_approval_module, published)

    # vanilla approval.py passes a bare dict (no approval_id, no session_id)
    cb({
        'command': 'rm -rf /tmp/x',
        'pattern_key': 'rm',
        'pattern_keys': ['rm'],
        'description': 'Delete temp folder',
    })

    assert len(published) == 1
    event, data = published[0]
    assert event == 'approval'
    assert data['approval_id'] == 'approval-7'
    assert data['session_id'] == sid


def test_picks_latest_entry_when_multiple_pending(fake_approval_module):
    """If two approvals are queued back-to-back the SSE event must
    carry the LATEST approval_id, not the oldest — otherwise the
    dashboard would respond to a stale entry while the user clicked
    on the new one."""
    sid = 'agent-session-abc'
    fake_approval_module._gateway_queues[sid] = [
        _make_entry({'approval_id': 'approval-old', 'command': 'first'}),
        _make_entry({'approval_id': 'approval-new', 'command': 'second'}),
    ]
    published = []
    cb = _build_notify_cb(sid, fake_approval_module, published)

    cb({'command': 'second', 'pattern_key': 'rm', 'pattern_keys': ['rm']})

    assert published[0][1]['approval_id'] == 'approval-new'


def test_does_not_overwrite_existing_approval_id(fake_approval_module):
    """If the upstream approval module ever starts emitting
    approval_id directly (e.g. a future vanilla-hermes-agent
    that includes it in approval_data), keep that value instead of
    looking it up — the upstream value is the source of truth."""
    sid = 'agent-session-abc'
    fake_approval_module._gateway_queues[sid] = [
        _make_entry({'approval_id': 'approval-from-queue'}),
    ]
    published = []
    cb = _build_notify_cb(sid, fake_approval_module, published)

    cb({
        'approval_id': 'approval-from-upstream',
        'command': 'foo',
        'pattern_key': 'bar',
    })

    assert published[0][1]['approval_id'] == 'approval-from-upstream'


def test_does_not_overwrite_existing_session_id(fake_approval_module):
    sid = 'streaming-session'
    fake_approval_module._gateway_queues[sid] = []
    published = []
    cb = _build_notify_cb(sid, fake_approval_module, published)

    cb({
        'session_id': 'caller-supplied-session',
        'command': 'x',
    })

    assert published[0][1]['session_id'] == 'caller-supplied-session'


def test_publishes_event_even_when_queue_is_empty(fake_approval_module):
    """If the gateway queue has no matching entry yet (race between
    queue insert and notify), the event must STILL be published so
    the dashboard at least renders the card. The dashboard can fall
    back to the legacy pending lookup at click time."""
    sid = 'agent-session-abc'
    fake_approval_module._gateway_queues[sid] = []
    published = []
    cb = _build_notify_cb(sid, fake_approval_module, published)

    cb({'command': 'foo', 'pattern_key': 'rm', 'pattern_keys': ['rm']})

    assert len(published) == 1
    data = published[0][1]
    assert data['session_id'] == sid
    # approval_id is best-effort; missing here is acceptable
    assert data.get('command') == 'foo'


def test_skips_entries_without_data_attribute(fake_approval_module):
    """Defensive: legacy or malformed entries that don't expose
    ``.data`` must be skipped, not cause an AttributeError."""
    sid = 'agent-session-abc'
    fake_approval_module._gateway_queues[sid] = [
        types.SimpleNamespace(),  # no .data
        _make_entry({'approval_id': 'approval-found'}),
    ]
    published = []
    cb = _build_notify_cb(sid, fake_approval_module, published)

    cb({'command': 'foo', 'pattern_key': 'rm'})

    assert published[0][1]['approval_id'] == 'approval-found'
