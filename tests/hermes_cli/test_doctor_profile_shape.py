"""Tests for the ◆ Profile Shape (silent drift) doctor section.

Hermes profiles do not inherit config (they deep-merge onto the code
DEFAULT_CONFIG, never onto the root profile's config.yaml), so a profile can
reference things it never defines and NOTHING errors — the plugin's tools
silently vanish, the guard hook silently never fires, the auxiliary judge
silently does nothing. Every test here fabricates profiles under ``tmp_path``;
none touches the developer's live ~/.hermes.
"""

import io
import contextlib
import os
import textwrap

import pytest

import hermes_cli.doctor as doctor_mod


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make_roots(tmp_path):
    """Fabricate an isolated (root_home, profiles_root, bundled_plugins) trio."""
    root_home = tmp_path / "hermes-root"
    profiles_root = tmp_path / "profiles"
    bundled = tmp_path / "bundled-plugins"
    root_home.mkdir()
    profiles_root.mkdir()
    bundled.mkdir()
    return root_home, profiles_root, bundled


def _make_profile(profiles_root, name, config_text=None):
    pdir = profiles_root / name
    pdir.mkdir()
    if config_text is not None:
        (pdir / "config.yaml").write_text(textwrap.dedent(config_text), encoding="utf-8")
    return pdir


def _make_plugin(base_dir, dirname, manifest_name=None, toolset=None, kind=None):
    """Fabricate a minimal plugin directory with plugin.yaml (+ source)."""
    plug = base_dir / "plugins" / dirname
    plug.mkdir(parents=True)
    manifest = f"name: {manifest_name or dirname}\n"
    if kind:
        manifest += f"kind: {kind}\n"
    (plug / "plugin.yaml").write_text(manifest, encoding="utf-8")
    source = "def register(ctx):\n    pass\n"
    if toolset:
        source = (
            "def register(ctx):\n"
            f'    ctx.register_tool(name="t", toolset="{toolset}", '
            "description='', parameters={}, handler=None)\n"
        )
    (plug / "__init__.py").write_text(source, encoding="utf-8")
    return plug


def _run_shape_check(tmp_path, root_home, profiles_root, bundled):
    """Run the section against fabricated roots; return (stdout, issues)."""
    issues = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod._check_profile_shape(
            issues,
            profiles_root=profiles_root,
            root_home=root_home,
            bundled_plugins_root=bundled,
        )
    return buf.getvalue(), issues


HEALTHY_CONFIG = """
    model:
      provider: myprov
      default: my-model
    providers:
      myprov:
        base_url: http://localhost:9999/v1
        default_model: my-model
    toolsets: [hermes-cli, orchestrate]
    plugins:
      enabled: [orchestrator]
    auxiliary:
      vision:
        provider: auto
        model: ''
"""


def _make_healthy_profile(tmp_path, profiles_root, name="healthy"):
    pdir = _make_profile(profiles_root, name, HEALTHY_CONFIG)
    _make_plugin(pdir, "orchestrator", toolset="orchestrate")
    # A hook whose script genuinely exists and is executable.
    script = tmp_path / f"{name}-guard.py"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    script.chmod(0o755)
    (pdir / "config.yaml").write_text(
        (pdir / "config.yaml").read_text(encoding="utf-8")
        + textwrap.dedent(
            f"""
            hooks:
              pre_tool_call:
                - matcher: terminal
                  command: {script}
                  timeout: 60
            """
        ),
        encoding="utf-8",
    )
    return pdir


# ---------------------------------------------------------------------------
# Healthy profile: quiet pass
# ---------------------------------------------------------------------------


