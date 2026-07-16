"""Tests for the spawn-time worker runtime registry (Tier 2, ticket 02).

Covers the **spawn adapter registry** that maps a task's
``worker_runtime`` to the spawn function the dispatcher invokes:

* the registry exposes a stable ``hermes`` adapter wrapping
  ``_default_spawn``;
* unknown runtimes fail closed (no silent fall-through);
* the registry is additive — a new adapter can be registered without
  touching the dispatcher;
* ready Hermes tasks with a valid assignee still dispatch end-to-end
  under the registry (regression: PID recorded, status ``running``);
* the Hermes adapter preserves the env pins ``_default_spawn`` injects;
* external runtimes skip the ``profile_exists`` gate and the
  assignee-required skip so they are not wrongly parked in
  ``skipped_nonspawnable`` / ``skipped_unassigned``;
* an explicit ``spawn_fn`` argument still overrides the registry
  (back-compat with the existing test-suite stubs).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_runtimes as runtimes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
def all_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture
def opencode_enabled(monkeypatch):
    """Force the opencode runtime to be enabled regardless of host binary."""
    real = runtimes.is_runtime_enabled

    def _enabled(name):
        if runtimes.normalize_runtime(name) == "opencode":
            return True
        return real(name)

    monkeypatch.setattr(runtimes, "is_runtime_enabled", _enabled)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

def test_registry_resolves_hermes_adapter():
    adapter = kb.resolve_spawn_adapter("hermes")
    assert adapter.name == "hermes"
    assert adapter.external is False
    assert callable(adapter.spawn)


def test_registry_defaults_to_hermes_for_missing_runtime():
    assert kb.resolve_spawn_adapter(None).name == "hermes"
    assert kb.resolve_spawn_adapter("").name == "hermes"


def test_registry_unknown_runtime_raises():
    with pytest.raises(ValueError, match="unknown worker runtime"):
        kb.resolve_spawn_adapter("nope")


def test_registry_is_additive():
    """A new adapter can be registered without touching the dispatcher."""
    sentinel = object()

    def _stub_spawn(task, workspace, *, board=None):
        return None

    adapter = kb.SpawnAdapter(name="acme", external=True, spawn=_stub_spawn)
    kb.register_spawn_adapter(adapter)
    try:
        resolved = kb.resolve_spawn_adapter("acme")
        assert resolved.name == "acme"
        assert resolved.external is True
        assert resolved.spawn is _stub_spawn
    finally:
        kb.unregister_spawn_adapter("acme")
    # After removal it is unknown again.
    with pytest.raises(ValueError, match="unknown worker runtime"):
        kb.resolve_spawn_adapter("acme")


# ---------------------------------------------------------------------------
# Regression: Hermes dispatch end-to-end under the registry
# ---------------------------------------------------------------------------

def test_hermes_task_dispatches_under_registry(kanban_home, all_spawnable):
    """A ready Hermes task with a valid assignee claims, spawns via the
    registry, records worker_pid, and lands in ``running``."""
    spawns = []

    def fake_spawn(task, workspace, *, board=None):
        spawns.append((task.id, task.assignee, task.worker_runtime, workspace))
        return 9999

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="hermes card", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)

    assert len(spawns) == 1
    assert spawns[0][0] == tid
    assert spawns[0][1] == "alice"
    assert spawns[0][2] == "hermes"
    assert task.status == "running"
    assert task.worker_pid == 9999


def test_hermes_adapter_preserves_env_pins(tmp_path, monkeypatch):
    """The Hermes adapter (wrapping _default_spawn) injects the same env
    pins the historic spawn path did — board DB, workspaces root, task id."""
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            self.pid = 4242

    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    task = kb.Task(
        id="t_env_pins", title="x", body=None, assignee="coder",
        status="ready", priority=0, created_by=None, created_at=0,
        started_at=None, completed_at=None, workspace_kind="worktree",
        workspace_path=str(tmp_path / "ws"), claim_lock=None,
        claim_expires=None, tenant=None, branch_name="wt/t_env_pins",
    )
    adapter = kb.resolve_spawn_adapter("hermes")
    pid = adapter.spawn(task, str(tmp_path / "ws"))

    assert pid == 4242
    env = captured["env"]
    assert env["HERMES_KANBAN_TASK"] == "t_env_pins"
    assert env["HERMES_KANBAN_DB"] == str(default_home / "kanban.db")
    assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(
        default_home / "kanban" / "workspaces"
    )
    assert env["HERMES_KANBAN_BRANCH"] == "wt/t_env_pins"
    # argv is the hermes profile-worker invocation.
    assert "-p" in captured["cmd"]
    assert "chat" in captured["cmd"]


def test_hermes_adapter_writes_log_file(tmp_path, monkeypatch):
    """The Hermes adapter writes worker output to the per-task log path."""
    default_home = tmp_path / ".hermes"
    default_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 7777

    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    task = kb.Task(
        id="t_log", title="x", body=None, assignee="coder",
        status="ready", priority=0, created_by=None, created_at=0,
        started_at=None, completed_at=None, workspace_kind="scratch",
        workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None,
    )
    kb.resolve_spawn_adapter("hermes").spawn(task, str(tmp_path / "ws"))

    log_path = kb.worker_logs_dir() / "t_log.log"
    assert log_path.exists()


# ---------------------------------------------------------------------------
# External runtime dispatch selection
# ---------------------------------------------------------------------------

def test_external_runtime_skips_profile_exists_gate(
    kanban_home, opencode_enabled, monkeypatch
):
    """An opencode task whose assignee is NOT a real Hermes profile must
    not be bucketed as ``skipped_nonspawnable`` — the profile_exists gate
    is Hermes-only."""
    from hermes_cli import profiles
    # No profile exists at all.
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)

    reached_spawn = []

    def fake_spawn(task, workspace, *, board=None):
        reached_spawn.append(task.id)
        return 1234

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="oc", assignee="not-a-profile",
            worker_runtime="opencode",
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert tid not in res.skipped_nonspawnable
    assert tid not in res.skipped_unassigned
    assert reached_spawn == [tid]


def test_external_runtime_no_assignee_still_spawns(
    kanban_home, opencode_enabled, all_spawnable
):
    """An opencode task with no assignee at all still reaches the spawn
    call — external runtimes may omit the assignee."""
    reached_spawn = []

    def fake_spawn(task, workspace, *, board=None):
        reached_spawn.append((task.id, task.assignee))
        return 5555

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="oc no-assignee", worker_runtime="opencode")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        task = kb.get_task(conn, tid)

    assert tid not in res.skipped_unassigned
    assert len(reached_spawn) == 1
    assert reached_spawn[0][0] == tid
    assert task.status == "running"
    assert task.worker_pid == 5555


def test_external_runtime_no_adapter_fails_closed(
    kanban_home, all_spawnable, monkeypatch
):
    """A known runtime with NO spawn adapter records a spawn failure — the
    circuit-breaker path — rather than silently no-op'ing.

    Uses a throwaway runtime rather than a real one: this asserts the
    *registry's* fail-closed behaviour, so it must keep testing that even
    after every shipped runtime has an adapter. (It once used ``opencode``,
    which stopped being an example of "no adapter" the moment one landed —
    a test that silently changes meaning is worse than no test.)
    """
    from hermes_cli import kanban_worker_runtimes as rt

    monkeypatch.setitem(
        rt._REGISTRY, "ghost", rt.WorkerRuntime(name="ghost", external=True)
    )
    monkeypatch.setattr(rt, "is_runtime_enabled", lambda name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ghost no-adapter", worker_runtime="ghost")
        res = kb.dispatch_once(conn)  # no spawn_fn override
        task = kb.get_task(conn, tid)

    # Spawn failed → task back to ready, failure counted.
    assert task.status == "ready"
    assert task.consecutive_failures >= 1
    assert tid in [t for t in res.auto_blocked] or task.consecutive_failures >= 1


# ---------------------------------------------------------------------------
# The OpenCode adapter
# ---------------------------------------------------------------------------

def _oc_task(tmp_path, **over):
    base = dict(
        id="t_oc", title="x", body=None, assignee=None,
        status="ready", priority=0, created_by=None, created_at=0,
        started_at=None, completed_at=None, workspace_kind="worktree",
        workspace_path=str(tmp_path / "ws"), claim_lock=None,
        claim_expires=None, tenant=None, branch_name="wt/t_oc",
        worker_runtime="opencode",
    )
    base.update(over)
    return kb.Task(**base)


def test_opencode_adapter_spawns_the_bridge_not_opencode(tmp_path, monkeypatch):
    """★ The PID handed to the dispatcher must be the lifecycle bridge.

    Spawning `opencode run` directly is the obvious implementation and it is
    broken: OpenCode has no kanban tools, so that process would do the work,
    exit, and never resolve its card — leaving it `running` until the claim
    expires and it is re-run, forever. The bridge is what closes the card, so
    the bridge is what the dispatcher must be watching.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    (tmp_path / "ws").mkdir()

    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            self.pid = 777

    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    pid = kb.resolve_spawn_adapter("opencode").spawn(
        _oc_task(tmp_path), str(tmp_path / "ws")
    )

    assert pid == 777
    assert captured["cmd"][1:] == ["-m", "hermes_cli.kanban_opencode_bridge"]
    assert "run" not in captured["cmd"], "spawned opencode directly, not the bridge"


