"""Owed auto-continue: a continuation a competing turn BLOCKED is parked and re-fired, not dropped.

Reproduces the 2026-09-20 11:58 incident: the main task turn (22 tool turns) ended in a
reasoning-only clean stop while a machine notification turn (process_complete / bg-review)
grabbed the session slot. The live schedulers' admission gate saw ``running=True`` and
returned silently — the owed attempt 2/3 was lost and the task stalled. Now:

- ``_session_has_human_turn_driving`` — a HUMAN turn (inflight without display_kind) owns the
  task (it continues manually); a MACHINE turn (display_kind set) only holds the slot, so the
  continuation stays OWED.
- ``_record_owed_auto_continue`` — parks the owed continuation on the session (called with the
  history_lock HELD; the lock is non-reentrant, so it must not acquire it again).
- ``_flush_owed_auto_continue`` — re-fires the parked continuation at an idle boundary (driven
  by the notification poller ~every 0.5s), bounded by the same per-episode attempt counter, with
  a parking freshness window so a long-idle task gets a fresh nudge instead of a stale one.
"""

from __future__ import annotations

import threading
import time
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


def _machine_session(display_kind="process_complete", **extra):
    """A session a MACHINE turn owns the slot of: running=True and the inflight turn carries a
    display_kind — exactly the 11:58 contention shape (process_complete / auto_continue turns)."""
    extra.pop("running", None)  # this shape is always running=True; ignore a redundant override
    return _session(running=True, inflight_turn={
        "assistant": "", "streaming": True, "user": "[IMPORTANT: Background process ...]",
        "display_kind": display_kind,
    }, **extra)


def _human_session(**extra):
    """A HUMAN turn owns the slot: running=True, inflight WITHOUT a display_kind."""
    return _session(running=True, inflight_turn={
        "assistant": "", "streaming": True, "user": "keep working on the GTA5 mod",
    }, **extra)


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


def _transient_result(**extra):
    d = {
        "failed": True,
        "failure_reason": "timeout",
        "messages": [{"role": "user", "content": "keep fixing the GTA5 mod"}],
    }
    d.update(extra)
    return d


def _park_owed(session, kind="reasoning_stall", age=0.0, count=0, original="keep fixing the GTA5 mod"):
    """Seed a parked owed marker the same shape _record_owed_auto_continue produces."""
    session["_owed_auto_continue"] = {
        "kind": kind,
        "note": _AUTO_CONTINUE_NOTE_PREFIX + " — park",
        "original": original,
        "at": time.time() - age,
        "count": count,
        "max": 3,
    }


# ── human vs machine classifier ──────────────────────────────────────

def test_classifier_human_vs_machine():
    assert server._session_has_human_turn_driving(_human_session()) is True
    assert server._session_has_human_turn_driving(_machine_session()) is False
    # No inflight at all (idle, or a slot claimed before its marker was written): NOT a human.
    assert server._session_has_human_turn_driving(_session(running=True)) is False
    assert server._session_has_human_turn_driving(_session()) is False


# ── schedulers: machine-held slot parks an owed continuation ────────

def test_stall_scheduler_parks_owed_when_machine_holds_slot(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls, events = [], []
    _fake_run_prompt_submit(monkeypatch, calls)
    _fake_emit(monkeypatch, events)
    session = _machine_session()

    server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())

    # NOT dispatched (a turn owns the slot) and NOT dropped: parked for the idle boundary, with
    # the attempt counter untouched (a blocked record must not spend budget).
    assert not calls and session.get("running") is True
    assert session.get("_reasoning_stall_attempts", {}).get("sk-1") is None
    owed = session.get("_owed_auto_continue")
    assert owed is not None and owed["kind"] == "reasoning_stall"
    assert owed["original"] == "keep fixing the GTA5 mod"
    assert any(e[0] == "status.update" for e in events) is False  # no user-visible dispatch
    # the marker carries a fresh parking timestamp for the staleness window
    assert 0 <= time.time() - owed["at"] < 5


