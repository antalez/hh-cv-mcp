#!/usr/bin/env python3
"""Fail loudly if a committed fixture carries personal data.

Fixtures are recorded from a real, logged-in account and then committed, which
in a published repo means published. record_fixtures.py reduces every response
to a skeleton (allowlist, not denylist) precisely so this cannot happen, but the
scrubber is code and code drifts. This is the check that notices.

It runs offline and needs nothing. Keep it in CI, or at least run it after every
re-record; record_fixtures.py prints a reminder saying so.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Substrings that must never appear. Deliberately concrete: this is the second
# line of defence, and a vague check that passes on everything is not one.
FORBIDDEN = {
    "any email address":      re.compile(r'[\w.+-]+@[\w-]+\.[a-z]{2,}'),
    "russian phone":          re.compile(r'(?:\+7|\b8)\d{10}\b'),
    # Free-text prose. The skeleton keeps only SCREAMING_CASE enums and
    # placeholders, so ANY run of real words means a value escaped scrubbing --
    # this also catches any real name or employer without naming one here.
    "human prose (cyrillic)": re.compile(r'[А-Яа-я]{4,}'),
    "session/cookie value":   re.compile(r'"(?:hhtoken|hhuid|_xsrf)"\s*:\s*"[^"]{8,}"'),
}

# The placeholder the scrubber emits for hash-shaped strings. Seeing it is
# correct; seeing a hash that ISN'T it means a real one got through.
PLACEHOLDER_HASH = re.compile(r'^(?:abcdef0123456789)+')


def test_no_personal_data_in_fixtures():
    files = sorted(FIXTURES.glob("*.json"))
    assert files, f"no fixtures in {FIXTURES}; run record_fixtures.py"
    for f in files:
        assert not f.name.endswith(".raw.json"), \
            f"{f.name} is an unscrubbed capture and must not be committed"
        raw = f.read_text(encoding="utf-8")
        for label, pat in FORBIDDEN.items():
            hits = pat.findall(raw)
            assert not hits, (
                f"{f.name} leaks {label}: {sorted(set(map(str, hits)))[:3]}. "
                f"Do NOT hand-edit the fixture; fix scrub() in record_fixtures.py "
                f"and re-record, or the next recording leaks again.")


def test_hashes_are_placeholders_not_real_ones():
    """Resume hashes are a live URL to a CV. Shape kept, value not."""
    for f in sorted(FIXTURES.glob("*.json")):
        raw = f.read_text(encoding="utf-8")
        for h in set(re.findall(r'\b[0-9a-f]{32,40}\b', raw)):
            assert PLACEHOLDER_HASH.match(h), \
                f"{f.name}: {h[:12]}… is not the scrubber's placeholder, so it is real"


def test_fixture_still_has_useful_shape():
    """A scrubber that deleted everything would pass the checks above and be
    worthless. Make sure something testable survived."""
    for f in sorted(FIXTURES.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        calls = d.get("calls") or []
        assert calls, f"{f.name}: no calls recorded"
        assert any("json" in c for c in calls), f"{f.name}: no JSON bodies to replay"
        for c in calls:
            assert c.get("url", "").startswith("http"), "a call lost its url"
            assert isinstance(c.get("status"), int), "a call lost its status"


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
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