def test_opencode_adapter_preserves_board_pins(tmp_path, monkeypatch):
    """A runtime swap changes who does the work, not how the board talks to
    it: the bridge gets the same board/workspace pins a Hermes worker gets."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    (tmp_path / "ws").mkdir()

    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["env"] = kwargs.get("env", {})
            self.pid = 778

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    kb.resolve_spawn_adapter("opencode").spawn(
        _oc_task(tmp_path, current_run_id=9), str(tmp_path / "ws")
    )

    env = captured["env"]
    assert env["HERMES_KANBAN_TASK"] == "t_oc"
    assert env["HERMES_KANBAN_DB"] == str(home / "kanban.db")
    assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(home / "kanban" / "workspaces")
    assert env["HERMES_KANBAN_BRANCH"] == "wt/t_oc"
    assert env["HERMES_KANBAN_RUN_ID"] == "9"
    assert env["HERMES_AGENT_CONTEXT"] == "worker"
    # ★ Without this the bridge's completion gate registers no shell hooks and
    #   allows everything — a gate that silently always passes.
    assert env["HERMES_ACCEPT_HOOKS"] == "1"


def test_opencode_adapter_survives_missing_assignee(tmp_path, monkeypatch):
    """External runtimes may omit the assignee — spawning must not require a
    Hermes profile to resolve."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    (tmp_path / "ws").mkdir()

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 779

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    assert kb.resolve_spawn_adapter("opencode").spawn(
        _oc_task(tmp_path, assignee=None), str(tmp_path / "ws")
    ) == 779


