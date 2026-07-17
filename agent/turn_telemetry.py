"""Per-turn generation telemetry: TPS + vLLM prefix-cache hit % surfaced to users.

Captures, per user turn, how fast the model generated (tokens/sec) and how
much of the prompt was served from the provider's prefix cache (vLLM's
``prompt_tokens_details.cached_tokens`` — see ``agent.usage_pricing.normalize_usage``,
which already parses this into ``CanonicalUsage.cache_read_tokens`` for every
API shape).  A user turn can span multiple API calls (the tool-calling loop);
this module aggregates across those calls into one ``TurnTelemetry`` record
per turn.

Three independent surfaces consume a ``TurnTelemetry`` record:
  - ``format_log_line`` — one INFO line in the gateway/CLI log.
  - ``append_turn_jsonl`` — one JSON line in ``$HERMES_HOME/telemetry/turns.jsonl``.
  - ``format_telegram_footer`` — a short suffix appended to the final message
    text (mirrors ``gateway/runtime_footer.py``'s footer pattern).

Every public function here is a pure computation or a best-effort I/O
side-effect wrapped in try/except — nothing in this module may raise into the
turn it's instrumenting.  Callers should still guard their own call sites
(the retry loop, ``finalize_turn``, the gateway delivery path) since a plain
function call can still fail on an unexpected input shape upstream.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Telegram's message-length limit is 4096 UTF-16 code units (see
# gateway/platforms/base.py:utf16_len / plugins/platforms/telegram/adapter.py
# MAX_MESSAGE_LENGTH).  Duplicated as a default here rather than imported —
# this module must stay import-cycle-safe from agent/conversation_loop.py and
# agent/turn_finalizer.py, and pulling in gateway.platforms.base would create
# one.  Callers that know the real per-platform limit should pass it explicitly.
_DEFAULT_TELEGRAM_LIMIT = 4096

_JSONL_ROTATE_BYTES = 5 * 1024 * 1024  # 5MB


@dataclass
class PerCallUsage:
    """Raw usage + timing from a single API call inside a turn's tool loop.

    Captured once per successful call in the ``conversation_loop`` retry
    loop, right where the existing cache-stats CLI display already reads
    ``canonical_usage`` (agent/conversation_loop.py ~L2142).
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    gen_seconds: float = 0.0
    approx: bool = False  # True when gen_seconds is whole-request, not decode-only


@dataclass
class TurnTelemetry:
    """Aggregated per-turn telemetry — the shape written to the JSONL sink,
    the log line, and the Telegram footer."""

    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_hit_pct: Optional[float]  # None when the first call reported no cache data
    gen_seconds: float
    tps: Optional[float]  # None when gen_seconds is 0 (div-zero guard)
    model: str
    provider: str
    session_id: str
    ts: float = field(default_factory=time.time)
    approx: bool = False


def compute_cache_hit_pct(cache_read_tokens: int, prompt_tokens: int) -> Optional[float]:
    """cache_read / prompt as a 0-100 percentage, or None when unknowable.

    None (not 0) signals "the provider didn't report cache data" — a genuine
    0% cache hit (cache_read_tokens == 0 with prompt_tokens > 0) is a valid,
    distinct value from "we don't know."  Guards div-by-zero when prompt_tokens
    is 0.
    """
    if prompt_tokens <= 0:
        return None
    if cache_read_tokens <= 0:
        return None
    return (cache_read_tokens / prompt_tokens) * 100.0


def compute_tps(completion_tokens: int, gen_seconds: float) -> Optional[float]:
    """completion_tokens / gen_seconds, or None when gen_seconds is 0 (div-zero guard)."""
    if gen_seconds <= 0:
        return None
    return completion_tokens / gen_seconds


def aggregate_turn_telemetry(
    calls: List[PerCallUsage],
    *,
    model: str,
    provider: str,
    session_id: str,
) -> Optional[TurnTelemetry]:
    """Combine a turn's per-call usage records into one TurnTelemetry.

    Multi-call agentic turns (tool loops) sum tokens and gen_seconds across
    every API call in the turn.  ``cache_hit_pct`` is the exception: it is
    read from the FIRST call only.  The first call is the one that hits the
    long conversation-prefix — every subsequent call in the same turn is
    warmed by the prior call's own KV cache (the provider just cached what it
    was sent seconds ago), so a naive sum/average would inflate the reported
    hit rate well above what the *user's actual prefix* achieved.

    Returns None for an empty ``calls`` list (nothing to report — e.g. a
    turn that failed before any API call completed).
    """
    if not calls:
        return None

    total_prompt = sum(c.prompt_tokens for c in calls)
    total_completion = sum(c.completion_tokens for c in calls)
    total_cache_read = sum(c.cache_read_tokens for c in calls)
    total_gen_seconds = sum(c.gen_seconds for c in calls)
    any_approx = any(c.approx for c in calls)

    first = calls[0]
    cache_hit_pct = compute_cache_hit_pct(first.cache_read_tokens, first.prompt_tokens)
    tps = compute_tps(total_completion, total_gen_seconds)

    return TurnTelemetry(
        prompt_tokens=total_prompt,
        completion_tokens=total_completion,
        cache_read_tokens=total_cache_read,
        cache_hit_pct=cache_hit_pct,
        gen_seconds=total_gen_seconds,
        tps=tps,
        model=model or "",
        provider=provider or "",
        session_id=session_id or "",
        approx=any_approx,
    )


