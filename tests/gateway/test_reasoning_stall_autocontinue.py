"""Gateway-side reasoning-stall auto-continue (``_hmwa_maybe_reasoning_stall_auto_continue``).

Same second failure class as the desktop patch, on the messaging-gateway turn path: a turn
that reported "complete" but stopped mid-planning-monologue (agent-core flag
``reasoning_only_stall``) queues one synthesized internal continuation (the adapter FIFO
drains it after the turn) and marks the session ``resume_pending`` (reason
``"reasoning_stall"``) so a later crash/boot still recovers it. Bounded per episode by
``gateway.reasoning_stall_auto_continue.max_attempts``.
"""

from __future__ import annotations

import pytest

from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.run_turn import GatewayTurnMixin


def test_reasoning_stall_in_auto_resume_reasons():
    assert "reasoning_stall" in GatewayRunner._AUTO_RESUME_REASONS


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

    def _reasoning_stall_auto_continue_config(self):
        return self._config

    def _adapter_for_source(self, source):
        return self

    async def handle_message(self, event):
        self.handlers.append(event)


def _stall_result(**extra):
    d = {"reasoning_only_stall": True, "completed": True, "failed": False}
    d.update(extra)
    return d


@pytest.mark.asyncio
async def test_stall_result_schedules_continuation():
    runner = _FakeRunner(config=(True, 2))
    src, sk = _FakeSource(), "sk-stall"
    out = await runner._hmwa_maybe_reasoning_stall_auto_continue(_stall_result(), src, sk, None)
    assert out == (1, 2)
    assert runner.store.marked == [(sk, "reasoning_stall")]
    assert len(runner.handlers) == 1
    ev = runner.handlers[0]
    assert isinstance(ev, MessageEvent) and ev.internal and ev.text == ""


@pytest.mark.asyncio
async def test_stall_attempt_budget_enforced():
    runner = _FakeRunner(config=(True, 2))
    src, sk = _FakeSource(), "sk-budget"
    assert await runner._hmwa_maybe_reasoning_stall_auto_continue(_stall_result(), src, sk, None) == (1, 2)
    assert await runner._hmwa_maybe_reasoning_stall_auto_continue(_stall_result(), src, sk, None) == (2, 2)
    assert await runner._hmwa_maybe_reasoning_stall_auto_continue(_stall_result(), src, sk, None) is None
    assert len(runner.handlers) == 2
    assert runner.store.marked[-1] == (sk, "reasoning_stall")


@pytest.mark.asyncio
async def test_non_stall_results_never_schedule():
    runner = _FakeRunner()
    src = _FakeSource()
    for result in (
        {"failed": True, "failure_reason": "timeout"},            # transient class, not stall
        {"completed": True, "final_response": "done"},             # clean success
        {"completed": True},                                        # no flag at all
        None,
    ):
        out = await runner._hmwa_maybe_reasoning_stall_auto_continue(result, src, "sk-x", None)
        assert out is None, result
    assert not runner.handlers and not runner.store.marked


@pytest.mark.asyncio
async def test_compression_exhausted_excluded():
    runner = _FakeRunner()
    out = await runner._hmwa_maybe_reasoning_stall_auto_continue(
        _stall_result(compression_exhausted=True), _FakeSource(), "sk-o", None)
    assert out is None and not runner.handlers


@pytest.mark.asyncio
async def test_disabled_and_zero_budget_keep_marker_only():
    for cfg in ((False, 3), (True, 0)):
        runner = _FakeRunner(config=cfg)
        out = await runner._hmwa_maybe_reasoning_stall_auto_continue(_stall_result(), _FakeSource(), f"sk-{cfg}", None)
        assert out is None
        assert runner.store.marked  # durable marker still set
        assert not runner.handlers