def test_opencode_task_dispatches_under_registry(
    kanban_home, opencode_enabled, all_spawnable, monkeypatch
):
    """End-to-end through the dispatcher: an opencode card reaches `running`
    with the bridge's PID recorded — no assignee, no profile."""
    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 31337

    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="oc card", worker_runtime="opencode")
        kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)

    assert task.status == "running"
    assert task.worker_pid == 31337


# ---------------------------------------------------------------------------
# Back-compat: explicit spawn_fn overrides the registry
# ---------------------------------------------------------------------------

def test_explicit_spawn_fn_overrides_registry(kanban_home, all_spawnable):
    """An explicit ``spawn_fn`` argument still wins over the registry so
    the existing test-suite stubs keep working."""
    registry_called = []
    real_hermes = kb.resolve_spawn_adapter("hermes").spawn

    def tracking_spawn(task, workspace, *, board=None):
        registry_called.append(task.id)
        return None

    # Patch the hermes adapter's spawn to detect if the registry was used.
    adapter = kb.resolve_spawn_adapter("hermes")
    original = adapter.spawn
    # We can't mutate a frozen adapter; instead verify spawn_fn is used by
    # checking that a custom stub is called and the registry is not.
    spawns = []

    def fake_spawn(task, workspace, *, board=None):
        spawns.append(task.id)
        return 1111

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="override", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [tid]
