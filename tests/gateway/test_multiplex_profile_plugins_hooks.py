"""Per-profile plugins and shell hooks in the multiplexed gateway.

The bug these tests pin down: the gateway discovered plugins and registered
shell hooks ONCE at startup, from the ROOT home, into one process-global
PluginManager. A Telegram topic routed to a secondary profile (via
``gateway.profile_routes`` or a ``/profile`` binding) got that profile's
model/config for the turn but NEITHER its plugins (``orchestrate_*`` tools,
``/orchestrator`` command never discovered) NOR its ``pre_tool_call`` shell
hooks — the profile's guards silently never fired, while working fine under
``hermes -p <profile>`` in the CLI.

The fix, covered here:

* ``hermes_cli.plugins.get_plugin_manager()`` resolves a PER-PROFILE manager
  when (and only when) multiplexing is active AND the context's HERMES_HOME
  override points at a non-default profile home.
* ``agent.shell_hooks.register_from_config`` keys its idempotence per
  manager, so two profiles may register the same (event, matcher, command).
* ``gateway.run._ensure_profile_plugin_runtime`` performs the per-profile
  discovery + hook registration (eagerly at startup via
  ``_setup_profile_plugin_runtimes``, lazily as a safety net from
  ``_resolve_profile_home_for_source``).
* ``model_tools`` hides (and refuses to dispatch) plugin tools owned
  exclusively by ANOTHER profile's manager.

With ``gateway.multiplex_profiles`` off, everything must behave exactly as
before: one global manager, no per-profile anything.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from agent import secret_scope, shell_hooks
from hermes_cli import plugins as plugins_mod
from hermes_constants import (
    reset_hermes_home_override,
    set_hermes_home_override,
)


# ── helpers ────────────────────────────────────────────────────────────────


@contextmanager
def _scoped(home: Path):
    """Enter a context-local HERMES_HOME override (what a routed turn gets)."""
    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _root_home() -> Path:
    """The per-test base HERMES_HOME installed by the global conftest."""
    return Path(os.environ["HERMES_HOME"])


def _make_profile(name: str) -> Path:
    prof = _root_home() / "profiles" / name
    prof.mkdir(parents=True, exist_ok=True)
    return prof


def _write_block_script(dirpath: Path, name: str, message: str) -> Path:
    """A pre_tool_call hook script that always blocks with *message*."""
    script = dirpath / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({{'action': 'block', 'message': {message!r}}}))\n"
    )
    script.chmod(0o755)
    return script


def _hook_cfg(script: Path) -> dict:
    return {
        "hooks": {
            "pre_tool_call": [{"command": f"python3 {script}"}],
        },
    }


def _register_plugin_tool(manager, tool_name: str, toolset: str) -> None:
    """Register a tool through the real PluginContext path on *manager*."""
    manifest = plugins_mod.PluginManifest(
        name="mux-test-plugin", source="user", kind="standalone",
        key="mux-test-plugin",
    )
    ctx = plugins_mod.PluginContext(manifest, manager)
    ctx.register_tool(
        name=tool_name,
        toolset=toolset,
        schema={
            "name": tool_name,
            "description": "multiplex isolation test tool",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args=None, **kwargs: "ok-from-plugin",
    )


@pytest.fixture(autouse=True)
def _multiplex_state_reset():
    """Keep the process-global multiplex flag and registries test-local."""
    secret_scope.set_multiplex_active(False)
    plugins_mod._reset_profile_plugin_managers_for_tests()
    shell_hooks.reset_for_tests()
    yield
    shell_hooks.reset_for_tests()
    plugins_mod._reset_profile_plugin_managers_for_tests()
    secret_scope.set_multiplex_active(False)


# ── (a) multiplexing OFF: byte-identical to historical behavior ───────────


class TestMultiplexOff:
    def test_home_override_does_not_change_manager(self):
        """Without multiplexing, an override still resolves the singleton."""
        base_manager = plugins_mod.get_plugin_manager()
        with _scoped(_make_profile("offprof")):
            assert plugins_mod.get_plugin_manager() is base_manager
            assert plugins_mod.current_profile_scope_key() is None
        assert plugins_mod.get_plugin_manager() is base_manager

    def test_foreign_plugin_tools_empty(self):
        """No foreign-tool set (nothing hidden) when multiplexing is off."""
        # Even with a leftover profile manager holding tools, the flag being
        # off means nothing is subtracted from anyone's tool list.
        stale = plugins_mod.PluginManager()
        stale._plugin_tool_names.add("ghost_tool")
        plugins_mod._profile_plugin_managers["/stale/home"] = stale
        assert plugins_mod.foreign_profile_plugin_tool_names() == set()

    def test_shell_hook_registration_dedupes_globally(self, tmp_path, monkeypatch):
        """Single-manager idempotence semantics are unchanged."""
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")
        script = _write_block_script(tmp_path, "hook.py", "nope")
        cfg = _hook_cfg(script)

        first = shell_hooks.register_from_config(cfg, accept_hooks=True)
        # A second registration — even from inside a profile-home override —
        # lands on the same global manager and dedupes, exactly as before.
        with _scoped(_make_profile("offprof")):
            second = shell_hooks.register_from_config(cfg, accept_hooks=True)

        assert len(first) == 1
        assert second == []
        manager = plugins_mod.get_plugin_manager()
        assert len(manager._hooks.get("pre_tool_call", [])) == 1

    def test_gateway_ensure_is_noop(self):
        from gateway import run as gw_run

        with mock.patch.object(gw_run, "_profile_runtime_scope") as scope:
            gw_run._ensure_profile_plugin_runtime(_make_profile("offprof"))
        scope.assert_not_called()

    def test_setup_profile_plugin_runtimes_noop(self):
        from gateway import run as gw_run
        from gateway.config import GatewayConfig

        runner = gw_run.GatewayRunner.__new__(gw_run.GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=False)
        with mock.patch("hermes_cli.profiles.profiles_to_serve") as pts:
            runner._setup_profile_plugin_runtimes()
        pts.assert_not_called()


# ── manager resolution under multiplexing ─────────────────────────────────


class TestProfileManagerResolution:
    def test_profile_scope_resolves_own_manager(self):
        secret_scope.set_multiplex_active(True)
        prof_a = _make_profile("profa")
        prof_b = _make_profile("profb")

        base_manager = plugins_mod.get_plugin_manager()
        with _scoped(prof_a):
            manager_a = plugins_mod.get_plugin_manager()
            assert manager_a is not base_manager
            # Stable: same scope → same manager.
            assert plugins_mod.get_plugin_manager() is manager_a
        with _scoped(prof_b):
            manager_b = plugins_mod.get_plugin_manager()
            assert manager_b is not base_manager
            assert manager_b is not manager_a
        # Unscoped → global singleton, untouched.
        assert plugins_mod.get_plugin_manager() is base_manager

    def test_default_profile_home_maps_to_global_manager(self):
        """A turn scoped to the multiplexer's OWN home uses the root manager."""
        secret_scope.set_multiplex_active(True)
        base_manager = plugins_mod.get_plugin_manager()
        with _scoped(_root_home()):
            assert plugins_mod.current_profile_scope_key() is None
            assert plugins_mod.get_plugin_manager() is base_manager


