"""Tests for the Kanban worker_runtime field (Tier 2 external workers).

Covers ticket 01 — Persist ``worker_runtime`` on tasks (default hermes):

* task rows store ``worker_runtime`` defaulting to ``hermes`` (no
  migration surprise for existing boards),
* the kernel, CLI, and ``kanban_create`` tool accept + persist it,
* show/list surface it,
* unknown and disabled runtimes are rejected at write time,
* Hermes runtime keeps requiring a profile assignee; external runtimes
  may omit it,
* Hermes-only flags (``goal_mode``, ``skills``) are rejected for
  external runtimes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_runtimes as runtimes


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def opencode_enabled(monkeypatch):
    """Force the opencode runtime to be enabled regardless of host binary."""
    real = runtimes.is_runtime_enabled

    def _enabled(name):
        if runtimes.normalize_runtime(name) == "opencode":
            return True
        return real(name)

    # create_task / validate_runtime look up is_runtime_enabled on the
    # runtimes module at call time, so patching it there is sufficient.
    monkeypatch.setattr(runtimes, "is_runtime_enabled", _enabled)


# ---------------------------------------------------------------------------
# Persistence / default
# ---------------------------------------------------------------------------

def test_default_runtime_is_hermes(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="alice")
        task = kb.get_task(conn, tid)
        assert task.worker_runtime == "hermes"


def test_explicit_hermes_runtime_persists(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="card", assignee="alice", worker_runtime="hermes"
        )
        assert kb.get_task(conn, tid).worker_runtime == "hermes"


def test_opencode_runtime_persists_without_assignee(kanban_home, opencode_enabled):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="oc card", worker_runtime="opencode")
        task = kb.get_task(conn, tid)
        assert task.worker_runtime == "opencode"
        assert task.assignee is None


def test_runtime_is_case_insensitive(kanban_home, opencode_enabled):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="oc", worker_runtime="OpenCode")
        assert kb.get_task(conn, tid).worker_runtime == "opencode"


def test_created_event_records_runtime(kanban_home, opencode_enabled):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="oc", worker_runtime="opencode")
        events = kb.list_events(conn, tid)
        created = [e for e in events if e.kind == "created"]
        assert created and created[0].payload["worker_runtime"] == "opencode"


# ---------------------------------------------------------------------------
# Legacy DB migration
# ---------------------------------------------------------------------------

def test_legacy_db_migrates_worker_runtime_column(tmp_path, monkeypatch):
    """A DB created before the column existed must gain worker_runtime
    defaulting to 'hermes' on first open — no migration surprise."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    db_path = kb.kanban_db_path()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="alice")
    # Simulate a legacy DB by dropping the column.
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute("BEGIN")
    conn.execute("ALTER TABLE tasks DROP COLUMN worker_runtime")
    conn.execute("COMMIT")
    conn.close()
    # Re-opening must re-add the column with the hermes default.
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.worker_runtime == "hermes"


# ---------------------------------------------------------------------------
# Validation: unknown / disabled
# ---------------------------------------------------------------------------

def test_unknown_runtime_rejected(kanban_home):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="unknown worker runtime"):
            kb.create_task(conn, title="bad", worker_runtime="codex")


def test_disabled_runtime_rejected(kanban_home, monkeypatch):
    """A known runtime that isn't enabled on this host is rejected so the
    card can't sit in ready waiting for a runtime that can never spawn."""
    monkeypatch.setattr(
        runtimes, "is_runtime_enabled",
        lambda name: runtimes.normalize_runtime(name) != "opencode",
    )
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="not enabled on this host"):
            kb.create_task(conn, title="bad", worker_runtime="opencode")


# ---------------------------------------------------------------------------
# Hermes-only flags rejected for external runtimes
# ---------------------------------------------------------------------------

def test_goal_mode_rejected_for_opencode(kanban_home, opencode_enabled):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="goal_mode is a Hermes-only"):
            kb.create_task(
                conn, title="bad", worker_runtime="opencode", goal_mode=True,
            )


def test_skills_rejected_for_opencode(kanban_home, opencode_enabled):
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="Hermes-only"):
            kb.create_task(
                conn, title="bad", worker_runtime="opencode", skills=["x"],
            )


def test_hermes_runtime_still_accepts_goal_mode_and_skills(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="ok", assignee="alice",
            worker_runtime="hermes", goal_mode=True, skills=["translation"],
        )
        task = kb.get_task(conn, tid)
        assert task.goal_mode is True
        assert task.skills == ["translation"]


# ---------------------------------------------------------------------------
# Edit path
# ---------------------------------------------------------------------------

def test_set_worker_runtime_changes_runtime(kanban_home, opencode_enabled):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="alice")
        new = kb.set_worker_runtime(conn, tid, "opencode")
        assert new == "opencode"
        assert kb.get_task(conn, tid).worker_runtime == "opencode"
        events = kb.list_events(conn, tid)
        changed = [e for e in events if e.kind == "runtime_changed"]
        assert changed and changed[0].payload == {"from": "hermes", "to": "opencode"}


def test_set_worker_runtime_rejects_unknown(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="alice")
        with pytest.raises(ValueError, match="unknown worker runtime"):
            kb.set_worker_runtime(conn, tid, "nope")


def test_set_worker_runtime_rejects_running_task(kanban_home, opencode_enabled):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="alice")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None  # status now 'running' with a claim_lock
        with pytest.raises(RuntimeError, match="currently running"):
            kb.set_worker_runtime(conn, tid, "opencode")


# ---------------------------------------------------------------------------
# list_tasks surfaces runtime
# ---------------------------------------------------------------------------

def test_list_tasks_surfaces_runtime(kanban_home, opencode_enabled):
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="h", assignee="alice")
        kb.create_task(conn, title="o", worker_runtime="opencode")
        tasks = kb.list_tasks(conn)
        runtimes_by_title = {t.title: t.worker_runtime for t in tasks}
        assert runtimes_by_title["h"] == "hermes"
        assert runtimes_by_title["o"] == "opencode"
