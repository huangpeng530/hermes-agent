"""Live-process reasoning-stall auto-continue for the Desktop/TUI backend.

Second "workflow just stops" failure class next to the transient stream drop: a turn that
reported "complete" but whose visible answer is a truncated planning monologue (reasoning-only
clean stop AFTER real tool work). The agent core stamps ``result['reasoning_only_stall']`` for
exactly this; ``_maybe_schedule_reasoning_stall_auto_continue`` re-queues one continuation
nudge so the unfinished task keeps going, bounded per session by
``desktop.reasoning_stall_auto_continue.max_attempts``. A stall turn must NOT reset its own
breaker budget (only a genuine non-stall completion does).
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


def _allow_admission(monkeypatch):
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


def _stall_result(**extra):
    d = {
        "reasoning_only_stall": True,
        "completed": True,
        "failed": False,
        "messages": [{"role": "user", "content": "keep fixing the GTA5 mod"}],
    }
    d.update(extra)
    return d


# ── recovery note ──────────────────────────────────────────────────────

def test_stall_note_uses_continue_wording_and_prefix():
    note = server._reasoning_stall_note()
    assert note.startswith(_AUTO_CONTINUE_NOTE_PREFIX)
    assert "CONTINUE from the first" in note
    assert "do NOT re-run steps" in note


def test_stall_note_with_original_task_appends_anchor():
    # 2026-09-23 19:34: a bg-review turn polluted the live context, so the generic note let the
    # model continue the WRONG task (skill review) and clean-stop it. Carrying the original human
    # prompt in the note pins the continuation to the real task.
    note = server._reasoning_stall_note("keep fixing the GTA5 mod")
    assert note.startswith(_AUTO_CONTINUE_NOTE_PREFIX)
    assert "The user's current task was:" in note
    assert "keep fixing the GTA5 mod" in note
    assert "ignore any background/auxiliary work" in note
    # the generic base wording is still present (anchor is appended, not a replacement)
    assert "CONTINUE from the first" in note


def test_stall_note_without_original_is_byte_identical():
    # No human task in history (image-only / no history yet): byte-identical to the pre-anchor
    # form, so those sessions' behavior is unchanged.
    assert server._reasoning_stall_note() == server._reasoning_stall_note(None)
    assert "The user's current task was:" not in server._reasoning_stall_note("   ")


def test_stall_note_truncates_runaway_original():
    long = "x" * 5000
    note = server._reasoning_stall_note(long)
    # the 500-char cap keeps the anchor from bloating the cache-stable nudge
    assert len(note) < len("x" * 5000)
    assert "The user's current task was:" in note


# ── config reader ──────────────────────────────────────────────────────

def test_stall_config_reader_defaults(monkeypatch):
    _fake_cfg(monkeypatch, {})
    assert server._reasoning_stall_auto_continue_config() == (True, 3)


def test_stall_config_reader_reads_section(monkeypatch):
    _fake_cfg(monkeypatch, {"desktop": {"reasoning_stall_auto_continue": {"enabled": False, "max_attempts": 2}}})
    assert server._reasoning_stall_auto_continue_config() == (False, 2)


# ── scheduler ──────────────────────────────────────────────────────────

def test_stall_failure_schedules_continuation(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls, events = [], []
    _fake_run_prompt_submit(monkeypatch, calls)
    _fake_emit(monkeypatch, events)
    session = _session(running=False)

    server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())

    assert session.get("running") is True
    assert session["_reasoning_stall_attempts"]["sk-1"] == 1
    assert len(calls) == 1 and calls[0]["kw"].get("display_kind") == "auto_continue"
    assert calls[0]["text"].startswith(_AUTO_CONTINUE_NOTE_PREFIX)
    # original task handed off for a later crash marker
    assert session["_auto_continue_prompt"] == "keep fixing the GTA5 mod"
    assert any(e[0] == "status.update" and "stalled mid-thinking" in str(e[2]) for e in events)


def test_non_stall_result_noop(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)
    # a plain successful turn (no reasoning_only_stall flag) never schedules
    server._maybe_schedule_reasoning_stall_auto_continue(
        "rid", "sid", session, {"completed": True, "final_response": "done", "messages": []})
    assert not calls


def test_failed_transient_result_noop_for_stall(monkeypatch):
    # The stall scheduler must not fire on a FAILED turn (that is the transient path's class);
    # and the transient scheduler must not fire on a stall-only result. Two classes, one flag each.
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)
    server._maybe_schedule_reasoning_stall_auto_continue(
        "rid", "sid", session, {"failed": True, "failure_reason": "timeout", "messages": []})
    assert not calls


def test_stall_attempt_budget_enforced(monkeypatch):
    _fake_cfg(monkeypatch, {"desktop": {"reasoning_stall_auto_continue": {"enabled": True, "max_attempts": 2}}})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)
    # model the real lifecycle: each failed/stall turn releases the session before post-turn
    # follow-ups run, so reset running between simulated stall turns.
    server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())
    session["running"] = False
    server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())
    assert session["_reasoning_stall_attempts"]["sk-1"] == 2
    assert len(calls) == 2
    # third stall over budget: no dispatch
    session["running"] = False
    server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())
    assert len(calls) == 2
    assert session.get("running") is False


def test_disabled_and_zero_budget_disable_scheduling(monkeypatch):
    for cfg in ({"desktop": {"reasoning_stall_auto_continue": {"enabled": False, "max_attempts": 3}}},
                {"desktop": {"reasoning_stall_auto_continue": {"enabled": True, "max_attempts": 0}}}):
        _fake_cfg(monkeypatch, cfg)
        _allow_admission(monkeypatch)
        calls = []
        _fake_run_prompt_submit(monkeypatch, calls)
        session = _session(running=False)
        server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())
        assert not calls, cfg
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
    server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())
    assert not calls


def test_bot_room_sessions_never_scheduled(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False, source="bot_room")
    server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())
    assert not calls
    assert session.get("running") is False


if __name__ == "__main__":
    pass