def _format_token_count(n: int) -> str:
    """Compact token count for the log line / footer (45000 -> '45k')."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def format_log_line(t: TurnTelemetry) -> str:
    """Render the gateway INFO log line.

    Example: ``turn-telemetry: model=aeon tps=14.2 cache=87% prompt=45k(+39k cached) completion=612 gen=43.1s``
    """
    tps_str = f"{t.tps:.1f}" if t.tps is not None else "n/a"
    cache_str = f"{t.cache_hit_pct:.0f}%" if t.cache_hit_pct is not None else "n/a"
    prompt_str = _format_token_count(t.prompt_tokens)
    if t.cache_read_tokens > 0:
        prompt_str += f"(+{_format_token_count(t.cache_read_tokens)} cached)"
    approx_str = " approx=true" if t.approx else ""
    return (
        f"turn-telemetry: model={t.model} tps={tps_str} cache={cache_str} "
        f"prompt={prompt_str} completion={t.completion_tokens} "
        f"gen={t.gen_seconds:.1f}s{approx_str}"
    )


def format_telegram_footer(
    t: Optional[TurnTelemetry],
    *,
    current_length: int = 0,
    limit: int = _DEFAULT_TELEGRAM_LIMIT,
) -> str:
    """Render the compact Telegram footer, or "" when it should be omitted.

    Example: ``\\n\\n⚡ 14.2 tok/s · cache 87% · 45k ctx``

    Omission rules (all degrade to returning ""):
      - telemetry is None (nothing captured this turn).
      - tps is None (div-zero guard tripped — no usable gen_seconds).
      - appending the footer would push the message past *limit* — the
        caller passes the length of the text the footer would be appended to;
        we never truncate or split the message ourselves, we just skip.
    """
    if t is None or t.tps is None:
        return ""

    parts = [f"{t.tps:.1f} tok/s"]
    if t.cache_hit_pct is not None:
        parts.append(f"cache {t.cache_hit_pct:.0f}%")
    if t.prompt_tokens > 0:
        parts.append(f"{_format_token_count(t.prompt_tokens)} ctx")

    footer = "\n\n⚡ " + " · ".join(parts)

    # UTF-16 code units, matching Telegram's own length accounting (see
    # gateway/platforms/base.py:utf16_len).  Avoid importing that module here
    # to keep this file free of a gateway -> agent import cycle risk; the
    # encode/len trick is the same one-liner it uses internally.
    footer_len = len(footer.encode("utf-16-le")) // 2
    if current_length + footer_len > limit:
        return ""
    return footer


def _telemetry_dir(hermes_home: Optional[str] = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "telemetry"


def _rotate_if_needed(path: Path) -> None:
    """Rename *path* to ``<path>.1`` if it exceeds the rotation threshold.

    Overwrites any previous ``.1`` (single-generation rotation, per spec —
    this is a lightweight debugging aid, not an audit trail).
    """
    try:
        if not path.exists():
            return
        if path.stat().st_size <= _JSONL_ROTATE_BYTES:
            return
        rotated = path.with_suffix(path.suffix + ".1")
        path.replace(rotated)
    except Exception:
        logger.debug("turn_telemetry: rotation check failed for %s", path, exc_info=True)


def append_turn_jsonl(t: TurnTelemetry, *, hermes_home: Optional[str] = None) -> None:
    """Append one JSON line for *t* to ``$HERMES_HOME/telemetry/turns.jsonl``.

    Creates the ``telemetry/`` directory if missing.  Rotates the file to
    ``turns.jsonl.1`` first if it has grown past 5MB.  Never raises — a
    failure here (disk full, permissions, whatever) must never affect the
    turn it's instrumenting, so every failure mode is swallowed and logged
    at debug level only.
    """
    try:
        directory = _telemetry_dir(hermes_home)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "turns.jsonl"
        _rotate_if_needed(path)
        record = {
            "prompt_tokens": t.prompt_tokens,
            "completion_tokens": t.completion_tokens,
            "cache_read_tokens": t.cache_read_tokens,
            "cache_hit_pct": t.cache_hit_pct,
            "gen_seconds": t.gen_seconds,
            "tps": t.tps,
            "model": t.model,
            "provider": t.provider,
            "session_id": t.session_id,
            "ts": t.ts,
            "approx": t.approx,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("turn_telemetry: failed to append JSONL turn record", exc_info=True)


def telemetry_config(user_config: Optional[dict[str, Any]]) -> dict[str, bool]:
    """Resolve the ``telemetry:`` config block against its documented defaults.

    ::

        telemetry:
          enabled: true          # master switch for capture+jsonl+log line
          telegram_footer: true  # the UI footer
          jsonl: true

    Config absent (or any individual key absent) falls back to its default
    (all True).  ``enabled: false`` is a hard master switch: callers should
    treat ``telegram_footer``/``jsonl`` as moot once ``enabled`` is False —
    this function returns the raw resolved dict; gating on ``enabled`` is the
    caller's job (mirrors how ``resolve_footer_config`` in
    ``gateway/runtime_footer.py`` separates resolution from the enabled-check).
    """
    resolved = {"enabled": True, "telegram_footer": True, "jsonl": True}
    cfg = (user_config or {}).get("telemetry")
    if isinstance(cfg, dict):
        for key in resolved:
            if key in cfg:
                resolved[key] = bool(cfg.get(key))
    return resolved
