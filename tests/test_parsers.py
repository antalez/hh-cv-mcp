#!/usr/bin/env python3
"""Replay recorded hh responses through the real parsers. No network, no session.

WHAT THIS CATCHES: the parsers walking hh's payloads are the part that silently
breaks, and they break in two ways. hh changes the shape (caught by the live
smoke via smoke.py), or WE change the parser and stop handling the
shape hh actually sends. The second is what this catches, on every run, with no
credentials and no traffic to hh.

WHAT IT DOES NOT CATCH, honestly: the fixtures are skeletons. Values were
replaced with same-shape placeholders so nothing personal is committed, and ids
of equal length collapse onto the same placeholder. So a join keyed on an id
still resolves, but resolving to the RIGHT row is not proven here. These are
shape and regression tests, not semantic ones. Correctness of a join is the live
smoke's job, where the data is real.

Run: python3 tests/test_parsers.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MCP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MCP))

import hh_client  # noqa: E402
from hh_client import HH, Session  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hh_responses.json"

# The landing page is recorded as a byte count only: it is a 2MB HTML blob and
# the one thing any parser wants from it is the build string. Synthesise that
# rather than committing two megabytes of somebody's rendered page.
FAKE_LANDING = b'<html><script>{"build":"26.36.4.4"}</script></html>'


def _load():
    d = json.loads(FIXTURES.read_text(encoding="utf-8"))
    by_route, ordered = {}, []
    for c in d["calls"]:
        route = re.sub(r'\?.*$', '', c["url"])
        body = (json.dumps(c["json"], ensure_ascii=False).encode("utf-8")
                if "json" in c else FAKE_LANDING)
        entry = (c.get("status", 200), c.get("content_type", "application/json"), body)
        by_route.setdefault(route, []).append(entry)
        ordered.append((route, entry))
    return by_route, ordered


def _replaying_hh():
    """An HH whose single network seam replays fixtures instead of calling hh.

    Everything funnels through HH._req (19 call sites, and the only raw socket
    opens are inside it), which is exactly why this is three lines rather than a
    mock framework.
    """
    by_route, _ordered = _load()
    used: dict[str, int] = {}
    hh = HH(session=Session({"hhtoken": "x"}, xsrf="x", captured_at=0.0), verbose=False)

    def replay(method, url, **kw):
        route = re.sub(r'\?.*$', '', url)
        if route in by_route:
            i = used.get(route, 0)
            used[route] = i + 1
            return by_route[route][min(i, len(by_route[route]) - 1)]
        if route.rstrip("/") in ("https://hh.ru", "http://hh.ru"):
            return 200, "text/html", FAKE_LANDING
        raise AssertionError(
            f"parser asked for an unrecorded route: {method} {route}. Either the "
            f"code now calls something new (re-record), or it should not be.")

    hh._req = replay
    return hh


def test_fixtures_load():
    by_route, ordered = _load()
    assert ordered, "no recorded calls"
    assert any("negotiations" in r for r, _ in ordered), "no negotiations fixture"


def test_static_version_parses_a_build():
    hh = _replaying_hh()
    v = hh.static_version()
    assert re.fullmatch(r'[\d.]+', v), f"build string looks wrong: {v!r}"


def test_negotiations_parses_without_crashing():
    """The join between topicList and vacanciesShort is the fragile bit: reading
    topicList alone yields rows with no company name, which shipped once."""
    hh = _replaying_hh()
    rows = hh.negotiations(all_pages=False)
    assert isinstance(rows, list), f"got {type(rows).__name__}"
    for r in rows:
        for key in ("state", "url", "vacancy_id"):
            assert key in r, f"row lost {key!r}: {sorted(r)[:8]}"


def test_chats_parses_without_crashing():
    hh = _replaying_hh()
    cs = hh.chats()
    assert isinstance(cs, list), f"got {type(cs).__name__}"
    for c in cs:
        assert "id" in c or "chat_id" in c, f"chat row lost its id: {sorted(c)[:8]}"


def test_activity_score_parses():
    hh = _replaying_hh()
    a = hh.activity_score()
    assert a is not None, "activity_score returned nothing"


def test_parser_never_reaches_the_network():
    """The point of the harness: prove it. A real socket here means a call
    slipped past _req, and the offline suite would start hitting hh."""
    hh = _replaying_hh()
    import urllib.request
    original = urllib.request.urlopen

    def forbidden(*a, **k):
        raise AssertionError("a parser opened a real socket during an offline test")

    urllib.request.urlopen = forbidden
    try:
        hh.negotiations(all_pages=False)
        hh.chats()
    finally:
        urllib.request.urlopen = original


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:                        # noqa: BLE001
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
