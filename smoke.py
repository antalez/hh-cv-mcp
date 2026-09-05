#!/usr/bin/env python3
"""Live read-only smoke test of the hh surface. Does the protocol still work?

The offline tests (tests/test_contract.py, tests/test_parsers.py) prove the code
is coherent and still parses hh's KNOWN shapes. Neither can tell you that hh
changed something this morning. Only a live call does, and a live call needs a
session, so this runs where the session already lives rather than in CI.

READS ONLY. It never applies, never messages an employer, never touches a CV.
Every check asserts SHAPE, not content: a field's presence and type, not what it
says, so it stays green as the owner's actual job search changes underneath it.

Exit code 0 if every check passed, 1 otherwise, so cron or any scheduler
can both just look at the status.

    python3 smoke.py            # human output
    python3 smoke.py --json     # machine output, for a caller to relay
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hh_client import HH, Session, SessionExpired  # noqa: E402


class Check:
    def __init__(self, name, note=""):
        self.name, self.note, self.ok, self.detail, self.secs = name, note, False, "", 0.0


def _run(check: Check, fn):
    t0 = time.time()
    try:
        check.detail = fn() or ""
        check.ok = True
    except SessionExpired as e:
        check.detail = f"SESSION DEAD: {e}"
    except AssertionError as e:
        check.detail = f"shape changed: {e}"
    except Exception as e:                            # noqa: BLE001
        check.detail = f"{type(e).__name__}: {e}"
    check.secs = time.time() - t0
    return check


def smoke(verbose: bool = True) -> tuple[int, list[Check]]:
    checks: list[Check] = []
    sess = Session.load()
    if not sess:
        c = Check("session", "no saved session")
        c.detail = "no .hh_session.json; run `python3 hh_client.py session`"
        # Print it: this path is a real failure mode (expired/absent cookie) and
        # a cron log that says nothing at all is the least useful kind.
        if verbose:
            print(f"  [FAIL] {c.name}: {c.detail}\n\n  smoke: FAIL (0/1 passed)")
        return 1, [c]

    hh = HH(session=sess, verbose=False)

    def negotiations():
        # all_pages=False keeps the smoke fast; pagination itself is pinned by
        # the offline fixture tests, this only asks whether hh still answers.
        rows = hh.negotiations(all_pages=False)
        assert isinstance(rows, list), f"negotiations returned {type(rows).__name__}"
        if rows:
            r = rows[0]
            # The join between topicList and vacanciesShort is the fragile part:
            # reading topicList alone gives rows with no company name at all.
            for key in ("state", "url"):
                assert key in r, f"row lost {key!r}"
            assert any(x.get("company") for x in rows), \
                "no row has a company: the vacanciesShort join broke"
        return f"{len(rows)} on page 0"

    def resumes():
        rs = hh.resumes()
        assert isinstance(rs, list) and rs, "no resumes returned"
        live = [r for r in rs if not r.get("error")]
        assert live, "every resume errored"
        h = live[0].get("hash") or ""
        # The identifier trap: writes need the 38-char hash, not the numeric id.
        assert len(h) == 38, f"resume hash is {len(h)} chars, expected 38"
        return f"{len(live)} resumes, hash width ok"

    def one_resume():
        rs = [r for r in hh.resumes() if not r.get("error")]
        r = hh.resume(rs[0]["hash"])
        assert isinstance(r, dict), "resume() did not return a dict"
        for key in ("title", "experience"):
            assert key in r, f"resume lost {key!r}"
        return f"{len(r.get('experience') or [])} experience entries"

    def chats():
        cs = hh.chats()
        assert isinstance(cs, list), "chats() did not return a list"
        return f"{len(cs)} chats"

    def activity():
        a = hh.activity_score()
        assert isinstance(a, (int, float)) or (isinstance(a, dict) and a), \
            f"activity_score returned {type(a).__name__}"
        return f"score={a if not isinstance(a, dict) else a.get('score', a)}"

    def build_version():
        v = hh.static_version()
        assert v and isinstance(v, str), "no build string"
        # If this ever starts hanging, something reintroduced /search/vacancy on
        # the hot path. That killed the whole inbox once.
        return f"build {v}"

    for name, fn, note in [
        ("build-version", build_version, "landing page, not /search/vacancy"),
        ("negotiations", negotiations, "the applications list"),
        ("resumes", resumes, "list + the 38-char hash trap"),
        ("resume-read", one_resume, "one CV's editable content"),
        ("chats", chats, "messenger, incl. employer-initiated"),
        ("activity", activity, "the account gauge"),
    ]:
        c = _run(Check(name, note), fn)
        checks.append(c)
        if verbose:
            print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: "
                  f"{c.detail}  ({c.secs:.1f}s)")

    fails = sum(not c.ok for c in checks)
    if verbose:
        print(f"\n  smoke: {'OK' if not fails else 'FAIL'}  "
              f"({len(checks) - fails}/{len(checks)} passed)")
    return (1 if fails else 0), checks


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    code, checks = smoke(verbose=not args.json)
    if args.json:
        print(json.dumps({"ok": code == 0,
                          "checks": [{"name": c.name, "ok": c.ok,
                                      "detail": c.detail, "secs": round(c.secs, 1)}
                                     for c in checks]}, ensure_ascii=False))
    sys.exit(code)
