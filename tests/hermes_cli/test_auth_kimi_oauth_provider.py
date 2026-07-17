"""Tests for Kimi Code OAuth provider authentication (hermes_cli/auth.py).

Covers: _kimi_cli_home, _kimi_cli_auth_path, _read_kimi_cli_tokens,
_save_kimi_cli_tokens, _kimi_access_token_is_expiring, _refresh_kimi_cli_tokens,
resolve_kimi_oauth_runtime_credentials, get_kimi_oauth_auth_status.
"""

import json
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.auth import (
    AuthError,
    DEFAULT_KIMI_OAUTH_BASE_URL,
    KIMI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    KIMI_OAUTH_CLIENT_ID,
    _kimi_access_token_is_expiring,
    _kimi_cli_auth_path,
    _kimi_cli_home,
    _kimi_oauth_token_url,
    _read_kimi_cli_tokens,
    _refresh_kimi_cli_tokens,
    _save_kimi_cli_tokens,
    get_kimi_oauth_auth_status,
    resolve_kimi_oauth_runtime_credentials,
)

# The exact on-disk key shape written by the Kimi CLI. Hermes round-trips this
# file, so drift here logs the user out of their CLI.
KIMI_CREDENTIAL_KEYS = {
    "access_token",
    "refresh_token",
    "expires_at",
    "scope",
    "token_type",
    "expires_in",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kimi_tokens(
    access_token="test-access-token",
    refresh_token="test-refresh-token",
    expires_at=None,
    **extra,
):
    """Create a Kimi CLI OAuth credential dict (expires_at is UNIX seconds)."""
    if expires_at is None:
        # 1 hour from now, in SECONDS
        expires_at = int(time.time()) + 3600
    data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "scope": "kimi-code",
        "token_type": "Bearer",
        "expires_in": 900,
    }
    data.update(extra)
    return data


def _write_kimi_creds(tokens=None):
    """Write tokens to the Kimi CLI credentials file and return the path."""
    creds_path = _kimi_cli_auth_path()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    if tokens is None:
        tokens = _make_kimi_tokens()
    creds_path.write_text(json.dumps(tokens), encoding="utf-8")
    return creds_path


