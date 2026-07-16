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

import threading
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
    """Queue up (returncode, stdout) results for successive opencode runs.

    Fakes Popen rather than run(): the bridge streams OpenCode's output line by
    line (a card takes minutes, and a log that only appears at the end cannot
    tell "working" from "hung"), so a run()-shaped double would be testing a
    call the bridge no longer makes.
    """
    calls = []
    queue = list(results)

    class _FakeProc:
        def __init__(self, rc, out):
            self._rc = rc
            self.stdout = iter(out.splitlines(keepends=True))

        def wait(self, timeout=None):
            return self._rc

        def kill(self):
            pass

    def _popen(argv, **kwargs):
        calls.append(argv)
        rc, out = queue.pop(0) if queue else (0, "done")
        return _FakeProc(rc, out)

    monkeypatch.setattr("subprocess.Popen", _popen)
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


def test_runtime_settings_come_from_the_dispatcher(card, monkeypatch):
    """★ The dispatcher resolved the binary and model against the ROOT config
    and passed them down; we must use its answer.

    We run with HERMES_HOME pointing at the assignee's profile, and Hermes
    profiles do not inherit — they merge onto DEFAULT_CONFIG, never onto the
    root's. A bridge that re-derives this reads a different file than the
    process that decided the card was spawnable, so a card validates, spawns,
    and then dies with "binary not found" because the profile's config never
    carried the path.
    """
    monkeypatch.setenv("HERMES_OPENCODE_COMMAND", "/opt/oc/opencode")
    monkeypatch.setenv("HERMES_OPENCODE_MODEL", "prov/model-x")
    monkeypatch.setenv("HERMES_OPENCODE_EXTRA_ARGS", '["--pure"]')
    calls = _fake_opencode(monkeypatch, (0, "done"))
    _gate_returns(monkeypatch)

    bridge.main()
    argv = calls[0]
    assert argv[0] == "/opt/oc/opencode"
    assert argv[argv.index("--model") + 1] == "prov/model-x"
    assert "--pure" in argv


def test_opencode_output_is_streamed_to_the_worker_log(card, monkeypatch, capsys):
    """A card is minutes of work. If its log only appears once the run ends,
    nobody watching `hermes kanban log` can tell working from hung — which is
    the one question a worker log must answer."""
    _fake_opencode(monkeypatch, (0, "step one\nstep two\nstep three\n"))
    _gate_returns(monkeypatch)

    bridge.main()
    out = capsys.readouterr().out
    assert "step one" in out and "step three" in out


def test_a_wedged_opencode_is_killed(card, monkeypatch):
    """★ The watchdog must not depend on OpenCode saying anything.

    We consume its pipe ourselves, so a CLI that wedges silently would block on
    readline forever with no deadline ever checked — the card holds its claim
    until the TTL expires. The kill has to come from a timer, not from the loop.
    """
    killed = {"v": False}

    class _Wedged:
        def __init__(self):
            self._ev = threading.Event()
            # A generator body does not run until the first next(), so this
            # blocks when the bridge READS the pipe — not when it opens it.
            # (Blocking in __init__ instead would hang inside Popen, before
            # the watchdog is even armed, and prove nothing.)
            self.stdout = self._block()

        def _block(self):
            self._ev.wait(10)   # released only by kill()
            return
            yield               # unreachable — makes this a generator

        def wait(self, timeout=None):
            return -9

        def kill(self):
            killed["v"] = True
            self._ev.set()

    def _popen(argv, **kwargs):
        return _Wedged()

    monkeypatch.setattr("subprocess.Popen", _popen)
    monkeypatch.setattr(bridge, "DEFAULT_TIMEOUT", 1)
    _gate_returns(monkeypatch)

    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET max_runtime_seconds=1 WHERE id=?", (card,))
        conn.commit()

    bridge.main()
    assert killed["v"], "a silently wedged opencode was never killed"
    with kb.connect() as conn:
        assert kb.get_task(conn, card).status != "done"


def test_card_model_override_beats_the_default(card, monkeypatch):
    """Same precedence a Hermes card's model_override gets."""
    monkeypatch.setenv("HERMES_OPENCODE_MODEL", "prov/default")
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET model_override=? WHERE id=?",
                     ("prov/special", card))
        conn.commit()
    calls = _fake_opencode(monkeypatch, (0, "done"))
    _gate_returns(monkeypatch)

    bridge.main()
    argv = calls[0]
    assert argv[argv.index("--model") + 1] == "prov/special"
