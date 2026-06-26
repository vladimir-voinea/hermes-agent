"""Process-wide registry of cancellable background agent tasks.

Background tasks spawned via ``/background`` (CLI, gateway, and TUI surfaces)
each run their own :class:`AIAgent` in a separate thread (CLI / TUI) or executor
thread driven by an asyncio task (gateway). The agent already supports
cooperative cancellation via :meth:`AIAgent.interrupt` (see ``run_agent.py``):
it sets a thread-scoped interrupt flag that the conversation loop checks between
turns, during API calls, in retry waits, and mid-stream, and it also aborts the
in-flight tool (e.g. a running ``terminal`` subprocess). The interrupt is scoped
to the agent's own execution thread, so cancelling one background task never
disturbs the foreground session or other background tasks.

What was missing was a *handle* to the running agent: each spawn site dropped the
agent reference on the floor, so there was nothing for ``/background cancel`` to
call. This registry keeps that handle, keyed by ``task_id``, plus a little
metadata for ``/background list``. It is intentionally tiny and dependency-free
(stdlib only, no import of ``run_agent``/``cli``) so every surface can share it
without import cycles.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class AmbiguousTaskError(ValueError):
    """A user-supplied token matched more than one background task id."""

    def __init__(self, token: str, matches: List[str]):
        self.token = token
        self.matches = matches
        super().__init__(
            f"{token!r} matches multiple tasks: {', '.join(matches)}"
        )


@dataclass
class BackgroundTaskRecord:
    """One running background task and the handle needed to cancel it."""

    task_id: str
    agent: Any  # AIAgent — typed Any to avoid an import cycle.
    prompt: str = ""
    surface: str = ""  # "cli" | "gateway" | "tui"
    started_at: float = field(default_factory=time.time)
    cancel_requested: bool = False

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def preview(self, width: int = 60) -> str:
        p = (self.prompt or "").strip().replace("\n", " ")
        return p[:width] + ("…" if len(p) > width else "")


class BackgroundTaskRegistry:
    """Thread-safe map of ``task_id`` → :class:`BackgroundTaskRecord`.

    All public methods take an internal lock, so spawn sites (which register and
    unregister from worker threads) and command handlers (which list / cancel
    from a different thread) can call concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: "dict[str, BackgroundTaskRecord]" = {}

    def register(
        self,
        task_id: str,
        agent: Any,
        *,
        prompt: str = "",
        surface: str = "",
    ) -> BackgroundTaskRecord:
        rec = BackgroundTaskRecord(
            task_id=task_id, agent=agent, prompt=prompt, surface=surface
        )
        with self._lock:
            self._tasks[task_id] = rec
        return rec

    def unregister(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def get(self, task_id: str) -> Optional[BackgroundTaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, *, surface: Optional[str] = None) -> List[BackgroundTaskRecord]:
        """Records in start order (oldest first), optionally filtered by surface."""
        with self._lock:
            records = list(self._tasks.values())
        if surface is not None:
            records = [r for r in records if r.surface == surface]
        records.sort(key=lambda r: r.started_at)
        return records

    def count(self, *, surface: Optional[str] = None) -> int:
        return len(self.list(surface=surface))

    def resolve(
        self, token: str, *, surface: Optional[str] = None
    ) -> Optional[BackgroundTaskRecord]:
        """Resolve a user token to a single task.

        Accepted forms, in priority order:

        * exact ``task_id`` (e.g. ``bg_224554_637db2``)
        * ``#N`` or bare ``N`` — 1-based index into the start-ordered list
          (background ids always start with ``bg_``, so a bare integer is never
          a valid id prefix and the two forms can't collide)
        * a unique ``task_id`` prefix

        Returns the matching record, or ``None`` if nothing matched. Raises
        :class:`AmbiguousTaskError` if a prefix matched more than one task.
        """
        token = (token or "").strip()
        if not token:
            return None
        records = self.list(surface=surface)

        for r in records:
            if r.task_id == token:
                return r

        idx_str = token[1:] if token.startswith("#") else token
        if idx_str.isdigit():
            i = int(idx_str) - 1
            return records[i] if 0 <= i < len(records) else None

        matches = [r for r in records if r.task_id.startswith(token)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousTaskError(token, [r.task_id for r in matches])
        return None

    def cancel(
        self,
        token: str,
        *,
        message: Optional[str] = None,
        surface: Optional[str] = None,
    ) -> Optional[BackgroundTaskRecord]:
        """Resolve ``token`` and ask that agent to stop.

        Returns the resolved record (so callers can report which task / prompt
        was cancelled), or ``None`` if the token matched nothing. The record is
        left in the registry: the task's own ``finally`` unregisters it once the
        interrupt has unwound the conversation loop, which keeps it visible as
        "cancelling" in ``list`` until it actually stops. Raises
        :class:`AmbiguousTaskError` on an ambiguous prefix.
        """
        rec = self.resolve(token, surface=surface)
        if rec is None:
            return None
        rec.cancel_requested = True
        try:
            rec.agent.interrupt(
                message=message or "Cancelled by user via /background cancel."
            )
        except Exception:  # pragma: no cover - defensive; interrupt is best-effort
            logger.exception("Failed to interrupt background task %s", rec.task_id)
        return rec


# Process-wide singleton shared by every surface.
background_tasks = BackgroundTaskRegistry()


__all__ = [
    "AmbiguousTaskError",
    "BackgroundTaskRecord",
    "BackgroundTaskRegistry",
    "background_tasks",
]