@pytest.fixture()
def kimi_env(tmp_path, monkeypatch):
    """Point KIMI_CODE_HOME at tmp_path so creds + lock stay in the sandbox."""
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / ".kimi-code"))
    monkeypatch.delenv("KIMI_CODE_BASE_URL", raising=False)
    monkeypatch.delenv("KIMI_CODE_OAUTH_HOST", raising=False)
    monkeypatch.delenv("KIMI_OAUTH_HOST", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Paths / env resolution
# ---------------------------------------------------------------------------

def test_kimi_cli_home_defaults_to_dot_kimi_code(monkeypatch):
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    assert _kimi_cli_home() == Path.home() / ".kimi-code"


def test_kimi_cli_home_honors_env(kimi_env, tmp_path):
    assert _kimi_cli_home() == tmp_path / ".kimi-code"


def test_kimi_cli_auth_path_returns_expected_location(monkeypatch):
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    assert (
        _kimi_cli_auth_path()
        == Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
    )


def test_kimi_oauth_token_url_default(kimi_env):
    assert _kimi_oauth_token_url() == "https://auth.kimi.com/api/oauth/token"


def test_kimi_oauth_token_url_kimi_code_oauth_host_wins(kimi_env, monkeypatch):
    monkeypatch.setenv("KIMI_CODE_OAUTH_HOST", "https://first.example")
    monkeypatch.setenv("KIMI_OAUTH_HOST", "https://second.example")
    assert _kimi_oauth_token_url() == "https://first.example/api/oauth/token"


def test_kimi_oauth_token_url_falls_back_to_kimi_oauth_host(kimi_env, monkeypatch):
    monkeypatch.setenv("KIMI_OAUTH_HOST", "https://second.example")
    assert _kimi_oauth_token_url() == "https://second.example/api/oauth/token"


# ---------------------------------------------------------------------------
# _read_kimi_cli_tokens
# ---------------------------------------------------------------------------

def test_read_kimi_cli_tokens_success(kimi_env):
    _write_kimi_creds(_make_kimi_tokens(access_token="my-access"))
    result = _read_kimi_cli_tokens()
    assert result["access_token"] == "my-access"
    assert result["refresh_token"] == "test-refresh-token"


def test_read_kimi_cli_tokens_missing_file(kimi_env):
    with pytest.raises(AuthError) as exc:
        _read_kimi_cli_tokens()
    assert exc.value.code == "kimi_auth_missing"


def test_read_kimi_cli_tokens_invalid_json(kimi_env):
    creds_path = _kimi_cli_auth_path()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text("not json{{{", encoding="utf-8")
    with pytest.raises(AuthError) as exc:
        _read_kimi_cli_tokens()
    assert exc.value.code == "kimi_auth_read_failed"


def test_read_kimi_cli_tokens_non_dict(kimi_env):
    creds_path = _kimi_cli_auth_path()
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    with pytest.raises(AuthError) as exc:
        _read_kimi_cli_tokens()
    assert exc.value.code == "kimi_auth_invalid"


# ---------------------------------------------------------------------------
# _save_kimi_cli_tokens
# ---------------------------------------------------------------------------

def test_save_kimi_cli_tokens_roundtrip(kimi_env):
    saved_path = _save_kimi_cli_tokens(_make_kimi_tokens(access_token="saved-token"))
    assert saved_path.exists()
    loaded = json.loads(saved_path.read_text(encoding="utf-8"))
    assert loaded["access_token"] == "saved-token"


def test_save_kimi_cli_tokens_creates_parent(kimi_env):
    saved_path = _save_kimi_cli_tokens(_make_kimi_tokens())
    assert saved_path.parent.exists()


def test_save_kimi_cli_tokens_permissions(kimi_env):
    saved_path = _save_kimi_cli_tokens(_make_kimi_tokens())
    mode = saved_path.stat().st_mode
    assert mode & stat.S_IRUSR  # owner read
    assert mode & stat.S_IWUSR  # owner write
    assert not (mode & stat.S_IRGRP)  # no group read
    assert not (mode & stat.S_IROTH)  # no other read


def test_save_kimi_cli_tokens_preserves_key_shape(kimi_env):
    saved_path = _save_kimi_cli_tokens(_make_kimi_tokens())
    loaded = json.loads(saved_path.read_text(encoding="utf-8"))
    assert set(loaded.keys()) == KIMI_CREDENTIAL_KEYS


# ---------------------------------------------------------------------------
# _kimi_access_token_is_expiring — expires_at is UNIX SECONDS, not ms
# ---------------------------------------------------------------------------

def test_expiring_token_not_expired():
    # 1 hour out, in seconds — comfortably outside the 300s skew.
    assert not _kimi_access_token_is_expiring(int(time.time()) + 3600)


def test_expiring_token_already_expired():
    assert _kimi_access_token_is_expiring(int(time.time()) - 3600)


def test_expiring_token_within_skew_is_expiring():
    # A token 60s from death IS expiring under the 300s skew. This is the
    # seconds-vs-milliseconds regression guard: read as ms, a 60s-out token
    # looks ~56,000 years fresh and every call 401s.
    assert _kimi_access_token_is_expiring(int(time.time()) + 60)


def test_expiring_token_seconds_not_milliseconds(kimi_env):
    """A ms-scale comparison would call a 60s-out token fresh, and a 3600s-out
    token is genuinely fresh. Both directions pin the unit."""
    assert _kimi_access_token_is_expiring(int(time.time()) + 60)
    assert not _kimi_access_token_is_expiring(int(time.time()) + 3600)


def test_expiring_token_just_outside_skew():
    near = int(time.time()) + KIMI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS + 30
    assert not _kimi_access_token_is_expiring(near)


def test_expiring_token_just_inside_skew():
    near = int(time.time()) + KIMI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS - 30
    assert _kimi_access_token_is_expiring(near)


def test_expiring_token_none_returns_true():
    assert _kimi_access_token_is_expiring(None)


def test_expiring_token_non_numeric_returns_true():
    assert _kimi_access_token_is_expiring("not-a-number")


def test_expiring_token_custom_skew():
    expires_at = int(time.time()) + 600
    assert not _kimi_access_token_is_expiring(expires_at, 300)
    assert _kimi_access_token_is_expiring(expires_at, 900)


# ---------------------------------------------------------------------------
# _refresh_kimi_cli_tokens
# ---------------------------------------------------------------------------

def _refresh_response(**overrides):
    resp = MagicMock()
    resp.status_code = 200
    payload = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 900,
        "scope": "kimi-code",
        "token_type": "Bearer",
    }
    payload.update(overrides)
    resp.json.return_value = payload
    return resp


