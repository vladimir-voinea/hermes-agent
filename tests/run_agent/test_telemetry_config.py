"""Tests for per-turn generation telemetry (TPS + vLLM prefix-cache hit %).

Covers:
  1. ``AIAgent._telemetry_config()`` — the env/config seam (mirrors
     ``_turn_completion_explainer_enabled`` / ``_file_mutation_verifier_enabled``,
     see tests/run_agent/test_turn_completion_explainer.py).
  2. An end-to-end ``run_conversation`` turn verifying the real capture path
     in agent/conversation_loop.py (the _first_chunk_time timestamp + the
     per-call PerCallUsage list) produces a populated ``result["turn_telemetry"]``,
     and that the JSONL sink + log line fire.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


# --------------------------------------------------------------------------
# Fixtures (mirrors tests/run_agent/test_turn_completion_explainer.py)
# --------------------------------------------------------------------------
def _mock_response(content="Hello", finish_reason="stop", tool_calls=None, usage=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=usage)


def _usage(prompt_tokens=45000, completion_tokens=612, cached_tokens=39000):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )


def _make_agent(max_iterations: int = 10, config: dict | None = None) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config or {}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._fallback_chain = []
    return agent


# --------------------------------------------------------------------------
# 1. Enable/disable seam
# --------------------------------------------------------------------------
def test_telemetry_enabled_by_default():
    agent = _make_agent()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_TELEMETRY", None)
        with patch("hermes_cli.config.load_config", return_value={}):
            cfg = agent._telemetry_config()
    assert cfg == {"enabled": True, "telegram_footer": True, "jsonl": True}


def test_telemetry_disabled_via_env():
    agent = _make_agent()
    with patch.dict(os.environ, {"HERMES_TELEMETRY": "0"}, clear=False):
        cfg = agent._telemetry_config()
    assert cfg["enabled"] is False


def test_telemetry_disabled_via_config():
    agent = _make_agent()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_TELEMETRY", None)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"telemetry": {"enabled": False}},
        ):
            cfg = agent._telemetry_config()
    assert cfg["enabled"] is False


def test_telemetry_footer_toggle_independent_of_master_switch():
    agent = _make_agent()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_TELEMETRY", None)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"telemetry": {"telegram_footer": False}},
        ):
            cfg = agent._telemetry_config()
    assert cfg == {"enabled": True, "telegram_footer": False, "jsonl": True}


def test_telemetry_config_env_override_ignores_config_file():
    """HERMES_TELEMETRY only overrides the master switch, per docstring —
    verifies the short-circuit returns before config.yaml is even read for
    the sub-toggles (they keep their defaults)."""
    agent = _make_agent()
    with patch.dict(os.environ, {"HERMES_TELEMETRY": "false"}, clear=False):
        with patch(
            "hermes_cli.config.load_config",
            return_value={"telemetry": {"telegram_footer": False, "jsonl": False}},
        ):
            cfg = agent._telemetry_config()
    assert cfg["enabled"] is False
    assert cfg["telegram_footer"] is True
    assert cfg["jsonl"] is True


# --------------------------------------------------------------------------
# 2. End-to-end: run_conversation populates result["turn_telemetry"]
# --------------------------------------------------------------------------
def test_run_conversation_populates_turn_telemetry(tmp_path):
    agent = _make_agent(max_iterations=10)
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="Done.", finish_reason="stop", usage=_usage()),
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=False),
    ):
        result = agent.run_conversation("do something")

    assert result["final_response"] == "Done."
    telemetry = result.get("turn_telemetry")
    assert telemetry is not None
    assert telemetry.prompt_tokens == 45000
    assert telemetry.completion_tokens == 612
    assert telemetry.cache_read_tokens == 39000
    assert telemetry.cache_hit_pct is not None
    assert telemetry.model == agent.model

    # The JSONL sink fired as a side effect of the same turn.
    jsonl_path = tmp_path / "telemetry" / "turns.jsonl"
    assert jsonl_path.exists()
    record = json.loads(jsonl_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["prompt_tokens"] == 45000
    assert record["completion_tokens"] == 612


def test_run_conversation_telemetry_disabled_skips_capture_and_jsonl(tmp_path):
    agent = _make_agent(max_iterations=10, config={"telemetry": {"enabled": False}})
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="Done.", finish_reason="stop", usage=_usage()),
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=False),
    ):
        os.environ.pop("HERMES_TELEMETRY", None)
        with patch(
            "hermes_cli.config.load_config",
            return_value={"telemetry": {"enabled": False}},
        ):
            result = agent.run_conversation("do something")

    assert "turn_telemetry" not in result
    assert not (tmp_path / "telemetry" / "turns.jsonl").exists()


def test_run_conversation_no_usage_reported_yields_no_telemetry(tmp_path):
    """A provider response with usage=None must not crash the finalizer and
    must simply omit turn_telemetry (nothing to report)."""
    agent = _make_agent(max_iterations=10)
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="Done.", finish_reason="stop", usage=None),
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=False),
    ):
        result = agent.run_conversation("do something")

    assert result["final_response"] == "Done."
    assert "turn_telemetry" not in result


def test_run_conversation_multi_call_turn_aggregates_across_tool_loop(tmp_path, monkeypatch):
    """A tool-calling turn (assistant calls a tool, then answers) spans two
    API calls; turn_telemetry must sum both and use the FIRST call's cache
    hit rate."""
    agent = _make_agent(max_iterations=10)

    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="terminal", arguments="{}"),
        type="function",
    )
    first = _mock_response(
        content=None, finish_reason="tool_calls", tool_calls=[tool_call],
        usage=_usage(prompt_tokens=40000, completion_tokens=20, cached_tokens=8000),  # 20% cold
    )
    second = _mock_response(
        content="Done.", finish_reason="stop",
        usage=_usage(prompt_tokens=40100, completion_tokens=592, cached_tokens=40000),  # near-100% warm
    )
    agent.client.chat.completions.create.side_effect = [first, second]

    # Stub tool execution the same way test_run_agent_codex_responses.py does
    # (agent._execute_tool_calls is a (assistant_message, messages,
    # effective_task_id) callable that appends role="tool" results in place).
    def _fake_execute_tool_calls(assistant_message, messages, effective_task_id):
        for call in assistant_message.tool_calls:
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": "tool ok",
            })

    monkeypatch.setattr(agent, "_execute_tool_calls", _fake_execute_tool_calls)

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}, clear=False),
    ):
        result = agent.run_conversation("do something with a tool")

    telemetry = result.get("turn_telemetry")
    assert telemetry is not None
    assert telemetry.prompt_tokens == 40000 + 40100
    assert telemetry.completion_tokens == 20 + 592
    assert telemetry.cache_read_tokens == 8000 + 40000
    # First call's ratio (20%), not a blend with the near-100% warm 2nd call.
    assert telemetry.cache_hit_pct < 25
