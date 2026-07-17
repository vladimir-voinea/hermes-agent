"""Bridge live kanban comments into a running worker's agent loop.

A dispatcher-spawned worker reads its task exactly once, at startup
(``kanban_show`` → :func:`hermes_cli.kanban_db.build_worker_context`).
Comments added *while it runs* are invisible: the worker never re-reads its
task, and nothing pushes board changes to it. So an operator who leaves a
comment on an in-progress card is talking to a wall until the worker is
re-spawned (block/unblock).

This watcher closes that gap. For a dispatcher-spawned worker process
(``HERMES_KANBAN_TASK`` set) it polls the task's comment thread on a
background daemon thread and, for every NEW comment authored by someone
*other than the worker itself*, calls :meth:`agent.steer` — injecting the
comment into the live loop at the next tool-result boundary, exactly like a
user typing mid-turn in the interactive CLI.

Author filter
    Comments the worker wrote itself (``author == HERMES_PROFILE``) are
    skipped, so a worker never steers on its own progress notes / handoffs.
    Operator comments from the dashboard (author ``"dashboard"``) and notes
    from other agents come through.

Trust note
    A dispatcher-spawned worker may only comment on its OWN task
    (``_enforce_worker_task_ownership`` in ``tools/kanban_tools.py``). So a
    non-self comment appearing on a *running* task originates from the
    authenticated dashboard or the orchestrator — a trusted operator
    channel, appropriate to steer on. This is the LIVE analogue of the
    persisted comment thread in ``build_worker_context``; it does not relax
    the #22452 hardening (which governs how *stored* comments are framed for
    the *next* worker's system prompt).

Disable with ``HERMES_KANBAN_COMMENT_STEER=0``; tune the poll cadence with
``HERMES_KANBAN_COMMENT_POLL_SECONDS`` (default 4s, floor 1s).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SECONDS = 4.0
_MIN_POLL_SECONDS = 1.0


def _poll_interval() -> float:
    raw = os.environ.get("HERMES_KANBAN_COMMENT_POLL_SECONDS")
    if not raw:
        return _DEFAULT_POLL_SECONDS
    try:
        return max(_MIN_POLL_SECONDS, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_POLL_SECONDS


def _enabled() -> bool:
    val = (os.environ.get("HERMES_KANBAN_COMMENT_STEER") or "").strip().lower()
    return val not in {"0", "false", "no", "off"}


class KanbanCommentSteerWatcher:
    """Daemon thread that turns new kanban comments into live steers."""

    def __init__(
        self,
        agent,
        *,
        task_id: str,
        self_author: str,
        interval: float = _DEFAULT_POLL_SECONDS,
    ) -> None:
        self._agent = agent
        self._task_id = task_id
        self._self_author = (self_author or "").strip()
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Highest comment id already accounted for. Seeded at start() so the
        # thread's existing comments (read into the worker's startup context)
        # are never re-delivered as steers.
        self._last_id = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "KanbanCommentSteerWatcher":
        self._last_id = self._max_comment_id()
        self._thread = threading.Thread(
            target=self._run,
            name="kanban-comment-steer",
            daemon=True,
        )
        self._thread.start()
        logger.debug(
            "kanban comment-steer watcher started for %s (baseline id=%d, "
            "interval=%.1fs)",
            self._task_id,
            self._last_id,
            self._interval,
        )
        return self

    def stop(self) -> None:
        self._stop.set()

    # -- internals ----------------------------------------------------------

    def _max_comment_id(self) -> int:
        """Highest comment id currently on the task (0 if none / on error)."""
        try:
            from hermes_cli import kanban_db as kb

            with kb.connect_closing() as conn:
                comments = kb.list_comments(conn, self._task_id)
            return max((int(c.id) for c in comments), default=0)
        except Exception as exc:  # never let a DB hiccup break startup
            logger.debug("kanban comment-steer baseline read failed: %s", exc)
            return 0

    def _new_comments(self):
        """Return comments with id > last-seen, oldest-first."""
        from hermes_cli import kanban_db as kb

        with kb.connect_closing() as conn:
            comments = kb.list_comments(conn, self._task_id)
        return [c for c in comments if int(c.id) > self._last_id]

    def _run(self) -> None:
        # Event.wait doubles as the sleep + a responsive stop signal.
        while not self._stop.wait(self._interval):
            try:
                fresh = self._new_comments()
            except Exception as exc:
                logger.debug("kanban comment-steer poll failed: %s", exc)
                continue
            if not fresh:
                continue
            # Advance the cursor across ALL fresh comments (including our own)
            # so a self-comment can't wedge the cursor and re-fire forever.
            self._last_id = max(int(c.id) for c in fresh)
            deliver = [c for c in fresh if (c.author or "").strip() != self._self_author]
            if not deliver:
                continue
            text = self._format_steer(deliver)
            try:
                self._agent.steer(text)
                logger.info(
                    "kanban comment-steer: delivered %d comment(s) to worker %s",
                    len(deliver),
                    self._task_id,
                )
            except Exception as exc:
                logger.debug("kanban comment-steer: agent.steer failed: %s", exc)

    def _format_steer(self, comments) -> str:
        header = (
            f"New comment(s) on your current kanban task {self._task_id}, "
            "posted by the operator while you are working. Treat this as live "
            "guidance from the human: factor it into what you are doing now "
            "and adjust course if it changes the plan."
        )
        lines = [header, ""]
        for c in comments:
            author = (c.author or "unknown").replace("`", "")
            lines.append(f"[comment from `{author}`] {c.body}")
        return "\n".join(lines)


def start_comment_steer_watcher(agent) -> Optional[KanbanCommentSteerWatcher]:
    """Start the watcher for the current kanban worker, or return None.

    No-op (returns None) unless this process is a dispatcher-spawned worker
    (``HERMES_KANBAN_TASK`` set), the feature is enabled, and ``agent``
    exposes a callable ``steer``. Never raises — worker startup must not hinge
    on the bridge.
    """
    try:
        task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        if not task_id:
            return None
        if not _enabled():
            return None
        if not callable(getattr(agent, "steer", None)):
            return None
        self_author = (os.environ.get("HERMES_PROFILE") or "").strip()
        watcher = KanbanCommentSteerWatcher(
            agent,
            task_id=task_id,
            self_author=self_author,
            interval=_poll_interval(),
        )
        return watcher.start()
    except Exception as exc:
        logger.debug("kanban comment-steer watcher not started: %s", exc)
        return None
