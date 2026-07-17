"""Worker runtime registry for Kanban tasks.

A *worker runtime* is the pluggable backend that knows how to spawn and
finish an attempt of a Kanban card. Today the only runtime is ``hermes``
(the historic ``hermes -p <profile> chat -q ...`` worker). The
``opencode`` runtime (a lifecycle bridge that runs OpenCode headlessly)
is the first external runtime — see the design spec in
``hermes-kanban-opencode-worker``.

This module owns the **write-time** surface of the registry:

* the set of known runtime names,
* which runtimes are *external* (skip the Hermes ``profile_exists`` gate
  and may omit an assignee),
* which runtimes are *enabled* on this host (so cards cannot be created
  for a runtime that can never spawn here),
* normalisation + validation helpers used by the kernel, CLI, and tools.

The **spawn-time** surface — mapping a runtime name to a spawn adapter
(``spawn(task, workspace, board) -> pid``) — is added in a follow-up
ticket (runtime registry + Hermes adapter). This module deliberately
exposes a stable name → info lookup so that step is additive and needs
no schema change.

Config (``~/.hermes/config.yaml``)::

    kanban:
      runtimes:
        opencode:
          enabled: true            # explicit on/off (wins over auto-detect)
          command: /usr/local/bin/opencode
          default_model: openrouter/anthropic/claude-sonnet-4
          extra_args: []

When ``enabled`` is unset, an external runtime is auto-enabled iff its
binary can be resolved (``command`` path exists, or the runtime name is
found on ``$PATH``). ``hermes`` is always enabled.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Optional

# Canonical runtime ids. Keep these stable — they are persisted on every
# task row and surfaced in CLI/API output.
HERMES_RUNTIME = "hermes"
DEFAULT_RUNTIME = HERMES_RUNTIME


@dataclass(frozen=True)
class WorkerRuntime:
    """Static, host-independent metadata for a worker runtime.

    ``external`` runtimes do not run a Hermes profile: the dispatcher
    skips ``profile_exists`` for them and the create path allows an
    omitted assignee (displayed as the runtime id). Hermes-native
    runtimes keep requiring a real profile assignee.
    """

    name: str
    external: bool
    # Hermes-only feature flags that have no meaning for this runtime.
    # The create path rejects setting these with a clear error so cards
    # do not silently pretend to have capabilities the runtime lacks.
    # (Spec: "goal_mode on OpenCode cards rejected or clearly no-op";
    # we reject — fail closed.)
    unsupported_flags: tuple[str, ...] = ()


# Static registry. Adding a runtime is a one-line change here plus an
# adapter in the spawn-time registry (follow-up ticket) — no schema
# migration, no API change.
_REGISTRY: dict[str, WorkerRuntime] = {
    HERMES_RUNTIME: WorkerRuntime(name=HERMES_RUNTIME, external=False),
    "opencode": WorkerRuntime(
        name="opencode",
        external=True,
        unsupported_flags=("goal_mode", "skills"),
    ),
}


def known_runtimes() -> tuple[str, ...]:
    """Return the sorted tuple of registered runtime ids."""
    return tuple(sorted(_REGISTRY))


def get_runtime(name: Optional[str]) -> Optional[WorkerRuntime]:
    """Return the runtime info for ``name`` (case-insensitive), or None."""
    if name is None:
        return None
    return _REGISTRY.get(str(name).strip().lower())


def normalize_runtime(name: Optional[str]) -> str:
    """Normalise a runtime id. ``None``/empty → :data:`DEFAULT_RUNTIME`.

    Does NOT validate existence — callers that need to reject unknown
    runtimes should use :func:`validate_runtime`.
    """
    if name is None:
        return DEFAULT_RUNTIME
    cleaned = str(name).strip().lower()
    if not cleaned:
        return DEFAULT_RUNTIME
    return cleaned


def is_external_runtime(name: Optional[str]) -> bool:
    """True iff ``name`` is a registered external (non-Hermes) runtime."""
    info = get_runtime(name)
    return info is not None and info.external


def _runtime_config(name: str) -> dict:
    """Read the ``kanban.runtimes.<name>`` config block (may be empty)."""
    name = normalize_runtime(name)
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    kanban = cfg.get("kanban", {}) or {}
    runtimes = kanban.get("runtimes", {}) or {}
    block = runtimes.get(name, {}) or {}
    return block if isinstance(block, dict) else {}


def _resolve_command(name: str) -> Optional[str]:
    """Resolve the configured/on-PATH command for an external runtime."""
    block = _runtime_config(name)
    command = block.get("command")
    if command:
        command = str(command).strip()
        if command:
            return command
    # Fall back to the runtime id itself on PATH.
    return name


def is_runtime_enabled(name: Optional[str]) -> bool:
    """Return True iff ``name`` is known and enabled on this host.

    ``hermes`` is always enabled. External runtimes are enabled when
    config explicitly sets ``enabled: true/false`` (explicit wins), else
    auto-enabled when the binary can be resolved.
    """
    info = get_runtime(name)
    if info is None:
        return False
    if not info.external:
        return True
    block = _runtime_config(info.name)
    if "enabled" in block:
        try:
            return bool(block["enabled"])
        except Exception:
            return False
    # Auto-detect: binary resolvable on PATH or via configured command.
    command = _resolve_command(info.name)
    if not command:
        return False
    if os.path.isabs(command):
        return os.path.exists(command)
    return shutil.which(command) is not None


def validate_runtime(name: Optional[str]) -> str:
    """Validate a runtime id for persistence on a task row.

    Returns the normalised id on success. Raises ``ValueError`` for:

    * unknown runtime ids (typo, not yet registered), and
    * known-but-disabled runtimes (binary missing + not explicitly
      enabled) — so a card cannot sit in ``ready`` forever waiting for a
      runtime that can never spawn on this host.

    ``None``/empty is accepted and returns :data:`DEFAULT_RUNTIME`
    (Hermes), preserving zero-migration behaviour for existing callers.
    """
    normalised = normalize_runtime(name)
    info = _REGISTRY.get(normalised)
    if info is None:
        raise ValueError(
            f"unknown worker runtime {normalised!r}; "
            f"known runtimes: {', '.join(known_runtimes())}"
        )
    if not is_runtime_enabled(info.name):
        raise ValueError(
            f"worker runtime {info.name!r} is not enabled on this host "
            "(install its binary or set kanban.runtimes."
            f"{info.name}.enabled: true in config)."
        )
    return info.name


def unsupported_flags_for(name: Optional[str]) -> tuple[str, ...]:
    """Return the Hermes-only flags a runtime does not support."""
    info = get_runtime(name)
    return info.unsupported_flags if info is not None else ()


def requires_assignee(name: Optional[str]) -> bool:
    """True iff the runtime requires a Hermes profile assignee.

    Hermes-native runtimes do; external runtimes may omit the assignee
    (displayed as the runtime id). Used by the create path to relax the
    assignee check without weakening it for Hermes cards.
    """
    return not is_external_runtime(name)