def test_refresh_kimi_cli_tokens_success(kimi_env):
    tokens = _make_kimi_tokens(refresh_token="old-refresh")

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = _refresh_response()
        result = _refresh_kimi_cli_tokens(tokens)

    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "new-refresh"
    # expires_at is derived: the endpoint returns expires_in only.
    assert abs(result["expires_at"] - (int(time.time()) + 900)) <= 5


def test_refresh_kimi_cli_tokens_posts_expected_form(kimi_env):
    tokens = _make_kimi_tokens(refresh_token="the-refresh")

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = _refresh_response()
        _refresh_kimi_cli_tokens(tokens)

    args, kwargs = mock_httpx.post.call_args
    assert args[0] == "https://auth.kimi.com/api/oauth/token"
    assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert kwargs["data"] == {
        "client_id": KIMI_OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "the-refresh",
    }


def test_refresh_kimi_cli_tokens_persists_rotated_refresh_token(kimi_env):
    """The refresh token ROTATES — the new one must hit disk immediately, or
    the user's Kimi CLI is left holding a spent token."""
    _write_kimi_creds(_make_kimi_tokens(refresh_token="old-refresh"))
    tokens = _read_kimi_cli_tokens()

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = _refresh_response(refresh_token="rotated-refresh")
        _refresh_kimi_cli_tokens(tokens)

    on_disk = json.loads(_kimi_cli_auth_path().read_text(encoding="utf-8"))
    assert on_disk["refresh_token"] == "rotated-refresh"
    assert on_disk["access_token"] == "new-access"
    # Re-reading through the normal path must see the rotation too.
    assert _read_kimi_cli_tokens()["refresh_token"] == "rotated-refresh"


def test_refresh_kimi_cli_tokens_disk_shape_is_exactly_six_keys(kimi_env):
    _write_kimi_creds(_make_kimi_tokens())
    tokens = _read_kimi_cli_tokens()

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = _refresh_response()
        _refresh_kimi_cli_tokens(tokens)

    on_disk = json.loads(_kimi_cli_auth_path().read_text(encoding="utf-8"))
    assert set(on_disk.keys()) == KIMI_CREDENTIAL_KEYS
    assert on_disk["scope"] == "kimi-code"
    assert on_disk["token_type"] == "Bearer"
    assert on_disk["expires_in"] == 900
    assert isinstance(on_disk["expires_at"], int)


def test_refresh_kimi_cli_tokens_mode_stays_0600(kimi_env):
    creds_path = _write_kimi_creds(_make_kimi_tokens())
    creds_path.chmod(0o600)
    tokens = _read_kimi_cli_tokens()

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = _refresh_response()
        _refresh_kimi_cli_tokens(tokens)

    assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600


def test_refresh_kimi_cli_tokens_keeps_old_refresh_if_absent_from_response(kimi_env):
    tokens = _make_kimi_tokens(refresh_token="keep-me")

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        resp = _refresh_response()
        resp.json.return_value = {"access_token": "new-access", "expires_in": 900}
        mock_httpx.post.return_value = resp
        result = _refresh_kimi_cli_tokens(tokens)

    assert result["refresh_token"] == "keep-me"


