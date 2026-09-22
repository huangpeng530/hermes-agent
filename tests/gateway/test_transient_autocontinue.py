"""Tests for transient-stream auto-continue (live-process recovery gap).

When a gateway turn dies after the provider stream's retry budget is spent on a
TRANSIENT fault (thinking-stage ReadTimeout / peer-closed / 5xx), the session is
marked ``resume_pending`` (reason ``"stream_exhausted"``) and a synthesized empty
internal resume turn is queued so the interrupted work continues without a user
re-prompt. These cover the recovery-note wording, the auto-resume reason set, and
the scheduler's budget / gating rules.
"""

from __future__ import annotations

import pytest

from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner, build_resume_recovery_note
from gateway.run_turn import GatewayTurnMixin


# ── Recovery-note wording ──────────────────────────────────────────────────────

def test_stream_exhausted_empty_message_forces_continuation_wording():
    # Auto-synthesized resume (empty message) must CONTINUE the work even on an
    # interactive platform — that is the whole point of the feature.
    note = build_resume_recovery_note("stream_exhausted", "", interactive=True)
    assert "CONTINUE the interrupted task" in note
    assert "transient provider stream interruption" in note
    assert "ask what they would like to do next" not in note.lower()


def test_restart_timeout_interactive_wording_unchanged():
    # Regression guard: a genuine restart on an interactive platform still asks.
    note = build_resume_recovery_note("restart_timeout", "", interactive=True)
    assert "ask what they would like to do next" in note


def test_stream_exhausted_real_message_addresses_new_message():
    # A real user message arriving while resume-pending is addressed FIRST.
    note = build_resume_recovery_note("stream_exhausted", "continue?", interactive=True)
    assert "Address the user's NEW message" in note


# ── Auto-resume reason set ─────────────────────────────────────────────────────

def test_stream_exhausted_in_auto_resume_reasons():
    assert "stream_exhausted" in GatewayRunner._AUTO_RESUME_REASONS
    # Existing restart/shutdown reasons stay intact.
    for r in ("restart_timeout", "shutdown_timeout", "restart_interrupted"):
        assert r in GatewayRunner._AUTO_RESUME_REASONS


# ── Scheduler (FakeRunner) ─────────────────────────────────────────────────────

class _FakeSource:
    platform = None
    user_id = "u1"


class _FakeSessionStore:
    def __init__(self):
        self.marked = []

    async def mark_resume_pending(self, session_key, reason="restart_timeout"):
        self.marked.append((session_key, reason))
        return True


class _FakeRunner(GatewayTurnMixin):
    def __init__(self, config=(True, 2)):
        self.store = _FakeSessionStore()
        self.handlers = []
        self._config = config

    @property
    def async_session_store(self):
        return self.store

    def _transient_auto_continue_config(self):
        return self._config

    def _adapter_for_source(self, source):
        return self

    async def handle_message(self, event):
        self.handlers.append(event)


def _result(reason="timeout", **extra):
    d = {"failed": True, "failure_reason": reason, "error": "simulated"}
    d.update(extra)
    return d


@pytest.mark.asyncio
async def test_transient_failure_schedules_resume():
    runner = _FakeRunner(config=(True, 2))
    src = _FakeSource()
    sk = "agent:wechat:u1"
    out = await runner._hmwa_maybe_transient_auto_continue(_result(), src, sk, None)
    assert out == (1, 2)
    assert runner.store.marked == [(sk, "stream_exhausted")]
    assert len(runner.handlers) == 1
    ev = runner.handlers[0]
    assert isinstance(ev, MessageEvent) and ev.internal and ev.text == "" and ev.source is src


@pytest.mark.asyncio
async def test_attempt_budget_enforced():
    runner = _FakeRunner(config=(True, 2))
    src, sk = _FakeSource(), "sk-budget"
    assert await runner._hmwa_maybe_transient_auto_continue(_result("timeout"), src, sk, None) == (1, 2)
    assert await runner._hmwa_maybe_transient_auto_continue(_result("server_error"), src, sk, None) == (2, 2)
    # 3rd attempt: budget spent -> no more scheduling, marker still set (durable).
    assert await runner._hmwa_maybe_transient_auto_continue(_result("unknown"), src, sk, None) is None
    assert len(runner.handlers) == 2
    assert runner.store.marked[-1] == (sk, "stream_exhausted")


@pytest.mark.asyncio
async def test_non_transient_reasons_never_schedule():
    runner = _FakeRunner()
    src = _FakeSource()
    for reason in ("billing", "auth_permanent", "model_not_found",
                   "content_policy_blocked", "context_overflow"):
        out = await runner._hmwa_maybe_transient_auto_continue(_result(reason), src, f"sk-{reason}", None)
        assert out is None, reason
    assert not runner.handlers
    assert not runner.store.marked


@pytest.mark.asyncio
async def test_disabled_and_zero_budget_disable_scheduling():
    for cfg in ((False, 3), (True, 0)):
        runner = _FakeRunner(config=cfg)
        src, sk = _FakeSource(), f"sk-{cfg}"
        out = await runner._hmwa_maybe_transient_auto_continue(_result(), src, sk, None)
        assert out is None
        # The durable marker is still recorded so the next message resumes.
        assert runner.store.marked
        assert not runner.handlers


@pytest.mark.asyncio
async def test_compression_exhausted_excluded():
    runner = _FakeRunner()
    out = await runner._hmwa_maybe_transient_auto_continue(
        _result("unknown", compression_exhausted=True), _FakeSource(), "sk-overflow", None)
    assert out is None
    assert not runner.handlers


def test_success_path_resets_counter():
    runner = _FakeRunner()
    budget = runner._transient_autocontinue_budget()
    budget["sk"] = 2
    assert budget.get("sk") == 2
    budget.pop("sk", None)
    assert "sk" not in budget


# ── Config reader (real _load_gateway_config path, monkeypatched) ─────────────

@pytest.mark.asyncio
async def test_config_reader_reads_gateway_section(monkeypatch):
    # Use a runner that does NOT override _transient_auto_continue_config so the REAL
    # config-reading path (gateway.run._load_gateway_config) is exercised.
    import gateway.run as gprun
    from gateway.run_turn import GatewayTurnMixin as _M

    class _RealConfigRunner(_M):
        @property
        def async_session_store(self):
            return _FakeSessionStore()

        def _adapter_for_source(self, source):
            return self

        async def handle_message(self, event):
            pass

    runner = _RealConfigRunner()

    holder = {"cfg": None}

    def _fake_load(config_path=None):
        return holder["cfg"]

    monkeypatch.setattr(gprun, "_load_gateway_config", _fake_load)
    holder["cfg"] = {"gateway": {"transient_auto_continue": {"enabled": False, "max_attempts": 5}}}
    assert runner._transient_auto_continue_config() == (False, 5)
    holder["cfg"] = {"gateway": {}}
    # Absent section -> defaults (enabled, 3).
    assert runner._transient_auto_continue_config() == (True, 3)
    holder["cfg"] = {}
    assert runner._transient_auto_continue_config() == (True, 3)
