"""Unit tests for agent.turn_telemetry — per-turn TPS + vLLM prefix-cache
hit % telemetry (capture math, JSONL sink, log line, Telegram footer)."""

from __future__ import annotations

import json

import pytest

from agent.turn_telemetry import (
    PerCallUsage,
    TurnTelemetry,
    aggregate_turn_telemetry,
    append_turn_jsonl,
    compute_cache_hit_pct,
    compute_tps,
    format_log_line,
    format_telegram_footer,
    telemetry_config,
)


# ---------------------------------------------------------------------------
# compute_cache_hit_pct / compute_tps — pure math + div-zero + None guards
# ---------------------------------------------------------------------------

def test_cache_hit_pct_basic_math():
    assert compute_cache_hit_pct(39000, 45000) == pytest.approx(86.666, abs=0.01)


def test_cache_hit_pct_zero_prompt_tokens_is_none():
    # div-zero guard: prompt_tokens <= 0 can never yield a percentage.
    assert compute_cache_hit_pct(0, 0) is None
    assert compute_cache_hit_pct(100, 0) is None


def test_cache_hit_pct_no_cache_read_is_none_not_zero():
    # None means "provider didn't report cache data" — distinct from a
    # genuine 0% cache hit rate. A provider that simply never populates
    # prompt_tokens_details.cached_tokens must not render "0% cache".
    assert compute_cache_hit_pct(0, 45000) is None


def test_cache_hit_pct_full_cache_hit():
    assert compute_cache_hit_pct(1000, 1000) == 100.0


def test_tps_basic_math():
    assert compute_tps(612, 43.1) == pytest.approx(14.199, abs=0.01)


def test_tps_zero_gen_seconds_is_none():
    # div-zero guard
    assert compute_tps(500, 0.0) is None
    assert compute_tps(500, -1.0) is None


# ---------------------------------------------------------------------------
# aggregate_turn_telemetry — multi-call summation + first-call cache rule
# ---------------------------------------------------------------------------

def test_aggregate_empty_calls_returns_none():
    assert aggregate_turn_telemetry([], model="aeon", provider="vllm", session_id="s1") is None


def test_aggregate_single_call():
    calls = [
        PerCallUsage(prompt_tokens=45000, completion_tokens=612, cache_read_tokens=39000, gen_seconds=43.1),
    ]
    t = aggregate_turn_telemetry(calls, model="aeon", provider="vllm", session_id="s1")
    assert t.prompt_tokens == 45000
    assert t.completion_tokens == 612
    assert t.cache_read_tokens == 39000
    assert t.cache_hit_pct == pytest.approx(86.666, abs=0.01)
    assert t.gen_seconds == pytest.approx(43.1)
    assert t.tps == pytest.approx(14.199, abs=0.01)
    assert t.model == "aeon"
    assert t.provider == "vllm"
    assert t.session_id == "s1"
    assert t.approx is False


def test_aggregate_sums_tokens_and_gen_seconds_across_calls():
    """A tool-calling turn spans several API calls; totals must sum."""
    calls = [
        PerCallUsage(prompt_tokens=45000, completion_tokens=100, cache_read_tokens=39000, gen_seconds=5.0),
        PerCallUsage(prompt_tokens=45200, completion_tokens=200, cache_read_tokens=45000, gen_seconds=8.0),
        PerCallUsage(prompt_tokens=45500, completion_tokens=300, cache_read_tokens=45300, gen_seconds=10.0),
    ]
    t = aggregate_turn_telemetry(calls, model="aeon", provider="vllm", session_id="s1")
    assert t.prompt_tokens == 45000 + 45200 + 45500
    assert t.completion_tokens == 100 + 200 + 300
    assert t.cache_read_tokens == 39000 + 45000 + 45300
    assert t.gen_seconds == pytest.approx(23.0)
    assert t.tps == pytest.approx(600 / 23.0)


def test_aggregate_cache_hit_pct_uses_first_call_only():
    """The FIRST call hits the long user-conversation prefix; later calls in
    the same tool loop are warmed by construction (the provider just cached
    what the prior call sent seconds ago) and would inflate the reported
    rate above what the user's actual prefix achieved."""
    calls = [
        PerCallUsage(prompt_tokens=45000, completion_tokens=50, cache_read_tokens=9000, gen_seconds=2.0),  # 20% cold
        PerCallUsage(prompt_tokens=45100, completion_tokens=50, cache_read_tokens=45000, gen_seconds=1.0),  # 99.7% warm
        PerCallUsage(prompt_tokens=45300, completion_tokens=50, cache_read_tokens=45200, gen_seconds=1.0),  # warm
    ]
    t = aggregate_turn_telemetry(calls, model="aeon", provider="vllm", session_id="s1")
    # Must reflect the first call's 20%, not a blended/averaged/summed figure.
    assert t.cache_hit_pct == pytest.approx(20.0, abs=0.1)


