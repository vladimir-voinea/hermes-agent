"""Runtime profile bindings — `/profile <name>` binds a chat/topic at runtime.

The static ``gateway.profile_routes`` path already existed; it needs the
thread_id to exist before you can write it and a restart to apply, which is
useless for "open a topic and decide what it is". These cover the runtime path.
"""

from __future__ import annotations

import pytest

from gateway import profile_bindings as pb
from gateway.profile_routing import match_profile_route, parse_profile_routes

CHAT = "-1003918327392"


@pytest.fixture(autouse=True)
def isolated_root(tmp_path, monkeypatch):
    """Never touch the operator's real bindings file."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pb, "_cache", {"path": None, "mtime": None, "data": []})
    return tmp_path


def who(routes, thread, chat=CHAT):
    m = match_profile_route(routes, platform="telegram", chat_id=chat, thread_id=thread)
    return m.profile if m else None


def test_binding_routes_one_topic_and_only_that_topic():
    pb.bind("telegram", CHAT, "1338", "orchestrator")
    r = pb.merged_routes([])
    assert who(r, "1338") == "orchestrator"
    assert who(r, "1510") is None, "binding one topic changed another"


def test_topics_can_hold_different_profiles_at_once():
    """The point of the whole feature: one group, per-topic personas."""
    pb.bind("telegram", CHAT, "1338", "orchestrator")
    pb.bind("telegram", CHAT, "1510", "tech")
    r = pb.merged_routes([])
    assert (who(r, "1338"), who(r, "1510"), who(r, "9999")) == ("orchestrator", "tech", None)


def test_rebinding_replaces_rather_than_duplicates():
    pb.bind("telegram", CHAT, "1338", "tech")
    pb.bind("telegram", CHAT, "1338", "orchestrator")
    assert len(pb.list_bindings()) == 1
    assert who(pb.merged_routes([]), "1338") == "orchestrator"


def test_unbind_returns_the_topic_to_default():
    pb.bind("telegram", CHAT, "1338", "orchestrator")
    pb.bind("telegram", CHAT, "1510", "tech")
    assert pb.unbind("telegram", CHAT, "1338") is True
    r = pb.merged_routes([])
    assert who(r, "1338") is None
    assert who(r, "1510") == "tech", "unbinding one topic dropped another"
    assert pb.unbind("telegram", CHAT, "1338") is False, "second unbind claimed success"


def test_ids_are_stored_as_strings():
    """★ The YAML path's trap: an int id never matches the adapter's string and
    the route silently does nothing. A binding cannot be written that way."""
    pb.bind("telegram", -1003918327392, 1338, "orchestrator")   # ints in
    row = pb.list_bindings()[0]
    assert isinstance(row["chat_id"], str) and isinstance(row["thread_id"], str)
    assert who(pb.merged_routes([]), "1338") == "orchestrator"


def test_binding_beats_an_equally_specific_static_route():
    """Someone typed it into the topic just now; config.yaml is older intent."""
    static = parse_profile_routes([{
        "name": "cfg", "platform": "telegram", "chat_id": CHAT,
        "thread_id": "1510", "profile": "public",
    }])
    assert who(static, "1510") == "public"
    pb.bind("telegram", CHAT, "1510", "tech")
    assert who(pb.merged_routes(static), "1510") == "tech"


def test_specificity_still_rules_over_source():
    """A thread-level static route beats a chat-level binding — precedence is
    about scope first, source only as a tiebreak."""
    pb.bind("telegram", CHAT, None, "reviewer")          # whole chat
    static = parse_profile_routes([{
        "name": "cfg", "platform": "telegram", "chat_id": CHAT,
        "thread_id": "2247", "profile": "public",
    }])
    r = pb.merged_routes(static)
    assert who(r, "2247") == "public", "a broad binding shadowed a specific route"
    assert who(r, "1880") == "reviewer"


def test_a_corrupt_file_keeps_the_last_good_copy(isolated_root):
    """Un-routing every live topic because a file got truncated is not an
    acceptable failure — serve the last good copy and warn."""
    pb.bind("telegram", CHAT, "1338", "orchestrator")
    assert who(pb.merged_routes([]), "1338") == "orchestrator"
    pb.bindings_path().write_text("{ this is not json")
    pb._cache.update(mtime=None)          # force a re-read of the corrupt file
    assert who(pb.merged_routes([]), "1338") == "orchestrator"


def test_missing_file_is_simply_no_bindings():
    assert pb.list_bindings() == []
    assert pb.merged_routes([]) == []


def test_a_binding_is_visible_without_a_restart(isolated_root):
    """The whole reason this exists rather than config.yaml: it applies to the
    next message, not the next process."""
    assert who(pb.merged_routes([]), "1338") is None
    pb.bind("telegram", CHAT, "1338", "orchestrator")
    assert who(pb.merged_routes([]), "1338") == "orchestrator", \
        "a fresh binding needed a restart to be seen"