# ── (b) + (c) routed profile hooks fire, and only for their profile ───────


class TestProfileHooksFire:
    def test_routed_profile_hook_fires_for_that_turn_only(self, tmp_path):
        secret_scope.set_multiplex_active(True)
        prof_a = _make_profile("profa")
        prof_b = _make_profile("profb")

        script = _write_block_script(tmp_path, "guard_a.py", "blocked-by-profa")
        with _scoped(prof_a):
            registered = shell_hooks.register_from_config(
                _hook_cfg(script), accept_hooks=True,
            )
        assert len(registered) == 1

        from hermes_cli.plugins import resolve_pre_tool_block

        # (b) The routed profile's hook fires on its turn.
        with _scoped(prof_a):
            assert (
                resolve_pre_tool_block("terminal", {"command": "x"})
                == "blocked-by-profa"
            )
        # (c) Another profile's turn does NOT see profa's hook.
        with _scoped(prof_b):
            assert resolve_pre_tool_block("terminal", {"command": "x"}) is None
        # Neither does the default profile (unscoped).
        assert resolve_pre_tool_block("terminal", {"command": "x"}) is None

    def test_same_hook_command_registers_on_both_profiles(self, tmp_path):
        """Two profiles declaring the SAME guard both get it (per-manager
        idempotence — a process-global dedupe set would silently disarm the
        second profile's guard)."""
        secret_scope.set_multiplex_active(True)
        prof_a = _make_profile("profa")
        prof_b = _make_profile("profb")

        script = _write_block_script(tmp_path, "shared_guard.py", "iron-rule")
        cfg = _hook_cfg(script)

        with _scoped(prof_a):
            first = shell_hooks.register_from_config(cfg, accept_hooks=True)
            manager_a = plugins_mod.get_plugin_manager()
        with _scoped(prof_b):
            second = shell_hooks.register_from_config(cfg, accept_hooks=True)
            manager_b = plugins_mod.get_plugin_manager()

        assert len(first) == 1
        assert len(second) == 1
        assert len(manager_a._hooks.get("pre_tool_call", [])) == 1
        assert len(manager_b._hooks.get("pre_tool_call", [])) == 1

        from hermes_cli.plugins import resolve_pre_tool_block

        with _scoped(prof_a):
            assert resolve_pre_tool_block("terminal", {}) == "iron-rule"
        with _scoped(prof_b):
            assert resolve_pre_tool_block("terminal", {}) == "iron-rule"

    def test_re_registration_on_same_profile_is_idempotent(self, tmp_path):
        secret_scope.set_multiplex_active(True)
        prof_a = _make_profile("profa")
        script = _write_block_script(tmp_path, "guard.py", "blocked")
        cfg = _hook_cfg(script)

        with _scoped(prof_a):
            first = shell_hooks.register_from_config(cfg, accept_hooks=True)
            second = shell_hooks.register_from_config(cfg, accept_hooks=True)
            manager_a = plugins_mod.get_plugin_manager()

        assert len(first) == 1
        assert second == []
        assert len(manager_a._hooks.get("pre_tool_call", [])) == 1


