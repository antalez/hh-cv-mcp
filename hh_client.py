#!/usr/bin/env python3
"""hh.ru client. No browser, no official API, no token.

Everything hh's own frontend does, done with an HTTP client. Session capture,
vacancy search, resume read and write, applications. Chromium is used exactly
once, to lift a login session, and never again.

WHAT THIS IS BUILT ON (all measured, none of it documented by hh)
-----------------------------------------------------------------
The official API is shut. The applicant API died 2025-12-15 and /vacancies now
403s everyone at the application layer, not the edge, so no IP or VPN trick
opens it. But the website is fully programmable:

  search      GET  /search/vacancy          + 3 headers -> JSON, no auth at all
  vacancy     GET  /vacancy/{id}                        -> HTML, no auth
  similar     GET  /vacancy/{id}/similar_vacancies      -> HTML state blob, session
              (the "Похожие вакансии" block under a posting; JSON Accept 406s and
               logged-out it degrades to a generic feed, so auth is required)
  resume read GET  /resume/{id}                         -> HTML state blob, session
  resume write POST /applicant/resume/edit              -> JSON, session + xsrf
  apply       POST /applicant/vacancy_response/popup    -> multipart, session + xsrf
  sent        GET  /applicant/negotiations              -> JSON, session, PAGES AT 20
  chat        GET  /chat/{chatId}                       -> JSON, session
  edit letter POST chatik.hh.ru/chatik/api/save         -> JSON, session + xsrf

An application is not as final as it looks: the cover letter is the first message
in the negotiation chat, and hh lets you rewrite it in place until the employer
discards you. See edit_message() and set_letter().

Verified quirks that will silently break naive implementations:
  * The 3 search headers are required TOGETHER. Any two give HTML or a 406.
  * X-Static-Version IS validated. A bogus value 406s. It changes every hh
    deploy, so it must be scraped live, never hardcoded.
  * DDoS-Guard serves a JS challenge to cold clients and is IP-dependent. A
    persistent cookie jar clears it; without one the first request returns a
    902-byte challenge page that parses as "no results".
  * fingerprintIteration2 in the resume payload is currently NOT validated
    (bogus and omitted both return 200). It is an anti-automation field they
    are collecting, so treat "ignored today" as temporary.
  * hh returns duplicate vacancies across pages. Dedupe on vacancy_id.
  * The publication field is publicationTime, not publishedAt.

EVERY WRITE IS VERIFIED BY RE-READING. Every failure mode on this platform is
silent: wrong values save with a 200, dropdowns discard without erroring, and
typed text lands in the wrong place. Do not trust a 200.

SCOPE
-----
Operates on ONE account: the session you supply. It is a tool for a person to
run against their own hh account, not a service to run against other people's.
The session is the user's credential and stays theirs.
"""
from __future__ import annotations

import datetime
import hashlib
import html
import http.cookiejar
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
BASE = "https://hh.ru"
# The chat is a separate service. Note the doubled segment: the path is already
# namespaced /chatik/..., and it is served from the chatik host, so hh.ru/chatik/*
# returns a 404 HTML PAGE (not a JSON error), which reads as "no such endpoint".
CHATIK = "https://chatik.hh.ru"
def _default_session_file() -> Path:
    """Where the captured cookie jar lives.

    This module ships inside mcp/ as part of the MCP, but in the owner's tree the
    credential sits at the repo root next to the agent. Look next to this file
    first (a lifted, standalone copy of mcp/), then one level up (this repo),
    and let HH_SESSION override both for anyone deploying it elsewhere.
    """
    env = os.environ.get("HH_SESSION")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    for cand in (here / ".hh_session.json", here.parent / ".hh_session.json"):
        if cand.exists():
            return cand
    return here / ".hh_session.json"


SESSION_FILE = _default_session_file()


class HHError(RuntimeError):
    pass


class SessionExpired(HHError):
    pass


# ====================================================================== session

