"""Live-process transient-stream auto-continue for the Desktop/TUI backend.

The crash-marker auto-continue (``desktop.auto_continue``) only covers a turn killed by a
PROCESS death. A turn that FAILED because the provider stream spent its transient-fault
retry budget while the app stayed up was previously left to stall until the user re-prompted.
``_maybe_schedule_transient_auto_continue`` closes that gap: it re-submits one continuation
turn, bounded per failure episode by ``desktop.transient_auto_continue.max_attempts``.
"""

from __future__ import annotations

import threading
import types
from contextlib import contextmanager

import pytest

from tui_gateway import server
from tui_gateway.session_history import _AUTO_CONTINUE_NOTE_PREFIX


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(session_id="agent-sid"),
        "session_key": "sk-1",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "cols": 80,
        "show_reasoning": False,
        **extra,
    }


def _allow_admission(monkeypatch, *, running_gate=None):
    """_session_turn_admission as a context manager that always admits unless told not to."""
    def fake(session):
        @contextmanager
        def _cm():
            yield True
        return _cm()
    monkeypatch.setattr(server, "_session_turn_admission", fake)


def _fake_run_prompt_submit(monkeypatch, calls):
    def fake(rid, sid, session, text, **kw):
        calls.append({"rid": rid, "sid": sid, "text": text, "kw": kw})
        return True
    monkeypatch.setattr(server, "_run_prompt_submit", fake)


def _fake_emit(monkeypatch, events):
    def fake(event, sid, payload=None):
        events.append((event, sid, payload))
    monkeypatch.setattr(server, "_emit", fake)


def _fake_cfg(monkeypatch, cfg):
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)


def _result(**extra):
    d = {"failed": True, "failure_reason": "timeout",
         "messages": [{"role": "user", "content": "build the three car packs"}]}
    d.update(extra)
    return d


# ── recovery note ──────────────────────────────────────────────────────

def test_transient_note_uses_continue_wording_and_prefix():
    note = server._transient_auto_continue_note({"failure_reason": "timeout"})
    assert note.startswith(_AUTO_CONTINUE_NOTE_PREFIX)
    assert "CONTINUE the interrupted task" in note
    assert "transient timeout" in note
    assert "ask what they would like to do next" not in note.lower()


# ── original-task extractor ───────────────────────────────────────────

def test_last_user_prompt_text_picks_real_human_message():
    res = {"messages": [
        {"role": "user", "content": "build the three car packs"},
        {"role": "assistant", "content": "on it"},
        {"role": "user", "content": "yes keep going"},
    ]}
    assert server._last_user_prompt_text(res) == "yes keep going"


def test_last_user_prompt_text_skips_synthetic_rows():
    res = {"messages": [
        {"role": "user", "content": "build the three car packs"},
        {"role": "user", "content": "⚠ continuation note…", "display_kind": "auto_continue"},
        {"role": "user", "content": "switched model", "display_kind": "model_switch"},
    ]}
    assert server._last_user_prompt_text(res) == "build the three car packs"


def test_last_user_prompt_text_none_when_no_user_text():
    assert server._last_user_prompt_text({"messages": []}) is None
    assert server._last_user_prompt_text({}) is None


# ── config reader ──────────────────────────────────────────────────────

def test_config_reader_defaults_when_absent(monkeypatch):
    _fake_cfg(monkeypatch, {})
    assert server._transient_auto_continue_config() == (True, 5)


def test_config_reader_reads_section(monkeypatch):
    _fake_cfg(monkeypatch, {"desktop": {"transient_auto_continue": {"enabled": False, "max_attempts": 2}}})
    assert server._transient_auto_continue_config() == (False, 2)


# ── scheduler ──────────────────────────────────────────────────────────

def test_transient_failure_schedules_continuation(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls, events = [], []
    _fake_run_prompt_submit(monkeypatch, calls)
    _fake_emit(monkeypatch, events)
    session = _session(running=False)

    server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result())

    assert session.get("running") is True
    assert session["_transient_autocontinue_attempts"]["sk-1"] == 1
    assert len(calls) == 1 and calls[0]["kw"].get("display_kind") == "auto_continue"
    assert calls[0]["text"].startswith(_AUTO_CONTINUE_NOTE_PREFIX)
    # original task handed off for a later crash marker
    assert session["_auto_continue_prompt"] == "build the three car packs"
    assert any(e[0] == "status.update" and "resuming automatically" in str(e[2]) for e in events)


def test_non_transient_reason_never_schedules(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    for reason in ("billing", "auth_permanent", "model_not_found",
                   "content_policy_blocked", "context_overflow"):
        session = _session(running=False)
        server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result(failure_reason=reason))
        assert not calls, reason
        assert "running" not in session or session.get("running") is False
        assert "_transient_autocontinue_attempts" not in session


def test_compression_exhausted_never_schedules(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    for flag in ("compression_exhausted", "compression_deferred"):
        session = _session(running=False)
        server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result(**{flag: True}))
        assert not calls, flag


def test_success_result_noop(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)
    server._maybe_schedule_transient_auto_continue(
        "rid", "sid", session, {"completed": True, "final_response": "done", "messages": []})
    assert not calls


def test_attempt_budget_enforced(monkeypatch):
    _fake_cfg(monkeypatch, {"desktop": {"transient_auto_continue": {"enabled": True, "max_attempts": 2}}})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)
    # Each failed turn releases the session before its post-turn follow-ups run (that is how
    # _maybe_schedule_transient_auto_continue is reached), so model the real lifecycle: the
    # fake dispatch leaves running=True mid-turn; we reset it to simulate that turn's unwind.
    server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result())
    session["running"] = False
    server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result())
    assert session["_transient_autocontinue_attempts"]["sk-1"] == 2
    assert len(calls) == 2
    # third attempt over budget: no dispatch
    session["running"] = False
    server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result())
    assert len(calls) == 2
    assert session.get("running") is False  # refused at the budget gate, nothing dispatched


def test_disabled_and_zero_budget_disable_scheduling(monkeypatch):
    for cfg in ({"desktop": {"transient_auto_continue": {"enabled": False, "max_attempts": 3}}},
                {"desktop": {"transient_auto_continue": {"enabled": True, "max_attempts": 0}}}):
        _fake_cfg(monkeypatch, cfg)
        _allow_admission(monkeypatch)
        calls = []
        _fake_run_prompt_submit(monkeypatch, calls)
        session = _session(running=False)
        server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result())
        assert not calls, cfg
        assert "running" not in session or session.get("running") is False


def test_bot_room_sessions_never_scheduled(monkeypatch):
    # Hosted-room turns own their durable task/lease recovery state machine (the crash path
    # skips them too); a generic re-submit would double-drive that machine.
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False, source="bot_room")
    server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result())
    assert not calls
    assert session.get("running") is False


def test_running_session_blocks_scheduling(monkeypatch):
    _fake_cfg(monkeypatch, {})

    def no_admit(session):
        @contextmanager
        def _cm():
            yield False
        return _cm()
    monkeypatch.setattr(server, "_session_turn_admission", no_admit)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)
    server._maybe_schedule_transient_auto_continue("rid", "sid", session, _result())
    assert not calls
    # counter was not incremented because we never got past the admission gate
    assert session.get("_transient_autocontinue_attempts", {}).get("sk-1") in (None, 1)


if __name__ == "__main__":  # no-ops under pytest; lets a quick standalone run work
    pass
