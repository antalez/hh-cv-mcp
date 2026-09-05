#!/usr/bin/env python3
"""Record real hh.ru responses once, scrub them, commit them as test fixtures.

WHY: the thing that actually breaks this client is hh changing the shape of what
they return. That is a parser problem, and a parser problem is testable offline
IF you have a real response to parse. Every network call funnels through one
seam, HH._req, so recording is a matter of wrapping it.

WHY SCRUBBED: fixtures are committed, and a published repo is public. A raw
capture carries the owner's name, his resume hashes, chat ids and the actual text
employers wrote to him. `scrub()` below replaces all of it with stable fakes,
and tests/test_fixtures_are_clean.py fails the build if anything slips through.

USAGE (needs a live session, run from the repo root):
    python3 tests/record_fixtures.py            # record + scrub + write
    python3 tests/record_fixtures.py --keep-raw # also keep the raw capture
                                                    # (gitignored) to eyeball

This is a manual, occasional step, NOT part of CI. Re-run it when hh changes
something and you want the fixtures to reflect the new shape.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

MCP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MCP))

from hh_client import HH, Session  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# ------------------------------------------------------------------ scrubbing

# ALLOWLIST, NOT DENYLIST. The first version of this hunted for known-bad
# patterns (his name, 38-char hashes, long digit runs) and leaked anyway: hashes
# survived as dict KEYS, and numeric ids survived because they are JSON numbers,
# not strings. Chasing leaks one pattern at a time over a megabyte of somebody
# else's JSON is the same mistake as a redaction filter on the ledger. It leaks
# by construction.
#
# So nothing survives unless it is explicitly safe. Every leaf value is replaced
# by a placeholder of the SAME TYPE, LENGTH and CHARACTER CLASS, which is what
# the parsers actually depend on: a 38-char hex hash stays a 38-char hex hash,
# so "the hash is 38 chars" is still testable, while the real one is gone.

# Enum-ish values that drive branching in the parsers, and carry nothing
# personal. SCREAMING_CASE only, which is how hh spells its states and types.
SAFE_ENUM = re.compile(r'^[A-Z][A-Z0-9_]{1,30}$')

# Keys are structure, not data, and the parsers navigate by them. Kept as-is
# unless the key IS an identifier (hh maps things by resume hash and vacancy id).
KEY_IS_ID = re.compile(r'^([0-9a-f]{32,40}|\d{6,})$')

MAX_LIST = 3          # three items prove "it is a list of these", a hundred do not


def _fake_like(s: str) -> str:
    """A placeholder with the same length and character class as the original."""
    if re.fullmatch(r'[0-9a-f]+', s):                 # hash-shaped
        return ("abcdef0123456789" * 4)[:len(s)]
    if re.fullmatch(r'\d+', s):                       # numeric id as a string
        return "1" * len(s)
    if re.fullmatch(r'[\d\-:.TZ+ ]+', s) and len(s) >= 8:   # timestamp-shaped
        return "2020-01-01T00:00:00+0300"[:len(s)]
    if s.startswith("http"):
        return "https://example.test/x"
    return "x" * min(len(s), 24)


def scrub(obj, _depth=0):
    """Reduce to a skeleton: keys, types, list shape, and safe enums only."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = _fake_like(k) if KEY_IS_ID.match(str(k)) else k
            out[key] = scrub(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [scrub(v, _depth + 1) for v in obj[:MAX_LIST]]
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, int):
        # Small numbers are counts, flags and page sizes: shape-critical and
        # harmless. Big ones are ids and timestamps.
        return obj if abs(obj) < 1000 else int("1" * len(str(abs(obj))))
    if isinstance(obj, float):
        return 1.0
    if isinstance(obj, str):
        return obj if SAFE_ENUM.match(obj) else _fake_like(obj)
    return obj


def scrub_url(url: str) -> str:
    """URLs keep their route (that is the contract being tested) and lose ids."""
    s = re.sub(r'[0-9a-f]{32,40}', lambda m: _fake_like(m.group(0)), url)
    return re.sub(r'\b\d{6,}\b', lambda m: "1" * len(m.group(0)), s)


# ------------------------------------------------------------------ recording

def record(keep_raw: bool = False) -> int:
    sess = Session.load()
    if not sess:
        print("no saved session; run: python3 hh_client.py session")
        return 1

    captured: list[dict] = []
    hh = HH(session=sess, verbose=False)
    original = hh._req

    def taping(method, url, **kw):
        status, ctype, body = original(method, url, **kw)
        entry = {"method": method, "url": url, "status": status, "content_type": ctype}
        try:
            entry["json"] = json.loads(body.decode("utf-8"))
        except Exception:
            # HTML pages: keep only length. No parser test needs the markup, and
            # a full page is where stray personal data hides.
            entry["html_bytes"] = len(body)
        captured.append(entry)
        return status, ctype, body

    hh._req = taping

    # One call per parser worth pinning. Reads only: nothing here writes.
    steps = [
        ("negotiations", lambda: hh.negotiations(all_pages=False)),
        ("chats",        lambda: hh.chats()),
        ("resumes",      lambda: hh.resumes()),
        ("activity",     lambda: hh.activity_score()),
    ]
    for label, fn in steps:
        try:
            fn()
            print(f"  recorded {label}")
        except Exception as e:                        # noqa: BLE001
            print(f"  {label} failed, skipping: {type(e).__name__}: {e}")

    FIXTURES.mkdir(parents=True, exist_ok=True)
    if keep_raw:
        raw = FIXTURES / "session.raw.json"           # gitignored
        raw.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  raw capture -> {raw} (gitignored)")

    clean = []
    for e in captured:
        c = {"method": e["method"], "url": scrub_url(e["url"]),
             "status": e["status"], "content_type": e["content_type"]}
        if "json" in e:
            c["json"] = scrub(e["json"])
        else:
            c["html_bytes"] = e["html_bytes"]
        clean.append(c)

    out = FIXTURES / "hh_responses.json"
    out.write_text(json.dumps(
        {"recorded_at": time.strftime("%Y-%m-%d"), "calls": clean},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  {len(clean)} calls -> {out}")
    print("  now run: python3 tests/test_fixtures_are_clean.py")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--keep-raw", action="store_true",
                    help="also write the unscrubbed capture (gitignored)")
    sys.exit(record(ap.parse_args().keep_raw))
