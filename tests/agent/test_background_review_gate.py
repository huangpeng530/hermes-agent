"""Post-turn background-review gate (``agent.turn_finalizer._background_review_gate``).

2026-09-23 incident: a reasoning-only stall turn (task unfinished, clean stop with a
truncated planning monologue) still spawned the bg-review fork at the same idle
boundary where the reasoning-stall auto-continue was owed. The fork fought the
continuation for the session slot: it got hard-superseded mid-API-call (wasting a
provider call, logging a stream-drop warning) and its interleaved context polluted
the continuation's view. The gate suppresses the fork whenever a continuation is
owed; the idle queue re-triggers the review after the task genuinely finishes, so
nothing is lost.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.turn_finalizer import _background_review_gate


def _agent(**extra):
    base = {"skip_background_review": False}
    base.update(extra)
    return SimpleNamespace(**base)


def test_gate_spawns_on_clean_completed_turn():
    assert _background_review_gate(_agent(), {}, "final answer", False, True, False) is None
    # skills-only target also spawns
    assert _background_review_gate(_agent(), {}, "final answer", False, False, True) is None


def test_gate_suppresses_on_reasoning_stall_owed():
    # The 19:28 incident shape: stall flag stamped by the finalizer before the gate runs.
    agent = _agent()
    assert _background_review_gate(agent, {"reasoning_only_stall": True}, "planning monologue",
        False, True, True) == "reasoning_stall_owed"
    # memory-only target is suppressed too
    assert _background_review_gate(agent, {"reasoning_only_stall": True}, "planning monologue",
        False, True, False) == "reasoning_stall_owed"


def test_gate_suppresses_interrupted_turn():
    assert _background_review_gate(_agent(), {}, "partial", True, True, False) == "interrupted"


def test_gate_suppresses_empty_response():
    # No visible answer -> nothing to review
    assert _background_review_gate(_agent(), {}, None, False, True, False) == "no_response"
    assert _background_review_gate(_agent(), {}, "", False, True, False) == "no_response"


def test_gate_suppresses_cron_and_review_disabled():
    assert _background_review_gate(_agent(skip_background_review=True), {}, "answer", False,
        True, False) == "skip_background_review"
    # no review target at all: neither memory nor skills due
    assert _background_review_gate(_agent(), {}, "answer", False, False, False) == "no_review_target"


def test_gate_priority_stall_beats_review_target_absence():
    # stall is checked before the target flags: an owed continuation suppresses the fork
    # even when the skill nudge was not due this turn.
    assert _background_review_gate(_agent(), {"reasoning_only_stall": True}, "planning", False,
        False, False) == "reasoning_stall_owed"
