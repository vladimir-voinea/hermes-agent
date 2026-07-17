"""Tests for inspect_ui's grounding-backend configuration (auxiliary.inspect_ui).

Verifies:
  - DEFAULT_CONFIG ships no endpoint, and in particular no private address:
    grounding needs a specialist model, so there is no sane universal default.
  - Unconfigured, the tool REFUSES rather than falling through to the general
    `vision` model. That fallback is the whole hazard: a general VLM answers a
    grounding question confidently and wrongly, and the caller clicks it.
  - Configured by either base_url or model, the tool proceeds.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re

import pytest


# ---------------------------------------------------------------------------
# The shipped default
# ---------------------------------------------------------------------------

def test_default_config_ships_no_inspect_ui_endpoint():
    """auxiliary.inspect_ui must default to unconfigured, like its siblings."""
    from hermes_cli.config import DEFAULT_CONFIG

    iu = DEFAULT_CONFIG["auxiliary"]["inspect_ui"]
    assert iu["base_url"] == "", "inspect_ui must not ship a default endpoint"
    assert iu["model"] == "", "inspect_ui must not ship a default model"
    assert iu["provider"] == "auto"


def test_default_config_has_no_private_addresses_anywhere():
    """No auxiliary task may ship somebody's LAN as a default.

    This is a public repo; a hardcoded 192.168.x.y is both a leak of the
    author's network and a default that cannot work for anyone else.
    """
    from hermes_cli.config import DEFAULT_CONFIG

    offenders = []
    for task, cfg in DEFAULT_CONFIG["auxiliary"].items():
        if not isinstance(cfg, dict):
            continue
        url = str(cfg.get("base_url") or "")
        for host in re.findall(r"//([^:/]+)", url):
            try:
                if ipaddress.ip_address(host).is_private:
                    offenders.append(f"auxiliary.{task}.base_url = {url}")
            except ValueError:
                pass  # a hostname, not an IP literal
    assert not offenders, f"private addresses in shipped defaults: {offenders}"


# ---------------------------------------------------------------------------
# Refusal when unconfigured
# ---------------------------------------------------------------------------

def _run(args):
    from tools.vision_tools import _handle_inspect_ui
    return json.loads(asyncio.run(_handle_inspect_ui(args)))


def test_refuses_when_unconfigured(monkeypatch):
    """Neither model nor base_url set => explicit error, and vision is NOT called."""
    import tools.vision_tools as vt

    called = []

    async def _boom(*a, **kw):  # pragma: no cover - must never run
        called.append(a)
        return json.dumps({"success": True, "analysis": "general vision answer"})

    monkeypatch.setattr(vt, "vision_analyze_tool", _boom)
    monkeypatch.setattr(
        vt, "cfg_get", lambda *a, **kw: "", raising=False
    )
    monkeypatch.setattr(
        "hermes_cli.config.cfg_get", lambda cfg, *path: "", raising=False
    )

    out = _run({"screenshot": "/tmp/x.png", "instruction": "find the button"})

    assert out["success"] is False
    assert "not configured" in out["error"]
    assert not called, "must not fall back to the general vision model"


@pytest.mark.parametrize(
    "configured",
    [
        {"base_url": "http://holo.example:8081/v1", "model": ""},
        {"base_url": "", "model": "holo-4b"},
        {"base_url": "http://holo.example:8081/v1", "model": "holo-4b"},
    ],
    ids=["base_url-only", "model-only", "both"],
)
def test_proceeds_when_configured(monkeypatch, configured):
    """Either knob alone is enough to count as configured."""
    import tools.vision_tools as vt

    called = []

    async def _ok(screenshot, prompt, model, **kw):
        called.append(kw.get("task"))
        return json.dumps({"success": True, "analysis": "grounded answer"})

    monkeypatch.setattr(vt, "vision_analyze_tool", _ok)
    monkeypatch.setattr(
        "hermes_cli.config.cfg_get",
        lambda cfg, *path: configured.get(path[-1], ""),
        raising=False,
    )

    out = _run({"screenshot": "/tmp/x.png", "instruction": "find the button"})

    assert out["success"] is True
    assert called == ["inspect_ui"], "must route through the inspect_ui vision task"
