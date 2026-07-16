"""Lifecycle bridge for the ``opencode`` worker runtime.

WHY A BRIDGE EXISTS AT ALL
--------------------------
A Hermes worker closes its own card: it has ``kanban_complete`` /
``kanban_block`` in its tool schema, so the card's lifecycle is driven from
inside the agent. OpenCode has never heard of our kanban. Point the dispatcher
straight at ``opencode run`` and you get a process that does the work and then
exits, leaving the card in ``running`` until the claim TTL expires and the
whole thing is re-run — forever.

So the dispatcher spawns *this* instead. It runs OpenCode headlessly and then
does, from the outside, what a Hermes worker does from the inside: resolve the
card. That is the entire job. This process IS the worker as far as the
dispatcher is concerned — it is the PID that gets watched, and its death is the
card's crash.

★ IT ALSO INHERITS THE HARNESS'S GUARANTEES
-------------------------------------------
The tempting version of this file completes the card by calling
``complete_task()`` the moment ``opencode run`` exits 0. That would quietly
punch a hole through every ``kanban_complete`` guard on the host: those are
``pre_tool_call`` hooks, and a direct DB write is not a tool call. A project
whose hook refuses to complete a card whose branch has no commits would find
that rule enforced for Hermes workers and silently waived for OpenCode ones —
the worst kind of security boundary, the kind that is only *usually* there.

So the bridge fires the real gate (:func:`resolve_pre_tool_block`) before it
completes anything, exactly as ``model_tools`` does for a tool call. Whatever
the host's hooks demand of a Hermes worker, they demand of OpenCode.

And because a block carries a *reason* written to be read by a worker ("commit
your work and call kanban_complete again"), the bridge hands it back to
OpenCode with ``--continue`` and lets it try again. That is what turns the gate
from a wall into a correction: the same "fix it and retry" contract a Hermes
worker gets, for an agent that cannot see the gate at all.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
``exit 0`` from an agent CLI means "the model stopped talking", never "the task
is done". The bridge does not pretend otherwise: it makes no judgement of its
own about the work, and it does not have a goal loop (the ``opencode`` runtime
declares ``goal_mode`` unsupported for exactly this reason). The only opinions
here are the host's hooks and the reviewer downstream.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from typing import Optional

# A card is a real piece of work: minutes to tens of minutes, not seconds.
# Bounded anyway — an agent CLI that wedges must not hold the claim forever.
DEFAULT_TIMEOUT = 3600
# How many times a *blocked* completion is handed back for correction. Small on
# purpose: if the gate still refuses after a couple of honest attempts, the card
# needs a human, not a tighter loop.
MAX_GATE_ATTEMPTS = 3
# The completion summary is the handover to the reviewer; the tail is where an
# agent CLI puts its closing report.
SUMMARY_CHARS = 4000


def _log(msg: str) -> None:
    """Stdout is the worker log — the adapter redirects it to <board>/logs/."""
    print(f"[bridge {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _prompt_for(task) -> str:
    """The card, rendered as a briefing.

    A worker starts cold and jailed: the body is all it knows. The completion
    contract is spelled out because OpenCode has no kanban tools to discover —
    it cannot see that anything is watching, so the prompt has to say so.
    """
    parts = [f"# {task.title}", ""]
    if task.body:
        parts += [task.body, ""]
    ws = os.environ.get("HERMES_KANBAN_WORKSPACE", "")
    branch = os.environ.get("HERMES_KANBAN_BRANCH", "")
    parts += [
        "---",
        f"You are working in {ws}.",
    ]
    if branch:
        parts += [
            f"You are on branch `{branch}`. COMMIT your work on it before you "
            f"finish: this directory is deleted when the card closes, and "
            f"anything you have not committed is lost — it is your commits, "
            f"not your files and not your closing message, that are the "
            f"deliverable.",
        ]
    parts += [
        "When you are done, just stop. Do not ask questions — nobody is "
        "reading this session. If the task is impossible, say so plainly in "
        "your final message and stop.",
    ]
    return "\n".join(parts)


def _runtime_settings() -> tuple[str, Optional[str], list]:
    """(command, default_model, extra_args).

    The dispatcher resolved these against the ROOT config and passed them in —
    prefer that. We run with HERMES_HOME pointed at the assignee's profile, and
    Hermes profiles do not inherit config, so re-deriving here would read a
    different file than the process that decided this card was spawnable.
    Config is only consulted as a fallback (a bridge run by hand, an older
    dispatcher).
    """
    cmd = os.environ.get("HERMES_OPENCODE_COMMAND", "").strip()
    model = os.environ.get("HERMES_OPENCODE_MODEL", "").strip() or None
    extra_raw = os.environ.get("HERMES_OPENCODE_EXTRA_ARGS", "").strip()
    extra: list = []
    if extra_raw:
        try:
            import json

            extra = json.loads(extra_raw)
        except ValueError:
            extra = shlex.split(extra_raw)
    if cmd:
        return cmd, model, extra

    from hermes_cli.kanban_worker_runtimes import _resolve_command, _runtime_config

    cfg = _runtime_config("opencode")
    return (
        _resolve_command("opencode") or "opencode",
        model or cfg.get("default_model"),
        extra or cfg.get("extra_args") or [],
    )


def _opencode_argv(task, prompt: str, *, continue_session: bool) -> list[str]:
    command, default_model, extra = _runtime_settings()
    argv = [command, "run"]
    if continue_session:
        argv.append("--continue")
    # Per-card override beats the configured default — same precedence as a
    # Hermes card's model_override.
    model = task.model_override or default_model
    if model:
        argv += ["--model", str(model)]
    # Headless means nobody can answer a permission prompt: without this the
    # run blocks on the first edit and dies at the timeout having done nothing.
    argv.append("--auto")
    if isinstance(extra, str):
        extra = shlex.split(extra)
    argv += [str(a) for a in extra]
    argv.append(prompt)
    return argv


def _run_opencode(task, prompt: str, *, continue_session: bool,
                  timeout: int) -> tuple[int, str]:
    """Run OpenCode, streaming its output to the worker log as it arrives.

    Streamed, not captured: a card is minutes of work, and a log that stays
    empty until the run ends is indistinguishable from a hung worker to anyone
    watching `hermes kanban log`. That is the one question a worker log has to
    be able to answer.

    The timeout is a watchdog thread rather than `subprocess.run(timeout=...)`
    because we are consuming the pipe ourselves: an agent CLI that wedges with
    no output would otherwise block on readline forever, with the deadline
    never checked.
    """
    argv = _opencode_argv(task, prompt, continue_session=continue_session)
    _log(f"$ {' '.join(shlex.quote(a) for a in argv[:-1])} <prompt {len(prompt)}b>")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=os.environ.get("HERMES_KANBAN_WORKSPACE") or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return 127, "opencode binary not found"

    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            proc.kill()
        except Exception:
            pass

    watchdog = threading.Timer(timeout, _kill)
    watchdog.start()
    chunks: list[str] = []
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            chunks.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = proc.wait()
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        return 124, f"opencode timed out after {timeout}s"
    return rc, "".join(chunks)


def _run_id() -> Optional[int]:
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _gate(task_id: str, summary: str) -> Optional[str]:
    """Fire the host's pre_tool_call hooks for kanban_complete.

    Returns the block reason, or None to proceed. Errors here fail OPEN and
    say so: a broken hook must not strand a card that did its work. (The
    guards that matter fail closed on their own side — see the merge gate.)
    """
    try:
        from hermes_cli.plugins import resolve_pre_tool_block

        return resolve_pre_tool_block(
            "kanban_complete",
            {"task_id": task_id, "summary": summary},
            task_id=task_id,
            session_id=f"opencode:{task_id}",
        )
    except Exception as exc:
        _log(f"WARNING: completion gate errored, allowing: {exc!r}")
        return None


def _bootstrap_hooks() -> None:
    """Load plugins + shell hooks, the way the CLI does at startup.

    Without this the gate is a no-op that always allows — which would look
    exactly like a passing gate. The dispatcher spawns us with
    HERMES_ACCEPT_HOOKS=1 for the same reason it passes `--accept-hooks` to a
    Hermes worker: a worker has no TTY to approve a hook at.
    """
    accept = os.environ.get("HERMES_ACCEPT_HOOKS", "").lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception as exc:
        _log(f"WARNING: plugin discovery failed: {exc!r}")
    try:
        from agent.shell_hooks import register_from_config
        from hermes_cli.config import load_config

        register_from_config(load_config(), accept_hooks=accept)
    except Exception as exc:
        _log(f"WARNING: shell-hook registration failed: {exc!r}")


def main() -> int:
    task_id = os.environ.get("HERMES_KANBAN_TASK") or ""
    if not task_id:
        _log("FATAL: HERMES_KANBAN_TASK not set")
        return 2

    from hermes_cli import kanban_db as kb

    board = os.environ.get("HERMES_KANBAN_BOARD") or None
    with kb.connect(board=board) as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        _log(f"FATAL: no such task {task_id}")
        return 2

    _bootstrap_hooks()

    timeout = int(task.max_runtime_seconds or DEFAULT_TIMEOUT)
    prompt = _prompt_for(task)
    continue_session = False

    for attempt in range(1, MAX_GATE_ATTEMPTS + 1):
        rc, out = _run_opencode(task, prompt, continue_session=continue_session,
                                timeout=timeout)
        if rc != 0:
            # The runtime failed, which is not the same as the work failing.
            # `transient` keeps it retryable and lets the recurrence limiter
            # escalate a permanently broken runtime to triage instead of
            # looping on it.
            _log(f"opencode exited {rc} — blocking card as transient")
            with kb.connect(board=board) as conn:
                kb.block_task(
                    conn, task_id,
                    reason=f"opencode runtime exited {rc}: {out[-800:].strip()}",
                    kind="transient",
                    expected_run_id=_run_id(),
                )
            return 1

        summary = out[-SUMMARY_CHARS:].strip() or "opencode run completed."
        reason = _gate(task_id, summary)
        if reason is None:
            with kb.connect(board=board) as conn:
                kb.complete_task(
                    conn, task_id,
                    summary=summary,
                    metadata={"worker_runtime": "opencode", "attempts": attempt},
                    expected_run_id=_run_id(),
                )
            _log(f"card completed (attempt {attempt})")
            return 0

        # Blocked. The reason was written for a worker to act on, so give it to
        # the worker — the gate is a correction, not a verdict.
        _log(f"completion gate refused (attempt {attempt}): {reason[:200]}")
        prompt = (
            "Your work was rejected when the card tried to close. This is "
            "automated and not negotiable — fix what it says and stop:\n\n"
            f"{reason}"
        )
        continue_session = True

    with kb.connect(board=board) as conn:
        kb.block_task(
            conn, task_id,
            reason=(
                f"the completion gate refused {MAX_GATE_ATTEMPTS} attempts; "
                f"last reason: {reason[:600]}"
            ),
            kind="needs_input",
            expected_run_id=_run_id(),
        )
    _log("gate still refusing — blocked for a human")
    return 1


if __name__ == "__main__":
    sys.exit(main())