def test_transient_scheduler_parks_owed_when_machine_holds_slot(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _machine_session(display_kind="auto_continue")

    server._maybe_schedule_transient_auto_continue("rid", "sid", session, _transient_result())

    assert not calls
    assert session.get("_transient_autocontinue_attempts", {}).get("sk-1") is None
    owed = session.get("_owed_auto_continue")
    assert owed is not None and owed["kind"] == "transient"
    assert owed["original"] == "keep fixing the GTA5 mod"


def test_stall_scheduler_silent_takeup_when_human_holds_slot(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _human_session()

    server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())

    # A HUMAN is driving the task — they continue it manually; nothing dispatched AND nothing parked.
    assert not calls
    assert "_owed_auto_continue" not in session
    assert session.get("_reasoning_stall_attempts", {}).get("sk-1") is None


# ── record: no reentrant lock, human-guard respected ────────────────

def test_record_is_lock_safe_and_parks(monkeypatch):
    """Called WITH the history_lock held (both call sites are inside _session_turn_admission,
    which holds the non-reentrant lock) — must not acquire it again or it self-deadlocks."""
    session = _machine_session()
    with session["history_lock"]:
        server._record_owed_auto_continue(session, "sid", "reasoning_stall", "note", "orig", 0, 3)
    assert session["_owed_auto_continue"]["kind"] == "reasoning_stall"
    # a HUMAN driving refuses the park
    session2 = _human_session()
    with session2["history_lock"]:
        server._record_owed_auto_continue(session2, "sid", "transient", "note", None, 0, 3)
    assert "_owed_auto_continue" not in session2


# ── flush: idle boundary re-fires the parked continuation ───────────

def test_flush_dispatches_parked_owed_when_idle(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _allow_admission(monkeypatch)
    calls, events = [], []
    _fake_run_prompt_submit(monkeypatch, calls)
    _fake_emit(monkeypatch, events)
    session = _session(running=False)
    _park_owed(session)

    server._flush_owed_auto_continue("rid", "sid", session)

    assert len(calls) == 1 and calls[0]["kw"].get("display_kind") == "auto_continue"
    assert calls[0]["text"].startswith(_AUTO_CONTINUE_NOTE_PREFIX)
    assert session.get("running") is True
    assert session["_reasoning_stall_attempts"]["sk-1"] == 1
    assert "_owed_auto_continue" not in session  # consumed
    assert session["_auto_continue_prompt"] == "keep fixing the GTA5 mod"
    assert any(e[0] == "status.update" and "stalled mid-thinking" in str(e[2]) for e in events)


def test_flush_noop_without_marker(monkeypatch):
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session()
    server._flush_owed_auto_continue("rid", "sid", session)
    assert not calls and session.get("running") is False


def test_flush_keeps_marker_while_session_running(monkeypatch):
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)
    _park_owed(session)
    session["running"] = True  # a turn is live again — retry at the next idle poll
    server._flush_owed_auto_continue("rid", "sid", session)
    assert not calls and "_owed_auto_continue" in session and session.get("running") is True


def test_flush_keeps_marker_while_human_driving(monkeypatch):
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _human_session()
    _park_owed(session)
    server._flush_owed_auto_continue("rid", "sid", session)
    assert not calls and "_owed_auto_continue" in session  # a human took over — keep, don't fire


def test_flush_drops_stale_marker(monkeypatch):
    _fake_cfg(monkeypatch, {})
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)
    _park_owed(session, age=901.0)  # parked longer than the 900s freshness window
    server._flush_owed_auto_continue("rid", "sid", session)
    assert not calls and "_owed_auto_continue" not in session and session.get("running") is False


def test_flush_drops_when_budget_spent(monkeypatch):
    _fake_cfg(monkeypatch, {"desktop": {"reasoning_stall_auto_continue": {"max_attempts": 3}}})
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False, _reasoning_stall_attempts={"sk-1": 3})
    _park_owed(session, count=3)
    server._flush_owed_auto_continue("rid", "sid", session)
    assert not calls and "_owed_auto_continue" not in session


def test_flush_release_running_on_dispatch_failure(monkeypatch):
    _fake_cfg(monkeypatch, {})
    _fake_hook = []

    def raise_submit(rid, sid, session, text, **kw):
        raise RuntimeError("backend refused")
    monkeypatch.setattr(server, "_run_prompt_submit", raise_submit)
    monkeypatch.setattr(server, "_hook_failure", lambda what, exc: _fake_hook.append(what))
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: None)
    session = _session(running=False)
    _park_owed(session)
    server._flush_owed_auto_continue("rid", "sid", session)
    assert session.get("running") is False and _fake_hook


# ── the anti-loop invariant: owed re-fires cannot exceed the budget ──

def test_owed_loop_is_bounded_by_attempt_budget(monkeypatch):
    """A continuation that stalls AGAIN while a machine keeps holding the slot must terminate:
    at most max_attempts dispatches total, then the budget gate stops records and the parked
    marker (if any) is dropped by the flush."""
    _fake_cfg(monkeypatch, {"desktop": {"reasoning_stall_auto_continue": {"max_attempts": 3}}})
    _allow_admission(monkeypatch)
    calls = []
    _fake_run_prompt_submit(monkeypatch, calls)
    session = _session(running=False)

    for stall_round in range(4):
        # The task turn stalls while a machine notification turn holds the slot.
        session.update(_machine_session(running=True))
        session["running"] = True
        server._maybe_schedule_reasoning_stall_auto_continue("rid", "sid", session, _stall_result())
        # The machine turn then finishes; the poller's idle boundary flushes what is owed.
        session["running"] = False
        session["inflight_turn"] = None
        server._flush_owed_auto_continue("rid", "sid", session)
        session["running"] = False  # the flushed continuation turn itself ends

    assert len(calls) == 3, f"expected exactly 3 continuations (budget), got {len(calls)}"
    assert session["_reasoning_stall_attempts"]["sk-1"] == 3
    assert "_owed_auto_continue" not in session  # the 4th stall saw a spent budget: no marker
