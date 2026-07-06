"""Gateway post-delivery scheduling for idle compaction + KV pre-warm.

These exercise ``GatewayRunner._schedule_background_compression_after_turn``
and its worker directly, with a minimal hand-built runner (no full gateway
construction). They verify:

* the soft-threshold arm registers a post-delivery callback that runs the
  background compress off-loop;
* a session that is busy at fire time is skipped (no compress, no warm);
* the pre-warm fires after an in-turn compaction even when no background
  compress was armed;
* each config flag (background / prewarm) off disables exactly its piece.
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# gateway conftest installs telegram/discord mocks at collection time.
from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL


class _CaptureAdapter:
    """Adapter stub that captures the registered post-delivery callback."""

    def __init__(self):
        self._active_sessions = {}
        self.registered = []

    def register_post_delivery_callback(self, session_key, cb, generation=None):
        self.registered.append((session_key, cb, generation))


def _make_runner(agent, *, adapter=None, running=None):
    """Hand-build a minimal runner exposing only what the method touches."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = {"sk": (agent, "sig", 0, "sid")}

    class _Lock:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    runner._agent_cache_lock = _Lock()
    runner._running_agents = running if running is not None else {}
    runner.adapters = {"telegram": adapter} if adapter is not None else {}
    runner._background_tasks = set()

    # Run "executor" work inline and synchronously so the test is deterministic.
    async def _run_inline(func, *args):
        return func(*args)

    runner._run_in_executor_with_context = _run_inline
    return runner


def _agent(*, usage, threshold=100_000, background=True, prewarm=True,
           enabled=True, soft_ratio=0.8, in_turn_compacted=False):
    compressor = MagicMock()
    compressor.threshold_tokens = threshold
    compressor.last_prompt_tokens = usage
    compressor.should_compress.return_value = True
    agent = SimpleNamespace(
        compression_enabled=enabled,
        compression_background=background,
        compression_prewarm=prewarm,
        compression_soft_ratio=soft_ratio,
        compression_prewarm_timeout=120.0,
        context_compressor=compressor,
        tools=None,
        session_id="sid",
        api_mode="chat_completions",
        _session_messages=[{"role": "user", "content": f"m{i}"} for i in range(20)],
        _cached_system_prompt="sys",
        _last_compaction_in_place=in_turn_compacted,
    )
    return agent


# A source stub whose .platform maps to the adapter key.
_SOURCE = SimpleNamespace(platform="telegram")


class TestSchedulingArmsAndDefers:
    def test_soft_band_registers_post_delivery_callback(self, monkeypatch):
        agent = _agent(usage=90_000)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)

        compress_calls = {"n": 0}
        prewarm_calls = {"n": 0}

        def _fake_compress(a, messages, sys_msg, *, task_id="default"):
            compress_calls["n"] += 1
            return ([{"role": "user", "content": "summary"}], "sys2")

        def _fake_prewarm(a, messages, *, timeout=120.0):
            prewarm_calls["n"] += 1
            return True

        monkeypatch.setattr(
            "agent.conversation_compression.compress_context",
            lambda a, m, s, **kw: ([{"role": "user", "content": "summary"}], "sys2"),
        )
        monkeypatch.setattr(
            "agent.conversation_compression.prewarm_after_compression", _fake_prewarm
        )

        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": False}
        )
        # A callback must have been registered (deferred, not run yet).
        assert len(adapter.registered) == 1
        assert prewarm_calls["n"] == 0

        # Fire the post-delivery callback (as the adapter would after sending).
        _, cb, _gen = adapter.registered[0]
        asyncio.run(cb())

        # Background compress ran, then the prefix was warmed.
        assert prewarm_calls["n"] == 1

    def test_no_arm_no_registration(self, monkeypatch):
        # Under the soft band and no in-turn compaction → nothing to schedule.
        agent = _agent(usage=50_000, in_turn_compacted=False)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)
        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": False}
        )
        assert adapter.registered == []


class TestBusySkip:
    def test_busy_at_fire_time_skips_compress_and_prewarm(self, monkeypatch):
        agent = _agent(usage=90_000)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)

        compress_ran = {"n": 0}
        prewarm_ran = {"n": 0}
        monkeypatch.setattr(
            "agent.conversation_compression.maybe_background_compress",
            lambda *a, **k: compress_ran.__setitem__("n", compress_ran["n"] + 1) or True,
        )
        monkeypatch.setattr(
            "agent.conversation_compression.prewarm_after_compression",
            lambda *a, **k: prewarm_ran.__setitem__("n", prewarm_ran["n"] + 1) or True,
        )

        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": False}
        )
        assert len(adapter.registered) == 1

        # Simulate a new turn starting on this session before the callback runs.
        runner._running_agents["sk"] = object()  # a live (non-sentinel) agent

        _, cb, _ = adapter.registered[0]
        asyncio.run(cb())

        assert compress_ran["n"] == 0
        assert prewarm_ran["n"] == 0

    def test_pending_sentinel_is_not_busy(self, monkeypatch):
        # The PENDING sentinel means "starting" — the worker's own re-check
        # treats only a real running agent as busy; sentinel should NOT block.
        agent = _agent(usage=90_000)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)
        runner._running_agents["sk"] = _AGENT_PENDING_SENTINEL

        ran = {"n": 0}
        monkeypatch.setattr(
            "agent.conversation_compression.maybe_background_compress",
            lambda *a, **k: ran.__setitem__("n", ran["n"] + 1) or True,
        )
        monkeypatch.setattr(
            "agent.conversation_compression.prewarm_after_compression",
            lambda *a, **k: True,
        )

        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": False}
        )
        _, cb, _ = adapter.registered[0]
        asyncio.run(cb())
        assert ran["n"] == 1


