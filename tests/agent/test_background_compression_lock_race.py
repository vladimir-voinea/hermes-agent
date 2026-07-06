"""Background vs in-turn compression must resolve to exactly one compression.

The whole safety argument for post-delivery background compaction is that it
takes the SAME state.db-backed per-session compression lock as the in-turn
path. If a real turn starts between the background arming check and the
background compress, the lock guarantees exactly one of them actually rewrites
the transcript; the loser returns the messages unchanged (a safe no-op).

This uses a real ``SessionDB`` so the lock is exercised for real (not mocked),
mirroring ``tests/agent/test_compression_concurrent_fork.py``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB


def _build_agent(db: SessionDB, session_id: str, *, compress_delay: float = 0.0):
    """Real AIAgent wired to ``db``; compressor stubbed to avoid an LLM call."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    compressor = MagicMock()
    calls = {"n": 0}

    def _compress(*_a, **_kw):
        calls["n"] += 1
        if compress_delay:
            time.sleep(compress_delay)
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]

    compressor.compress.side_effect = _compress
    compressor.compression_count = 1
    compressor.threshold_tokens = 100_000
    compressor.last_prompt_tokens = 90_000  # in the soft band
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    compressor.should_compress.return_value = True
    agent.context_compressor = compressor
    # Exercise the rotation path (child-session side effects are observable in
    # state.db), independent of the global in_place default.
    agent.compression_in_place = False
    agent.compression_enabled = True
    agent.compression_background = True
    agent.compression_soft_ratio = 0.8
    agent._compress_calls = calls
    return agent


def _count_children(db: SessionDB, parent_sid: str) -> int:
    rows = db._conn.execute(
        "SELECT id FROM sessions WHERE parent_session_id = ?",
        (parent_sid,),
    ).fetchall()
    return len(rows)


def test_background_and_inturn_compress_yield_one_compression(tmp_path: Path) -> None:
    """A background compress racing an in-turn compress rotates the session once.

    Two agents share a session_id (as the gateway's cached agent and a
    concurrently-starting real turn would). One drives the background path
    (``maybe_background_compress``), the other the in-turn path
    (``_compress_context``). The state.db lock must let exactly one rotate: at
    most one child session, never two (the fork).
    """
    from agent.conversation_compression import maybe_background_compress

    db = SessionDB(db_path=tmp_path / "state.db")
    parent_sid = "PARENT_RACE_SESSION"
    db.create_session(parent_sid, source="telegram")

    # Both sleep inside compress() so their lock windows genuinely overlap.
    agent_bg = _build_agent(db, parent_sid, compress_delay=0.25)
    agent_turn = _build_agent(db, parent_sid, compress_delay=0.25)

    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    results = {}

    def _run_bg():
        try:
            results["bg"] = maybe_background_compress(agent_bg, messages, "sys")
        except Exception as exc:  # pragma: no cover - defensive
            results["bg_exc"] = exc

    def _run_turn():
        try:
            agent_turn._compress_context(messages, "sys", approx_tokens=90_000)
            results["turn"] = True
        except Exception as exc:  # pragma: no cover - defensive
            results["turn_exc"] = exc

    t1 = threading.Thread(target=_run_bg)
    t2 = threading.Thread(target=_run_turn)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert "bg_exc" not in results, results.get("bg_exc")
    assert "turn_exc" not in results, results.get("turn_exc")

    # The forbidden outcome is a transcript fork (2+ children). Exactly one
    # path rotates under the lock (or 0 if the winner's child-create lost a DB
    # write race and rolled back to the parent — also safe). Never 2.
    assert _count_children(db, parent_sid) <= 1

    # The loser's compress() must have been a no-op (returned input unchanged),
    # so it did NOT invoke the underlying compressor.compress twice-with-effect.
    # Total real compressions across both agents is exactly 1: the winner ran
    # its compressor once; the loser bailed at the lock before calling compress.
    total_compress_calls = (
        agent_bg._compress_calls["n"] + agent_turn._compress_calls["n"]
    )
    assert total_compress_calls == 1, (
        f"expected exactly one real compression, got {total_compress_calls}"
    )


def test_background_loses_lock_is_safe_noop(tmp_path: Path) -> None:
    """When the lock is already held, the background path is a clean no-op.

    Directly hold the compression lock for the session, then run the background
    compress. It must not rotate, not call the compressor, and return False.
    """
    from agent.conversation_compression import maybe_background_compress

    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "PARENT_HELD_SESSION"
    db.create_session(sid, source="telegram")

    # Someone else holds the lock.
    assert db.try_acquire_compression_lock(sid, "other-holder", ttl_seconds=60.0)

    agent = _build_agent(db, sid)
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    ran = maybe_background_compress(agent, messages, "sys")
    assert ran is False
    assert agent._compress_calls["n"] == 0  # never reached the compressor
    assert _count_children(db, sid) == 0  # no rotation

    db.release_compression_lock(sid, "other-holder")