@dataclass
class Session:
    """An hh login, reduced to what the HTTP layer actually needs."""
    cookies: dict[str, str] = field(default_factory=dict)
    xsrf: str | None = None
    captured_at: float = 0.0

    @property
    def header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def save(self, path: Path = SESSION_FILE) -> None:
        path.write_text(json.dumps({"cookies": self.cookies, "xsrf": self.xsrf,
                                    "captured_at": self.captured_at}))
        try:
            path.chmod(0o600)          # it is a live credential
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path = SESSION_FILE) -> "Session | None":
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        return cls(d.get("cookies", {}), d.get("xsrf"), d.get("captured_at", 0.0))

    # Measured 2026-09-04, by replaying one authenticated read with progressively
    # smaller cookie sets: `hhtoken` ALONE authenticates. The other 35 cookies
    # Chrome hands over are analytics, layout and anti-bot noise. `_xsrf` is not
    # needed to read, but IS needed to write: it rides in the X-XSRFToken header
    # and again inside the body of apply / resume-edit / chat-save.
    REQUIRED_COOKIE = "hhtoken"
    WRITE_COOKIE = "_xsrf"

    @classmethod
    def from_cookie_string(cls, raw: str) -> "Session":
        """Build a session from a pasted cookie string. No browser, no playwright.

        Accepts what a browser gives you when you copy it: a raw `k=v; k=v` list,
        or a whole `Cookie: ...` request header, or just the two values on their
        own lines. Anything unparseable is ignored rather than fatal, because the
        common case is pasting more than is needed.

        The minimum is `hhtoken`. Add `_xsrf` if you intend to write.
        """
        raw = (raw or "").strip()
        if raw.lower().startswith("cookie:"):
            raw = raw.split(":", 1)[1]
        jar: dict[str, str] = {}
        for part in re.split(r'[;\n\r]+', raw):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip().strip('"')
            if k and v:
                jar[k] = v
        if cls.REQUIRED_COOKIE not in jar:
            raise HHError(
                f"no {cls.REQUIRED_COOKIE} in what you pasted (found: "
                f"{', '.join(sorted(jar)) or 'nothing'}).\n"
                f"In a browser logged into hh.ru: DevTools -> Application -> "
                f"Cookies -> https://hh.ru, copy the value of {cls.REQUIRED_COOKIE} "
                f"(and {cls.WRITE_COOKIE} if you want to write).")
        return cls(jar, jar.get(cls.WRITE_COOKIE), time.time())

    def can_write(self) -> bool:
        """Reads need hhtoken; writes also need the xsrf token."""
        return bool(self.xsrf)

    @classmethod
    def from_chrome(cls, cdp: str = "http://127.0.0.1:9222") -> "Session":
        """Lift a session from a Chrome you already logged into.

        This is the only place a browser appears. hh's login has SMS and captcha
        paths that are not worth automating and should not be: a human logs in
        once, this copies the resulting cookies, and everything after is HTTP.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise HHError("playwright needed for session capture: pip install playwright") from e
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(cdp)
            ctx = browser.contexts[0]
            jar = {c["name"]: c["value"] for c in ctx.cookies() if "hh.ru" in c["domain"]}
            if "_xsrf" not in jar:
                raise HHError("no _xsrf cookie: is this Chrome logged into hh.ru?")
            return cls(jar, jar.get("_xsrf"), time.time())
        finally:
            pw.stop()

    @classmethod
    def acquire(cls, cdp: str = "http://127.0.0.1:9222") -> "Session":
        """Cached session, re-captured from Chrome when missing or stale."""
        s = cls.load()
        if s and s.cookies:
            return s
        s = cls.from_chrome(cdp)
        s.save()
        return s


def _salary(v):
    """Normalise a salary for hh: a number, or {"amount", "currency"}.

    Default currency comes from HH_CURRENCY (RUR if unset) rather than being
    hardcoded, because hh.ru also serves KZ, BY, AZ and UZ. Forcing RUR silently
    rewrote a non-Russian user's live CV to the wrong currency.
    """
    if isinstance(v, dict):
        amount = v.get("amount")
        cur = (v.get("currency") or os.environ.get("HH_CURRENCY") or "RUR").upper()
    else:
        amount, cur = v, (os.environ.get("HH_CURRENCY") or "RUR").upper()
    return {"amount": int(amount), "currency": cur}


# ======================================================================= client

class HH:
    def __init__(self, session: Session | None = None, pause: float = 1.2,
                 verbose: bool = True, refuse_dashes: bool | None = None):
        # An em dash is ordinary Russian punctuation. Refusing it is one
        # person's house style, and baking it into the client meant a stranger's
        # perfectly normal CV edit was rejected by a rule they never set. Style
        # belongs to the caller: opt in with HH_REFUSE_DASHES=1, or pass the
        # flag. This repo's agent sets it, because for its owner it is a rule.
        self.refuse_dashes = (os.environ.get("HH_REFUSE_DASHES", "") not in ("", "0", "false")
                              if refuse_dashes is None else refuse_dashes)
        self.session = session
        self.pause = pause
        self.verbose = verbose
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._version: str | None = None
        self._version_at = 0.0

    # ---------------------------------------------------------------- plumbing

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    def _req(self, method: str, url: str, *, headers=None, data=None,
             auth=False, timeout=30) -> tuple[int, str, bytes]:
        h = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9", **(headers or {})}
        if auth:
            if not self.session:
                raise HHError("this call needs a session; use HH(session=Session.acquire())")
            h["Cookie"] = self.session.header
            if self.session.xsrf:
                h["X-XSRFToken"] = self.session.xsrf
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        for attempt in range(4):
            try:
                with self._opener.open(req, timeout=timeout) as r:
                    body = r.read()
                    # DDoS-Guard challenge: cookies are now set, one retry clears it
                    if b"<title>DDoS-Guard" in body[:400]:
                        self._log("    ddos-guard challenge, retrying with cookies")
                        time.sleep(1.5)
                        with self._opener.open(req, timeout=timeout) as r2:
                            return r2.status, r2.headers.get("content-type", ""), r2.read()
                    return r.status, r.headers.get("content-type", ""), body
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):
                    wait = int(e.headers.get("Retry-After") or 5 * (attempt + 1))
                    self._log(f"    {e.code}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                if e.code == 401 and auth:
                    raise SessionExpired(
                        f"HTTP 401 on an authenticated call. Session is dead: "
                        "capture a fresh cookie (hh_client.py session).") from e
                if e.code == 403 and auth:
                    # NOT the same thing as 401, and conflating them sent us
                    # chasing a dead session that was fine: hh answers 403 for a
                    # route this ACCOUNT TYPE may not touch (employer-only shards
                    # return it to a perfectly valid applicant session). Only 401
                    # means the cookie is spent.
                    raise HHError(
                        f"HTTP 403 on {url.split('?')[0]}. The session is valid but "
                        f"not allowed here -- usually an employer-only route, or one "
                        f"this account lacks the service for. Check with whoami before "
                        f"assuming the session expired.") from e
                return e.code, "", e.read()
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        return 0, "", b""

    def static_version(self, force=False) -> str:
        """hh's frontend build, required by the JSON search route and validated."""
        if self._version and not force and time.time() - self._version_at < 1800:
            return self._version
        # Source the build from the landing page, NOT /search/vacancy: hh throttles
        # the search endpoint to a hang (TCP connect timeout from Mac and box alike,
        # 2026-08-21), and it used to be the first call every negotiations read made,
        # so the whole inbox died before reading a single application. The landing
        # page carries the identical "build" string and answers in ~2s. auth=True so
        # the session cookie rides along and we get the logged-in markup.
        _, _, body = self._req("GET", f"{BASE}/", auth=True)
        text = body.decode("utf-8", "replace")
        # The build string has no fixed part count: it was 26.33.2.6 (four parts)
        # and became 26.33.3 (three) within one afternoon, which broke a regex that
        # demanded exactly four. Match three-or-more dotted numbers so a routine hh
        # deploy that drops or adds a component does not read as lost access.
        ver = r'(\d+(?:\.\d+){2,})'
        for pat in (rf'"build"\s*:\s*"{ver}"',
                    rf'sentry-release=xhh%40{ver}',
                    rf'sentry-release=xhh@{ver}'):
            m = re.search(pat, text)
            if m:
                self._version, self._version_at = m.group(1), time.time()
                self._log(f"  hh build {self._version}")
                return self._version
        raise HHError("static version not found; hh markup changed")

    @staticmethod
    def _state_blob(text: str, *, merge_all: bool = False) -> dict | None:
        """The page's embedded state.

        A page can carry MORE THAN ONE state template, which cost us a capability
        before it was noticed: hh's AI resume advice is server-rendered into a
        SIBLING template keyed by CLASS (`<template class="ResumeProfileFront-
        InitialState">`), while this only ever matched `id="HH-Lux-InitialState"`.
        Reading the shell and concluding "the data is not on this page" was wrong;
        the data was in the template next to it.

        Default stays the shell alone so existing callers are unaffected.
        merge_all=True folds in every other state template it can find, later
        ones winning, which is what you want when hunting for a field.
        """
        blobs: list[dict] = []
        for m in re.finditer(r'<template[^>]*>(.*?)</template>', text, re.S):
            tag = text[m.start():m.start(1)]
            is_shell = 'id="HH-Lux-InitialState"' in tag
            if not (is_shell or merge_all):
                continue
            if not is_shell and "InitialState" not in tag:
                continue
            try:
                d = json.loads(html.unescape(m.group(1)).strip())
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                blobs.insert(0, d) if is_shell else blobs.append(d)
        if not blobs:
            return None
        out: dict = {}
        for d in blobs:
            out.update(d)
        return out

    # ------------------------------------------------------------------ search

    _AREAS: dict | None = None

    def areas(self) -> dict[str, str]:
        """hh's whole geography, name -> area id, flattened and cached.

        Nine countries, not just Russia: hh.ru also serves KZ, BY, AZ, UZ and
        others, and `search` takes an `area` id it gives no way to discover.
        The tree is ~1MB, so it is fetched once per process. Keys are
        lowercased; the FIRST occurrence of a name wins, which keeps the
        country/region level (Москва) ahead of a same-named district.
        """
        if self._AREAS is not None:
            return self._AREAS
        _, _, body = self._req(
            "GET", f"{BASE}/shards/regions_tree", auth=True,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
                     "X-Static-Version": self.static_version()})
        flat: dict[str, str] = {}

        def walk(node):
            if isinstance(node, list):
                for x in node:
                    walk(x)
            elif isinstance(node, dict):
                name, aid = node.get("text"), node.get("id")
                if name and aid and str(name).lower() not in flat:
                    flat[str(name).lower()] = str(aid)
                walk(node.get("items"))

        try:
            walk(json.loads(body.decode("utf-8", "replace")).get("items"))
        except ValueError:
            return {}
        type(self)._AREAS = flat
        return flat

    def resolve_area(self, where: str) -> str | None:
        """A city or region NAME to hh's area id. Digits pass straight through."""
        w = str(where).strip()
        if w.isdigit():
            return w
        areas = self.areas()
        if w.lower() in areas:
            return areas[w.lower()]
        # tolerate a partial name ("Санкт-Петер"), preferring the shortest match
        hits = sorted((n for n in areas if w.lower() in n), key=len)
        return areas[hits[0]] if hits else None

    def search(self, text: str, *, pages: int = 10, per_page: int = 100,
               dedupe: bool = True, **filters) -> list[dict]:
        """Vacancy search. No auth. JSON route with an HTML-blob fallback.

        `area` may be an hh area id or a place NAME, which resolve_area maps.
        """
        if filters.get("area") and not str(filters["area"]).isdigit():
            resolved = self.resolve_area(str(filters["area"]))
            if not resolved:
                raise HHError(f"unknown place {filters['area']!r}: no matching hh area")
            filters["area"] = resolved
        out, seen, total = [], set(), None
        for page in range(pages):
            params = {"text": text, "page": page, "items_on_page": per_page, **filters}
            url = f"{BASE}/search/vacancy?" + urllib.parse.urlencode(params, doseq=True)
            payload = None
            try:
                st, ct, body = self._req("GET", url, headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                    "X-Static-Version": self.static_version()})
                if st == 200 and "json" in ct:
                    payload = json.loads(body.decode("utf-8", "replace"))
            except Exception as e:
                self._log(f"    json route failed ({e}), falling back to HTML")
            if payload is None:
                st, _, body = self._req("GET", url)
                payload = self._state_blob(body.decode("utf-8", "replace"))
                if payload is None:
                    self._log(f"    page {page}: both routes failed, stopping")
                    break
            vr = payload.get("vacancySearchResult") or {}
            total = vr.get("totalResults", total)
            items = vr.get("vacancies") or []
            if not items:
                break
            for v in items:
                vid = str(v.get("vacancyId") or "")
                if dedupe and vid in seen:
                    continue
                seen.add(vid)
                out.append(self._vacancy_row(v))
            self._log(f"    page {page}: {len(items)} items"
                      + (f" (total {total})" if page == 0 and total else ""))
            if total and len(seen) >= total:
                break
            time.sleep(self.pause)
        return out

    def similar_vacancies(self, vacancy_id: str, *, limit: int = 20) -> dict:
        """hh's "Похожие вакансии" block: the recommended opportunities shown
        below a posting, ranked by similarity to that vacancy.

        Served as HTML only (a JSON Accept header 406s), and it needs auth: logged
        out, hh drops the similarity ranking and returns a generic feed. The blob's
        vacancySearchResult.totalResults is the global pool, not the block size, so
        the ranked head is what matters; `limit` caps it.
        """
        st, _, body = self._req("GET",
                                f"{BASE}/vacancy/{vacancy_id}/similar_vacancies",
                                auth=True)
        if st != 200:
            raise HHError(f"similar_vacancies HTTP {st}")
        blob = self._state_blob(body.decode("utf-8", "replace")) or {}
        vr = blob.get("vacancySearchResult") or {}
        rows = [self._vacancy_row(v) for v in (vr.get("vacancies") or [])[:limit]]
        return {"source_vacancy": vacancy_id,
                "type": blob.get("relatedVacanciesType"),
                "count": len(rows), "vacancies": rows}

    @staticmethod
    def _vacancy_row(v: dict) -> dict:
        comp = v.get("compensation") or {}
        pub = v.get("publicationTime") or {}          # NOT publishedAt
        vid = str(v.get("vacancyId") or "")
        company = v.get("company") or {}
        # workFormats shape: [{"workFormatsElement": ["ON_SITE", "HYBRID"]}]
        # (values: REMOTE / HYBRID / ON_SITE)
        fmts: list = []
        for f in (v.get("workFormats") or []):
            if isinstance(f, dict):
                el = f.get("workFormatsElement")
                if isinstance(el, list):
                    fmts.extend(el)
                elif f.get("id"):
                    fmts.append(f.get("id"))
        return {"id": vid, "name": v.get("name"),
                "company": company.get("name"),
                "employer_id": company.get("id"),
                "area": (v.get("area") or {}).get("name"),
                "work_format": [f for f in fmts if f],
                # responsesCount is applicants to THIS role; totalResponsesCount is
                # the employer's total across all postings (much bigger, misleading
                # as competition). Show the per-role number.
                "responses": v.get("responsesCount"),
                "employer_responses": v.get("totalResponsesCount"),
                "salary_from": comp.get("from"), "salary_to": comp.get("to"),
                "currency": comp.get("currencyCode"),
                # THE PIECE RATE. from/to are normalised to a month; perMode* is what
                # the employer actually wrote, in the unit `pay_mode` names. A SHIFT
                # role showing 135000-257148 is really 9000-12000 per shift.
                "pay_mode": comp.get("mode"), "pay_frequency": comp.get("frequency"),
                "per_unit_from": comp.get("perModeFrom"), "per_unit_to": comp.get("perModeTo"),
                "published": pub.get("$") if isinstance(pub, dict) else pub,
                "url": f"{BASE}/vacancy/{vid}" if vid else None}

    def _vacancy_page(self, vacancy_id: str) -> tuple[dict, str]:
        """Shared fetch behind vacancy() and contact_info() -- both parse the same
        GET /vacancy/{id} page (vacancyView in the embedded state blob), so fetch
        it once. Cached per vacancy_id for the life of this instance: a workflow
        that wants both a vacancy's detail and its contact info was paying for
        the identical page twice.
        """
        if not hasattr(self, "_vacancy_cache"):
            self._vacancy_cache = {}
        if vacancy_id not in self._vacancy_cache:
            _, _, body = self._req("GET", f"{BASE}/vacancy/{vacancy_id}")
            text = body.decode("utf-8", "replace")
            self._vacancy_cache[vacancy_id] = (self._state_blob(text) or {}, text)
        return self._vacancy_cache[vacancy_id]

    def vacancy(self, vacancy_id: str) -> dict:
        """Full vacancy detail, including description text. No auth."""
        blob, text = self._vacancy_page(vacancy_id)
        def grab(qa):
            m = re.search(rf'data-qa="{qa}"[^>]*>(.*?)</', text, re.S)
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else None
        vv = (blob.get("vacancyView") or {})
        desc = vv.get("description") or ""
        comp = vv.get("compensation") or {}
        return {"id": vacancy_id, "name": vv.get("name") or grab("vacancy-title"),
                "company": ((vv.get("company") or {}).get("name")),
                "experience": (vv.get("experience") or {}).get("name"),
                "salary_from": comp.get("from"), "salary_to": comp.get("to"),
                "currency": comp.get("currencyCode") or comp.get("currency"),
                "gross": comp.get("gross"),
                "skills": [s.get("name") for s in (vv.get("keySkills") or {}).get("keySkill", [])
                           if isinstance(s, dict)] or None,
                "description": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc)).strip(),
                "url": f"{BASE}/vacancy/{vacancy_id}"}

    def contact_info(self, vacancy_id: str) -> dict:
        """Recruiter's name and phone for a posting that publishes them.

        REWRITTEN 2026-09-04, and the reason matters. This used to read
        `vacancyView.contactInfo` out of the vacancy page blob. hh has since
        emptied that: the page now ships `contactInfo: {"contactsHidden": true}`
        and nothing else, so the old implementation returned None for every
        posting and looked like "corporates usually omit contacts". It was not
        omission, it was a move. Measured on 10 postings with `@showContact`
        true: the page blob gave nothing on all 10, this shard gave a name and a
        phone on all 10.

        The data now lives at /shards/vacancy/contact_info, which is applicant-
        only (401 anonymous), free (no quota, cart or paywall anywhere in hh's
        own contacts component) and still honours hh's visibility flag: where
        `@showContact` is false it returns no phones. It is not a way around the
        UI, it is the same call the UI makes.

        `virtual_phone_state` is worth keeping rather than flattening:
          NONE     -- a real direct number
          CREATED  -- hh minted a call-tracking proxy for this posting
          PENDING  -- a proxy is being provisioned
        """
        _, _, body = self._req(
            "GET", f"{BASE}/shards/vacancy/contact_info?id={vacancy_id}",
            auth=True,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
                     "X-Static-Version": self.static_version()})
        try:
            d = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            d = {}
        phones = []
        for p in (d.get("phones") or []):
            digits = " ".join(str(p.get(k) or "") for k in ("country", "city", "number")).strip()
            if digits:
                phones.append({"number": digits,
                               "comment": p.get("comment"),
                               "virtual_phone_state": p.get("virtualPhoneState")})
        return {"id": vacancy_id,
                "fio": d.get("fio"),
                "email": d.get("email"),
                "phone": phones[0]["number"] if phones else None,
                "phones": phones,
                # kept so callers that predate the rewrite keep working
                "call_tracking": (phones[0]["virtual_phone_state"] != "NONE"
                                  if phones else None)}

    def view(self, vacancy_id: str) -> dict:
        """Register an authenticated view of a vacancy: the +2% activity action.

        A GET of the vacancy page while logged in is what hh counts as a
        "просмотр вакансии" (verified: 5 views moved the applicant activity gauge
        48% -> 58%). Used by the activity tick to keep the resume high in recruiter
        search. No write, no employer contact, so it stays outside the approval gate.
        """
        st, _, _ = self._req("GET", f"{BASE}/vacancy/{vacancy_id}", auth=True)
        return {"id": str(vacancy_id), "status": st, "viewed": st == 200}

    def _header_resumes(self) -> dict:
        """The shard behind hh's own header CV widget: titles, attributes and the
        7-day funnel in ~17KB. Cheapest source for anything about CVs that is not
        the CV body itself."""
        _, _, body = self._req(
            "GET", f"{BASE}/shards/applicant/header_resumes", auth=True,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
                     "X-Static-Version": self.static_version()})
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return {}

    def resume_attributes(self) -> dict:
        """Every CV's `_attributes` in ONE 2.6KB call, keyed by hash.

        Found 2026-09-04 in hh's shard layer, which is not linked from any page
        the toolkit was reading. It carries id, hash, canTouch, nextTouchAt,
        updated, isSearchable, renewal and the rest -- everything except the
        title and the CV body.

        This matters because the obvious way to get those, and what this client
        did until now, is to fetch each resume PAGE: ~1.1MB of rendered HTML per
        CV, 3.3MB and ~20 seconds for three. This is 2,673 bytes in 0.6s. Use it
        for anything that needs attributes rather than CV content.
        """
        _, _, body = self._req(
            "GET", f"{BASE}/shards/applicant/resumes", auth=True,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json",
                     "X-Static-Version": self.static_version()})
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return {}
        out = {}
        for r in data.get("resumes") or []:
            a = r.get("_attributes") or {}
            h = a.get("hash")
            if h:
                out[h] = a
        return out

    def _profile_state(self) -> dict:
        """The applicant profile payload. Several tools read different slices of
        it, so it is fetched in one place. ~400KB, so do not call it in a loop."""
        _, _, body = self._req(
            "GET", f"{BASE}/applicant/profile/me", auth=True,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json",
                     "X-Static-Version": self.static_version()})
        text = body.decode("utf-8", "replace")
        try:
            return json.loads(text) if text.lstrip().startswith("{") else (self._state_blob(text) or {})
        except ValueError:
            return self._state_blob(text) or {}

    def resume_stats(self) -> list[dict]:
        """Per-CV funnel: search impressions, employer opens, invitations.

        hh returns this on the profile payload and it is the only feedback loop
        on whether a CV actually works, which makes it the number to read before
        deciding which CV to attach to an application or which one to rewrite.

        Two traps. The counts are keyed by the NUMERIC resume id, not the 38-char
        hash every write needs, so this joins them via each resume's page. And
        `views` is not a subset of `searchShows`: an employer opening your CV
        from an application you sent counts as a view with no impression behind
        it, which is why a CV can show an open rate above 100%.
        """
        # ONE 17KB call. `header_resumes` carries titles, hashes, numeric ids AND
        # the statistics together. The obvious sources are far worse: a resume
        # PAGE per CV is 1.1MB of HTML (3.3MB for three), and the profile payload
        # is ~400KB. Same data, 20x smaller, and it is the shard the site's own
        # header uses, so it stays warm.
        prof = self._header_resumes()
        stats = ((prof.get("statistics") or {}).get("resumes") or {})
        out = []
        for entry in (prof.get("resumes") or []):
            a = entry.get("_attributes") or {}
            title = entry.get("title")
            if isinstance(title, list):
                title = (title[0] or {}).get("string") if title and isinstance(title[0], dict) else title[0]
            r = {"title": title, "hash": a.get("hash")}
            numeric = str(a.get("id") or "") or None
            s = (stats.get(numeric) or {})
            st = s.get("statistics") or {}
            shows = (st.get("searchShows") or {}).get("count")
            views = (st.get("views") or {}).get("count")
            out.append({
                "title": r["title"], "hash": r["hash"], "resume_id": numeric,
                "period_days": st.get("periodDays"),
                "search_shows": shows,
                "views": views,
                "views_new": (st.get("views") or {}).get("countNew"),
                "invitations": (st.get("invitations") or {}).get("count"),
                "open_rate": round(100 * views / shows, 1) if shows and views is not None else None,
                "hh_recommendation": s.get("recommendation"),
            })
        return out

    def favorites(self, *, all_pages: bool = True) -> list[dict]:
        """Vacancies saved with hh's star ("Избранное").

        PROTOCOL TRAP, the reverse of everywhere else: this page 406s if you send
        X-Requested-With. It is served as HTML only, and the data lives in the
        page's state blob. Sending the XHR headers that every other JSON read
        here needs is exactly what breaks it.

        PAGING DOES NOT WORK HERE, which is worth knowing because everything else
        on this site pages at 20 and the reflex is to loop. Verified 2026-09-04:
        `?page=0..3` all return the IDENTICAL 20 rows. Looping produced 60 rows of
        which 20 were unique. So this returns the one page hh serves, and reports
        hh's own `total` alongside it rather than pretending the list is complete.
        If `total` exceeds what you get back, the rest is not reachable this way.
        """
        _, _, body = self._req("GET", f"{BASE}/applicant/favorite_vacancies", auth=True)
        blob = self._state_blob(body.decode("utf-8", "replace")) or {}
        fav = blob.get("favoritesVacancies") or {}
        out, seen = [], set()
        for v in (fav.get("vacancies") or []):
            vid = str(v.get("vacancyId") or "")
            if vid in seen:
                continue
            seen.add(vid)
            comp = v.get("company") or {}
            sal = v.get("compensation") or {}
            out.append({"vacancy_id": vid,
                        "name": v.get("name"),
                        "company": comp.get("visibleName") or comp.get("name"),
                        "salary_from": sal.get("from"), "salary_to": sal.get("to"),
                        "currency": sal.get("currencyCode"),
                        "archived": bool(v.get("@archived")),
                        "hh_total": fav.get("totalCount"),
                        "url": f"{BASE}/vacancy/{vid}"})
        return out

    def thread_read_state(self, *, live_only: bool = True) -> list[dict]:
        """Has the employer actually READ your last message in each thread?

        chat_data carries `lastViewedByOpponentMessageId`. Compare it against the
        id of your own most recent message and you learn something the inbox
        cannot show: silence after being read is a decision, silence before being
        read is a queue. They deserve opposite follow-ups, and until now both
        looked identical.

        Costs one call per thread, so it is a deliberate read, not something to
        put on a hot path.
        """
        out = []
        negs = [n for n in self.negotiations()
                if not live_only or (n.get("state") or "") != "DISCARD"]
        for n in negs:
            cid = n.get("chat_id")
            if not cid:
                continue
            try:
                _, _, body = self._req(
                    f"GET", f"{CHATIK}/chatik/api/chat_data?chatId={cid}", auth=True,
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Accept": "application/json",
                             "X-Static-Version": self.static_version()})
                ch = json.loads(body.decode("utf-8", "replace")).get("chat") or {}
            except Exception:                          # noqa: BLE001
                continue
            msgs = ((ch.get("messages") or {}).get("items")) or []
            me = str(ch.get("currentParticipantId"))
            mine = [int(m.get("id") or 0) for m in msgs if str(m.get("participantId")) == me]
            seen = int(ch.get("lastViewedByOpponentMessageId") or 0)
            out.append({
                "chat_id": cid, "vacancy_id": n.get("vacancy_id"),
                "company": n.get("company"), "vacancy": n.get("name"),
                "state": n.get("state"),
                "my_last_message_id": max(mine) if mine else None,
                "employer_read": (seen >= max(mine)) if mine else None,
                "last_activity": ch.get("lastActivityTime"),
            })
            time.sleep(0.5)
        return out

    def resume_advice(self, resume_id: str, *, start: bool = False) -> dict:
        """hh's OWN AI critique of one CV.

        Two-step by design: a task must be created before there is anything to
        read (`create_task`), and the result is NOT returned by any of the four
        ai_recommendations routes. It is server-rendered into
        /profile/resume/<hash>/advice, inside a SIBLING state template keyed by
        class rather than the usual `id="HH-Lux-InitialState"` -- which is why
        this looked empty until `_state_blob(merge_all=True)` existed.

        start=True creates the task (a POST) and returns immediately with
        status PROCESSING; call again without it to read the result. Left opt-in
        so a read never silently starts work on hh's side.
        """
        if start:
            st, _, body = self._req(
                "POST", f"{BASE}/shards/resume/ai_recommendations/create_task",
                auth=True, data=json.dumps({"resumeHash": resume_id}).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "X-Requested-With": "XMLHttpRequest",
                         "X-Static-Version": self.static_version(),
                         "Referer": f"{BASE}/resume/{resume_id}", "Origin": BASE})
            try:
                return {"started": True, **json.loads(body.decode("utf-8", "replace"))}
            except ValueError:
                return {"started": st == 200, "status": f"HTTP {st}"}

        _, _, body = self._req("GET", f"{BASE}/profile/resume/{resume_id}/advice", auth=True)
        blob = self._state_blob(body.decode("utf-8", "replace"), merge_all=True) or {}
        rec = (((blob.get("resumeAiRecommendations") or {}).get("recommendations") or {})
               .get(resume_id) or {})
        out = []
        for bucket, items in (rec.get("advices") or {}).items():
            for it in items or []:
                out.append({"section": bucket, "advice": it.get("advice"),
                            "advice_id": it.get("advice_id"),
                            "ref_item_id": it.get("ref_item_id")})
        return {"started": False, "resume": resume_id, "advices": out,
                "hint": ("empty means no task has been run for this CV; call with "
                         "start=True, wait a few seconds, then read again")}

    def resume_views(self, resume_id: str) -> list[dict]:
        """WHICH EMPLOYERS opened this CV, by name and date.

        Found 2026-09-04 by mining hh's own JS bundle for its route table; it is
        not linked from anywhere the toolkit was looking. Arguably the most
        useful thing hh gives an applicant: `resume_stats` says four employers
        opened a CV, this says which four and when, so a follow-up can be aimed
        at a company that has already read you.

        The query parameter is `resumeHash`, NOT `resume`; passing `resume`
        returns HTTP 400 with no explanation. The payload nests
        years -> days -> companies, which is flattened here into one row per
        (company, view).
        """
        _, _, body = self._req(
            "GET", f"{BASE}/applicant/resumeview/history?resumeHash={resume_id}", auth=True)
        blob = self._state_blob(body.decode("utf-8", "replace")) or {}
        hist = (blob.get("applicantResumeViewHistory") or {}).get("historyViews") or {}
        out = []
        for year in hist.get("years") or []:
            y = year.get("year")
            for day in year.get("days") or []:
                for comp in day.get("companies") or []:
                    for ts in (comp.get("views") or [None]):
                        out.append({
                            "company": comp.get("name"),
                            "employer_id": str(comp.get("id") or ""),
                            "date": f"{y}-{day.get('month'):02d}-{day.get('day'):02d}"
                                    if day.get("month") and day.get("day") else None,
                            "at": (datetime.datetime.fromtimestamp(ts / 1000).isoformat(" ", "minutes")
                                   if isinstance(ts, (int, float)) else None),
                            # hh marks whether the employer opened it or only saw
                            # it in a list. False means an impression, not a read.
                            "opened": bool(comp.get("viewed")),
                        })
        out.sort(key=lambda r: r.get("at") or "", reverse=True)
        return out

    def suitable_vacancies(self) -> list[dict]:
        """hh's own matching: vacancies it considers suited to your CVs.

        Distinct from `recommended()`, which is the landing-page feed. This is
        the dedicated matcher behind /applicant/resumes/suitable_vacancies and
        reports how many it found in total, which is a useful reality check on
        how large the addressable market actually is.
        """
        _, _, body = self._req(
            "GET", f"{BASE}/applicant/resumes/suitable_vacancies", auth=True)
        blob = self._state_blob(body.decode("utf-8", "replace")) or {}
        rel = blob.get("relatedVacancies") or {}
        out = []
        for v in (rel.get("vacancies") or []):
            comp = v.get("company") or {}
            sal = v.get("compensation") or {}
            out.append({"vacancy_id": str(v.get("vacancyId") or ""),
                        "name": v.get("name"),
                        "company": comp.get("visibleName") or comp.get("name"),
                        "salary_from": sal.get("from"), "salary_to": sal.get("to"),
                        "currency": sal.get("currencyCode"),
                        "remote": v.get("@workSchedule") == "remote",
                        "total_found": rel.get("resultsFound"),
                        "url": f"{BASE}/vacancy/{v.get('vacancyId')}"})
        return out

    def whoami(self) -> dict:
        """Which hh account this session actually belongs to.

        The first question anyone asks of a credential, and the fastest way to
        catch the bad case: a session file copied from somewhere else, or a stale
        one that still authenticates as an account you no longer meant to use.
        Reads the profile page state rather than any API, like everything here.
        """
        _, _, body = self._req(
            "GET", f"{BASE}/applicant/profile/me", auth=True,
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json",
                     "X-Static-Version": self.static_version()})
        text = body.decode("utf-8", "replace")
        try:
            blob = json.loads(text) if text.lstrip().startswith("{") else (self._state_blob(text) or {})
        except ValueError:
            blob = self._state_blob(text) or {}
        acc = blob.get("account") or blob.get("hhidAccount") or {}
        resumes = blob.get("applicantResumes")
        name = " ".join(x for x in (acc.get("firstName"), acc.get("lastName")) if x)
        return {"name": name or None,
                "email": acc.get("email"),
                "hhid": blob.get("hhid"),
                "user_id": blob.get("userId"),
                "user_type": blob.get("userType"),
                "resume_count": len(resumes) if isinstance(resumes, list) else None,
                "captured_at": self.session.captured_at if self.session else None,
                "can_write": bool(self.session and self.session.xsrf)}

    def activity_score(self) -> dict:
        """The account-level applicant activity gauge ('Ваша активность' on hh.ru/).

        Read from `applicantActivity.userActivityScore` on the main page. hh decays
        it when idle ("проценты сгорают") and pays +2% per vacancy view, +8% per
        apply or call; hh's own advice is to keep it >= 80%. This is the single
        account number the owner sees, NOT the per-CV completeness in resume().
        """
        _, _, body = self._req("GET", f"{BASE}/", auth=True)
        blob = self._state_blob(body.decode("utf-8", "replace")) or {}
        a = blob.get("applicantActivity") or {}
        return {"score": a.get("userActivityScore"),
                "change": a.get("userActivityScoreChange")}

    def recommended(self) -> list[dict]:
        """hh's personalized vacancy recommendations, the feed on the applicant
        landing page (`recommendedVacancies` in the `/` state blob). Based on the
        resume + behaviour, so high signal. hh serves ~6 at a time; each row is
        marked with whether you've already applied and whether the employer can DM
        you (`inboxPossibility`)."""
        _, _, body = self._req("GET", f"{BASE}/", auth=True)
        blob = self._state_blob(body.decode("utf-8", "replace")) or {}
        rec = blob.get("recommendedVacancies") or {}
        applied = self.applied_vacancy_ids()
        out = []
        for v in (rec.get("vacancies") or []):
            row = self._vacancy_row(v)
            row["inbox"] = bool(v.get("inboxPossibility"))
            row["applied"] = row["id"] in applied
            out.append(row)
        return out

    # ---------------------------------------------------------------- employer

    # Names that claim a state body. Used only to label an account for a human,
    # never to score it: see _employer_flags for why hh metadata cannot verify
    # whether the claim is true.
    _STATE_NAME = re.compile(
        r"\b(ФКУ|ФГУП|ФГБУ|ФГБОУ|ГБУ|ГАУ|МБУ|МАУ|ГУП|МУП|ФГКУ|ФКП|"
        r"Министерство|Управление|Департамент|Администрация|Комитет|"
        r"Федеральн\w+|Государственн\w+|УФС|МВД|ФСБ|ФСС|ФНС|ФССП|ОУПДС)\b",
        re.I)

    def employer_id_of(self, vacancy_id: str) -> int | None:
        """The employer behind a posting. hh puts it on the vacancy blob, not the page."""
        blob = self.json_page_anon(f"/vacancy/{vacancy_id}")
        comp = ((blob.get("vacancyView") or {}).get("company") or {})
        eid = comp.get("id")
        return int(eid) if eid else None

    def employer(self, employer_id: str | int) -> dict:
        """Employer dossier from hh's own /employer/{id} page state. No auth.

        Read this before believing any field here: hh publishes no INN and no
        OGRN, and nothing on this route establishes that an account is the
        organisation it names. Identity has to come from an external registry.
        The dossier is for context and for the vacancy list, not for attribution.
        """
        blob = self.json_page_anon(f"/employer/{employer_id}")
        info = blob.get("employerInfo") or {}
        if not info.get("id"):
            raise HHError(f"employer {employer_id}: no employerInfo "
                          f"(dead id, or hh changed the page)")
        inds = info.get("industries") or []
        # Both fields are richer than they look: site is {hostname, href}, and an
        # industry labels itself with `trl`, not `name`. Reading either as a plain
        # string yields None for every employer that has one.
        site = info.get("site")
        if isinstance(site, dict):
            site = site.get("href") or site.get("hostname")
        d = {
            "id": info.get("id"),
            "name": info.get("name"),
            "url": f"{BASE}/employer/{info.get('id')}",
            "address": info.get("address"),
            "site": site or None,
            "category": info.get("category"),
            "size": info.get("sizeCategory"),
            "description": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ",
                                                      info.get("description") or "")).strip(),
            "industries": [i.get("trl") or i.get("name") for i in inds
                           if isinstance(i, dict) and (i.get("trl") or i.get("name"))] or None,
            "esia_identified": bool(info.get("isIdentifiedByEsia")),
            "accredited_it": bool(info.get("accreditedITEmployer")),
            "trusted": bool(info.get("isTrusted")),
            "country": info.get("employerCountryCode"),
            "active_vacancies": blob.get("activeEmployerVacancyCount"),
        }
        d["flags"] = self._employer_flags(d)
        return d

    @staticmethod
    def _employer_flags(d: dict) -> list[str]:
        """Account-completeness observations. NOT evidence, and deliberately weak.

        Measured against known-genuine state employers on 2026-08-12: ФКУ Военный
        комиссариат Новосибирской области has no site, an empty description and one
        open vacancy, which is precisely the profile of a thin shell account. Real
        small budget organisations and fake ones are indistinguishable here, so
        these lines exist to send you to a registry, never to stand in for one.

        `isIdentifiedByEsia` is read but not flagged: it came back false for every
        employer tested, including Гознак, Сбер, Почта России, УФК and a federal
        military commissariat. It is not populated on this route and means nothing.
        Do not reintroduce it as a signal; that was tried on 2026-08-12 and it fired
        on 100% of employers.
        """
        f = []
        if not d["site"] and not d["description"]:
            f.append("thin account: no site, no description. A genuine small state "
                     "body looks the same. Verify by INN/OGRN, not from here")
        if HH._STATE_NAME.search(d.get("name") or ""):
            f.append("name claims a state body: hh does not verify this, and the "
                     "account is an ordinary employer account either way")
        if d["accredited_it"]:
            f.append("accredited IT employer: can offer conscription deferral")
        return f

    def employer_vacancies(self, employer_id: str | int, *, pages: int = 10) -> list[dict]:
        """Every currently open posting for one employer, via the search filter."""
        return self.search("", pages=pages, employer_id=str(employer_id))

    # Big, rigid employers whose office policy won't bend for a single hire.
    # `hunt` exists to find the opposite: small shops where the hiring manager
    # can just say yes to remote. Matched as a lowercase substring of the name.
    BIG_EMPLOYERS = (
        "сбер", "sber", "втб", "мтс", "mts", "альфа", "тинькоф", "т-банк", "т банк",
        "вконтакте", "vk ", "vk|", "яндекс", "yandex", "озон", "ozon", "авито", "avito",
        "wildberries", "вайлдберриз", "газпром", "ростелеком", "росатом", "касперск",
        "kaspersky", "positive tech", "билайн", "мегафон", "megafon", "x5", "райффайзен",
        "почта россии", "самолет", "самолёт", "магнит", "сибур", "северсталь", "лукойл",
        "росбанк", "совкомбанк", "home credit", "хоум кредит", "ланит", "крок", "лаборатория",
        "озон банк", "яндекс", "т‑банк", "гпб", "газпромбанк", "мвидео", "м.видео",
    )

    # Titles that mention AI but are not AI-engineering work: sales, 1C, support,
    # data annotation, appointment-setting. Kills the noise before the fit read.
    # Titles `hunt` drops by default. This is ONE PERSON'S saved search, not a
    # universal truth: it discards designers, testers, product and project
    # managers, support and sales, which are somebody else's whole career. It
    # stays as the default because hunt is explicitly the opinionated tool, but
    # it is overridable per call and the tool says out loud that it filters.
    STOP_TITLES = (
        "1с", "1c ", "продаж", "sales", "маркет", "аннотат", "annotation",
        "назначению встреч", "business development", "менеджер по прод", "рекрут",
        "поддержк", "справочно", "линии поддержки", "3-й линии", "1-й линии",
        "оператор", "тестировщ", "delivery manager", "project manager", "продукт",
        "бухгалтер", "юрист", "дизайнер", "копирайт", "контент",
    )

    def hunt(self, queries, *, pages=2, formats=None, limit=15,
             max_open=40, size=True, stop_titles=None) -> list[dict]:
        """AI/LLM roles at *small* employers, ranked for a remote pitch.

        The goal is the owner's: small shops he can talk into full remote, not big
        corps with a fixed office mandate. So we drop the known giants by name,
        keep one best posting per surviving employer, and rank by low competition
        (fewest applicants) then pay. `formats` filters by work format
        (REMOTE / HYBRID / ON_SITE); None keeps all, and HYBRID/ON_SITE are the
        ones actually worth a remote pitch. When `size` is on we look up each
        finalist's open-role count and drop anyone over `max_open` (a real size
        gate, since a genuinely small employer has few postings).

        Always drops vacancies already applied to, including ones since
        rejected and dropped from negotiations() (applied_vacancy_ids()) --
        without this a hunt loop would keep resurfacing jobs that already
        said no.
        """
        want = {f.upper() for f in formats} if formats else None
        applied = self.applied_vacancy_ids()
        pool, seen = [], set()
        for q in queries:
            for v in self.search(q, pages=pages):
                vid = v["id"]
                if not vid or vid in seen or vid in applied:
                    continue
                seen.add(vid)
                nm = (v.get("company") or "").lower()
                if not nm or any(b in nm for b in self.BIG_EMPLOYERS):
                    continue
                title = (v.get("name") or "").lower()
                if any(s in title for s in (self.STOP_TITLES if stop_titles is None
                                            else tuple(stop_titles))):
                    continue
                if want and not (set(v.get("work_format") or []) & want):
                    continue
                v["_rank"] = len(pool)          # hh relevance order across queries
                pool.append(v)
        # one posting per employer, keeping the most relevant (first-seen) one;
        # order by relevance, not competition (competition sorting floats junk).
        best: dict = {}
        for v in pool:
            eid = v.get("employer_id") or v.get("company")
            if eid not in best:
                best[eid] = v
        rank = sorted(best.values(), key=lambda v: v.get("_rank", 9999))
        if not size:
            return rank[:limit]
        out, looked = [], 0
        for v in rank:
            if len(out) >= limit or looked >= limit * 3:
                break
            eid = v.get("employer_id")
            if eid:
                looked += 1
                try:
                    v["open_roles"] = len(self.employer_vacancies(eid, pages=1))
                except Exception:
                    v["open_roles"] = None
                if v["open_roles"] is not None and v["open_roles"] > max_open:
                    continue
            out.append(v)
        return out

    def archive_vacancy(self, vacancy_id: str, dest: Path) -> dict:
        """Freeze one posting to disk: raw HTML, hh's JSON state, readable text.

        Postings like these get pulled, and a screenshot is not evidence. The raw
        bytes plus a sha256 and a capture time are what makes a later citation
        checkable by someone who was not here. Writes locally, sends nothing.
        """
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        _, _, body = self._req("GET", f"{BASE}/vacancy/{vacancy_id}")
        digest = hashlib.sha256(body).hexdigest()
        captured = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

        (dest / f"{vacancy_id}.html").write_bytes(body)
        try:
            blob = self.json_page_anon(f"/vacancy/{vacancy_id}")
            (dest / f"{vacancy_id}.json").write_text(
                json.dumps(blob.get("vacancyView") or blob, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception as e:
            self._log(f"    json state unavailable for {vacancy_id}: {e}")

        v = self.vacancy(vacancy_id)
        (dest / f"{vacancy_id}.md").write_text(
            f"# {v.get('name')}\n\n"
            f"- employer: {v.get('company')}\n"
            f"- url: {v.get('url')}\n"
            f"- captured: {captured}\n"
            f"- sha256(html): {digest}\n"
            f"- skills: {', '.join(v.get('skills') or []) or '-'}\n\n"
            f"{v.get('description') or ''}\n", encoding="utf-8")
        return {"id": vacancy_id, "name": v.get("name"), "company": v.get("company"),
                "captured": captured, "sha256": digest, "bytes": len(body),
                "dir": str(dest)}

    @staticmethod
    def archive_index(dest: Path) -> list[dict]:
        """Re-read an archive directory into citable rows.

        A findings document that cites bare vacancy ids is not checkable by anyone
        who was not there: ids alone do not resolve, and the posting they point at
        gets pulled. This regenerates url, capture time and hash from the frozen
        copies, so the citation and the evidence cannot drift apart.
        """
        rows = []
        for md in sorted(Path(dest).glob("*.md")):
            text = md.read_text(encoding="utf-8", errors="replace")
            def field(label):
                m = re.search(rf"^- {re.escape(label)}: (.*)$", text, re.M)
                return m.group(1).strip() if m else None
            title = re.search(r"^# (.*)$", text, re.M)
            rows.append({
                "id": md.stem,
                "name": title.group(1).strip() if title else None,
                "company": field("employer"),
                "url": field("url") or f"{BASE}/vacancy/{md.stem}",
                "captured": field("captured"),
                "sha256": (field("sha256(html)") or "")[:16],
            })
        return rows

    # ------------------------------------------------------------------ resume

    @staticmethod
    def _unwrap(v):
        """hh wraps every resume field as [{"string": value}, ...].

        Richer fields carry extra keys, e.g. professionalRole is
        [{"string": 160, "id": 160, "text": "DevOps-инженер"}]. Verified live:
        assuming a plain scalar here returns None for every field.
        """
        if not isinstance(v, list):
            return v
        out = []
        for item in v:
            if not isinstance(item, dict):
                out.append(item); continue
            if "text" in item and "id" in item:
                out.append({"id": item["id"], "text": item["text"]})
            elif "string" in item:
                out.append(item["string"])
            else:
                out.append(item)
        if len(out) == 1 and not isinstance(out[0], dict):
            return out[0]
        return out

    def resume(self, resume_id: str) -> dict:
        """Current resume state, read from the page's own data blob."""
        _, _, body = self._req("GET", f"{BASE}/resume/{resume_id}", auth=True)
        blob = self._state_blob(body.decode("utf-8", "replace"))
        if not blob:
            raise HHError("resume state blob not found; not logged in, or markup changed")
        # the object is applicantResume, NOT resume: the latter exists and is empty
        r = blob.get("applicantResume") or {}
        if not r:
            raise SessionExpired("applicantResume empty; session is probably dead")
        u = self._unwrap
        exp = []
        for e in (r.get("experience") or []):
            if isinstance(e, dict):
                exp.append({"company": e.get("companyName"), "position": e.get("position"),
                            "start": e.get("startDate"), "end": e.get("endDate"),
                            "description": (e.get("description") or "")})
        months = u(r.get("totalExperience"))
        # _unwrap collapses a one-item list only when the item is NOT a dict, so
        # salary comes back as [{"amount":...}] rather than the dict callers want.
        salary = u(r.get("salary"))
        if isinstance(salary, list):
            salary = salary[0] if len(salary) == 1 and isinstance(salary[0], dict) else None
        # _attributes.percent is per-CV resume COMPLETENESS (заполненность): it
        # tracks filled fields, so a CV missing salary reads a few points lower.
        # It is NOT the account-level "активность" gauge (that is one number and
        # lives elsewhere, still to be located). renewal = автоподнятие on/off.
        attrs = r.get("_attributes")
        if isinstance(attrs, list):
            attrs = attrs[0] if attrs else {}
        attrs = attrs or {}
        return {"id": resume_id,
                "title": u(r.get("title")),
                "completeness_percent": attrs.get("percent"),
                "auto_renewal": attrs.get("renewal"),
                # Bump state ("Обновить дату"). Verified 2026-09-04: an ordinary
                # save IS the bump -- saving flipped can_touch true->false and
                # pushed next_touch_at to +4h, so hh counts the edit as having
                # spent the touch. There is no separate touch endpoint to call.
                # Extra saves inside the window still move `updated`, but the
                # ranking budget is already spent, so can_touch is the flag that
                # actually matters. See HH.resume_touch.
                # hh's OWN verdict on the CV, and the structured skills behind
                # the flat chip list. Both were being parsed and thrown away: the
                # parser surfaced 21 of 54 fields, which is how a declared C1
                # English once read as "no languages set".
                "left_to_fill": (r.get("fieldStatuses") or {}).get("leftToFillFields") or [],
                "red_fields": (r.get("fieldStatuses") or {}).get("redFields") or [],
                "languages": [{"name": x.get("name"),
                               "level": (x.get("level") or {}).get("internalId"),
                               "level_name": (x.get("level") or {}).get("name")}
                              for x in (r.get("resumeApplicantSkills") or [])
                              if x.get("category") == "LANG"],
                "skills_detailed": [{"name": x.get("name"),
                                     "level": (x.get("level") or {}).get("internalId")}
                                    for x in (r.get("resumeApplicantSkills") or [])
                                    if x.get("category") == "SKILL"],
                # general=True marks hh's common/indexed vocabulary. Every one of
                # the owner's differentiators (LLM, RAG, MCP...) comes back False,
                # while the commodity stack comes back True.
                "skills_canonical": [x.get("name") for x in (r.get("advancedKeySkills") or [])
                                     if x.get("general")],
                "skills_freetext": [x.get("name") for x in (r.get("advancedKeySkills") or [])
                                    if not x.get("general")],
                "access_type": u(r.get("accessType")),
                "job_search_status": ((r.get("jobSearchStatus") or [{}])[0]
                                      .get("jobSearchStatus") or {}).get("name"),
                "can_touch": attrs.get("canTouch"),
                "next_touch_at": attrs.get("nextTouchAt"),
                "updated_at": attrs.get("updated"),
                "last_edit_at": attrs.get("lastEditTime"),
                "is_searchable": attrs.get("isSearchable"),
                "salary": salary,
                # hh's field naming is inverted: "skills" is the О себе free text,
                # "keySkills" are the tag chips. Expose both under sane names so
                # resume_update's verify step (which maps skills->about) can check
                # О себе instead of reporting None. See the keymap in resume_update.
                "about": u(r.get("skills")),
                "total_experience_months": months,
                "total_experience_years": round(months / 12, 1) if isinstance(months, int) else None,
                "professional_roles": u(r.get("professionalRole")),
                "skills": u(r.get("keySkills")),
                "employment_forms": u(r.get("employmentForms")),
                "work_formats": u(r.get("workFormats")),
                "business_trips": u(r.get("businessTripReadiness")),
                "area": u(r.get("area")),
                "experience": exp,
                "raw": r}

    def resume_update(self, resume_id: str, fields: dict, *, verify=True) -> dict:
        """Write resume fields. Values must be arrays, as hh's own frontend sends.

        fingerprintIteration2 is deliberately omitted: tested and not validated.
        If hh starts enforcing it this call will begin failing, which is why the
        verify step exists and defaults on.
        """
        url = (f"{BASE}/applicant/resume/edit?resume={resume_id}"
               f"&hhtmSource=resume_partial_edit")
        st, _, body = self._req("POST", url, auth=True,
                                data=json.dumps(fields).encode(),
                                headers={"Content-Type": "application/json",
                                         "Accept": "application/json",
                                         "X-Requested-With": "XMLHttpRequest",
                                         "Referer": f"{BASE}/resume/edit/{resume_id}/position",
                                         "Origin": BASE})
        if st != 200:
            raise HHError(f"resume_update HTTP {st}: {body[:200]!r}")
        if not verify:
            return {"status": st, "verified": None}
        time.sleep(1.0)
        after = self.resume(resume_id)
        # map payload keys to parsed keys so verification is meaningful rather
        # than a 200-check. Silent wrong-value saves are the norm here.
        keymap = {"title": "title", "salary": "salary",
                  "professionalRole": "professional_roles",
                  "employmentForms": "employment_forms",
                  "workFormats": "work_formats",
                  "businessTripReadiness": "business_trips",
                  # hh's naming is inverted from what you would guess:
                  # "skills" is the О себе free text, "keySkills" are the tags.
                  "skills": "about", "keySkills": "skills"}
        checked = {}
        for k in fields:
            if k == "fingerprintIteration2":
                continue
            pk = keymap.get(k)
            checked[k] = after.get(pk) if pk else "unchecked"
        return {"status": st, "verified": True, "fields": checked, "after": after}

    def experience_edit(self, resume_id: str, find: str, replace: str, *,
                        dry_run: bool = True, verify: bool = True) -> dict:
        """Find/replace text inside experience descriptions, structure-safe.

        hh's experience entries are structured objects (id, dates, companyName,
        position, description). A naive resume_update that re-sends only the
        description drops the rest of the entry. This preserves the whitelist of
        structural keys and only rewrites description text where `find` occurs.

        Refuses em/en dashes in the replacement (the owner's standing rule). Dry-run
        by default: returns the hit count and which companies would change without
        writing. This is the guarded home for the wording fixes that were being
        done by hand against a live account.
        """
        if self.refuse_dashes and any(c in replace for c in "—–"):
            raise HHError("replacement contains an em/en dash (HH_REFUSE_DASHES is on)")
        exp = self.resume(resume_id)["raw"].get("experience") or []
        KEEP = ("id", "startDate", "endDate", "companyName", "position", "description")
        touched, new_exp, hits = [], [], 0
        for e in exp:
            if not isinstance(e, dict):
                new_exp.append(e)
                continue
            entry = {k: e[k] for k in KEEP if k in e}
            desc = entry.get("description") or ""
            n = desc.count(find)
            if n:
                hits += n
                entry["description"] = desc.replace(find, replace)
                touched.append({"company": entry.get("companyName"), "occurrences": n})
            new_exp.append(entry)
        if hits == 0:
            raise HHError(f"phrase not found in any experience entry: {find!r}")
        if dry_run:
            return {"dry_run": True, "hits": hits, "entries_touched": touched}
        res = self.resume_update(resume_id, {"experience": new_exp}, verify=False)
        out = {"dry_run": False, "status": res["status"], "hits": hits,
               "entries_touched": touched}
        if verify:
            time.sleep(1.0)
            blob = json.dumps(self.resume(resume_id)["raw"].get("experience"),
                              ensure_ascii=False)
            out["verified"] = {"old_gone": find not in blob, "new_present": replace in blob}
        return out

    def experience_dates(self, resume_id: str, position_match: str, *,
                         start: str | None = None, end: str | None = None,
                         dry_run: bool = True, verify: bool = True) -> dict:
        """Set the start or end date of an existing experience entry.

        The gap this fills: experience_edit rewrites description TEXT and
        experience_add appends, so neither can close an open-ended job. An
        older role left with endDate None reads as a second CURRENT job on every
        CV an employer opens, so setting its end date is a common fix.

        Matched on a substring of `position`, and it REFUSES to write if the
        substring matches more than one entry, because silently dating the wrong
        job is worse than doing nothing. `end=""` reopens an entry as current.

        professionId and professionName are preserved along with the structural
        keys: hh derives its own classification from them (it already reads the
        AI entry as ML-инженер) and dropping them on a date edit would quietly
        reclassify the job.
        """
        for d in (start, end):
            if d and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                raise HHError(f"date must be YYYY-MM-DD, got {d!r}")
        if start is None and end is None:
            raise HHError("nothing to set: pass start and/or end")
        KEEP = ("id", "startDate", "endDate", "companyName", "position",
                "description", "professionId", "professionName")
        exp = self.resume(resume_id)["raw"].get("experience") or []
        matches = [e for e in exp if isinstance(e, dict)
                   and position_match.lower() in (e.get("position") or "").lower()]
        if not matches:
            raise HHError(f"no experience entry matching {position_match!r}")
        if len(matches) > 1:
            raise HHError(
                f"{position_match!r} matches {len(matches)} entries "
                f"({[m.get('position') for m in matches]}); be more specific")
        target = matches[0]
        before = {"position": target.get("position"),
                  "startDate": target.get("startDate"),
                  "endDate": target.get("endDate")}
        new_exp = []
        for e in exp:
            if not isinstance(e, dict):
                new_exp.append(e)
                continue
            entry = {k: e[k] for k in KEEP if k in e}
            if e is target:
                if start is not None:
                    entry["startDate"] = start or None
                if end is not None:
                    entry["endDate"] = end or None
            new_exp.append(entry)
        after = {"startDate": start if start is not None else before["startDate"],
                 "endDate": (end or None) if end is not None else before["endDate"]}
        if dry_run:
            return {"dry_run": True, "resume": resume_id,
                    "before": before, "after": after}
        res = self.resume_update(resume_id, {"experience": new_exp}, verify=False)
        out = {"dry_run": False, "resume": resume_id, "status": res["status"],
               "before": before, "after": after}
        if verify:
            time.sleep(1.0)
            live = self.resume(resume_id)["raw"].get("experience") or []
            got = next((e for e in live if isinstance(e, dict)
                        and e.get("position") == before["position"]), None)
            out["verified"] = bool(got) and \
                got.get("startDate") == after["startDate"] and \
                got.get("endDate") == after["endDate"]
            out["live"] = {"startDate": (got or {}).get("startDate"),
                           "endDate": (got or {}).get("endDate")}
        return out

    def experience_add(self, resume_id: str, entries: list[dict], *,
                       dry_run: bool = True, verify: bool = True) -> dict:
        """Append new experience entries, preserving the existing ones.

        Uses the exact write path experience_edit relies on (POST the whole
        experience array to /applicant/resume/edit), so the "experience block is
        not writable" caveat in the KB is stale: experience_edit has been writing
        it all along. New entries carry no id (hh assigns one); each needs at
        least company, position and start ("YYYY-MM-DD"). end omitted/None reads
        as a current role. Refuses em/en dashes. Dry-run by default; verifies by
        read-back that the count grew and each new position is present.
        """
        KEEP = ("id", "startDate", "endDate", "companyName", "position", "description")
        norm = []
        for e in entries:
            blob = (e.get("position", "") or "") + (e.get("description", "") or "") \
                   + (e.get("company") or e.get("companyName") or "")
            if self.refuse_dashes and any(c in blob for c in "—–"):
                raise HHError("a new experience entry contains an em/en dash "
                              "(HH_REFUSE_DASHES is on)")
            start = e.get("start") or e.get("startDate")
            if not start:
                raise HHError("each new entry needs a start date (YYYY-MM-DD)")
            if not (e.get("company") or e.get("companyName")):
                raise HHError("each new entry needs a company")
            if not e.get("position"):
                raise HHError("each new entry needs a position")
            norm.append({"companyName": e.get("company") or e.get("companyName"),
                         "position": e.get("position"),
                         "startDate": start,
                         "endDate": e.get("end") or e.get("endDate"),
                         "description": e.get("description") or ""})
        cur = self.resume(resume_id)["raw"].get("experience") or []
        existing = [{k: x[k] for k in KEEP if k in x} if isinstance(x, dict) else x
                    for x in cur]
        combined = existing + norm
        if dry_run:
            return {"dry_run": True, "current": len(existing), "adding": len(norm),
                    "result_total": len(combined),
                    "new": [{"company": i["companyName"], "position": i["position"],
                             "start": i["startDate"], "end": i["endDate"]} for i in norm]}
        res = self.resume_update(resume_id, {"experience": combined}, verify=False)
        out = {"dry_run": False, "status": res["status"],
               "was": len(existing), "expected": len(combined)}
        if verify:
            time.sleep(1.0)
            after = self.resume(resume_id)["raw"].get("experience") or []
            positions = {(x.get("position") or "") for x in after if isinstance(x, dict)}
            out["verified"] = {"count_after": len(after),
                               "grew": len(after) == len(combined),
                               "new_present": all(i["position"] in positions for i in norm)}
        return out

    # --------------------------------------------------------- resume portfolio

    def resumes(self) -> list[dict]:
        """Every resume on the account, with the hash the other calls need.

        /applicant/resumes 406s, so this comes out of the profile blob. Note the
        identifier trap: negotiations report a NUMERIC resumeId, but every URL
        and every write takes the 38-char HASH. Passing the numeric one gives an
        empty applicantResume, which surfaces as "session is probably dead".
        """
        # ONE 17KB call. This is the first thing any agent calls, and it used to
        # fetch each CV's rendered PAGE to fill in the title and salary: ~1.1MB
        # apiece, 3.3MB and ~20s for three, to list three lines. header_resumes
        # carries titles, hashes, salary and attributes together.
        hdr = self._header_resumes()
        out = []
        for r in (hdr.get("resumes") or []):
            a = r.get("_attributes") or {}
            h = a.get("hash")
            if not h:
                continue
            title = r.get("title")
            if isinstance(title, list):
                title = (title[0] or {}).get("string") if title and isinstance(title[0], dict) else title[0]
            sal = self._unwrap(r.get("salary"))
            if isinstance(sal, list):
                sal = sal[0] if len(sal) == 1 and isinstance(sal[0], dict) else None
            out.append({"hash": h, "title": title, "salary": sal,
                        "skills": len(r.get("keySkills") or []),
                        # The shard reports percent=0 for every CV, so it is
                        # absent rather than zero. Reporting 0 would be worse
                        # than None: a caller believes a number. Real
                        # completeness comes from resume() / resume_scorecard.
                        "completeness_percent": (a.get("percent") or None),
                        # Likewise: this shard does not carry total experience.
                        "experience_months": None,
                        "auto_renewal": a.get("renewal"),
                        "can_touch": a.get("canTouch")})
        if not out:
            # header_resumes went away or changed shape: fall back to the old
            # profile-blob scrape rather than reporting an empty account.
            blob = self.json_page("/applicant/profile/me")
            seen = set()
            for m in re.finditer(r'"hash"\s*:\s*"([0-9a-f]{30,})"',
                                 json.dumps(blob, ensure_ascii=False)):
                h = m.group(1)
                if h in seen:
                    continue
                seen.add(h)
                try:
                    r = self.resume(h)
                    out.append({"hash": h, "title": r["title"], "salary": r["salary"],
                                "skills": len(r["skills"] or []),
                                "experience_months": r["total_experience_months"],
                                "completeness_percent": r.get("completeness_percent"),
                                "auto_renewal": r.get("auto_renewal")})
                except HHError as e:
                    out.append({"hash": h, "error": str(e)[:80]})
            time.sleep(self.pause)
        return out

    # hh cap: 20 resumes per account (resumeLimits {max:20}). A CV per vacancy is
    # not buildable; a small portfolio plus per-application routing is.
    CV_FIELDS = {
        "title":            ("title", lambda v: [v]),
        # Accepts a bare number (currency defaults to HH_CURRENCY, itself RUR)
        # or an explicit {"amount": N, "currency": "KZT"}. It used to force RUR
        # onto every write, so a Kazakh or Belarusian user's salary was silently
        # relabelled in their live CV: same number, wrong currency, and nothing
        # said so. hh.ru serves several countries.
        "salary":           ("salary", lambda v: [_salary(v)]),
        "about":            ("skills", lambda v: [v]),          # О себе, see keymap above
        "skills":           ("keySkills", lambda v: list(v)),   # tags
        "roles":            ("professionalRole", lambda v: [int(x) for x in v]),
        "employment_forms": ("employmentForms", lambda v: list(v)),
        "work_formats":     ("workFormats", lambda v: list(v)),
        "business_trips":   ("businessTripReadiness", lambda v: [v]),
        "travel_time":      ("travelTime", lambda v: [v]),
    }

    def resume_touch(self, resume_id: str, *, force: bool = False) -> dict:
        """Bump a CV's date, hh's "Обновить дату", so it rises in employer search.

        There is no touch endpoint. Measured 2026-09-04: an ordinary partial save
        IS the touch. Writing the title back byte-for-byte moved `updated` and
        `lastEditTime` to now, pushed `nextTouchAt` to +4h, and flipped `canTouch`
        to false -- hh accounted the edit as having spent the touch. Nothing else
        moved: `moderated` was unchanged and `isSearchable` stayed true, so this
        does not trigger re-moderation or drop the CV out of search.

        So the whole operation is: rewrite one field to its current value. The
        useful part is not the write, it is `canTouch` -- saves inside the 4-hour
        window still move `updated` but the ranking budget is already spent, so
        hammering it buys nothing. Skips by default when hh says it is too soon.

        Note this cuts both ways: any real CV edit also spends the touch. Batch
        edits and a bump compete for the same 4-hour budget.
        """
        a = self.resume_attributes().get(resume_id) or {}
        if not force and a.get("canTouch") is False:
            return {"touched": False, "reason": "too soon, hh's 4h window is not up",
                    "next_touch_at": a.get("nextTouchAt"), "title": None}
        cur = self.resume(resume_id)          # only now, for the title to rewrite
        title = cur.get("title")
        if not title:
            raise HHError(f"resume {resume_id[:8]} has no title to rewrite; refusing "
                          f"to guess a field to touch")
        res = self.resume_update(resume_id, {"title": [title]}, verify=False)
        after = self.resume(resume_id)
        return {"touched": True, "status": res.get("status"), "title": title,
                "updated_at": after.get("updated_at"),
                "next_touch_at": after.get("next_touch_at")}

    def resume_push(self, resume_id: str, variant: dict, *, dry_run: bool = False) -> dict:
        """Apply a CV variant to a live resume.

        This is the step the toolkit was missing: variants used to render to
        paste-ready text, which meant a human retyping it into the web form and
        hitting every UI trap in HH-KB section 8. Everything here is writable
        over HTTP, verified by re-reading.
        """
        unknown = [k for k in variant if k not in self.CV_FIELDS and not k.startswith("_")]
        if unknown:
            raise HHError(f"not writable via this endpoint: {unknown}. "
                          f"Writable: {sorted(self.CV_FIELDS)}")
        payload = {}
        for k, v in variant.items():
            if k.startswith("_") or v is None:
                continue
            hh_key, conv = self.CV_FIELDS[k]
            payload[hh_key] = conv(v)
        # His hard rule, enforced here because a CV is the worst place to break it.
        for k, v in payload.items():
            if self.refuse_dashes and any(c in str(v) for c in "—–"):
                raise HHError(f"refusing: {k} contains an em/en dash "
                              f"(HH_REFUSE_DASHES is on)")
        if dry_run:
            return {"dry_run": True, "payload": payload}
        return self.resume_update(resume_id, payload)

    # ------------------------------------------------------------------- apply

    def apply(self, vacancy_id: str, resume_id: str, letter: str = "",
              *, verify=True) -> dict:
        """Submit an application. Multipart, exactly as hh's own form posts it."""
        if not self.session or not self.session.xsrf:
            raise HHError("apply needs a session with _xsrf")
        boundary = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
        parts = {"_xsrf": self.session.xsrf, "vacancy_id": str(vacancy_id),
                 "resume_hash": resume_id, "ignore_postponed": "true"}
        if letter:
            parts["letter"] = letter
            parts["lux"] = "true"
        chunks = []
        for k, v in parts.items():
            chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
        chunks.append(f"--{boundary}--\r\n")
        data = "".join(chunks).encode()
        st, _, body = self._req("POST", f"{BASE}/applicant/vacancy_response/popup",
                                auth=True, data=data,
                                headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                         "X-Requested-With": "XMLHttpRequest",
                                         "Referer": f"{BASE}/vacancy/{vacancy_id}",
                                         "Origin": BASE})
        ok = st in (200, 201)
        result = {"status": st, "vacancy": vacancy_id, "sent": ok}
        if ok and verify:
            time.sleep(1.5)
            # refresh=True is REQUIRED: any earlier applied_to() call (such as a
            # caller's own duplicate guard) primes the cache, and verifying
            # against that stale set reports a successful apply as unverified.
            result["verified"] = self.applied_to(vacancy_id, refresh=True)
        elif not ok:
            # Never swallow a failure. hh puts a machine code in the body, e.g.
            # {"error":"test-required"}; turn it into something you can act on.
            result["reason"], result["detail"] = self._apply_error(st, body, vacancy_id)
        return result

    # hh's own error codes from the apply endpoint body -> a plain explanation.
    APPLY_ERRORS = {
        "test-required": "hh requires this vacancy's test/questionnaire; a direct "
                         "apply is blocked. Open the vacancy and complete the test.",
        "negotiation-limit-exceeded": "hit hh's daily application limit; try later.",
        "already-applied": "you have already applied to this vacancy.",
        "resume-not-found": "the resume hash is wrong or that CV was deleted.",
        "vacancy-not-found": "the vacancy is gone (closed or archived).",
        "vacancy-archived": "the vacancy is archived and no longer takes applications.",
        "letter-required": "this employer requires a cover letter; send one with --letter.",
        "resume-not-finished": "the CV is incomplete for this vacancy's requirements.",
    }

    def _apply_error(self, status: int, body: bytes, vacancy_id: str) -> tuple[str, str]:
        """(reason, detail) for a failed apply. Reads hh's error code from the body
        and maps it; falls back to the raw body so nothing is silently lost."""
        raw = (body or b"").decode("utf-8", "replace")[:300]
        code = ""
        try:
            if raw.strip().startswith("{"):
                code = (json.loads(raw) or {}).get("error", "") or ""
        except Exception:
            code = ""
        if code:
            return code, self.APPLY_ERRORS.get(code, f"hh rejected the apply: {code}")
        try:
            if self.applied_to(vacancy_id, refresh=True):
                return "already-applied", self.APPLY_ERRORS["already-applied"]
        except Exception:
            pass
        return f"http-{status}", f"hh returned HTTP {status}: {raw!r}"

    def applied_vacancy_ids(self, refresh: bool = False) -> set[str]:
        """Every vacancy_id ever applied to, including ones hh has since dropped
        from negotiations() after a rejection (see lost_negotiations()).

        The one shared source of truth for "have I applied to this already" --
        `applied_to()`, `hunt()`'s dedup, and every "[ALREADY APPLIED]" marker
        in `search`/`similar`/`recommended` all read this instead of each
        re-deriving it from negotiations() alone, which is how five separate
        copies of this check went silently wrong the same way at once: every
        one missed a vacancy that had since been rejected and dropped.
        """
        if refresh or not hasattr(self, "_applied_cache"):
            vids = {t["vacancy_id"] for t in self.negotiations() if t.get("vacancy_id")}
            vids |= {c["vacancy_id"] for c in self.lost_negotiations() if c.get("vacancy_id")}
            self._applied_cache = vids
        return self._applied_cache

    def applied_to(self, vacancy_id: str, refresh: bool = False) -> bool:
        """Have you already applied to this vacancy (including a since-rejected one).

        Do not be tempted to string-match "Вы откликнулись" in the HTML: that
        phrase sits in hh's i18n dictionary and appears 3 times on EVERY
        vacancy page, applied or not. That false positive silently blocked
        five applications before it was caught, reporting them all as
        duplicates.
        """
        return str(vacancy_id) in self.applied_vacancy_ids(refresh=refresh)

    def negotiations(self, *, all_pages: bool = True) -> list[dict]:
        """Applications sent, joined to vacancy detail.

        The payload splits them: applicantNegotiations.topicList holds state and
        a vacancyId, while the human-readable vacancy sits in vacanciesShort
        keyed separately. Reading topicList alone gives rows with no names,
        which is what an earlier version of this method did.

        IT PAGES AT 20. The earlier version read page 0 and returned it as the
        whole list, which silently hid 7 of 22 applications, including a rejection
        and an interview invitation. `pageCount` is a STRING in the payload.
        """
        out, page, pages = [], 0, 1
        while page < pages:
            blob = self._negotiations_page(page)
            an = blob.get("applicantNegotiations") or {}
            if page == 0 and all_pages:
                try:
                    pages = max(1, int(an.get("pageCount") or 1))
                except (TypeError, ValueError):
                    pages = 1
            short = ((blob.get("vacanciesShort") or {}).get("vacanciesList")) or []
            by_id = {str(v.get("vacancyId") or v.get("id") or ""): v
                     for v in (short if isinstance(short, list) else [])}
            for t in an.get("topicList") or []:
                vid = str(t.get("vacancyId") or "")
                v = by_id.get(vid, {})
                out.append({
                    "vacancy_id": vid,
                    "name": v.get("name"),
                    "company": (v.get("company") or {}).get("name") if isinstance(v.get("company"), dict) else v.get("companyName"),
                    "state": t.get("lastState"),
                    "substate": t.get("applicantSubState"),
                    "viewed_by_employer": t.get("viewedByOpponent"),
                    "has_letter": t.get("hasResponseLetter"),
                    "unread": t.get("hasNewMessages"),
                    "messages": t.get("conversationMessagesCount"),
                    "chat_id": t.get("chatId"),
                    "created": t.get("creationTime"),
                    "modified": t.get("lastModified"),
                    "url": f"{BASE}/vacancy/{vid}" if vid else None,
                })
            page += 1
            if not all_pages:
                break
            if page < pages:
                time.sleep(self.pause)
        return out

    def lost_negotiations(self) -> list[dict]:
        """Negotiation-linked chats absent from negotiations() -- hh dropped the topic.

        Verified 2026-08-30: once a topic resolves to DISCARD (rejected), hh's
        `/applicant/negotiations` eventually stops returning it on ANY page --
        confirmed by walking every page and finding it genuinely gone, not
        paginated away. The chat survives in the messenger backend (`chats()`)
        with the rejection text intact. hh's own page embeds a
        `negotiationsCounters` block with a `deleted` bucket (51 vs `all`: 28 on
        the account this was found against) that the negotiations endpoint
        never surfaces.

        NOT instant, verified 2026-08-31: a rejection that just landed sits in
        negotiations() with state DISCARD for a while (at least tens of
        minutes) before hh purges it, so this method misses that window --
        it only catches ones already gone. `sent`/`inbox` calling
        negotiations() directly still see the fresh ones via their own state
        field; this method alone is not "the complete rejection list."

        Without this, `sent`/`inbox` -- built on negotiations() alone -- go
        silently blind on every OLD rejection, which is the whole point of
        running them.
        """
        neg_vids = {t["vacancy_id"] for t in self.negotiations()}
        return [c for c in self.chats()
                if c["type"] == "NEGOTIATION" and c.get("vacancy_id")
                and c["vacancy_id"] not in neg_vids]

    def inbox_view(self) -> list[dict]:
        """Everything awaiting a reply, joined and classified in one cheap pass.

        Three distinct cases, and collapsing any two of them misleads about what
        a conversation IS: an active negotiation (state from negotiations()), a
        DIRECT chat with no negotiation behind it (type COMMON, e.g. an employer
        writing first), and UNLISTED -- a negotiation-linked chat hh has dropped
        from negotiations() entirely (see lost_negotiations()). Two calls total:
        negotiations() for state, chats() for who-spoke-last, joined on chat_id.
        """
        states = {t["chat_id"]: t for t in self.negotiations() if t.get("chat_id")}
        out = []
        for c in self.chats():
            if c["last_mine"] or not c["last_text"]:
                continue                      # ball is in their court
            neg = states.get(c["chat_id"])
            if neg and neg.get("state"):
                state = neg["state"]
                flag = "INVITATION, needs a reply" if state == "INTERVIEW" else ""
            elif c["is_direct"]:
                state = "DIRECT/" + (c["subtype"] or "?")
                flag = "THEY WROTE FIRST, no application behind it"
            else:
                state = "UNLISTED"
                flag = "you applied, but hh no longer lists this negotiation"
            out.append({**c, "state": state, "flag": flag})
        return out

    def trash_negotiation(self, key: str) -> dict:
        """Move a negotiation to trash (hh's HIDE, "переместить в архив"). `key` is a
        vacancy id or a chat id.

        Reverse-engineered from ApplicantNegotiations-route.js: the delete action does
        postFormData('/applicant/negotiations/trash', {topic, vacancyId, employerId,
        query, substate:'HIDE'}). The plausible `topicList`/`id` params all return a
        200 with `<doc/>` and do nothing (hh's legacy no-op signature); only this exact
        shape hides the thread. `topic` is the negotiation id (topicList item `id`),
        NOT the chat id. Reversible: the thread moves to the trash bucket, not deleted.
        """
        key, page, pages, hit = str(key), 0, 1, None
        while page < pages and not hit:
            an = self._negotiations_page(page).get("applicantNegotiations") or {}
            if page == 0:
                try:
                    pages = max(1, int(an.get("pageCount") or 1))
                except (TypeError, ValueError):
                    pages = 1
            for t in an.get("topicList") or []:
                if key in (str(t.get("vacancyId")), str(t.get("chatId")), str(t.get("id"))):
                    hit = t
                    break
            page += 1
        if not hit:
            raise HHError(f"no active negotiation for {key!r}")
        data = urllib.parse.urlencode({
            "topic": hit.get("id"), "vacancyId": hit.get("vacancyId"),
            "employerId": hit.get("employerId"), "query": "", "substate": "HIDE"}).encode()
        st, _, body = self._req(
            "POST", f"{BASE}/applicant/negotiations/trash", auth=True, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Referer": f"{BASE}/applicant/negotiations", "Origin": BASE})
        if st != 200:
            raise HHError(f"trash HTTP {st}: {body[:160]!r}")
        return {"topic": hit.get("id"), "vacancy_id": str(hit.get("vacancyId")),
                "chat_id": hit.get("chatId"), "trashed": True}

    def _negotiations_page(self, page: int) -> dict:
        _, ct, body = self._req("GET", f"{BASE}/applicant/negotiations?page={page}",
                                auth=True,
                                headers={"X-Requested-With": "XMLHttpRequest",
                                         "Accept": "application/json",
                                         "X-Static-Version": self.static_version()})
        text = body.decode("utf-8", "replace")
        return json.loads(text) if "json" in ct else (self._state_blob(text) or {})

    def counters(self, rows: list[dict] | None = None) -> dict:
        """Funnel counts by state.

        hh used to expose applicantNegotiationsCounters; it is absent from the
        current payload, so this counts lastState across all pages instead.
        States seen: RESPONSE (pending), INTERVIEW (invited), DISCARD (rejected).
        Pass an already-fetched `negotiations()` list to avoid a second paged read.
        """
        from collections import Counter
        return dict(Counter(t["state"] or "UNKNOWN" for t in (rows if rows is not None else self.negotiations())))

    # -------------------------------------------------------------------- chat

    def chat(self, chat_id: str | int) -> dict:
        """One negotiation chat, with authorship and read state resolved.

        Authorship is `participantId == chat.currentParticipantId`. Do NOT infer
        it from `canEdit`: that flag is about permission, not authorship, and it
        is False on your own messages in some closed chats, which mislabels your
        own cover letter as the employer's reply.
        """
        blob = self.json_page(f"/chat/{chat_id}")
        ch = ((blob.get("chatData") or {}).get("chat")) or {}
        me = ch.get("currentParticipantId")
        seen_upto = ch.get("lastViewedByOpponentMessageId") or 0

        # The first window is not always the whole thread. `messages.hasMore`
        # says so, and ignoring it truncates silently -- the same failure that
        # made chats() return 20 of 61, one layer down and on the read this
        # toolkit uses most. Page backwards on the oldest id we hold.
        # Untested against a genuinely long thread: no chat on the development
        # account exceeds one window, so this path is defensive.
        items = list((ch.get("messages") or {}).get("items") or [])
        more = bool((ch.get("messages") or {}).get("hasMore"))
        guard = 0
        while more and items and guard < 20:
            guard += 1
            oldest = min(int(m.get("id") or 0) for m in items if m.get("id"))
            try:
                _, _, body = self._req(
                    "GET", f"{CHATIK}/chatik/api/chat_data?chatId={chat_id}"
                           f"&lastMessageId={oldest}", auth=True,
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Accept": "application/json",
                             "X-Static-Version": self.static_version()})
                page = (json.loads(body.decode("utf-8", "replace")).get("chat") or {})
            except Exception:                          # noqa: BLE001
                break
            pm = page.get("messages") or {}
            older = [m for m in (pm.get("items") or [])
                     if int(m.get("id") or 0) < oldest]
            if not older:
                break
            items = older + items
            more = bool(pm.get("hasMore"))
            time.sleep(self.pause)

        msgs = []
        for m in items:
            disp = m.get("participantDisplay") or {}
            mine = m.get("participantId") == me
            msgs.append({
                "id": m.get("id"),
                "mine": mine,
                "author": disp.get("name"),
                "is_bot": disp.get("isBot"),
                "text": (m.get("text") or "").strip(),
                "time": m.get("creationTime"),
                "can_edit": bool(m.get("canEdit")),
                "can_delete": bool(m.get("canDelete")),
                # per-message receipt, more precise than the topic-level flag
                "read_by_employer": bool(mine and seen_upto and m.get("id") and
                                         int(m["id"]) <= int(seen_upto)),
            })
        # The vacancy id is not in the chat blob (it lives in the negotiation
        # topic, which `chats()` resolves), but the employer id is, under
        # ownerEmployerInfo. Surfacing it means `chat_read` links straight to the
        # employer dossier instead of a hand-written negotiations() lookup.
        return {"chat_id": ch.get("id"), "unread": ch.get("unread_count") or ch.get("unreadCount"),
                "employer_id": (ch.get("ownerEmployerInfo") or {}).get("id"),
                "write_allowed": bool(((blob.get("chatData") or {}).get("chatStates") or {})
                                      .get("writeMessageState", {}).get("allowed")),
                "messages": msgs}

    def chats(self, *, all_pages: bool = True) -> list[dict]:
        """Every chat in hh's messenger, including ones with no application behind them.

        `negotiations()` only ever sees chats of type NEGOTIATION. hh also has
        type **COMMON**: an employer opening a conversation with you directly,
        subType **GENAI** being hh's AI-recruiter outreach. Those carry a real
        vacancy and a real employer, `write_allowed` is True on them, and they
        are INVISIBLE to every negotiation-derived view -- `sent`, `inbox` and
        the watcher alike. Found 2026-08-11, after a 200-500k salestech offer
        sat unseen in one.

        Route: `chatik.hh.ru/chatik/api/chats`, doubled segment exactly as in
        the save route. Every plausible sibling name (/chat_list, /dialogs,
        /init, /get_chats, /state) returns **200 with hh.ru's SPA state**
        instead of a chat list, which parses as "no chats" rather than raising,
        so assert on `chats.items` and never on the status code.

        IT PAGES AT 20, and NOT the way the rest of the site does. This route
        IGNORES `?page=` entirely -- page=1 is byte-identical to page=0 -- and
        the response has no `pages` key at all, only `found`, `items` and
        `nextFrom`. The previous version asked for `pages`, got nothing, fell
        back to 1, and returned the first 20 chats as though they were all of
        them. Measured 2026-09-04: 20 returned of 61 found, so 41 chats were
        invisible, among them exactly the employer-initiated COMMON threads this
        method exists to catch.

        The real pager is a CURSOR: pass `?from=<nextFrom>` to get the next 20.
        Trust `found` over what you have, and stop when a page repeats or the
        cursor stops moving.
        """
        out, cursor, seen_ids, found = [], None, set(), None
        while True:
            blob = self._chats_page(cursor=cursor)
            ch = blob.get("chats") or {}
            items = ch.get("items")
            if items is None:
                raise HHError("chat list did not negotiate to JSON: got a page, not a chat "
                              "list. Check the route and X-Static-Version.")
            if found is None:
                found = ch.get("found")
            fresh = [c for c in items if c.get("id") not in seen_ids]
            if not fresh:
                break
            seen_ids.update(c.get("id") for c in fresh)
            items = fresh
            res = blob.get("resources") or {}
            vacs = res.get("vacancies") or {}
            emps = res.get("employers") or {}
            disp = blob.get("chatsDisplayInfo") or {}
            for c in items:
                rs = c.get("resources") or {}
                vid = (rs.get("VACANCY") or [None])[0]
                eid = (rs.get("EMPLOYER") or [None])[0]
                topic = (rs.get("NEGOTIATION_TOPIC") or [None])[0]
                d = disp.get(str(c.get("id"))) or {}
                lm = c.get("lastMessage") or {}
                pd = lm.get("participantDisplay") or {}
                mine = lm.get("participantId") == c.get("currentParticipantId")
                out.append({
                    "chat_id": c.get("id"),
                    "type": c.get("type"),
                    "subtype": c.get("subType"),
                    # the whole point of this method: no topic means no application
                    "is_direct": c.get("type") != "NEGOTIATION",
                    "topic_id": topic,
                    "unread": c.get("unreadCount") or 0,
                    "vacancy_id": str(vid) if vid else None,
                    "vacancy": d.get("title") or (vacs.get(str(vid)) or {}).get("name"),
                    "company": d.get("subtitle") or (emps.get(str(eid)) or {}).get("name"),
                    "last_author": pd.get("name"),
                    "last_is_bot": bool(pd.get("isBot")),
                    "last_mine": mine,
                    "last_text": (lm.get("text") or "").strip(),
                    "last_time": lm.get("creationTime"),
                    "status": ((lm.get("metadata") or {}).get("clientData") or {}).get("status"),
                    "url": f"{BASE}/vacancy/{vid}" if vid else None,
                })
            nxt = ch.get("nextFrom")
            if not all_pages or not nxt or nxt == cursor:
                break
            cursor = nxt
            time.sleep(self.pause)
        if isinstance(found, int) and all_pages and len(out) < found:
            self._log(f"  warning: {len(out)} chats read but hh reports {found}")
        return out

    def _chats_page(self, *, cursor: str | None = None) -> dict:
        """One window of the chat list. `from` is a cursor, not a page number."""
        url = f"{CHATIK}/chatik/api/chats"
        if cursor:
            url += "?from=" + urllib.parse.quote(str(cursor))
        _, ct, body = self._req("GET", url, auth=True,
                                headers={"X-Requested-With": "XMLHttpRequest",
                                         "Accept": "application/json",
                                         "X-Static-Version": self.static_version()})
        text = body.decode("utf-8", "replace")
        return json.loads(text) if "json" in ct else (self._state_blob(text) or {})

    def edit_message(self, message_id: str | int, text: str) -> dict:
        """Rewrite an already-sent chat message, including a cover letter.

        An application is NOT irreversible: the cover letter is just the first
        message in the negotiation chat and the sender can rewrite it in place.

        Three traps, each of which cost an hour:
          * The host is chatik.hh.ru, and the path keeps its /chatik prefix.
            hh.ru/chatik/api/save 404s with an HTML page.
          * Permission is `canEdit` ON THE MESSAGE. The chat-level
            operations.allowed lists only LEAVE_CHAT/DISABLE_NOTIFICATIONS and
            every message carries operations:null, which reads as "not editable"
            and is not.
          * A successful save returns {"enable_dark_theme":false,...}, which has
            nothing to do with the outcome. Verify by re-reading the chat.
        """
        if not self.session or not self.session.xsrf:
            raise HHError("editing a message needs a session with _xsrf")
        st, _, body = self._req(
            "POST", f"{CHATIK}/chatik/api/save", auth=True,
            data=json.dumps({"text": text, "messageId": int(message_id)}).encode(),
            headers={"Content-Type": "application/json",
                     "X-XSRFToken": self.session.xsrf,
                     "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json",
                     "Origin": BASE, "Referer": f"{BASE}/chat/"})
        if st not in (200, 201):
            raise HHError(f"edit refused (HTTP {st}): {body[:200]!r}")
        return {"status": st, "message_id": int(message_id)}

    def send_message(self, chat_id: str | int, text: str) -> dict:
        """Post a NEW message into a chat, rather than rewriting an existing one.

        Sending is NOT `save` with a chatId. `save` is the edit path and takes
        `messageId`; handing it `chatId` returns **HTTP 400 with the same
        `{"enable_dark_theme":false}` body a success returns**, so the body tells
        you nothing and only the status code does. The send route is
        `/chatik/api/send`, and it wants an **`idempotencyKey`**: a client-side
        uuid4 that stops a retry from posting the message twice.

        Found by walking hh's own bundle: `remote.chatik.js` carries the webpack
        chunk map, chunk 136 holds the edit path and chunk 346 the send path.
        """
        if not self.session or not self.session.xsrf:
            raise HHError("sending a message needs a session with _xsrf")
        text = (text or "").strip()
        if not text:
            raise HHError("refusing to send an empty message")
        st, _, body = self._req(
            "POST", f"{CHATIK}/chatik/api/send?hhtmSource=chat&hhtmSourceLabel=chat",
            auth=True,
            data=json.dumps({"chatId": int(chat_id),
                             "idempotencyKey": str(uuid.uuid4()),
                             "text": text}).encode(),
            headers={"Content-Type": "application/json",
                     "X-XSRFToken": self.session.xsrf,
                     "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json",
                     "Origin": BASE, "Referer": f"{BASE}/chat/"})
        if st not in (200, 201):
            raise HHError(f"send refused (HTTP {st}): {body[:200]!r}")
        time.sleep(1.5)
        msgs = self.chat(chat_id)["messages"]
        # Verify by finding MY message with this text, NOT by assuming it is the
        # last one. A bot or employer can reply inside the 1.5s window and push
        # my message off the end; the old "is the last message mine" check then
        # reported a good send as verified=False and returned the bot's id. That
        # is exactly what a fast screening bot triggers.
        mine = [m for m in msgs if m.get("mine")
                and (m.get("text") or "").strip() == text.strip()]
        verified = bool(mine)
        chosen = (mine[-1] if mine else (msgs[-1] if msgs else {}))
        return {"status": st, "chat_id": int(chat_id), "sent": True,
                "verified": verified, "message_id": chosen.get("id"),
                "note": None if verified else
                        "sent OK (HTTP 200), but the text was not found on readback "
                        "(the employer or a bot may have replied first). Re-check the chat."}

    def leave_chat(self, chat_id: str | int) -> dict:
        """Leave a chat, removing it from the messenger. `chat_id` from `chats()`.

        hh has NO archive for a messenger chat. `trash_negotiation` hides the
        *отклик* (the negotiation), but the chatik conversation stays in the chat
        list: the only chat-level actions hh's own UI offers on a NEGOTIATION
        thread are LEAVE_CHAT and DISABLE_NOTIFICATIONS (its `operations.allowed`).
        This is LEAVE_CHAT, the one that actually clears the thread out.

        Route recovered from the chatik bundle, not guessed: remote.chatik.js's
        chunk 822 binds the leave button to `post('/chatik/api/leave', {chatId})`,
        then navigates away and drops the chat from the store. Same host, xsrf and
        header shape as `send_message`. NOT reversible from here: hh removes the
        thread from your list, so use it on dead threads (a rejection), never a
        live one. Verifying costs a full chat re-list, so the caller does that
        once after a batch rather than this method paying it on every id.
        """
        if not self.session or not self.session.xsrf:
            raise HHError("leaving a chat needs a session with _xsrf")
        st, _, body = self._req(
            "POST", f"{CHATIK}/chatik/api/leave", auth=True,
            data=json.dumps({"chatId": int(chat_id)}).encode(),
            headers={"Content-Type": "application/json",
                     "X-XSRFToken": self.session.xsrf,
                     "X-Requested-With": "XMLHttpRequest",
                     "Accept": "application/json",
                     "Origin": BASE, "Referer": f"{BASE}/chat/"})
        if st not in (200, 201, 204):
            raise HHError(f"leave refused (HTTP {st}): {body[:200]!r}")
        return {"status": st, "chat_id": int(chat_id), "left": True}

    def letter(self, vacancy_id: str) -> dict | None:
        """The cover letter you sent for one application, with its edit state.

        Applying WITHOUT a letter still creates your message: a `SIMPLE` message
        with `hasContent: false` carrying the workflow transition and your resume.
        It is editable, so a missing cover letter can be added after the fact.
        Requiring non-empty text here made that impossible and reported the most
        fixable case, an application with no letter at all, as "not found".
        """
        for t in self.negotiations():
            if t["vacancy_id"] != str(vacancy_id):
                continue
            if not t.get("chat_id"):
                return None
            mine = [m for m in self.chat(t["chat_id"])["messages"] if m["mine"]]
            chosen = next((m for m in mine if m["text"]),
                          next((m for m in mine if m["can_edit"]), None))
            if chosen is None:
                return None
            return {**chosen, "company": t["company"], "vacancy": t["name"],
                    "state": t["state"], "empty": not chosen["text"],
                    "viewed_by_employer": t["viewed_by_employer"]}
        return None

    def set_letter(self, vacancy_id: str, text: str, *,
                   only_if_unread: bool = True, verify: bool = True) -> dict:
        """Replace a sent cover letter.

        Refuses by default once the employer has opened it: rewriting under
        someone who has already read it changes what they were replying to. Pass
        only_if_unread=False to override deliberately.
        """
        cur = self.letter(vacancy_id)
        if cur is None:
            raise HHError(f"no editable letter found for vacancy {vacancy_id}")
        if not cur["can_edit"]:
            raise HHError(f"hh refuses edits on this message (state={cur['state']}); "
                          "canEdit goes False once an application is discarded")
        # The guard protects against changing words someone already read. If there
        # were no words, there is nothing to contradict: adding a letter to a
        # letterless application is strictly more information for the employer.
        if only_if_unread and not cur.get("empty") and (
                cur["read_by_employer"] or cur["viewed_by_employer"]):
            raise HHError(f"{cur['company']} has already opened this letter; "
                          "pass only_if_unread=False to rewrite it anyway")
        if cur["text"].strip() == text.strip():
            return {"changed": False, "reason": "identical", "message_id": cur["id"]}
        self.edit_message(cur["id"], text)
        result = {"changed": True, "message_id": cur["id"],
                  "was": len(cur["text"]), "now": len(text)}
        if verify:
            time.sleep(2.0)
            after = self.letter(vacancy_id)
            result["verified"] = bool(after and after["text"].strip() == text.strip())
        return result

    def json_page_anon(self, path: str) -> dict:
        """Same negotiation trick, deliberately WITHOUT the session.

        /search/resume works anonymously: hh publishes candidate resumes with
        PII already redacted and charges only for contacts. Sending a session
        here would attach an identity to the request for no benefit.
        """
        st, ct, body = self._req("GET", BASE + path, auth=False,
                                 headers={"X-Requested-With": "XMLHttpRequest",
                                          "Accept": "application/json",
                                          "X-Static-Version": self.static_version()})
        if "json" not in ct:
            raise HHError(f"{path} did not negotiate to JSON (HTTP {st}, {ct})")
        return json.loads(body.decode("utf-8", "replace"))

    def json_page(self, path: str) -> dict:
        """Any hh route as JSON.

        The three search headers are not a search API: they are site-wide content
        negotiation. /resume/{id}, /vacancy/{id}, /applicant/negotiations,
        /applicant/profile/me, /applicant/favorite_vacancies and /applicant/settings
        all return their page state as JSON. A few routes 406 or 404; those are the
        exception, not the rule.
        """
        st, ct, body = self._req("GET", BASE + path, auth=True,
                                 headers={"X-Requested-With": "XMLHttpRequest",
                                          "Accept": "application/json",
                                          "X-Static-Version": self.static_version()})
        if "json" not in ct:
            raise HHError(f"{path} did not negotiate to JSON (HTTP {st}, {ct})")
        return json.loads(body.decode("utf-8", "replace"))


# ========================================================================= cli

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="hh.ru client, no browser required")
    sub = ap.add_subparsers(dest="cmd", required=True)

    se = sub.add_parser("session", help="save a login session (paste a cookie, or lift one from Chrome)")
    se.add_argument("--cookie", metavar="STR",
                    help="cookie string to use, e.g. 'hhtoken=...; _xsrf=...'. "
                         "Use - to read it from stdin. No browser needed.")
    se.add_argument("--from-chrome", action="store_true",
                    help="instead, lift cookies from a Chrome on --cdp (needs playwright)")
    se.add_argument("--cdp", default="http://127.0.0.1:9222",
                    help="Chrome debugging endpoint for --from-chrome")

    sub.add_parser("whoami", help="which hh account this session belongs to")
    sub.add_parser("resume-stats", help="per-CV funnel: shown / opened / invited")
    sub.add_parser("favorites", help="vacancies saved with hh's star")

    s = sub.add_parser("search"); s.add_argument("text")
    s.add_argument("--pages", type=int, default=3); s.add_argument("--remote", action="store_true")
    s.add_argument("--order-by", choices=["relevance", "publication_time", "salary_desc", "salary_asc"],
                   help="hh sort order; publication_time = newest first (first ~2-3 rows may "
                        "still be pinned/promoted postings that ignore the sort)")
    s.add_argument("--period", type=int, metavar="DAYS",
                   help="search_period in days, e.g. 1 for postings from today")
    # Payment shape. hh names these clusters compensation_mode/compensation_frequency
    # but the QUERY params it emits are salary_mode/salary_frequency (verified against
    # searchClusters 2026-09-04). SERVICE + PER_PROJECT are the piece-rate filters:
    # they find employers who pay per job rather than per month.
    s.add_argument("--pay-per", choices=["MONTH", "SHIFT", "HOUR", "SERVICE", "FLY_IN_FLY_OUT"],
                   help="what the pay is PER. SERVICE = «За услугу», i.e. per job. "
                        "Implies only_with_salary: alone, hh reads it as 'no salary stated'")
    s.add_argument("--pay-every", choices=["DAILY", "TWICE_PER_MONTH", "WEEKLY",
                                           "MONTHLY", "PER_PROJECT"],
                   help="how OFTEN it is paid. PER_PROJECT = «За проект»")
    s.add_argument("--employment-form", choices=["FULL", "PART", "PROJECT", "FLY_IN_FLY_OUT"],
                   help="PROJECT is hh's «Подработка». NB hh derives this from your text "
                        "too: searching «подработка» selects PROJECT on its own")
    s.add_argument("--gph", action="store_true",
                   help="accept_temporary: «Оформление по ГПХ или по совместительству», "
                        "the civil-law-contract gigs")
    s.add_argument("--field", choices=["name", "company_name", "description"],
                   help="restrict matching to one field. All three are searched by "
                        "default, so this only ever NARROWS: --field name is title-only")

    hn = sub.add_parser("hunt",
                        help="AI roles at small employers, ranked for a remote pitch")
    hn.add_argument("queries", nargs="+", help="one or more search queries")
    hn.add_argument("--pages", type=int, default=2)
    hn.add_argument("--format", default=None,
                    help="comma list: remote,hybrid,on_site (default: all)")
    hn.add_argument("--limit", type=int, default=15)
    hn.add_argument("--no-size", action="store_true",
                    help="skip the employer open-role lookup (faster, no size gate)")

    v = sub.add_parser("vacancy"); v.add_argument("id")

    ct = sub.add_parser("contacts", help="recruiter's direct fio/phone/email from the "
                                          "vacancy page, when hh ships it (often empty)")
    ct.add_argument("id")

    sm = sub.add_parser("similar",
                        help="recommended/similar vacancies shown below a posting")
    sm.add_argument("id", help="vacancy id to find lookalikes of")
    sm.add_argument("--limit", type=int, default=20)

    em = sub.add_parser("employer", help="employer dossier: identity, verification gaps, open roles")
    em.add_argument("id", nargs="?", help="employer id")
    em.add_argument("--vacancy", help="resolve the employer from a vacancy id instead")
    em.add_argument("--vacancies", action="store_true", help="also list every open posting")
    em.add_argument("--pages", type=int, default=10, help="pages of postings to read with --vacancies (100 each)")
    em.add_argument("--unique", action="store_true",
                    help="collapse duplicate postings by title: one row per distinct role, "
                         "with the clone count. Flood-posters list the same job dozens of times")
    em.add_argument("--json", action="store_true")

    ar = sub.add_parser("archive", help="freeze postings to disk: raw html, json state, text")
    ar.add_argument("ids", nargs="*", help="vacancy ids")
    ar.add_argument("--employer", help="archive every open posting for this employer")
    ar.add_argument("--dest", default="archive", help="destination directory")

    ai = sub.add_parser("archive-index",
                        help="citable index of an archive dir: title, url, capture time, hash")
    ai.add_argument("--dest", default="archive", help="archive directory to index")
    ai.add_argument("--markdown", action="store_true", help="emit a markdown table")

    r = sub.add_parser("resume"); r.add_argument("id")

    rx = sub.add_parser("resume-exp",
                        help="find/replace text inside a resume's experience, structure-safe")
    rx.add_argument("--resume", required=True, help="resume HASH, not the numeric id")
    rx.add_argument("--find", required=True)
    rx.add_argument("--replace", required=True)
    rx.add_argument("--yes", action="store_true", help="required to write; without it, dry run")

    rd = sub.add_parser("resume-exp-dates",
                        help="set the start/end date of an existing experience entry")
    rd.add_argument("--resume", required=True,
                    help="resume HASH, or 'all' for every CV on the account")
    rd.add_argument("--position", required=True,
                    help="substring of the job title; must match exactly one entry")
    rd.add_argument("--start", default=None, help="YYYY-MM-DD")
    rd.add_argument("--end", default=None,
                    help="YYYY-MM-DD, or empty string to reopen as current")
    rd.add_argument("--yes", action="store_true", help="required to write; without it, dry run")

    ra = sub.add_parser("resume-exp-add",
                        help="append new experience entries (preserves existing), structure-safe")
    ra.add_argument("--resume", required=True, help="resume HASH, not the numeric id")
    ra.add_argument("--file", required=True,
                    help="JSON list of {company,position,start,end,description}")
    ra.add_argument("--yes", action="store_true", help="required to write; without it, dry run")

    sub.add_parser("resumes", help="all CVs on the account, with their hashes")
    sub.add_parser("activity", help="account activity gauge (Ваша активность); keep >= 80%%")
    sub.add_parser("recommended", help="hh's personalized vacancy feed (your landing page)")

    cp = sub.add_parser("cv-push", help="apply a CV variant JSON to a live resume")
    cp.add_argument("--resume", required=True, help="resume HASH, not the numeric id")
    cp.add_argument("--file", required=True, help="variant JSON")
    cp.add_argument("--dry-run", action="store_true")
    cp.add_argument("--yes", action="store_true", help="required to write")

    n = sub.add_parser("sent", help="every application with its state")
    n.add_argument("--state", help="filter: RESPONSE | INTERVIEW | DISCARD")
    n.add_argument("--unread-only", action="store_true",
                   help="only those the employer has not opened yet")

    sub.add_parser("lost", help="negotiations hh dropped from `sent` entirely "
                                "(usually a rejection); chat survives, `sent` does not see it")

    ib = sub.add_parser("inbox", help="employer replies and anything awaiting you")
    ib.add_argument("--all", action="store_true", help="include rejections")

    ch = sub.add_parser("chats", help="every messenger chat, including employer-initiated ones")
    ch.add_argument("--direct-only", action="store_true",
                    help="only chats with no application behind them (invisible to `sent`)")

    cr = sub.add_parser("chat", help="read one chat by id, newest last")
    cr.add_argument("chat_id")
    cr.add_argument("--last", type=int, default=0, help="only the last N messages")

    cs = sub.add_parser("chat-send", help="post a new message into a chat")
    cs.add_argument("chat_id")
    cs.add_argument("--file", required=True, help="file holding the message text")
    cs.add_argument("--yes", action="store_true", help="actually send; without it, dry run")

    tr = sub.add_parser("trash", help="archive negotiations (move to hh trash) by vacancy or chat id")
    tr.add_argument("ids", nargs="+", help="vacancy id(s) or chat id(s) to archive")

    cle = sub.add_parser("chat-leave",
                         help="LEAVE chats (remove from messenger). hh has no chat archive; "
                              "this is the only way to clear a dead thread. NOT reversible")
    cle.add_argument("chat_ids", nargs="+", help="chat id(s) from `chats`")
    cle.add_argument("--yes", action="store_true", help="required: leaving cannot be undone")

    lt = sub.add_parser("letter", help="show or replace a sent cover letter")
    lt.add_argument("vacancy")
    lt.add_argument("--set", metavar="FILE", help="replace with the contents of FILE")
    lt.add_argument("--force", action="store_true",
                    help="rewrite even if the employer has already read it")
    lt.add_argument("--yes", action="store_true", help="required to write")

    a = sub.add_parser("apply")
    a.add_argument("--vacancy", required=True); a.add_argument("--resume", required=True)
    a.add_argument("--letter", default="")
    a.add_argument("--letter-file", help="read the cover letter from a file (multi-line)")
    a.add_argument("--yes", action="store_true",
        help="required: applications cannot be unsent")

    args = ap.parse_args()

    if args.cmd == "session":
        if args.from_chrome:
            sess = Session.from_chrome(args.cdp)
        elif args.cookie:
            raw = sys.stdin.read() if args.cookie == "-" else args.cookie
            sess = Session.from_cookie_string(raw)
        else:
            # Default to the paste path: it needs no browser automation, no
            # debugging port and no playwright, and one cookie is enough.
            print("Paste your hh.ru cookie, then Ctrl-D.\n"
                  "  DevTools -> Application -> Cookies -> https://hh.ru\n"
                  f"  Need: {Session.REQUIRED_COOKIE} (to read), "
                  f"plus {Session.WRITE_COOKIE} (to write).\n"
                  "  A whole 'Cookie:' header pasted verbatim is fine.\n"
                  "  (--from-chrome lifts it automatically instead.)\n", file=sys.stderr)
            sess = Session.from_cookie_string(sys.stdin.read())
        sess.save()
        print(f"saved {len(sess.cookies)} cookie(s) -> {SESSION_FILE}")
        print(f"  reads: yes    writes: {'yes' if sess.can_write() else 'NO (no _xsrf)'}")
        return

    sess = Session.load()
    hh = HH(session=sess)

    if args.cmd == "whoami":
        w = hh.whoami()
        print(f"  {w.get('name') or '?'}  <{w.get('email') or '?'}>")
        print(f"  hhid {w.get('hhid')}  |  {w.get('resume_count')} resumes  |  "
              f"writes: {'yes' if w.get('can_write') else 'NO (no _xsrf cookie)'}")
        return

    if args.cmd == "resume-stats":
        rows = hh.resume_stats()
        print(f"{'CV':40}{'shown':>8}{'opened':>8}{'invited':>9}{'open rate':>11}")
        for r in sorted(rows, key=lambda x: -(x.get("search_shows") or 0)):
            rate = f"{r['open_rate']}%" if r.get("open_rate") is not None else "-"
            print(f"  {(r['title'] or '?')[:38]:38}{r.get('search_shows') or 0:>8}"
                  f"{r.get('views') or 0:>8}{r.get('invitations') or 0:>9}{rate:>11}")
        return

    if args.cmd == "favorites":
        rows = hh.favorites()
        for v in rows:
            print(f"  {(v['company'] or '?')[:24]:26} {(v['name'] or '')[:44]:46} {v['vacancy_id']}")
        total = rows[0].get("hh_total") if rows else 0
        print(f"\n  {len(rows)} shown" + (f" (hh reports {total}; its paging is broken here)"
                                          if isinstance(total, int) and total > len(rows) else ""))
        return

    if args.cmd == "search":
        f = {"schedule": "remote"} if args.remote else {}
        if args.order_by:
            f["order_by"] = args.order_by
        if args.period:
            f["search_period"] = args.period
        if args.pay_per:
            # Without only_with_salary hh reads salary_mode as "no salary stated" and
            # hands back 150k noCompensation rows. Verified 2026-09-04. Always pair them.
            f["salary_mode"] = args.pay_per
            f["only_with_salary"] = "true"
        if args.pay_every:
            f["salary_frequency"] = args.pay_every
        if args.employment_form:
            f["employment_form"] = args.employment_form
        if args.gph:
            f["accept_temporary"] = "true"
        if args.field:
            f["search_field"] = args.field
        applied = hh.applied_vacancy_ids()
        for v in hh.search(args.text, pages=args.pages, **f):
            sal = f"{v['salary_from'] or ''}-{v['salary_to'] or ''}".strip("-") or "не указана"
            pub = (v.get("published") or "")[:16].replace("T", " ")
            mark = "  [ALREADY APPLIED]" if v["id"] in applied else ""
            print(f"{v['id']:>10}  {pub:<16}  {(v['name'] or '')[:52]:<54} "
                  f"{(v['company'] or '')[:20]:<22} {sal}{mark}")
    elif args.cmd == "hunt":
        fmts = ([f.strip().upper().replace("-", "_") for f in args.format.split(",")]
                if args.format else None)
        rows = hh.hunt(args.queries, pages=args.pages, formats=fmts,
                       limit=args.limit, size=not args.no_size)
        FMT = {"REMOTE": "удал", "HYBRID": "гибрид", "ON_SITE": "офис"}
        print(f"  {len(rows)} roles at small employers (hh relevance order)\n")
        for v in rows:
            sal = f"{v['salary_from'] or ''}-{v['salary_to'] or ''}".strip("-") or "з/п н/у"
            fm = "/".join(FMT.get(x, x) for x in (v.get("work_format") or [])) or "?"
            resp = v.get("responses")
            comp = f"{resp} откл" if resp is not None else ""
            openr = v.get("open_roles")
            size = f"{openr} ролей" if openr is not None else ""
            print(f"{v['id']:>10}  {(v['name'] or '')[:42]:<44} "
                  f"{(v['company'] or '')[:18]:<20} {fm:<12} "
                  f"{(v.get('area') or '')[:12]:<14} {sal:<14} {comp:<9} {size}")
    elif args.cmd == "vacancy":
        d = hh.vacancy(args.id)
        money = (f"{d['salary_from'] or ''}-{d['salary_to'] or ''} {d['currency'] or ''}"
                 f"{' gross' if d.get('gross') else ' net' if d.get('gross') is False else ''}".strip("- ").strip()
                 if (d.get('salary_from') or d.get('salary_to')) else "не указана")
        print(f"{d['name']} | {d['company']} | опыт: {d['experience']} | з/п: {money}")
        print(f"skills: {', '.join(d['skills'] or []) or '-'}\n")
        print((d["description"] or "")[:1500])
    elif args.cmd == "contacts":
        c = hh.contact_info(args.id)
        if not (c["fio"] or c["phone"] or c["email"]):
            print("no contactInfo on this posting (common when a screening bot runs it)")
        else:
            print(f"fio: {c['fio'] or '-'}\nphone: {c['phone'] or '-'}"
                  f"  (call_tracking={c['call_tracking']})\nemail: {c['email'] or '-'}")
    elif args.cmd == "similar":
        res = hh.similar_vacancies(args.id, limit=args.limit)
        applied = hh.applied_vacancy_ids()
        print(f"{res['count']} similar to vacancy {args.id} (type={res['type']})\n")
        for v in res["vacancies"]:
            money = (f"{v['salary_from'] or ''}-{v['salary_to'] or ''} {v['currency'] or ''}".strip()
                     if (v['salary_from'] or v['salary_to']) else "")
            mark = "  [APPLIED]" if v["id"] in applied else ""
            print(f"  {v['id']:>10}  {(v['company'] or '?')[:24]:<26}"
                  f"{(v['name'] or '')[:44]:<46}{money}{mark}")
    elif args.cmd == "employer":
        eid = args.id
        if not eid and args.vacancy:
            eid = hh.employer_id_of(args.vacancy)
            if not eid:
                print(f"no employer on vacancy {args.vacancy}"); return
        if not eid:
            print("give an employer id, or --vacancy <id> to resolve one"); return
        d = hh.employer(eid)
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
        else:
            print(f"{d['name']}  [{d['id']}]  {d['url']}")
            print(f"  адрес:     {d['address'] or '-'}")
            print(f"  сайт:      {d['site'] or '-'}")
            print(f"  отрасли:   {', '.join(d['industries'] or []) or '-'}")
            print(f"  ESIA:      {'да' if d['esia_identified'] else 'НЕТ'}"
                  f"   IT-аккредитация: {'да' if d['accredited_it'] else 'нет'}")
            print(f"  вакансий:  {d['active_vacancies']}")
            if d["description"]:
                print(f"  описание:  {d['description'][:300]}")
            for f in d["flags"]:
                print(f"  ! {f}")
        if args.vacancies:
            print()
            vs = hh.employer_vacancies(d["id"], pages=args.pages)
            if args.unique:
                # One row per distinct title, keeping the newest posting as the
                # representative. A high-volume employer lists the same role
                # 20x, so the raw list says nothing about what is actually open.
                by_title: dict[str, list[dict]] = {}
                for v in vs:
                    by_title.setdefault((v["name"] or "").strip().lower(), []).append(v)
                rows = sorted(by_title.values(), key=lambda g: -len(g))
                print(f"  {len(vs)} postings -> {len(rows)} distinct roles")
                for g in rows:
                    v = max(g, key=lambda x: x.get("published") or "")
                    sal = f"{v['salary_from'] or ''}-{v['salary_to'] or ''}".strip("-") or "не указана"
                    print(f"{v['id']:>10}  x{len(g):<4} {(v['name'] or '')[:56]:<58} "
                          f"{(v['area'] or '')[:18]:<20} {sal}")
            else:
                for v in vs:
                    sal = f"{v['salary_from'] or ''}-{v['salary_to'] or ''}".strip("-") or "не указана"
                    print(f"{v['id']:>10}  {(v['name'] or '')[:56]:<58} {(v['area'] or '')[:18]:<20} {sal}")
    elif args.cmd == "archive":
        ids = list(args.ids)
        if args.employer:
            ids += [v["id"] for v in hh.employer_vacancies(args.employer)]
        if not ids:
            print("nothing to archive: give vacancy ids or --employer <id>"); return
        for vid in dict.fromkeys(ids):
            try:
                r = hh.archive_vacancy(vid, Path(args.dest))
                print(f"{r['id']:>10}  {r['bytes']:>7}b  {r['sha256'][:16]}  "
                      f"{(r['name'] or '')[:44]}")
            except Exception as e:
                print(f"{vid:>10}  FAILED: {e}")
        print(f"\n-> {args.dest}/")
    elif args.cmd == "archive-index":
        rows = hh.archive_index(Path(args.dest))
        if not rows:
            print(f"no archived postings in {args.dest}/"); return
        if args.markdown:
            print("| id | posting | employer | captured | sha256 |")
            print("|---|---|---|---|---|")
            for r in rows:
                print(f"| [`{r['id']}`]({r['url']}) | {r['name'] or '?'} | "
                      f"{r['company'] or '?'} | {(r['captured'] or '')[:10]} | `{r['sha256']}` |")
        else:
            for r in rows:
                print(f"{r['id']:>10}  {r['url']:<34}  {(r['name'] or '')[:44]}")
        print(f"\n{len(rows)} archived", file=sys.stderr)
    elif args.cmd == "resume":
        print(json.dumps({k: v for k, v in hh.resume(args.id).items() if k != "raw"},
                         ensure_ascii=False, indent=2))
    elif args.cmd == "resume-exp":
        res = hh.experience_edit(args.resume, args.find, args.replace, dry_run=not args.yes)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if not args.yes:
            print("\ndry run: re-run with --yes to write", file=sys.stderr)
    elif args.cmd == "resume-exp-dates":
        # resumes() keys the hash as "hash"; there is no "id". The numeric id
        # that negotiations report is a different identifier and returns an
        # empty resume, which is the trap the KB warns about.
        targets = ([r["hash"] for r in hh.resumes()] if args.resume == "all"
                   else [args.resume])
        for rid in targets:
            try:
                out = hh.experience_dates(rid, args.position, start=args.start,
                                          end=args.end, dry_run=not args.yes)
            except HHError as e:
                print(f"  {rid[:12]}  SKIP: {e}", file=sys.stderr)
                continue
            tag = "DRY" if out["dry_run"] else ("OK" if out.get("verified") else "CHECK")
            print(f"  {tag:5} {rid[:12]}  {out['before']['position'][:44]}")
            print(f"        {out['before']['startDate']} -> {out['before']['endDate']}"
                  f"   becomes   {out['after']['startDate']} -> {out['after']['endDate']}")
        if not args.yes:
            print("\n  dry run. add --yes to write.")

    elif args.cmd == "resume-exp-add":
        entries = json.loads(Path(args.file).read_text())
        res = hh.experience_add(args.resume, entries, dry_run=not args.yes)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if not args.yes:
            print("\ndry run: re-run with --yes to write", file=sys.stderr)
    elif args.cmd == "activity":
        a = hh.activity_score()
        s = a.get("score")
        chg = f" ({a['change']:+d} recently)" if a.get("change") else ""
        flag = "" if s is None else ("  BELOW hh's 80% target" if s < 80 else "  ok")
        print(f"  activity: {s}%{chg}{flag}")

    elif args.cmd == "recommended":
        FMT = {"REMOTE": "удал", "HYBRID": "гибрид", "ON_SITE": "офис"}
        rows = hh.recommended()
        print(f"  {len(rows)} personalized recommendations (hh, based on your resume)\n")
        for v in rows:
            sal = f"{v['salary_from'] or ''}-{v['salary_to'] or ''}".strip("-") or "з/п н/у"
            fm = "/".join(FMT.get(x, x) for x in (v.get("work_format") or [])) or "?"
            resp = v.get("responses")
            comp = f"{resp} откл" if resp is not None else ""
            tag = " [APPLIED]" if v.get("applied") else (" [DM]" if v.get("inbox") else "")
            print(f"{v['id']:>10}  {(v['name'] or '')[:38]:<40} "
                  f"{(v['company'] or '')[:16]:<18} {fm:<14} "
                  f"{(v.get('area') or '')[:12]:<14} {sal:<14} {comp:<9}{tag}")

    elif args.cmd == "resumes":
        for r in hh.resumes():
            if r.get("error"):
                print(f"  {r['hash']}  ERROR {r['error']}")
                continue
            # _unwrap gives a dict when a salary is set and an empty LIST when it
            # is not, so this cannot assume a mapping.
            sal = r["salary"] if isinstance(r["salary"], dict) else {}
            money = f"{sal.get('amount')} {sal.get('currency')}" if sal else "не указана"
            pct = r.get("completeness_percent")
            pctstr = f"{pct}%" if pct is not None else "?"
            ren = " ↑auto" if r.get("auto_renewal") else ""
            print(f"  {r['hash']}  {(r['title'] or '?')[:34]:<36} {money:>13}  "
                  f"skills={r['skills']:<3} exp={r['experience_months'] or 0}м  "
                  f"заполн={pctstr}{ren}")

    elif args.cmd == "cv-push":
        variant = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if args.dry_run:
            print(json.dumps(hh.resume_push(args.resume, variant, dry_run=True),
                             ensure_ascii=False, indent=2))
        elif not args.yes:
            sys.exit("refusing without --yes: this rewrites your live CV")
        else:
            res = hh.resume_push(args.resume, variant)
            print(f"HTTP {res['status']} verified={res['verified']}")
            for k, v in (res.get("fields") or {}).items():
                print(f"  {k}: {json.dumps(v, ensure_ascii=False)[:100]}")

    elif args.cmd == "sent":
        everything = hh.negotiations()
        rows = everything
        if args.state:
            rows = [t for t in rows if (t["state"] or "") == args.state.upper()]
        if args.unread_only:
            rows = [t for t in rows if not t["viewed_by_employer"]]
        print(f"  {'state':<10} {'seen':<5} {'letter':<7} {'msgs':<5} {'chat_id':<10} "
              f"{'company':<22} {'vacancy':<40} url")
        print("  " + "-" * 130)
        for t in sorted(rows, key=lambda x: (x["state"] or "", x["company"] or "")):
            vid = t.get("vacancy_id")
            url = f"{BASE}/vacancy/{vid}" if vid else ""
            print(f"  {t['state'] or '?':<10} {'yes' if t['viewed_by_employer'] else 'no':<5} "
                  f"{'yes' if t['has_letter'] else 'NONE':<7} {str(t['messages'] or 0):<5} "
                  f"{str(t.get('chat_id') or ''):<10} "
                  f"{(t['company'] or '?')[:21]:<22} {(t['name'] or '')[:40]:<40} {url}")
        counts = hh.counters(everything)
        print(f"\n  {len(rows)} shown | all: " +
              ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    elif args.cmd == "lost":
        rows = hh.lost_negotiations()
        if not rows:
            print("  nothing missing: every negotiation-linked chat is still listed")
            print("  NOT the same as \"no rejections\" -- a fresh DISCARD sits in "
                  "negotiations() for a while before hh drops it. Check `sent --state "
                  "DISCARD` too, or just use `inbox --all` which covers both.",
                  file=sys.stderr)
            return
        print(f"  {'chat_id':<12} {'vacancy_id':<11} {'last':<11} company | vacancy")
        for c in rows:
            print(f"  {c['chat_id']:<12} {c['vacancy_id']:<11} {(c['last_time'] or '')[:10]:<11} "
                  f"{c['company'] or '?'} | {c['vacancy'] or ''}")
        print(f"\n  {len(rows)} negotiations hh dropped from `sent`/`negotiations` "
              f"entirely (usually a rejection); `chat <id>` for the full thread. "
              f"This misses a rejection that just landed and hasn't been dropped yet -- "
              f"check `sent --state DISCARD` too, or use `inbox --all` which covers both.",
              file=sys.stderr)

    elif args.cmd == "inbox":
        for c in hh.inbox_view():
            if c["state"] == "DISCARD" and not args.all:
                continue
            print("=" * 78)
            flag = f"  <-- {c['flag']}" if c["flag"] else ""
            print(f"[{c['state']}] {c['company']} | {(c['vacancy'] or '')[:44]}"
                  f"  [vacancy {c['vacancy_id']} | chat {c['chat_id']}]{flag}")
            who = c["last_author"] or "работодатель"
            print(f"  {who}{' (bot)' if c['last_is_bot'] else ''}, {(c['last_time'] or '')[:16]}")
            print("    " + c["last_text"][:900].replace("\n", "\n    "))
            if c["url"]:
                print(f"  {c['url']}")
        print("\n  (only the latest message per chat; `chat <id>` for the full thread)",
              file=sys.stderr)

    elif args.cmd == "chats":
        rows = hh.chats()
        if args.direct_only:
            rows = [c for c in rows if c["is_direct"]]
        print(f"  {'chat':12s} {'kind':14s} {'unread':>6s} {'last':17s} "
              f"{'who':18s} company | vacancy")
        for c in rows:
            kind = "DIRECT/" + (c["subtype"] or "?") if c["is_direct"] else "negotiation"
            who = ("you" if c["last_mine"] else (c["last_author"] or "?"))[:16]
            vac = f"  vac={c['vacancy_id']}" if c.get("vacancy_id") else ""
            print(f"  {c['chat_id']:<12} {kind:14s} {c['unread']:>6} "
                  f"{(c['last_time'] or '')[:16]:17s} {who:18s} "
                  f"{(c['company'] or '?')[:22]} | {(c['vacancy'] or '')[:34]}{vac}")
        direct = sum(1 for c in rows if c["is_direct"])
        print(f"\n  {len(rows)} chats | {direct} employer-initiated "
              f"(these never appear in `sent`)")

    elif args.cmd == "chat":
        c = hh.chat(args.chat_id)
        msgs = c["messages"][-args.last:] if args.last else c["messages"]
        emp = f" | employer={c['employer_id']}" if c.get("employer_id") else ""
        print(f"  chat {args.chat_id} | {len(c['messages'])} messages | "
              f"unread={c['unread']} | write_allowed={c['write_allowed']}{emp}")
        for m in msgs:
            who = "you" if m["mine"] else (m["author"] or "?")
            bot = " (bot)" if m["is_bot"] else ""
            print("-" * 78)
            print(f"  [{(m['time'] or '')[:16]}] {who}{bot}")
            print("    " + (m["text"] or "").replace("\n", "\n    "))

    elif args.cmd == "chat-send":
        text = Path(args.file).read_text(encoding="utf-8").strip()
        if not text:
            sys.exit("refusing to send an empty message")
        cur = hh.chat(args.chat_id)
        if not cur["write_allowed"]:
            sys.exit("this chat does not accept messages (write_allowed is false)")
        last = cur["messages"][-1] if cur["messages"] else {}
        print(f"  chat {args.chat_id} | {len(cur['messages'])} messages | "
              f"last from {'you' if last.get('mine') else last.get('author')}")
        print("  --- sending ---")
        print("  " + text.replace("\n", "\n  "))
        if not args.yes:
            print("\n  DRY RUN. re-run with --yes to send.")
        else:
            r = hh.send_message(args.chat_id, text)
            print(f"\n  sent: HTTP {r['status']} | verified={r['verified']} "
                  f"| message_id={r['message_id']}")
            if r.get("note"):
                print(f"  note: {r['note']}")

    elif args.cmd == "trash":
        for key in args.ids:
            try:
                r = hh.trash_negotiation(key)
                print(f"  archived {key}: topic {r['topic']}, vacancy {r['vacancy_id']}")
            except HHError as e:
                print(f"  {key}: {e}")

    elif args.cmd == "chat-leave":
        if not args.yes:
            print("  DRY RUN. chat-leave removes the chat from your messenger and "
                  "CANNOT be undone. Re-run with --yes.")
            for cid in args.chat_ids:
                print(f"  would leave chat {cid}")
            return
        left = []
        for cid in args.chat_ids:
            try:
                r = hh.leave_chat(cid)
                left.append(str(cid))
                print(f"  left chat {cid}: HTTP {r['status']}")
            except HHError as e:
                print(f"  {cid}: {e}")
        if left:
            # Verify once for the whole batch: the left chats should be gone.
            remaining = {str(c["chat_id"]) for c in hh.chats()}
            for cid in left:
                print(f"  verify {cid}: {'GONE (ok)' if cid not in remaining else 'STILL LISTED'}")

    elif args.cmd == "letter":
        if not args.set:
            cur = hh.letter(args.vacancy)
            if not cur:
                sys.exit("no cover letter found for that application")
            print(f"{cur['company']} | {cur['vacancy']}")
            print(f"state={cur['state']} read_by_employer={cur['read_by_employer']} "
                  f"editable={cur['can_edit']}\n")
            print(cur["text"])
        else:
            if not args.yes:
                sys.exit("refusing without --yes: this rewrites what an employer sees")
            text = Path(args.set).read_text(encoding="utf-8").strip()
            if hh.refuse_dashes and any(c in text for c in "—–"):
                sys.exit("refusing: text contains an em/en dash (HH_REFUSE_DASHES is on)")
            print(hh.set_letter(args.vacancy, text, only_if_unread=not args.force))
    elif args.cmd == "apply":
        if not args.yes:
            sys.exit("refusing without --yes: an application cannot be withdrawn cleanly")
        letter = args.letter
        if args.letter_file:
            letter = Path(args.letter_file).read_text(encoding="utf-8").strip()
        if any(c in letter for c in "—–"):
            sys.exit("refusing: letter contains an em/en dash")
        if hh.applied_to(args.vacancy):
            sys.exit(f"already applied to {args.vacancy}")
        res = hh.apply(args.vacancy, args.resume, letter)
        if res["sent"]:
            print(f"  applied to {args.vacancy}: HTTP {res['status']} | "
                  f"verified={res.get('verified')}")
        else:
            print(f"  NOT applied to {args.vacancy}: HTTP {res['status']} | "
                  f"{res.get('reason', '?')}\n      {res.get('detail', '')}")


if __name__ == "__main__":
    try:
        _cli()
    except SessionExpired as e:
        sys.exit(f"session dead: {e}")
    except HHError as e:
        # These are refusals and hh-side rejections, not crashes. A traceback here
        # reads as a bug in the tool when it is usually the tool doing its job.
        sys.exit(f"refused: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