class TestPrewarmAfterInTurnCompaction:
    def test_inturn_compaction_warms_without_background_arm(self, monkeypatch):
        # Usage BELOW the soft band (no background arm), but an in-turn
        # compaction fired this turn → we still warm the new prefix.
        agent = _agent(usage=50_000, in_turn_compacted=True)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)

        bg_ran = {"n": 0}
        warm_ran = {"n": 0}
        monkeypatch.setattr(
            "agent.conversation_compression.maybe_background_compress",
            lambda *a, **k: bg_ran.__setitem__("n", bg_ran["n"] + 1) or True,
        )
        monkeypatch.setattr(
            "agent.conversation_compression.prewarm_after_compression",
            lambda *a, **k: warm_ran.__setitem__("n", warm_ran["n"] + 1) or True,
        )

        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": True}
        )
        assert len(adapter.registered) == 1
        _, cb, _ = adapter.registered[0]
        asyncio.run(cb())

        # No background compress (not armed), but the prefix WAS warmed.
        assert bg_ran["n"] == 0
        assert warm_ran["n"] == 1

    def test_result_flag_alone_triggers_prewarm(self, monkeypatch):
        # In-turn compaction signalled via the result dict (agent flag False).
        agent = _agent(usage=50_000, in_turn_compacted=False)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)

        warm_ran = {"n": 0}
        monkeypatch.setattr(
            "agent.conversation_compression.prewarm_after_compression",
            lambda *a, **k: warm_ran.__setitem__("n", warm_ran["n"] + 1) or True,
        )
        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": True}
        )
        assert len(adapter.registered) == 1
        _, cb, _ = adapter.registered[0]
        asyncio.run(cb())
        assert warm_ran["n"] == 1


class TestConfigGatingAtGatewaySeam:
    def test_background_off_still_warms_after_inturn(self, monkeypatch):
        # background disabled, prewarm enabled, in-turn compaction fired →
        # no background compress armed, but prewarm still runs.
        agent = _agent(usage=90_000, background=False, prewarm=True, in_turn_compacted=True)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)

        bg = {"n": 0}
        warm = {"n": 0}
        monkeypatch.setattr(
            "agent.conversation_compression.maybe_background_compress",
            lambda *a, **k: bg.__setitem__("n", bg["n"] + 1) or True,
        )
        monkeypatch.setattr(
            "agent.conversation_compression.prewarm_after_compression",
            lambda *a, **k: warm.__setitem__("n", warm["n"] + 1) or True,
        )
        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": True}
        )
        assert len(adapter.registered) == 1
        _, cb, _ = adapter.registered[0]
        asyncio.run(cb())
        assert bg["n"] == 0
        assert warm["n"] == 1

    def test_prewarm_off_still_background_compresses(self, monkeypatch):
        # prewarm disabled, background enabled, in the soft band →
        # background compress runs, no warm.
        agent = _agent(usage=90_000, background=True, prewarm=False)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)

        bg = {"n": 0}
        warm = {"n": 0}
        monkeypatch.setattr(
            "agent.conversation_compression.maybe_background_compress",
            lambda *a, **k: bg.__setitem__("n", bg["n"] + 1) or True,
        )
        monkeypatch.setattr(
            "agent.conversation_compression.prewarm_after_compression",
            lambda *a, **k: warm.__setitem__("n", warm["n"] + 1) or True,
        )
        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": False}
        )
        assert len(adapter.registered) == 1
        _, cb, _ = adapter.registered[0]
        asyncio.run(cb())
        assert bg["n"] == 1
        assert warm["n"] == 0

    def test_both_off_registers_nothing(self, monkeypatch):
        agent = _agent(usage=90_000, background=False, prewarm=False, in_turn_compacted=True)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)
        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": True}
        )
        # background off (no arm) + prewarm off (nothing to warm) → no callback.
        assert adapter.registered == []

    def test_master_switch_off_registers_nothing(self, monkeypatch):
        agent = _agent(usage=90_000, enabled=False)
        adapter = _CaptureAdapter()
        runner = _make_runner(agent, adapter=adapter)
        runner._schedule_background_compression_after_turn(
            _SOURCE, "sk", {"compacted_in_place": True}
        )
        assert adapter.registered == []