# ── gateway wiring: eager startup + lazy safety net ───────────────────────


class TestGatewayProfileRuntimeSetup:
    def test_ensure_registers_profile_hooks_from_profile_config(self, tmp_path):
        """The real gateway path: profile config.yaml declares a
        pre_tool_call hook; _ensure_profile_plugin_runtime makes it fire for
        that profile's turns (and nobody else's)."""
        from gateway import run as gw_run

        secret_scope.set_multiplex_active(True)
        prof = _make_profile("guarded")
        script = _write_block_script(tmp_path, "guard.py", "iron-rule-block")
        (prof / "config.yaml").write_text(
            "hooks_auto_accept: true\n"
            "hooks:\n"
            "  pre_tool_call:\n"
            f"    - command: python3 {script}\n"
        )

        gw_run._ensure_profile_plugin_runtime(prof)

        from hermes_cli.plugins import resolve_pre_tool_block

        with _scoped(prof):
            assert (
                resolve_pre_tool_block("terminal", {"command": "rm"})
                == "iron-rule-block"
            )
        # The default profile's turns are untouched.
        assert resolve_pre_tool_block("terminal", {"command": "rm"}) is None

    def test_ensure_is_idempotent(self, tmp_path):
        from gateway import run as gw_run

        secret_scope.set_multiplex_active(True)
        prof = _make_profile("guarded")
        script = _write_block_script(tmp_path, "guard.py", "blocked")
        (prof / "config.yaml").write_text(
            "hooks_auto_accept: true\n"
            "hooks:\n"
            "  pre_tool_call:\n"
            f"    - command: python3 {script}\n"
        )

        gw_run._ensure_profile_plugin_runtime(prof)
        gw_run._ensure_profile_plugin_runtime(prof)

        with _scoped(prof):
            manager = plugins_mod.get_plugin_manager()
        assert len(manager._hooks.get("pre_tool_call", [])) == 1

    def test_ensure_skips_base_home(self):
        """The default profile's home is the startup path's job — the
        per-profile helper must never re-scope the root manager."""
        from gateway import run as gw_run

        secret_scope.set_multiplex_active(True)
        with mock.patch(
            "hermes_cli.plugins.discover_plugins"
        ) as discover:
            gw_run._ensure_profile_plugin_runtime(_root_home())
        discover.assert_not_called()

    def test_startup_covers_every_served_profile(self):
        from gateway import run as gw_run
        from gateway.config import GatewayConfig

        runner = gw_run.GatewayRunner.__new__(gw_run.GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)

        seen = []
        with mock.patch.object(
            gw_run, "_ensure_profile_plugin_runtime",
            side_effect=lambda home: seen.append(str(home)),
        ), mock.patch(
            "hermes_cli.profiles.profiles_to_serve",
            return_value=[
                ("default", Path("/base/home")),
                ("orchestrator", Path("/base/home/profiles/orchestrator")),
                ("tech", Path("/base/home/profiles/tech")),
            ],
        ):
            runner._setup_profile_plugin_runtimes()

        assert seen == [
            "/base/home",
            "/base/home/profiles/orchestrator",
            "/base/home/profiles/tech",
        ]

    def test_resolve_profile_home_triggers_lazy_setup(self, monkeypatch):
        """Profiles bound after startup (e.g. /profile in a new topic) get
        their runtime set up on first resolution."""
        from gateway import run as gw_run
        from gateway.config import GatewayConfig

        runner = gw_run.GatewayRunner.__new__(gw_run.GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        prof = _make_profile("lazyprof")

        source = mock.MagicMock()
        source.profile = "lazyprof"
        source.chat_id = "123"

        calls = []
        monkeypatch.setattr(
            gw_run, "_ensure_profile_plugin_runtime",
            lambda home: calls.append(Path(home)),
        )
        resolved = runner._resolve_profile_home_for_source(source)

        assert resolved == prof
        assert calls == [prof]


# ── (d) plugin tool visibility is per profile ─────────────────────────────


class TestPluginToolVisibility:
    TOOL = "mux_orchestrate_test_tool"
    TOOLSET = "mux_orchestrate_test"

    @pytest.fixture(autouse=True)
    def _registry_cleanup(self):
        yield
        from tools.registry import registry

        try:
            registry.deregister(self.TOOL)
        except Exception:
            pass

    def _setup_profiles_with_tool(self):
        secret_scope.set_multiplex_active(True)
        prof_a = _make_profile("orch")
        prof_b = _make_profile("tech")
        with _scoped(prof_a):
            manager_a = plugins_mod.get_plugin_manager()
            _register_plugin_tool(manager_a, self.TOOL, self.TOOLSET)
        with _scoped(prof_b):
            # Materialize profile B's manager (a routed turn would).
            plugins_mod.get_plugin_manager()
        return prof_a, prof_b

    def test_tool_schemas_visible_only_to_owning_profile(self):
        prof_a, prof_b = self._setup_profiles_with_tool()
        import model_tools

        def _names(scope_home=None):
            if scope_home is None:
                defs = model_tools.get_tool_definitions(
                    quiet_mode=True, skip_tool_search_assembly=True,
                )
            else:
                with _scoped(scope_home):
                    defs = model_tools.get_tool_definitions(
                        quiet_mode=True, skip_tool_search_assembly=True,
                    )
            return {d["function"]["name"] for d in defs}

        assert self.TOOL in _names(prof_a)
        # The other profile must NOT see it — including on the memoized
        # path (both calls share every cache-key input except the profile
        # scope, so this also guards the cache-key fix).
        assert self.TOOL not in _names(prof_b)
        # Nor the default profile.
        assert self.TOOL not in _names(None)
        # And the owning profile still sees it afterwards (cache intact).
        assert self.TOOL in _names(prof_a)

    def test_foreign_tool_dispatch_is_unknown(self):
        prof_a, prof_b = self._setup_profiles_with_tool()
        import model_tools

        with _scoped(prof_b):
            result = model_tools.handle_function_call(self.TOOL, {})
        assert json.loads(result)["error"] == f"Unknown tool: {self.TOOL}"

        with _scoped(prof_a):
            result = model_tools.handle_function_call(self.TOOL, {})
        assert result == "ok-from-plugin"

    def test_tool_shared_by_both_profiles_stays_visible(self):
        """A plugin enabled by BOTH profiles is foreign to neither."""
        secret_scope.set_multiplex_active(True)
        prof_a = _make_profile("orch")
        prof_b = _make_profile("tech")
        with _scoped(prof_a):
            _register_plugin_tool(
                plugins_mod.get_plugin_manager(), self.TOOL, self.TOOLSET,
            )
        with _scoped(prof_b):
            manager_b = plugins_mod.get_plugin_manager()
            # Same plugin enabled on B: its manager tracks the same name
            # (the registry.register overwrite is a no-op for our purposes).
            manager_b._plugin_tool_names.add(self.TOOL)

        with _scoped(prof_b):
            assert plugins_mod.foreign_profile_plugin_tool_names() == set()
        with _scoped(prof_a):
            assert plugins_mod.foreign_profile_plugin_tool_names() == set()
