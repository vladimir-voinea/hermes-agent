"""Tests for the OpenCode lifecycle bridge.

The bridge is the only thing standing between "an agent CLI exited" and "a
card is closed", so the cases here are about that boundary:

* it resolves the card at all (the bug it exists to prevent is a card left
  ``running`` forever because OpenCode has no kanban tools);
* ★ it fires the host's ``kanban_complete`` hooks before completing — a direct
  DB write would waive, for OpenCode workers only, every guard the host
  enforces on Hermes workers;
* a blocked completion is handed BACK to OpenCode to correct, not turned into
  an immediate failure;
* a runtime failure is not confused with a work failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_opencode_bridge as bridge


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def no_hook_bootstrap(monkeypatch):
    """Don't load the host's real plugins/hooks into the test process."""
    monkeypatch.setattr(bridge, "_bootstrap_hooks", lambda: None)


@pytest.fixture
def card(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do the thing", body="the body",
                             worker_runtime="opencode")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(kanban_home.parent))
    return tid


def _fake_opencode(monkeypatch, *results):
    """Queue up (returncode, stdout) results for successive opencode runs."""
    calls = []
    queue = list(results)

    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def _run(argv, **kwargs):
        calls.append(argv)
        rc, out = queue.pop(0) if queue else (0, "done")
        return _R(rc, out)

    monkeypatch.setattr("subprocess.run", _run)
    return calls


def _gate_returns(monkeypatch, *reasons):
    """Queue up completion-gate verdicts (None = allow)."""
    seen = []
    queue = list(reasons)

    def _resolve(tool_name, args, **kwargs):
        seen.append((tool_name, args))
        return queue.pop(0) if queue else None

    monkeypatch.setattr("hermes_cli.plugins.resolve_pre_tool_block", _resolve)
    return seen


def test_bridge_completes_the_card(card, monkeypatch):
    """The whole reason the bridge exists: OpenCode cannot close its own card,
    so somebody must."""
    _fake_opencode(monkeypatch, (0, "I added calc.py and committed it."))
    _gate_returns(monkeypatch)

    assert bridge.main() == 0
    with kb.connect() as conn:
        task = kb.get_task(conn, card)
    assert task.status == "done"


def test_bridge_summary_carries_opencodes_report(card, monkeypatch):
    """The summary is the handover to the reviewer — it must be OpenCode's
    actual closing output, not a fabricated 'ok'."""
    _fake_opencode(monkeypatch, (0, "IMPLEMENTED: add() and multiply()"))
    _gate_returns(monkeypatch)

    bridge.main()
    with kb.connect() as conn:
        task = kb.get_task(conn, card)
        runs = kb.list_runs(conn, card)
    blob = (task.result or "") + "".join((r.summary or "") for r in runs)
    assert "IMPLEMENTED: add() and multiply()" in blob


def test_bridge_fires_the_completion_gate(card, monkeypatch):
    """★ The gate is consulted as `kanban_complete`, so every hook the host
    applies to a Hermes worker's completion applies here too."""
    _fake_opencode(monkeypatch, (0, "done"))
    seen = _gate_returns(monkeypatch)

    bridge.main()
    assert [t for t, _ in seen] == ["kanban_complete"]


def test_bridge_respects_a_blocking_gate(card, monkeypatch):
    """★ A refused completion must NOT close the card. Without this, a host
    rule like 'a branch with no commits cannot complete' would hold for Hermes
    workers and be silently waived for OpenCode ones."""
    _fake_opencode(monkeypatch, (0, "done"), (0, "still done"), (0, "really done"))
    _gate_returns(monkeypatch, "NOTHING COMMITTED", "NOTHING COMMITTED",
                  "NOTHING COMMITTED")

    assert bridge.main() == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, card)
    assert task.status != "done", "a gated card was completed anyway"
    assert task.status in {"blocked", "triage"}


def test_bridge_hands_the_block_reason_back_to_opencode(card, monkeypatch):
    """A block reason is written for a worker to act on. OpenCode cannot see
    the gate, so the bridge relays it and lets it fix the work — the same
    'fix it and retry' contract a Hermes worker gets."""
    calls = _fake_opencode(monkeypatch, (0, "forgot to commit"), (0, "committed now"))
    _gate_returns(monkeypatch, "NOTHING COMMITTED — commit your work", None)

    assert bridge.main() == 0
    assert len(calls) == 2, "the gate's correction never reached opencode"
    # The retry continues the same session (it has the context) and carries
    # the reason verbatim.
    assert "--continue" in calls[1]
    assert "NOTHING COMMITTED — commit your work" in calls[1][-1]
    with kb.connect() as conn:
        assert kb.get_task(conn, card).status == "done"


def test_bridge_gives_up_to_a_human_not_a_loop(card, monkeypatch):
    """A gate that keeps refusing means the card needs a person. Bounded
    retries, then `needs_input` — not an infinite correction loop."""
    _fake_opencode(monkeypatch, *[(0, "nope")] * 10)
    _gate_returns(monkeypatch, *["STILL WRONG"] * 10)

    bridge.main()
    with kb.connect() as conn:
        task = kb.get_task(conn, card)
    assert task.status in {"blocked", "triage"}


def test_runtime_failure_is_not_a_work_failure(card, monkeypatch):
    """opencode exiting non-zero means the runtime broke, not that the work is
    wrong: `transient` keeps it retryable and lets the recurrence limiter
    escalate a permanently broken runtime instead of spinning on it."""
    _fake_opencode(monkeypatch, (127, "opencode: command not found"))
    _gate_returns(monkeypatch)

    assert bridge.main() == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, card)
    assert task.status != "done"
    assert task.status in {"blocked", "triage", "todo"}


def test_prompt_tells_the_worker_to_commit(card, monkeypatch):
    """OpenCode cannot discover the completion gate — nothing in its context
    hints that anything is watching — so the briefing has to say it."""
    monkeypatch.setenv("HERMES_KANBAN_BRANCH", "wt/1-thing")
    calls = _fake_opencode(monkeypatch, (0, "done"))
    _gate_returns(monkeypatch)

    bridge.main()
    prompt = calls[0][-1]
    assert "do the thing" in prompt and "the body" in prompt
    assert "wt/1-thing" in prompt
    assert "commit" in prompt.lower()


def test_headless_run_cannot_wait_for_permission(card, monkeypatch):
    """Without --auto the run blocks on the first edit's permission prompt and
    dies at the timeout having done nothing."""
    calls = _fake_opencode(monkeypatch, (0, "done"))
    _gate_returns(monkeypatch)

    bridge.main()
    assert "--auto" in calls[0]


def test_missing_task_is_fatal_not_silent(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_does_not_exist")
    assert bridge.main() == 2