def test_refresh_kimi_cli_tokens_missing_refresh_token():
    with pytest.raises(AuthError) as exc:
        _refresh_kimi_cli_tokens({"access_token": "at", "refresh_token": ""})
    assert exc.value.code == "kimi_refresh_token_missing"


def test_refresh_kimi_cli_tokens_http_error(kimi_env):
    resp = MagicMock()
    resp.status_code = 401
    resp.text = "unauthorized"

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = resp
        with pytest.raises(AuthError) as exc:
            _refresh_kimi_cli_tokens(_make_kimi_tokens())
    assert exc.value.code == "kimi_refresh_failed"
    assert "kimi" in str(exc.value).lower()


def test_refresh_kimi_cli_tokens_network_error(kimi_env):
    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.side_effect = ConnectionError("timeout")
        with pytest.raises(AuthError) as exc:
            _refresh_kimi_cli_tokens(_make_kimi_tokens())
    assert exc.value.code == "kimi_refresh_failed"


def test_refresh_kimi_cli_tokens_invalid_json_response(kimi_env):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("bad json")

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = resp
        with pytest.raises(AuthError) as exc:
            _refresh_kimi_cli_tokens(_make_kimi_tokens())
    assert exc.value.code == "kimi_refresh_invalid_json"


def test_refresh_kimi_cli_tokens_missing_access_token_in_response(kimi_env):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"something": "but no access_token"}

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = resp
        with pytest.raises(AuthError) as exc:
            _refresh_kimi_cli_tokens(_make_kimi_tokens())
    assert exc.value.code == "kimi_refresh_invalid_response"


def test_refresh_kimi_cli_tokens_default_expires_in(kimi_env):
    """When expires_in is missing, fall back to the 900s Kimi TTL."""
    with patch("hermes_cli.auth.httpx") as mock_httpx:
        resp = _refresh_response()
        resp.json.return_value = {"access_token": "new"}
        mock_httpx.post.return_value = resp
        result = _refresh_kimi_cli_tokens(_make_kimi_tokens())

    assert result["expires_in"] == 900
    assert abs(result["expires_at"] - (int(time.time()) + 900)) <= 5


def test_refresh_kimi_cli_tokens_honors_oauth_host_override(kimi_env, monkeypatch):
    monkeypatch.setenv("KIMI_CODE_OAUTH_HOST", "https://staging.example")

    with patch("hermes_cli.auth.httpx") as mock_httpx:
        mock_httpx.post.return_value = _refresh_response()
        _refresh_kimi_cli_tokens(_make_kimi_tokens())

    assert mock_httpx.post.call_args[0][0] == "https://staging.example/api/oauth/token"


# ---------------------------------------------------------------------------
# resolve_kimi_oauth_runtime_credentials
# ---------------------------------------------------------------------------

def test_resolve_kimi_fresh_token_does_not_refresh(kimi_env):
    _write_kimi_creds(_make_kimi_tokens(access_token="fresh-at"))

    with patch("hermes_cli.auth._refresh_kimi_cli_tokens") as mock_refresh:
        creds = resolve_kimi_oauth_runtime_credentials()

    mock_refresh.assert_not_called()
    assert creds["provider"] == "kimi-oauth"
    assert creds["api_key"] == "fresh-at"
    assert creds["base_url"] == DEFAULT_KIMI_OAUTH_BASE_URL
    assert creds["source"] == "kimi-code-cli"
    assert creds["auth_file"] == str(_kimi_cli_auth_path())


def test_resolve_kimi_expiring_token_triggers_refresh(kimi_env):
    # 60s of life left — inside the 300s skew.
    _write_kimi_creds(
        _make_kimi_tokens(access_token="old", expires_at=int(time.time()) + 60)
    )
    refreshed = _make_kimi_tokens(access_token="refreshed-at")

    with patch(
        "hermes_cli.auth._refresh_kimi_cli_tokens", return_value=refreshed
    ) as mock_refresh:
        creds = resolve_kimi_oauth_runtime_credentials()

    mock_refresh.assert_called_once()
    assert creds["api_key"] == "refreshed-at"


