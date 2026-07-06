"""Unit tests for post-delivery (idle-time) compression + KV pre-warm.

Covers the design-sensitive pieces of the feature:

* soft-threshold arming math (``should_background_compress``) — under the soft
  band → no schedule; in the soft band → schedule; at/over the hard threshold →
  no schedule (the in-turn backstop owns that state);
* usage accounting reuse (``current_context_usage_tokens``) — same source the
  in-turn threshold check reads, including the post-compaction ``-1`` sentinel;
* ``maybe_background_compress`` gating (feature flag, master switch, anti-thrash
  guard) and its no-op-on-unchanged contract;
* KV pre-warm kwargs equality — the warm-up kwargs are byte-identical to the
  real next call except for ``max_tokens`` / ``stream``;
* pre-warm failure is swallowed and never raises;
* config gating — each flag off disables exactly its piece.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

from agent.conversation_compression import (
    current_context_usage_tokens,
    maybe_background_compress,
    prewarm_after_compression,
    should_background_compress,
)


# ── soft-threshold arming math ───────────────────────────────────────────────

class TestShouldBackgroundCompress:
    THRESHOLD = 100_000
    SOFT = 0.8  # soft band starts at 80_000

    def test_under_soft_band_does_not_arm(self):
        # 79_999 < 80_000 soft floor
        assert should_background_compress(
            usage_tokens=79_999,
            threshold_tokens=self.THRESHOLD,
            soft_ratio=self.SOFT,
        ) is False

    def test_at_soft_floor_arms(self):
        assert should_background_compress(
            usage_tokens=80_000,
            threshold_tokens=self.THRESHOLD,
            soft_ratio=self.SOFT,
        ) is True

    def test_inside_soft_band_arms(self):
        assert should_background_compress(
            usage_tokens=95_000,
            threshold_tokens=self.THRESHOLD,
            soft_ratio=self.SOFT,
        ) is True

    def test_at_hard_threshold_does_not_arm(self):
        # The in-turn hard backstop owns usage >= threshold — background stands
        # down so the two never both fire for one over-threshold state.
        assert should_background_compress(
            usage_tokens=self.THRESHOLD,
            threshold_tokens=self.THRESHOLD,
            soft_ratio=self.SOFT,
        ) is False

    def test_over_hard_threshold_does_not_arm(self):
        assert should_background_compress(
            usage_tokens=150_000,
            threshold_tokens=self.THRESHOLD,
            soft_ratio=self.SOFT,
        ) is False

    def test_zero_threshold_never_arms(self):
        assert should_background_compress(
            usage_tokens=50_000, threshold_tokens=0, soft_ratio=self.SOFT
        ) is False

    @pytest.mark.parametrize("bad_ratio", [0.0, 1.0, -0.5, 1.5])
    def test_out_of_range_ratio_never_arms(self, bad_ratio):
        assert should_background_compress(
            usage_tokens=90_000,
            threshold_tokens=self.THRESHOLD,
            soft_ratio=bad_ratio,
        ) is False


# ── usage accounting reuse ───────────────────────────────────────────────────

def _agent_with_compressor(last_prompt_tokens, threshold=100_000, tools=None):
    compressor = SimpleNamespace(
        last_prompt_tokens=last_prompt_tokens,
        threshold_tokens=threshold,
    )
    return SimpleNamespace(
        context_compressor=compressor,
        tools=tools,
        session_id="s1",
    )


class TestCurrentContextUsageTokens:
    def test_prefers_provider_prompt_tokens(self):
        agent = _agent_with_compressor(last_prompt_tokens=88_000)
        # messages should be ignored when a real provider count exists
        assert current_context_usage_tokens(agent, [{"role": "user", "content": "x" * 10_000}]) == 88_000

    def test_minus_one_sentinel_is_treated_as_zero(self):
        # -1 = "compaction just ran, no real usage yet" — the in-turn path
        # treats it as no-pressure (#36718); so must we.
        agent = _agent_with_compressor(last_prompt_tokens=-1)
        assert current_context_usage_tokens(agent, [{"role": "user", "content": "x" * 10_000}]) == 0

    def test_zero_falls_back_to_rough_estimate(self):
        # 0 = provider gave no usage / disconnect (#2153) → rough estimate.
        big = "x" * 400_000  # ~100K tokens at 4 chars/token
        agent = _agent_with_compressor(last_prompt_tokens=0)
        usage = current_context_usage_tokens(agent, [{"role": "user", "content": big}])
        assert usage > 50_000

    def test_missing_compressor_returns_zero(self):
        agent = SimpleNamespace(context_compressor=None, tools=None)
        assert current_context_usage_tokens(agent, [{"role": "user", "content": "hi"}]) == 0


# ── maybe_background_compress gating ─────────────────────────────────────────

def _bg_agent(*, usage, threshold=100_000, background=True, enabled=True,
              soft_ratio=0.8, should_compress=True, compress_result=None):
    """Build a fake agent whose compressor is fully stubbed (no LLM call)."""
    compressor = MagicMock()
    compressor.threshold_tokens = threshold
    compressor.last_prompt_tokens = usage
    compressor.should_compress.return_value = should_compress
    agent = SimpleNamespace(
        compression_enabled=enabled,
        compression_background=background,
        compression_soft_ratio=soft_ratio,
        context_compressor=compressor,
        tools=None,
        session_id="s1",
    )
    agent._compress_result = compress_result
    return agent


class TestMaybeBackgroundCompress:
    def _patch_compress(self, agent, new_messages):
        """Patch compress_context to return new_messages without side effects."""
        return patch(
            "agent.conversation_compression.compress_context",
            return_value=(new_messages, "sys-prompt"),
        )

    def test_armed_in_soft_band_compresses(self):
        agent = _bg_agent(usage=90_000)
        messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        compacted = [{"role": "user", "content": "summary"}]
        with self._patch_compress(agent, compacted) as cc:
            ran = maybe_background_compress(agent, messages, "sys")
        assert ran is True
        cc.assert_called_once()

    def test_under_soft_band_does_not_compress(self):
        agent = _bg_agent(usage=50_000)
        with patch("agent.conversation_compression.compress_context") as cc:
            ran = maybe_background_compress(agent, [{"role": "user", "content": "hi"}], "sys")
        assert ran is False
        cc.assert_not_called()

    def test_over_hard_threshold_does_not_background_compress(self):
        # In-turn backstop territory — background must stand down.
        agent = _bg_agent(usage=120_000)
        with patch("agent.conversation_compression.compress_context") as cc:
            ran = maybe_background_compress(agent, [{"role": "user", "content": "hi"}], "sys")
        assert ran is False
        cc.assert_not_called()

    def test_feature_flag_off_disables(self):
        agent = _bg_agent(usage=90_000, background=False)
        with patch("agent.conversation_compression.compress_context") as cc:
            ran = maybe_background_compress(agent, [{"role": "user", "content": "hi"}], "sys")
        assert ran is False
        cc.assert_not_called()

    def test_master_switch_off_disables(self):
        agent = _bg_agent(usage=90_000, enabled=False)
        with patch("agent.conversation_compression.compress_context") as cc:
            ran = maybe_background_compress(agent, [{"role": "user", "content": "hi"}], "sys")
        assert ran is False
        cc.assert_not_called()

    def test_anti_thrash_guard_blocks(self):
        # In soft band, but the compressor's should_compress() declines (cooldown
        # / anti-thrash). Background must respect that and not compress.
        agent = _bg_agent(usage=90_000, should_compress=False)
        with patch("agent.conversation_compression.compress_context") as cc:
            ran = maybe_background_compress(agent, [{"role": "user", "content": "hi"}], "sys")
        assert ran is False
        cc.assert_not_called()

    def test_no_op_compress_returns_false(self):
        # compress_context returns the input unchanged (lost the lock / aborted).
        agent = _bg_agent(usage=90_000)
        messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        with self._patch_compress(agent, messages):  # same-length → no-op
            ran = maybe_background_compress(agent, messages, "sys")
        assert ran is False

    def test_compress_exception_is_swallowed(self):
        agent = _bg_agent(usage=90_000)
        with patch(
            "agent.conversation_compression.compress_context",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            ran = maybe_background_compress(agent, [{"role": "user", "content": "hi"}], "sys")
        assert ran is False


# ── KV pre-warm ──────────────────────────────────────────────────────────────

def _tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


class _FakeOpenAI:
    def __init__(self, **kw):
        self.api_key = kw.get("api_key", "test")
        self.base_url = kw.get("base_url", "http://test")

    def close(self):
        pass


def _make_real_agent(monkeypatch, provider="openrouter",
                     base_url="https://openrouter.ai/api/v1", model=None):
    """Build a real AIAgent wired to fakes (mirrors test_provider_parity)."""
    monkeypatch.setattr(
        "run_agent.get_tool_definitions", lambda **kw: _tool_defs("web_search", "terminal")
    )
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.OpenAI", _FakeOpenAI)
    from run_agent import AIAgent

    kwargs = dict(
        api_key="test-key",
        base_url=base_url,
        provider=provider,
        api_mode="chat_completions",
        max_iterations=4,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    if model:
        kwargs["model"] = model
    return AIAgent(**kwargs)


class TestPrewarmKwargsEquality:
    """The warm-up prefix MUST equal the real next call's prefix byte-for-byte.

    Build the real next-call kwargs and the warm-up kwargs from the SAME agent
    state (same post-compaction message list, tools, system prompt, params) and
    assert deep-equality except for the two intentional overrides.
    """

    def test_warmup_kwargs_match_real_call_except_max_tokens_and_stream(self, monkeypatch):
        agent = _make_real_agent(monkeypatch)
        messages = [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]

        # The REAL next call's kwargs (what conversation_loop.py sends).
        real_kwargs = agent._build_api_kwargs(list(messages))

        # Capture what the warm-up would send by intercepting the client call.
        captured = {}

        class _CapClient:
            class chat:  # noqa: N801
                class completions:  # noqa: N801
                    @staticmethod
                    def create(**kw):
                        captured.update(kw)
                        return SimpleNamespace(choices=[])

        monkeypatch.setattr(
            agent, "_ensure_primary_openai_client", lambda **kw: _CapClient
        )
        ok = prewarm_after_compression(agent, list(messages), timeout=10.0)
        assert ok is True
        assert captured, "warm-up did not issue a request"

        # The two intentional overrides.
        assert captured.get("stream") is False
        # max_tokens (or provider-specific alias) must be exactly 1.
        _warm_mt = captured.get("max_tokens", captured.get("max_completion_tokens"))
        assert _warm_mt == 1

        # Everything else must match the real call. Normalize the two knobs out
        # of both dicts, then deep-compare.
        def _strip(d):
            d = dict(d)
            d.pop("stream", None)
            d.pop("max_tokens", None)
            d.pop("max_completion_tokens", None)
            return d

        assert _strip(captured) == _strip(real_kwargs)
        # Tools are part of the templated prompt server-side — they MUST be
        # present and identical (byte-identical-prefix rule).
        assert captured.get("tools") == real_kwargs.get("tools")
        assert captured.get("messages") == real_kwargs.get("messages")

    def test_prewarm_disabled_flag_skips(self, monkeypatch):
        agent = _make_real_agent(monkeypatch)
        agent.compression_prewarm = False
        called = {"n": 0}

        def _client(**kw):
            called["n"] += 1
            raise AssertionError("should not be called")

        monkeypatch.setattr(agent, "_ensure_primary_openai_client", _client)
        assert prewarm_after_compression(agent, [{"role": "user", "content": "hi"}]) is False
        assert called["n"] == 0

    def test_prewarm_failure_is_swallowed(self, monkeypatch):
        agent = _make_real_agent(monkeypatch)

        class _BoomClient:
            class chat:  # noqa: N801
                class completions:  # noqa: N801
                    @staticmethod
                    def create(**kw):
                        raise RuntimeError("connection refused")

        monkeypatch.setattr(
            agent, "_ensure_primary_openai_client", lambda **kw: _BoomClient
        )
        # Must not raise, returns False.
        assert prewarm_after_compression(agent, [{"role": "user", "content": "hi"}], timeout=10.0) is False

    def test_prewarm_skips_non_chat_completions_mode(self, monkeypatch):
        agent = _make_real_agent(monkeypatch)
        agent.api_mode = "anthropic_messages"
        called = {"n": 0}
        monkeypatch.setattr(
            agent,
            "_ensure_primary_openai_client",
            lambda **kw: (_ for _ in ()).throw(AssertionError("should not build client")),
        )
        assert prewarm_after_compression(agent, [{"role": "user", "content": "hi"}]) is False


# ── config plumbing (defaults + gating) ──────────────────────────────────────

class TestConfigPlumbing:
    """The four config keys must land on the agent with the documented defaults
    when absent, and be individually overridable."""

    def _agent_with_config(self, monkeypatch, compression_cfg):
        # Patch load_config (imported locally inside agent_init.initialize) to
        # return a copy of DEFAULT_CONFIG carrying our compression override.
        import copy as _copy

        from hermes_cli.config import DEFAULT_CONFIG

        cfg = _copy.deepcopy(DEFAULT_CONFIG)
        if compression_cfg is not None:
            cfg.setdefault("compression", {})
            cfg["compression"].update(compression_cfg)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: cfg)
        return _make_real_agent(monkeypatch)

    def test_defaults_when_absent(self, monkeypatch):
        # Empty compression override → documented defaults on the agent.
        agent = self._agent_with_config(monkeypatch, {})
        assert agent.compression_background is True
        assert agent.compression_soft_ratio == 0.8
        assert agent.compression_prewarm is True
        assert agent.compression_prewarm_timeout == 120.0

    def test_each_flag_overridable(self, monkeypatch):
        agent = self._agent_with_config(
            monkeypatch,
            {
                "background": False,
                "soft_ratio": 0.6,
                "prewarm": False,
                "prewarm_timeout": 45,
            },
        )
        assert agent.compression_background is False
        assert agent.compression_soft_ratio == 0.6
        assert agent.compression_prewarm is False
        assert agent.compression_prewarm_timeout == 45.0

    def test_out_of_range_soft_ratio_falls_back(self, monkeypatch):
        for bad in (0.0, 1.0, 1.5, -1.0, "nope"):
            agent = self._agent_with_config(monkeypatch, {"soft_ratio": bad})
            assert agent.compression_soft_ratio == 0.8, bad

    def test_non_positive_timeout_falls_back(self, monkeypatch):
        for bad in (0, -5, "x"):
            agent = self._agent_with_config(monkeypatch, {"prewarm_timeout": bad})
            assert agent.compression_prewarm_timeout == 120.0, bad
