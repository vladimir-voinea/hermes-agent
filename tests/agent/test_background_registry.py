"""Tests for the shared background-task registry (agent/background_registry.py).

The registry holds a handle to each running /background agent so a later
`/background cancel <id>` can call AIAgent.interrupt() on it. These tests cover
token resolution (exact id, #n / n index, unique prefix, ambiguous prefix) and
that cancel() actually signals the agent.
"""

import pytest

from agent.background_registry import (
    AmbiguousTaskError,
    BackgroundTaskRegistry,
)


class FakeAgent:
    """Minimal stand-in exposing the one method the registry calls."""

    def __init__(self):
        self.interrupt_message = None
        self.interrupt_calls = 0

    def interrupt(self, message=None):
        self.interrupt_calls += 1
        self.interrupt_message = message


@pytest.fixture
def reg():
    return BackgroundTaskRegistry()


def _seed(reg, n=2, surface="cli"):
    agents = []
    for i in range(n):
        a = FakeAgent()
        agents.append(a)
        reg.register(f"bg_00000{i}_aaaaa{i}", a, prompt=f"prompt number {i}", surface=surface)
    return agents


def test_register_and_list_in_start_order(reg):
    _seed(reg, 3)
    ids = [r.task_id for r in reg.list()]
    assert ids == ["bg_000000_aaaaa0", "bg_000001_aaaaa1", "bg_000002_aaaaa2"]
    assert reg.count() == 3


def test_unregister_removes(reg):
    _seed(reg, 2)
    reg.unregister("bg_000000_aaaaa0")
    assert reg.count() == 1
    assert reg.get("bg_000000_aaaaa0") is None


def test_resolve_exact_id(reg):
    _seed(reg, 2)
    assert reg.resolve("bg_000001_aaaaa1").task_id == "bg_000001_aaaaa1"


def test_resolve_index_forms(reg):
    _seed(reg, 2)
    assert reg.resolve("#1").task_id == "bg_000000_aaaaa0"
    assert reg.resolve("2").task_id == "bg_000001_aaaaa1"
    assert reg.resolve("#9") is None  # out of range


def test_resolve_unique_prefix(reg):
    _seed(reg, 2)
    assert reg.resolve("bg_000001").task_id == "bg_000001_aaaaa1"


def test_resolve_ambiguous_prefix_raises(reg):
    _seed(reg, 2)
    with pytest.raises(AmbiguousTaskError) as exc:
        reg.resolve("bg_00000")
    assert set(exc.value.matches) == {"bg_000000_aaaaa0", "bg_000001_aaaaa1"}


def test_resolve_unknown_returns_none(reg):
    _seed(reg, 1)
    assert reg.resolve("does-not-exist") is None
    assert reg.resolve("") is None


def test_cancel_interrupts_agent_and_flags_record(reg):
    agents = _seed(reg, 2)
    rec = reg.cancel("#2", message="stop now")
    assert rec.task_id == "bg_000001_aaaaa1"
    assert rec.cancel_requested is True
    assert agents[1].interrupt_calls == 1
    assert agents[1].interrupt_message == "stop now"
    # Other task untouched.
    assert agents[0].interrupt_calls == 0
    # Cancel leaves it registered until the task's own finally unregisters it.
    assert reg.count() == 2


def test_cancel_unknown_returns_none(reg):
    _seed(reg, 1)
    assert reg.cancel("nope") is None


def test_surface_filtering(reg):
    _seed(reg, 1, surface="cli")
    _seed(reg, 1, surface="gateway")  # ids collide with the cli ones above
    # Both share the bg_000000 id, so the second register overwrites the first
    # under the same key — use distinct ids to test surface filtering cleanly.
    reg2 = BackgroundTaskRegistry()
    reg2.register("bg_a", FakeAgent(), surface="cli")
    reg2.register("bg_b", FakeAgent(), surface="gateway")
    assert [r.task_id for r in reg2.list(surface="cli")] == ["bg_a"]
    assert [r.task_id for r in reg2.list(surface="gateway")] == ["bg_b"]
    # resolve honours the surface filter too.
    assert reg2.resolve("bg_a", surface="gateway") is None