def test_resolve_kimi_expired_token_triggers_refresh(kimi_env):
    _write_kimi_creds(
        _make_kimi_tokens(access_token="old", expires_at=int(time.time()) - 3600)
    )
    refreshed = _make_kimi_tokens(access_token="refreshed-at")

    with patch(
        "hermes_cli.auth._refresh_kimi_cli_tokens", return_value=refreshed
    ) as mock_refresh:
        creds = resolve_kimi_oauth_runtime_credentials()

    mock_refresh.assert_called_once()
    assert creds["api_key"] == "refreshed-at"


def test_resolve_kimi_refresh_if_expiring_false_skips_refresh(kimi_env):
    _write_kimi_creds(
        _make_kimi_tokens(access_token="stale-at", expires_at=int(time.time()) - 3600)
    )

    with patch("hermes_cli.auth._refresh_kimi_cli_tokens") as mock_refresh:
        creds = resolve_kimi_oauth_runtime_credentials(refresh_if_expiring=False)

    mock_refresh.assert_not_called()
    assert creds["api_key"] == "stale-at"


def test_resolve_kimi_force_refresh(kimi_env):
    _write_kimi_creds(_make_kimi_tokens(access_token="old-at"))
    refreshed = _make_kimi_tokens(access_token="force-refreshed")

    with patch(
        "hermes_cli.auth._refresh_kimi_cli_tokens", return_value=refreshed
    ) as mock_refresh:
        creds = resolve_kimi_oauth_runtime_credentials(force_refresh=True)

    mock_refresh.assert_called_once()
    assert creds["api_key"] == "force-refreshed"


def test_resolve_kimi_rereads_under_lock_and_uses_peer_rotation(kimi_env):
    """If a peer rotated the pair while we waited on the lock, use their fresh
    token rather than spending our now-stale refresh token."""
    _write_kimi_creds(
        _make_kimi_tokens(access_token="stale-at", expires_at=int(time.time()) + 60)
    )

    real_lock = None

    def _peer_rotates_then_locks(*args, **kwargs):
        # Simulate the peer's write landing while we blocked on the lock.
        _write_kimi_creds(
            _make_kimi_tokens(
                access_token="peer-at",
                refresh_token="peer-refresh",
                expires_at=int(time.time()) + 900,
            )
        )
        return real_lock(*args, **kwargs)

    from hermes_cli import auth as auth_mod

    real_lock = auth_mod._kimi_oauth_lock
    with patch.object(auth_mod, "_kimi_oauth_lock", _peer_rotates_then_locks):
        with patch("hermes_cli.auth._refresh_kimi_cli_tokens") as mock_refresh:
            creds = resolve_kimi_oauth_runtime_credentials()

    mock_refresh.assert_not_called()
    assert creds["api_key"] == "peer-at"


def test_resolve_kimi_missing_access_token(kimi_env):
    _write_kimi_creds(_make_kimi_tokens(access_token=""))

    with pytest.raises(AuthError) as exc:
        resolve_kimi_oauth_runtime_credentials(refresh_if_expiring=False)
    assert exc.value.code == "kimi_access_token_missing"


def test_resolve_kimi_missing_credentials_file(kimi_env):
    with pytest.raises(AuthError) as exc:
        resolve_kimi_oauth_runtime_credentials()
    assert exc.value.code == "kimi_auth_missing"


def test_resolve_kimi_base_url_default(kimi_env):
    _write_kimi_creds(_make_kimi_tokens())
    creds = resolve_kimi_oauth_runtime_credentials()
    assert creds["base_url"] == "https://api.kimi.com/coding/v1"


def test_resolve_kimi_base_url_env_override(kimi_env, monkeypatch):
    _write_kimi_creds(_make_kimi_tokens())
    monkeypatch.setenv("KIMI_CODE_BASE_URL", "https://custom.kimi.example/coding/v1")

    creds = resolve_kimi_oauth_runtime_credentials()
    assert creds["base_url"] == "https://custom.kimi.example/coding/v1"


