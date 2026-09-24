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
        return agent


REASONING = "Let me parse the dump file to find the crash module. MINIDUMP layout (Windows x64, MINI..."


def _assistant_msg(content, reasoning_content, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=reasoning_content)


def _call_finish(agent, assistant_message, messages):
    return finish_text_response(
        agent,
        assistant_message=assistant_message,
        response=SimpleNamespace(choices=[SimpleNamespace(message=assistant_message, finish_reason="stop")], model="test"),
        finish_reason="stop",
        messages=messages,
        api_messages=list(messages),
        conversation_history=[],
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


def test_stall_flag_set_on_mid_chain_steer_no_tools_this_turn(loop_agent):
    # P3: the 2026-09-24 15:40 incident. A steer / mid-turn user row lands RIGHT AFTER a tool
    # result (immediate predecessor is a tool row), and THIS turn makes 0 tool calls before a
    # reasoning-only clean stop. The old rule saw 0 tools-after-last-user and treated it as a
    # normal Q&A answer (no stall, no auto-continue) — stranding the task the steer was driving.
    # A user row whose predecessor is a tool row can only be a mid-chain injection (a completed
    # turn always ends on an assistant answer row before the next user message), so it IS a stall.
    tool_calls = [SimpleNamespace(id="c1", type="function",
                                   function=SimpleNamespace(name="terminal", arguments="{}"))]
    messages = [
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "c1", "content": "step result"},
        {"role": "user", "content": "你自主决定"},   # mid-chain steer = the LAST user row
    ]
    _call_finish(loop_agent, _assistant_msg("", REASONING), messages)
    assert loop_agent._reasoning_only_stall is True


def test_stall_flag_clear_when_user_after_completed_answer(loop_agent):
    # P3 counter-example guard: a fresh user message that arrives AFTER a completed assistant
    # answer row (not right after a tool row) is a standalone question, not a mid-chain steer.
    # 0 tools this turn + reasoning-only stop on such a row is a legitimate Q&A answer -> no stall.
    tool_calls = [SimpleNamespace(id="c1", type="function",
                                   function=SimpleNamespace(name="terminal", arguments="{}"))]
    messages = [
        {"role": "user", "content": "do the task"},
        {"role": "assistant", "content": "", "tool_calls": tool_calls},
        {"role": "tool", "tool_call_id": "c1", "content": "step result"},
        {"role": "assistant", "content": "done, here is the answer"},   # completed turn
        {"role": "user", "content": "what is the answer to 2+2?"},     # fresh question, last row
    ]
    _call_finish(loop_agent, _assistant_msg("", REASONING), messages)
    assert loop_agent._reasoning_only_stall is False
