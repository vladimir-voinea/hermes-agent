"""Runtime profile bindings — "make THIS topic the orchestrator", from chat.

``gateway.profile_routes`` in config.yaml already routes a chat/thread to a
profile, but it is operator config: you must know the thread_id before the
topic exists, edit YAML, and restart the gateway. That is fine for a fixed
layout and useless for the actual workflow, which is: open a topic, decide
what it is, use it.

So this is the same routing, bound at runtime and persisted. ``/profile
<name>`` in a topic writes a binding here; the next message in that topic is
served by that profile. No restart, no YAML, no thread_id hunting.

WHY IT REUSES ProfileRoute
--------------------------
Bindings are parsed into the same :class:`ProfileRoute` objects the static
routes use and matched by the same :func:`match_profile_route`. One matcher,
two sources. A second matching implementation would be a second answer to
"which profile serves this message", and they would disagree eventually.

WHERE IT LIVES
--------------
At the Hermes ROOT (``get_default_hermes_root()``), never in a profile's home.
Under multiplexing ``get_hermes_home()`` is a contextvar that the inbound path
rebinds per turn — so a bindings file resolved through it would be written into
whichever profile happened to be serving, and the multiplexer would never find
it again. The board has this exact bug in its history; routing must not repeat
it. Bindings are cross-profile by nature: they are how a message FINDS its
profile, so they cannot live inside one.

PRECEDENCE
----------
Bindings and static routes are merged and sorted by specificity, so a
thread-level route still beats a chat-level one whatever its source. On a tie
the binding wins: someone typed it into that topic, which is a more recent and
more deliberate act than a line in config.yaml.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FILENAME = "gateway-profile-bindings.json"

# (path, mtime) -> parsed. The gateway asks on every inbound message, so this
# is read-hot; but a binding must take effect on the NEXT message, so we
# re-read whenever the file changes rather than caching for a TTL.
_cache: Dict[str, Any] = {"path": None, "mtime": None, "data": []}


def bindings_path() -> Path:
    """``<hermes root>/gateway-profile-bindings.json`` — the ROOT, always.

    See the module docstring: resolving this through ``get_hermes_home()``
    under multiplexing would scatter bindings into whichever profile served
    the turn.
    """
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root()) / _FILENAME


def _key(platform: str, chat_id: Optional[str], thread_id: Optional[str]) -> str:
    return f"{platform}|{chat_id or ''}|{thread_id or ''}"


def _read_raw() -> List[Dict[str, Any]]:
    p = bindings_path()
    try:
        st = p.stat()
    except FileNotFoundError:
        _cache.update(path=str(p), mtime=None, data=[])
        return []
    except OSError as exc:
        logger.warning("profile bindings unreadable at %s: %s", p, exc)
        return list(_cache.get("data") or [])

    if _cache.get("path") == str(p) and _cache.get("mtime") == st.st_mtime:
        return list(_cache["data"])

    try:
        raw = json.loads(p.read_text() or "[]")
        data = raw if isinstance(raw, list) else []
    except (OSError, ValueError) as exc:
        # A corrupt bindings file must not silently un-route every topic:
        # keep serving the last good copy and say so.
        logger.warning("profile bindings at %s are unreadable (%s); "
                       "keeping the last good copy", p, exc)
        return list(_cache.get("data") or [])

    _cache.update(path=str(p), mtime=st.st_mtime, data=data)
    return list(data)


def _write_raw(rows: List[Dict[str, Any]]) -> None:
    p = bindings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a half-written file read by the inbound path would un-route live
    # topics. Same reason the rest of Hermes uses atomic_replace.
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".bindings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(rows, fh, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _cache.update(path=None, mtime=None, data=[])   # force a re-read


def list_bindings() -> List[Dict[str, Any]]:
    return _read_raw()


def get_binding(platform: str, chat_id: Optional[str],
                thread_id: Optional[str]) -> Optional[str]:
    """The profile bound to exactly this scope, or None."""
    k = _key(platform, chat_id, thread_id)
    for row in _read_raw():
        if _key(row.get("platform", ""), row.get("chat_id"), row.get("thread_id")) == k:
            return row.get("profile")
    return None


def bind(platform: str, chat_id: Optional[str], thread_id: Optional[str],
         profile: str) -> None:
    """Bind this scope to ``profile``, replacing any existing binding.

    IDs are stored as strings on purpose. The adapters stamp ``chat_id`` /
    ``thread_id`` as strings, and ``ProfileRoute.matches`` compares with
    ``!=`` — so an int here would never match and the route would silently do
    nothing. That is a real trap in the YAML path; this one cannot hit it.
    """
    rows = [r for r in _read_raw()
            if _key(r.get("platform", ""), r.get("chat_id"), r.get("thread_id"))
            != _key(platform, chat_id, thread_id)]
    rows.append({
        "platform": str(platform),
        "chat_id": str(chat_id) if chat_id is not None else None,
        "thread_id": str(thread_id) if thread_id is not None else None,
        "profile": str(profile),
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    _write_raw(rows)


def unbind(platform: str, chat_id: Optional[str],
           thread_id: Optional[str]) -> bool:
    """Drop this scope's binding. True if one existed."""
    rows = _read_raw()
    keep = [r for r in rows
            if _key(r.get("platform", ""), r.get("chat_id"), r.get("thread_id"))
            != _key(platform, chat_id, thread_id)]
    if len(keep) == len(rows):
        return False
    _write_raw(keep)
    return True


def binding_routes() -> List:
    """Bindings as ``ProfileRoute`` objects — the same type static routes use."""
    from gateway.profile_routing import parse_profile_routes

    return parse_profile_routes([
        {
            "name": f"binding:{r.get('platform')}:{r.get('chat_id')}:{r.get('thread_id')}",
            "platform": r.get("platform", ""),
            "chat_id": r.get("chat_id"),
            "thread_id": r.get("thread_id"),
            "profile": r.get("profile", ""),
        }
        for r in _read_raw()
    ])


def merged_routes(static_routes: Optional[List] = None) -> List:
    """Bindings + static routes, most specific first; bindings win ties.

    ``parse_profile_routes`` sorts by specificity with Python's stable sort, so
    putting bindings first preserves them ahead of an equally specific config
    route while still letting a more specific config route win outright.
    """
    from gateway.profile_routing import parse_profile_routes  # noqa: F401

    routes = binding_routes() + list(static_routes or [])
    routes.sort(key=lambda r: r.specificity, reverse=True)
    return routes