def test_resolve_kimi_base_url_env_override_strips_trailing_slash(kimi_env, monkeypatch):
    _write_kimi_creds(_make_kimi_tokens())
    monkeypatch.setenv("KIMI_CODE_BASE_URL", "https://custom.kimi.example/coding/v1/")

    creds = resolve_kimi_oauth_runtime_credentials()
    assert creds["base_url"] == "https://custom.kimi.example/coding/v1"


def test_resolve_kimi_expires_at_passthrough(kimi_env):
    expires_at = int(time.time()) + 3600
    _write_kimi_creds(_make_kimi_tokens(expires_at=expires_at))

    creds = resolve_kimi_oauth_runtime_credentials()
    assert creds["expires_at"] == expires_at


# ---------------------------------------------------------------------------
# get_kimi_oauth_auth_status
# ---------------------------------------------------------------------------

def test_get_kimi_oauth_auth_status_logged_in(kimi_env):
    _write_kimi_creds(_make_kimi_tokens(access_token="status-at"))

    status = get_kimi_oauth_auth_status()
    assert status["logged_in"] is True
    assert status["api_key"] == "status-at"
    assert status["source"] == "kimi-code-cli"


def test_get_kimi_oauth_auth_status_refreshes_expired_token(kimi_env):
    _write_kimi_creds(
        _make_kimi_tokens(access_token="old-at", expires_at=int(time.time()) - 3600)
    )
    refreshed = _make_kimi_tokens(access_token="refreshed-at")

    with patch(
        "hermes_cli.auth._refresh_kimi_cli_tokens", return_value=refreshed
    ) as mock_refresh:
        status = get_kimi_oauth_auth_status()

    mock_refresh.assert_called_once()
    assert status["logged_in"] is True
    assert status["api_key"] == "refreshed-at"


def test_get_kimi_oauth_auth_status_expired_unrefreshable_is_not_logged_in(kimi_env):
    _write_kimi_creds(
        _make_kimi_tokens(access_token="dead-at", expires_at=int(time.time()) - 3600)
    )

    with patch(
        "hermes_cli.auth._refresh_kimi_cli_tokens",
        side_effect=AuthError(
            "Kimi OAuth refresh failed. Re-run 'kimi' and complete the login.",
            provider="kimi-oauth",
            code="kimi_refresh_failed",
        ),
    ) as mock_refresh:
        status = get_kimi_oauth_auth_status()

    mock_refresh.assert_called_once()
    assert status["logged_in"] is False
    assert "kimi" in status["error"].lower()


def test_get_kimi_oauth_auth_status_not_logged_in(kimi_env):
    status = get_kimi_oauth_auth_status()
    assert status["logged_in"] is False
    assert "error" in status


# ---------------------------------------------------------------------------
# Registry / provider wiring
# ---------------------------------------------------------------------------

def test_kimi_oauth_in_provider_registry():
    from hermes_cli.auth import PROVIDER_REGISTRY

    pconfig = PROVIDER_REGISTRY["kimi-oauth"]
    assert pconfig.auth_type == "oauth_external"
    assert pconfig.inference_base_url == DEFAULT_KIMI_OAUTH_BASE_URL
    assert pconfig.client_id == KIMI_OAUTH_CLIENT_ID


@pytest.mark.parametrize(
    "alias", ["kimi-oauth", "kimi-code", "kimi-code-oauth", "kimi-coding-oauth"]
)
def test_kimi_oauth_aliases_resolve(alias):
    from hermes_cli.auth import resolve_provider

    assert resolve_provider(alias) == "kimi-oauth"


@pytest.mark.parametrize("alias", ["kimi", "moonshot", "kimi-for-coding"])
def test_api_key_kimi_aliases_unchanged(alias):
    """The api-key Kimi provider must keep resolving to kimi-coding."""
    from hermes_cli.auth import resolve_provider

    assert resolve_provider(alias) == "kimi-coding"