def test_aggregate_first_call_no_cache_data_is_none_even_if_later_calls_have_it():
    calls = [
        PerCallUsage(prompt_tokens=1000, completion_tokens=50, cache_read_tokens=0, gen_seconds=1.0),
        PerCallUsage(prompt_tokens=1000, completion_tokens=50, cache_read_tokens=900, gen_seconds=1.0),
    ]
    t = aggregate_turn_telemetry(calls, model="aeon", provider="vllm", session_id="s1")
    assert t.cache_hit_pct is None


def test_aggregate_propagates_approx_flag_if_any_call_is_approx():
    calls = [
        PerCallUsage(prompt_tokens=1000, completion_tokens=50, cache_read_tokens=0, gen_seconds=1.0, approx=False),
        PerCallUsage(prompt_tokens=1000, completion_tokens=50, cache_read_tokens=0, gen_seconds=1.0, approx=True),
    ]
    t = aggregate_turn_telemetry(calls, model="aeon", provider="vllm", session_id="s1")
    assert t.approx is True


def test_aggregate_zero_gen_seconds_total_gives_none_tps():
    calls = [PerCallUsage(prompt_tokens=100, completion_tokens=50, cache_read_tokens=0, gen_seconds=0.0)]
    t = aggregate_turn_telemetry(calls, model="m", provider="p", session_id="s")
    assert t.tps is None


# ---------------------------------------------------------------------------
# format_log_line
# ---------------------------------------------------------------------------

def _telemetry(**overrides) -> TurnTelemetry:
    defaults = dict(
        prompt_tokens=45000,
        completion_tokens=612,
        cache_read_tokens=39000,
        cache_hit_pct=86.6,
        gen_seconds=43.1,
        tps=14.2,
        model="aeon",
        provider="vllm",
        session_id="sess-1",
        ts=1000.0,
        approx=False,
    )
    defaults.update(overrides)
    return TurnTelemetry(**defaults)


def test_format_log_line_matches_spec_example_shape():
    line = format_log_line(_telemetry())
    assert line == (
        "turn-telemetry: model=aeon tps=14.2 cache=87% prompt=45k(+39k cached) "
        "completion=612 gen=43.1s"
    )


def test_format_log_line_omits_cached_suffix_when_no_cache_data():
    t = _telemetry(cache_read_tokens=0, cache_hit_pct=None)
    line = format_log_line(t)
    assert "cached)" not in line
    assert "cache=n/a" in line


def test_format_log_line_tps_na_when_none():
    t = _telemetry(tps=None)
    line = format_log_line(t)
    assert "tps=n/a" in line


def test_format_log_line_marks_approx():
    t = _telemetry(approx=True)
    line = format_log_line(t)
    assert "approx=true" in line


# ---------------------------------------------------------------------------
# format_telegram_footer — omission cases + length-guard degrade-safely
# ---------------------------------------------------------------------------

def test_footer_matches_spec_example_shape():
    footer = format_telegram_footer(_telemetry())
    assert footer == "\n\n⚡ 14.2 tok/s · cache 87% · 45k ctx"


def test_footer_omitted_when_telemetry_none():
    assert format_telegram_footer(None) == ""


def test_footer_omitted_when_tps_none():
    t = _telemetry(tps=None)
    assert format_telegram_footer(t) == ""


def test_footer_omits_cache_segment_when_unknown():
    t = _telemetry(cache_read_tokens=0, cache_hit_pct=None)
    footer = format_telegram_footer(t)
    assert "cache" not in footer
    assert "14.2 tok/s" in footer
    assert "45k ctx" in footer


def test_footer_degrades_safely_when_would_exceed_limit():
    """Never split the message — skip the footer entirely instead."""
    t = _telemetry()
    footer_len = len(format_telegram_footer(t).encode("utf-16-le")) // 2
    # current_length right at the boundary where footer would push past limit
    out = format_telegram_footer(t, current_length=4096 - footer_len + 1, limit=4096)
    assert out == ""


def test_footer_fits_exactly_at_limit_boundary():
    t = _telemetry()
    footer_len = len(format_telegram_footer(t).encode("utf-16-le")) // 2
    out = format_telegram_footer(t, current_length=4096 - footer_len, limit=4096)
    assert out != ""


def test_footer_uses_default_limit_of_4096():
    t = _telemetry()
    # A message body already near Telegram's real limit should suppress the
    # footer without the caller having to know the constant.
    out = format_telegram_footer(t, current_length=4090)
    assert out == ""


# ---------------------------------------------------------------------------
# append_turn_jsonl — valid JSON lines, rotation, write-failure swallowed
# ---------------------------------------------------------------------------

