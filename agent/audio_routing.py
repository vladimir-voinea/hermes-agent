"""Routing helpers for inbound voice recordings (/voice).

Two modes:

  native — attach the raw WAV as an OpenAI-style ``input_audio`` content
           part on the user turn. Audio-native models served through
           OpenAI-compatible endpoints (e.g. Nemotron-3 Nano Omni on vLLM)
           hear the actual audio — prosody, tone, and all.

  stt    — transcribe the WAV with the configured STT backend (whisper)
           and send the transcript as plain text. This is the pre-existing
           behaviour and still the right choice for audio-blind models.

The decision is made by :func:`decide_audio_input_mode`. It reads
``agent.audio_input_mode`` from config.yaml (``auto`` | ``native`` | ``stt``,
default ``auto``) and the active model's capability metadata.

In ``auto`` mode:
  - If the active model reports ``supports_audio_input=True`` (via config
    override or models.dev metadata), we attach natively.
  - Otherwise we fall back to STT — the safe default for every model that
    can't take audio.

STT remains the fallback even after a "native" decision: the caller
(cli.py chat()) re-decides against the turn-resolved model and degrades to
``transcribe_recording`` whenever native parts can't be built.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.image_routing import _supports_capability_override

logger = logging.getLogger(__name__)


_VALID_MODES = frozenset({"auto", "native", "stt"})

# Voice-mode guidance — mirrors the STT-path prefix cli.py prepends when
# self._voice_mode is active, so native-audio turns get the same concise
# conversational style.
_VOICE_MODE_GUIDANCE = (
    "[Voice input — do the task exactly as you normally would: use tools, "
    "run commands, edit files, and reason through as many steps as it takes. "
    "This only shapes your final spoken reply, which will be read aloud by "
    "text-to-speech — so write that reply for the ear, not the screen: plain "
    "spoken language, no markdown, code blocks, bullet lists, tables, URLs, or "
    "symbols to be read out. If you did work, say what you did and how it turned "
    "out rather than pasting it. Let the length fit the content — a word or two "
    "when that's the answer, a few sentences when more is genuinely needed — but "
    "stay tight and skip filler.]"
)


def _coerce_mode(raw: Any) -> str:
    """Normalize a config value into one of the valid modes."""
    if not isinstance(raw, str):
        return "auto"
    val = raw.strip().lower()
    if val in _VALID_MODES:
        return val
    return "auto"


def _lookup_supports_audio_input(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """Return True/False if we can resolve caps, None if unknown.

    Consults the user's ``supports_audio_input`` override in config.yaml
    first (so custom/local audio-native models don't fall through to STT
    in ``auto`` mode), then falls back to models.dev.
    """
    override = _supports_capability_override(
        cfg, provider, model, key="supports_audio_input"
    )
    if override is not None:
        return override
    if not provider or not model:
        return None
    try:
        from agent.models_dev import get_model_capabilities
        caps = get_model_capabilities(provider, model)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("audio_routing: caps lookup failed for %s:%s — %s", provider, model, exc)
        caps = None
    if caps is not None:
        return bool(caps.supports_audio_input)
    return None


def decide_audio_input_mode(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]],
) -> str:
    """Return ``"native"`` or ``"stt"`` for the given turn.

    Args:
      provider: active inference provider ID (e.g. ``"custom"``, ``"openai"``).
      model:    active model slug as it would be sent to the provider.
      cfg:      loaded config.yaml dict, or None. When None, behaves as auto.
    """
    mode_cfg = "auto"
    if isinstance(cfg, dict):
        agent_cfg = cfg.get("agent") or {}
        if isinstance(agent_cfg, dict):
            mode_cfg = _coerce_mode(agent_cfg.get("audio_input_mode"))

    if mode_cfg == "native":
        return "native"
    if mode_cfg == "stt":
        return "stt"

    # auto: native only when the model is known audio-capable. Unknown
    # capability means STT — the historical path that works everywhere.
    supports = _lookup_supports_audio_input(provider, model, cfg)
    if supports is True:
        return "native"
    return "stt"


def build_native_audio_parts(
    user_text: str,
    wav_path: str,
    voice_mode: bool = False,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Build an OpenAI-style ``content`` list carrying a WAV recording.

    Shape (verified against vLLM's OpenAI-compatible endpoint):
      # voice-only (no typed text, not voice-mode) — audio ALONE, like nemo:
      [{"type": "input_audio", "input_audio": {"data": "<base64 wav>", "format": "wav"}}]
      # with a typed caption and/or voice-mode guidance — a leading text part:
      [{"type": "text", "text": "<voice guidance> <user caption>"},
       {"type": "input_audio", ...}]

    No "attached audio" caption and no ``[Audio message attached at: <path>]``
    file hint are ever synthesized. Audio-native models just hear the clip
    (nemo needs no caption); a file-framed text part makes agentic, text-first
    models like gemma hunt for a path / call video_analyze instead of listening
    (empirically verified). A text part appears only for the user's own typed
    caption and/or voice-mode reply-style guidance (which shapes the spoken
    reply — it does not describe the audio).

    Returns ``(parts, None)`` on success, ``(None, reason)`` when the WAV
    can't be read — the caller degrades to STT.
    """
    p = Path(wav_path)
    try:
        raw = p.read_bytes()
    except Exception as exc:
        logger.warning("audio_routing: failed to read %s — %s", wav_path, exc)
        return None, f"failed to read {wav_path}: {exc}"
    if not raw:
        return None, f"empty recording at {wav_path}"

    b64 = base64.b64encode(raw).decode("ascii")

    # Do NOT synthesize an "attached audio" caption for a voice-only turn.
    # Audio-native models just hear the clip (nemo needs no caption); a
    # file-framed caption ("...in the attached audio") makes agentic, text-first
    # models like gemma hunt for a path or reach for video_analyze/transcription
    # tools instead of listening — and no path hint either (same reason; worse on
    # transcript replay where the base64 is stripped but the note survives).
    # Only emit a text part when there's something real to say: the user's own
    # typed caption, and/or voice-mode reply-style guidance (which shapes the
    # spoken reply — it does not describe the audio). A voice-only turn carries
    # the audio part ALONE, exactly like nemo receives it.
    lead_bits: List[str] = []
    if voice_mode:
        lead_bits.append(_VOICE_MODE_GUIDANCE)
    text = (user_text or "").strip()
    if text:
        lead_bits.append(text)

    parts: List[Dict[str, Any]] = []
    if lead_bits:
        parts.append({"type": "text", "text": " ".join(lead_bits)})
    parts.append({"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}})
    return parts, None


__all__ = [
    "decide_audio_input_mode",
    "build_native_audio_parts",
]
