"""Kimi Code provider profile — OAuth (coding plan).

Same wire API as the ``kimi-coding`` profile, but authenticated by reusing the
Kimi CLI's OAuth login (~/.kimi-code) instead of an API key. Access tokens live
900s, so a static key is impossible; hermes_cli.auth.resolve_kimi_oauth_runtime_
credentials() handles the refresh.

This plugin is deliberately BUNDLED in the repo rather than dropped under
``$HERMES_HOME/plugins/model-providers/``: $HERMES_HOME differs per profile, so
only the repo copy is shared by every Hermes profile.
"""

from typing import Any

from providers import register_provider
from providers.base import OMIT_TEMPERATURE, ProviderProfile


class KimiOAuthProfile(ProviderProfile):
    """Kimi Code — temperature omitted, thinking xor reasoning_effort."""

    # build_api_kwargs_extras below is copied VERBATIM from the sibling bundled
    # plugin plugins/model-providers/kimi-coding/__init__.py (KimiProfile).
    # It is duplicated rather than imported because the sibling is not reachable
    # by a normal import: ``plugins/model-providers`` is hyphenated and has no
    # __init__.py, so ``plugins.model_providers.kimi_coding`` exists only as a
    # synthetic sys.modules entry created by providers._import_plugin_dir(), and
    # only once discovery has already imported it. Importing it here would bind
    # this plugin's correctness to plugin discovery order. Keep the two copies in
    # sync — the request shape they encode is the same Moonshot quirk.

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Kimi reasoning controls.

        Moonshot's wire shape treats ``extra_body.thinking`` (a binary toggle)
        and a top-level ``reasoning_effort`` as mutually exclusive — sending
        both is at best redundant and risks "cannot specify both 'thinking' and
        'reasoning_effort'" (HTTP 400). This mirrors the kimi-k2 handling on the
        opencode-go relay: send effort when one is requested, otherwise fall
        back to ``extra_body.thinking`` — never both.
        """
        extra_body = {}
        top_level = {}

        if not reasoning_config or not isinstance(reasoning_config, dict):
            # No config → thinking enabled, let the server pick the depth.
            # (Previously also sent reasoning_effort="medium", which paired
            # thinking + effort on every default call.)
            extra_body["thinking"] = {"type": "enabled"}
            return extra_body, top_level

        enabled = reasoning_config.get("enabled", True)
        if enabled is False:
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, top_level

        # Enabled: prefer an explicit effort; only fall back to extra_body
        # thinking when no recognized effort is requested.
        effort = (reasoning_config.get("effort") or "").strip().lower()
        if effort in {"low", "medium", "high"}:
            top_level["reasoning_effort"] = effort
        else:
            extra_body["thinking"] = {"type": "enabled"}

        return extra_body, top_level


kimi_oauth = KimiOAuthProfile(
    name="kimi-oauth",
    aliases=("kimi-code", "kimi-code-oauth", "kimi-coding-oauth"),
    base_url="https://api.kimi.com/coding/v1",
    auth_type="oauth_external",
    fixed_temperature=OMIT_TEMPERATURE,
    default_max_tokens=32768,
    fallback_models=("k3", "kimi-for-coding", "kimi-for-coding-highspeed"),
    supports_vision=True,
    default_headers={"User-Agent": "hermes-agent/1.0"},
)

register_provider(kimi_oauth)