def test_append_turn_jsonl_writes_valid_json_line(tmp_path):
    t = _telemetry()
    append_turn_jsonl(t, hermes_home=str(tmp_path))

    path = tmp_path / "telemetry" / "turns.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["prompt_tokens"] == 45000
    assert record["completion_tokens"] == 612
    assert record["cache_read_tokens"] == 39000
    assert record["cache_hit_pct"] == pytest.approx(86.6)
    assert record["tps"] == pytest.approx(14.2)
    assert record["model"] == "aeon"
    assert record["provider"] == "vllm"
    assert record["session_id"] == "sess-1"
    assert record["ts"] == 1000.0
    assert record["approx"] is False


def test_append_turn_jsonl_creates_directory(tmp_path):
    target_home = tmp_path / "does" / "not" / "exist" / "yet"
    append_turn_jsonl(_telemetry(), hermes_home=str(target_home))
    assert (target_home / "telemetry" / "turns.jsonl").exists()


def test_append_turn_jsonl_appends_multiple_lines(tmp_path):
    append_turn_jsonl(_telemetry(session_id="a"), hermes_home=str(tmp_path))
    append_turn_jsonl(_telemetry(session_id="b"), hermes_home=str(tmp_path))
    path = tmp_path / "telemetry" / "turns.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["session_id"] == "a"
    assert json.loads(lines[1])["session_id"] == "b"


def test_append_turn_jsonl_rotates_when_over_threshold(tmp_path):
    directory = tmp_path / "telemetry"
    directory.mkdir(parents=True)
    path = directory / "turns.jsonl"
    # Pre-seed a file already over the 5MB rotation threshold.
    path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))

    append_turn_jsonl(_telemetry(), hermes_home=str(tmp_path))

    rotated = directory / "turns.jsonl.1"
    assert rotated.exists()
    assert rotated.stat().st_size == 5 * 1024 * 1024 + 1
    # The live file now holds only the new record.
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_append_turn_jsonl_rotation_replaces_previous_dot_one(tmp_path):
    directory = tmp_path / "telemetry"
    directory.mkdir(parents=True)
    path = directory / "turns.jsonl"
    old_rotated = directory / "turns.jsonl.1"
    old_rotated.write_text("stale-previous-rotation\n", encoding="utf-8")
    path.write_bytes(b"y" * (5 * 1024 * 1024 + 1))

    append_turn_jsonl(_telemetry(), hermes_home=str(tmp_path))

    # The stale .1 was replaced by the just-rotated file, not left in place
    # or appended to.
    assert old_rotated.read_bytes() == b"y" * (5 * 1024 * 1024 + 1)


def test_append_turn_jsonl_under_threshold_does_not_rotate(tmp_path):
    directory = tmp_path / "telemetry"
    directory.mkdir(parents=True)
    path = directory / "turns.jsonl"
    path.write_text("small\n", encoding="utf-8")

    append_turn_jsonl(_telemetry(), hermes_home=str(tmp_path))

    assert not (directory / "turns.jsonl.1").exists()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # "small" + the new record


def test_append_turn_jsonl_write_failure_is_swallowed(tmp_path, monkeypatch):
    """A failure writing the JSONL sink must never raise into the turn."""
    import agent.turn_telemetry as tt

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(tt, "open", _boom, raising=False)
    # Patching builtins.open via the module namespace doesn't intercept the
    # bare `open()` call inside the function (which resolves through
    # builtins), so patch mkdir instead to force the failure deterministically.
    monkeypatch.setattr(
        tt.Path, "mkdir",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk full")),
    )
    # Must not raise.
    append_turn_jsonl(_telemetry(), hermes_home=str(tmp_path))
    assert not (tmp_path / "telemetry" / "turns.jsonl").exists()


def test_append_turn_jsonl_defaults_to_hermes_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    append_turn_jsonl(_telemetry())
    assert (tmp_path / "telemetry" / "turns.jsonl").exists()


# ---------------------------------------------------------------------------
# telemetry_config — defaults + partial overrides
# ---------------------------------------------------------------------------

def test_telemetry_config_defaults_all_true_when_absent():
    assert telemetry_config(None) == {"enabled": True, "telegram_footer": True, "jsonl": True}
    assert telemetry_config({}) == {"enabled": True, "telegram_footer": True, "jsonl": True}


def test_telemetry_config_respects_explicit_false():
    cfg = telemetry_config({"telemetry": {"enabled": False}})
    assert cfg["enabled"] is False
    # Sibling keys keep their defaults when not explicitly set.
    assert cfg["telegram_footer"] is True
    assert cfg["jsonl"] is True


def test_telemetry_config_partial_override_keeps_other_defaults():
    cfg = telemetry_config({"telemetry": {"telegram_footer": False}})
    assert cfg == {"enabled": True, "telegram_footer": False, "jsonl": True}


def test_telemetry_config_ignores_malformed_block():
    cfg = telemetry_config({"telemetry": "on"})
    assert cfg == {"enabled": True, "telegram_footer": True, "jsonl": True}
