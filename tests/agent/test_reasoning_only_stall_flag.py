"""Machine-readable ``reasoning_only_stall`` marker (agent core).

A reasoning-only clean stop promotes the model's truncated planning monologue to the visible
final response, so the turn reports "complete" even though the in-progress task stopped
mid-plan. Live-process surfaces (desktop / gateway auto-continue) need a machine-readable
verdict for exactly this class:

* the flag is set ONLY on a reasoning-only clean stop that happened AFTER real tool work
  in THIS turn (a clean stop with no tool calls is a legitimate Q&A answer, not a stall);
* it is reset at the start of each text-candidate decision so a later real answer cannot
  inherit a stale flag from an earlier reasoning-only candidate;
* the finalizer stamps it into the terminal result as ``reasoning_only_stall`` and
  consumes the agent flag so it never leaks into the next turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.turn_final_response import finish_text_response


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent
    from unittest.mock import MagicMock, patch
    # P4's reasoning-only stall marker rides the reasoning-promotion path, which upstream
    # (38880bd) gates behind `answer_in_reasoning_capability` (non-trusted routes keep
    # reasoning private and take the continuation ladder instead of promoting). Hold the gate
    # open ACROSS the whole test body (yield inside the patch) so the P4 stall leg is
    # reachable: the construction-time patches below can drop at construction, but this one
    # must outlive `return agent` / `yield` or it reverts to the real (non-trusted) verdict.
    with patch("agent.turn_final_response.answer_in_reasoning_capability", return_value=True):
        with (
            patch("model_tools.get_tool_definitions", return_value=[]),
            patch("model_tools.check_toolset_requirements", return_value={}),
            patch("agent.process_bootstrap.OpenAI"),
        ):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://api.deepseek.com/v1",
                model="deepseek-reasoner",
                provider="deepseek",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            agent.client = MagicMock()
            agent._cached_system_prompt = "You are helpful."
            agent._use_prompt_caching = False
            agent.compression_enabled = False
            agent.save_trajectories = False
        yield agent


REASONING = "Let me parse the dump file to find the crash module. MINIDUMP layout (Windows x64, MINI..."


def _assistant_msg(content, reasoning_content, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=reasoning_content)


def _call_finish(agent, assistant_message, messages, conversation_history=None):
    return finish_text_response(
        agent,
        assistant_message=assistant_message,
        response=SimpleNamespace(choices=[SimpleNamespace(message=assistant_message, finish_reason="stop")], model="test"),
        finish_reason="stop",
        messages=messages,
        api_messages=list(messages),
        conversation_history=list(conversation_history or []),
        api_call_count=1,
        user_message="continue the task",
        active_system_prompt="You are helpful.",
        final_response="",
        _turn_exit_reason="",
        _preflight_compression_blocked=False,
        codex_ack_continuations=0,
        truncated_response_parts=[],
        length_continue_retries=0,
        _pending_verification_response=None,
        _pending_verification_response_previewed=None,
        effective_task_id="task-1",
    )


def test_stall_flag_set_when_reasoning_only_stop_after_tool_work(loop_agent):
    # user -> assistant(tool_calls) -> tool -> assistant(reasoning-only clean stop): the task
    # was actively running when the model cut off mid-plan.
    tool_calls = [SimpleNamespace(id="call_1", type="function",
                                  function=SimpleNamespace(name="terminal", arguments='{"command": "ls"}'))]
    messages = [
        {"role": "user", "content": "continue the task"},
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    _call_finish(loop_agent, _assistant_msg("", REASONING), messages)
    assert loop_agent._reasoning_only_stall is True


def test_stall_flag_clear_when_no_tool_work_this_turn(loop_agent):
    # user -> assistant(reasoning-only clean stop): no tool calls after the user message, so
    # this is a "model answered in thinking" Q&A shape, NOT a stalled task.
    messages = [{"role": "user", "content": "what is the answer?"}]
    _call_finish(loop_agent, _assistant_msg("", REASONING), messages)
    assert loop_agent._reasoning_only_stall is False


def test_stall_flag_reset_between_candidates(loop_agent):
    # A reasoning-only candidate followed by a REAL answer: the second candidate must clear
    # the flag so the finalizer does not stamp a stall onto a normal completion.
    tool_calls = [SimpleNamespace(id="c1", type="function",
                                   function=SimpleNamespace(name="terminal", arguments="{}"))]
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    # First candidate: reasoning-only clean stop after tool work -> flag set.
    _call_finish(loop_agent, _assistant_msg("", REASONING), messages)
    assert loop_agent._reasoning_only_stall is True
    # Second candidate: visible content -> flag cleared, turn is a normal completion.
    verdict = _call_finish(loop_agent, _assistant_msg("done, no stall here", None), messages)
    assert loop_agent._reasoning_only_stall is False
    assert verdict.action in ("return", "break", "continue")


def _deep_history(n):
    # A session that has already accumulated n conversation messages (the task ran many
    # turns before this final reasoning-only stop).
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"step {i}"} for i in range(n)]


def test_stall_flag_set_when_reasoning_only_stop_in_deep_session(loop_agent):
    # P4: a fresh task prompt into a DEEP session, zero tool calls this turn, empty visible
    # answer, reasoning-only clean stop -> the long task has derailed; flag it so the
    # live-process scheduler re-prompts once. (The 2026-10-08 17:13 incident: 6056-msg
    # session, "检查并升级hermes", model stopped mid-rebuild-mono on a 0-tool empty stop.)
    messages = [{"role": "user", "content": "检查并升级hermes"}]
    _call_finish(
        loop_agent, _assistant_msg("", REASONING), messages,
        conversation_history=_deep_history(30),
    )
    assert loop_agent._reasoning_only_stall is True


def test_stall_flag_clear_when_reasoning_only_stop_in_shallow_session(loop_agent):
    # Preserved exemption: a shallow session (few messages) with a reasoning-only, 0-tool
    # clean stop is still a legitimate in-head Q&A -> NOT a stall.
    messages = [{"role": "user", "content": "what is the answer?"}]
    _call_finish(loop_agent, _assistant_msg("", REASONING), messages)
    assert loop_agent._reasoning_only_stall is False


def test_stall_flag_depth_boundary(loop_agent):
    # Just below the 20-message threshold stays a Q&A exemption; at/above it is a stall.
    messages = [{"role": "user", "content": "go"}]
    _call_finish(
        loop_agent, _assistant_msg("", REASONING), messages,
        conversation_history=_deep_history(19),
    )
    assert loop_agent._reasoning_only_stall is False
    _call_finish(
        loop_agent, _assistant_msg("", REASONING), messages,
        conversation_history=_deep_history(20),
    )
    assert loop_agent._reasoning_only_stall is True


def test_stall_flag_set_when_reasoning_only_stop_in_deep_session_with_no_history_param(loop_agent):
    # conversation_history is the authoritative depth source; `messages` alone does NOT
    # extend it. A single-user `messages` with an empty history stays a Q&A exemption.
    messages = [{"role": "user", "content": "go"}]
    _call_finish(loop_agent, _assistant_msg("", REASONING), messages, conversation_history=[])
    assert loop_agent._reasoning_only_stall is False