class TestHealthyProfile:
    def test_healthy_profile_passes_silently(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_healthy_profile(tmp_path, profiles_root)

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "Profile Shape (silent drift)" in out
        assert "no silent drift found" in out
        assert issues == []
        # No per-profile finding lines at all.
        assert "healthy:" not in out

    def test_profile_without_config_is_quiet(self, tmp_path):
        # Missing config.yaml is the Profiles section's finding, not ours;
        # shape checks simply have nothing to say.
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(profiles_root, "bare")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "no silent drift found" in out
        assert issues == []


# ---------------------------------------------------------------------------
# (1) plugins.enabled vs plugin presence / toolset allow-list
# ---------------------------------------------------------------------------


class TestPluginChecks:
    def test_enabled_plugin_missing_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            plugins:
              enabled: [ghost-plugin]
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "plugins.enabled 'ghost-plugin' not found" in out
        assert "silently never loads" in out
        assert any("Profile 'p'" in i for i in issues)

    def test_enabled_plugin_with_excluded_toolset_is_detected(self, tmp_path):
        # The real incident: plugins.enabled: [orchestrator] with
        # toolsets: [hermes-cli, kanban], while the plugin registers its
        # tools under toolset "orchestrate" — entire plugin absent from the
        # schema with every other signal looking healthy.
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        pdir = _make_profile(
            profiles_root,
            "p",
            """
            toolsets: [hermes-cli, kanban]
            plugins:
              enabled: [orchestrator]
            """,
        )
        _make_plugin(pdir, "orchestrator", toolset="orchestrate")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "toolset 'orchestrate'" in out
        assert "allow-list" in out
        assert "silently absent from the model's schema" in out

    def test_enabled_plugin_with_allowed_toolset_is_quiet(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        pdir = _make_profile(
            profiles_root,
            "p",
            """
            toolsets: [hermes-cli, orchestrate]
            plugins:
              enabled: [orchestrator]
            """,
        )
        _make_plugin(pdir, "orchestrator", toolset="orchestrate")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "no silent drift found" in out
        assert issues == []

    def test_no_allowlist_means_no_toolset_finding(self, tmp_path):
        # A profile without any toolsets/platform_toolsets allow-list runs on
        # defaults — nothing to exclude the plugin's toolset from.
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        pdir = _make_profile(
            profiles_root,
            "p",
            """
            plugins:
              enabled: [orchestrator]
            """,
        )
        _make_plugin(pdir, "orchestrator", toolset="orchestrate")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "no silent drift found" in out
        assert issues == []

    def test_dirname_vs_manifest_name_mismatch_is_detected(self, tmp_path):
        # plugin.yaml's name: IS the plugin key; the directory name is ignored.
        # Enabling the directory name silently never matches.
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        pdir = _make_profile(
            profiles_root,
            "p",
            """
            plugins:
              enabled: [orchestrator]
            """,
        )
        _make_plugin(pdir, "orchestrator", manifest_name="orchestr8")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "matches a plugin DIRECTORY" in out
        assert "name: 'orchestr8'" in out
        assert "silently never loads" in out

    def test_bundled_plugin_presence_is_accepted(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            plugins:
              enabled: [disk-cleanup]
            """,
        )
        # Bundled plugins live in the repo tree, not the profile.
        plug = bundled / "plugins" if False else bundled
        plug = plug / "disk-cleanup"
        plug.mkdir(parents=True)
        (plug / "plugin.yaml").write_text("name: disk-cleanup\n", encoding="utf-8")
        (plug / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "disk-cleanup" not in out
        assert issues == []

    def test_unreadable_plugin_manifest_is_a_finding(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        pdir = _make_profile(profiles_root, "p", "plugins:\n  enabled: []\n")
        plug = pdir / "plugins" / "brokenmanifest"
        plug.mkdir(parents=True)
        (plug / "plugin.yaml").write_text("name: [unclosed", encoding="utf-8")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "plugin.yaml is unreadable" in out
        assert "plugin cannot load" in out


# ---------------------------------------------------------------------------
# (2)+(3) hooks: fail-open scripts and the silent timeout clamp
# ---------------------------------------------------------------------------


class TestHookChecks:
    def _profile_with_hook(self, profiles_root, hook_yaml):
        return _make_profile(
            profiles_root,
            "p",
            f"""
            hooks:
              pre_tool_call:
{textwrap.indent(textwrap.dedent(hook_yaml), '                ')}
            """,
        )

    def test_missing_hook_script_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        self._profile_with_hook(
            profiles_root,
            "- command: /nowhere/does-not-exist.py\n",
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "/nowhere/does-not-exist.py does not exist" in out
        assert "FAIL OPEN" in out
        assert any("Profile 'p'" in i for i in issues)

    def test_dangling_hook_symlink_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        link = tmp_path / "guard.py"
        link.symlink_to(tmp_path / "renamed-away.py")
        self._profile_with_hook(profiles_root, f"- command: {link}\n")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "dangling symlink" in out
        assert "FAIL OPEN" in out

    def test_non_executable_hook_script_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        script = tmp_path / "guard.py"
        script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        script.chmod(0o644)
        self._profile_with_hook(profiles_root, f"- command: {script}\n")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "not executable" in out
        assert "FAIL OPEN" in out

    def test_interpreter_form_missing_script_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        self._profile_with_hook(
            profiles_root,
            "- command: python3 /nowhere/guard.py\n",
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "script /nowhere/guard.py does not exist" in out

    def test_timeout_above_ceiling_is_detected(self, tmp_path):
        # agent/shell_hooks.py silently clamps to MAX_TIMEOUT_SECONDS, and a
        # killed hook returns no directive — which the host reads as ALLOW.
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        script = tmp_path / "slow-guard.py"
        script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        script.chmod(0o755)
        self._profile_with_hook(
            profiles_root,
            f"- command: {script}\n  timeout: 960\n",
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "timeout 960s exceeds the Hermes ceiling" in out
        assert "ALLOW" in out

    def test_timeout_at_ceiling_is_quiet(self, tmp_path):
        from agent.shell_hooks import MAX_TIMEOUT_SECONDS

        root_home, profiles_root, bundled = _make_roots(tmp_path)
        script = tmp_path / "guard.py"
        script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        script.chmod(0o755)
        self._profile_with_hook(
            profiles_root,
            f"- command: {script}\n  timeout: {MAX_TIMEOUT_SECONDS}\n",
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "no silent drift found" in out
        assert issues == []

    def test_unknown_hook_event_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            hooks:
              pre_tool_cal:
                - command: /bin/ls
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "not a valid hook event" in out
        assert "pre_tool_call" in out  # did-you-mean suggestion

    def test_hook_entry_without_command_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        self._profile_with_hook(profiles_root, "- matcher: terminal\n")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "has no command" in out
        assert "guard silently off" in out


# ---------------------------------------------------------------------------
# (4)+(5) provider references the profile never defines
# ---------------------------------------------------------------------------


class TestProviderReferenceChecks:
    def test_undefined_model_provider_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            model:
              provider: qwen-nvfp4-nonexistent
              default: whatever
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "model.provider 'qwen-nvfp4-nonexistent' is not defined" in out
        assert "do NOT inherit" in out

    def test_profile_defined_provider_is_quiet(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            model:
              provider: myprov
              default: m
            providers:
              myprov:
                base_url: http://localhost:1/v1
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "no silent drift found" in out
        assert issues == []

    def test_builtin_provider_is_quiet(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            model:
              provider: openrouter
              default: m
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "model.provider" not in out

    def test_undefined_auxiliary_provider_is_detected(self, tmp_path):
        # e.g. auxiliary.goal_judge pointing at a provider only the root
        # defines — the /goal completion judge silently fails open.
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            auxiliary:
              goal_judge:
                provider: only-in-root-config
                model: judge-model
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "auxiliary.goal_judge.provider 'only-in-root-config'" in out
        assert "no inheritance" in out

    def test_auxiliary_auto_and_base_url_are_quiet(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            auxiliary:
              vision:
                provider: auto
              compression:
                provider: some-direct-endpoint
                base_url: http://localhost:2/v1
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "auxiliary" not in out
        assert issues == []

    def test_custom_provider_slug_reference_is_resolved(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            model:
              provider: custom:hpc-ai
              default: deepseek/deepseek-v4-flash
            custom_providers:
              - name: hpc-ai
                base_url: https://hpc-ai.example/v1
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "model.provider" not in out

    def test_custom_provider_slug_missing_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            model:
              provider: custom:not-defined-here
              default: m
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "model.provider 'custom:not-defined-here' is not defined" in out


# ---------------------------------------------------------------------------
# (6) toolsets entries that resolve to nothing (incl. MCP references)
# ---------------------------------------------------------------------------


class TestToolsetReferenceChecks:
    def test_ghost_toolset_entry_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            toolsets: [hermes-cli, ghost-toolset]
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "'ghost-toolset'" in out
        assert "silently dropped" in out

    def test_enabled_mcp_server_reference_is_quiet(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            toolsets: [hermes-cli, my-mcp]
            mcp_servers:
              my-mcp:
                command: /bin/true
                enabled: true
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "my-mcp" not in out
        assert issues == []

    def test_disabled_mcp_server_reference_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(
            profiles_root,
            "p",
            """
            toolsets: [hermes-cli, my-mcp]
            mcp_servers:
              my-mcp:
                command: /bin/true
                enabled: false
            """,
        )

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "MCP server 'my-mcp'" in out
        assert "enabled: false" in out


# ---------------------------------------------------------------------------
# (7) dangling symlinks under skills/ and plugins/
# ---------------------------------------------------------------------------


class TestDanglingSymlinkChecks:
    def test_dangling_skill_symlink_is_detected(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        pdir = _make_profile(profiles_root, "p", "model: {}\n")
        skills = pdir / "skills" / "security"
        skills.mkdir(parents=True)
        (skills / "semgrep").symlink_to(tmp_path / "moved-away" / "semgrep")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "skills/security/semgrep is a dangling symlink" in out
        assert "silently invisible" in out

    def test_root_profile_skills_are_scanned_too(self, tmp_path):
        # The war story: ~/.hermes/skills/security/* dangled after a
        # directory rename — the ROOT home is a profile for this check.
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        skills = root_home / "skills"
        skills.mkdir()
        (skills / "gone").symlink_to(tmp_path / "does-not-exist")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "root: skills/gone is a dangling symlink" in out

    def test_healthy_symlinked_skill_is_quiet(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        pdir = _make_profile(profiles_root, "p", "model: {}\n")
        real_skill = tmp_path / "real-skill"
        real_skill.mkdir()
        skills = pdir / "skills"
        skills.mkdir()
        (skills / "real").symlink_to(real_skill)

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "no silent drift found" in out
        assert issues == []

    def test_many_dangling_symlinks_are_capped(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        pdir = _make_profile(profiles_root, "p", "model: {}\n")
        skills = pdir / "skills"
        skills.mkdir()
        for i in range(10):
            (skills / f"gone-{i}").symlink_to(tmp_path / f"missing-{i}")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "more dangling symlink(s)" in out


# ---------------------------------------------------------------------------
# Malformed profiles are findings, not tracebacks
# ---------------------------------------------------------------------------


class TestMalformedProfiles:
    def test_malformed_config_yaml_is_a_finding(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(profiles_root, "p", "model: [unclosed\n  {{{{\n")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "config.yaml is unreadable/malformed" in out
        assert "pure code defaults" in out
        assert any("Profile 'p'" in i for i in issues)

    def test_non_mapping_config_yaml_is_a_finding(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(profiles_root, "p", "- just\n- a\n- list\n")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "config.yaml is not a mapping" in out

    def test_non_dict_hooks_is_a_finding(self, tmp_path):
        root_home, profiles_root, bundled = _make_roots(tmp_path)
        _make_profile(profiles_root, "p", "hooks: not-a-dict\n")

        out, issues = _run_shape_check(tmp_path, root_home, profiles_root, bundled)

        assert "hooks: is not a mapping" in out
        assert "every guard silently off" in out


# ---------------------------------------------------------------------------
# Default path resolution goes through hermes_cli.profiles
# ---------------------------------------------------------------------------


def test_default_roots_resolve_via_profiles_module(tmp_path, monkeypatch):
    """Without explicit kwargs the section resolves the default HERMES_HOME
    and profiles root through hermes_cli.profiles — so run_doctor needs no
    plumbing and tests can isolate via monkeypatch."""
    from hermes_cli import profiles as profiles_mod

    root_home, profiles_root, bundled = _make_roots(tmp_path)
    _make_profile(
        profiles_root,
        "p",
        """
        plugins:
          enabled: [ghost-plugin]
        """,
    )
    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: root_home)
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: profiles_root)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "empty-project")

    issues = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod._check_profile_shape(issues)
    out = buf.getvalue()

    assert "Profile Shape (silent drift)" in out
    assert "plugins.enabled 'ghost-plugin' not found" in out
