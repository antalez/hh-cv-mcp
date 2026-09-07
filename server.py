#!/usr/bin/env python3
"""hh-cv — an MCP server exposing the full read/write surface onto one hh.ru
account (search, resumes, applying, chat, archiving) over plain HTTP.

THE OFFICIAL API IS GONE; THE SITE ITSELF IS NOT
-------------------------------------------------
hh.ru discontinued the applicant API on 2025-12-15. As of 2026-08-10, /vacancies
and /employers return an application-level {"errors":[{"type":"forbidden"}]} from
every origin tested (datacenter, residential, and in-country IPs alike) — an auth
boundary hh enforces at its own backend, not IP reputation or geography.
Only /dictionaries, /areas, /professional_roles and /suggests still answer there.

But hh's own website content-negotiates to JSON for a client holding a captured
login session, so everything the site does, this does too, over plain HTTP: no
browser, no token. That protocol lives in hh_client.py (see its module docstring
for the verified quirks) and is exposed here as TOOLS via _hh() — search, resume
read/write, apply, chat read/send, trash, archive.

SELF-CONTAINED, ON PURPOSE
--------------------------
This folder is the whole product: server.py + hh_client.py, no third-party
dependencies, nothing imported from outside it. Copy mcp/ anywhere, point
HH_SESSION at a captured cookie jar (or drop .hh_session.json beside it), and
every tool works. It holds no personal data: the grounding, the claims ledger
and any owner-specific health checks are deliberately kept OUT of this package,
because they read one person's files.

AN OPEN CAPABILITY LAYER, ALSO ON PURPOSE
-----------------------------------------
Every write tool here executes for real the moment it's called: no confirm
argument, no dry run, no vetting of the text. That is not this layer's job.
Deciding whether a write should happen at all, and routing that decision to a
human, belongs to whatever agent wraps this server: it should suspend every
write for a human to approve before the call reaches here. Point a client straight at this server
and you get the raw capability with no safety net; that is the deal with a
reusable tool surface, and the reason `apply` and `chat_send` say so loudly in
their own descriptions.

Transport: JSON-RPC 2.0 over stdio.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Nothing outside this folder is imported, on purpose: hh_client.py sits beside
# this file and Python puts the script's own directory on sys.path, so a copy of
# mcp/ runs anywhere. Reaching up to the repo root for a shared helper is exactly
# what made this unpublishable before; if you need one, vendor it in here.


# ------------------------------------------------------- applications and chat

def _hh():
    from hh_client import HH, Session
    sess = Session.load()
    if not sess:
        raise ToolError("session_missing", "no saved hh.ru session", _SESSION_FIX)
    return HH(session=sess, verbose=False)


def t_sent(args):
    """Every application with its real state, across all pages."""
    rows = _hh().negotiations()
    if args.get("state"):
        rows = [t for t in rows if (t["state"] or "") == args["state"].upper()]
    if args.get("unread_only"):
        rows = [t for t in rows if not t["viewed_by_employer"]]
    if not rows:
        return "nothing matches"
    out = []
    for t in sorted(rows, key=lambda x: (x["state"] or "", x["company"] or "")):
        out.append(f"{t['state'] or '?':<10} seen={'y' if t['viewed_by_employer'] else 'n'} "
                   f"letter={'y' if t['has_letter'] else 'NONE':<4} "
                   f"{(t['company'] or '?')[:22]:<24} {(t['name'] or '')[:44]}  [{t['vacancy_id']}]")
    return "\n".join(out) + f"\n\n{len(rows)} shown"


def t_lost(args):
    """Negotiations hh dropped from `sent`/negotiations() entirely, usually a rejection.

    hh eventually drops a topic that resolves to DISCARD -- verified 2026-08-30,
    absent from every page even though the chat and the rejection text survive
    in the messenger backend. Same class of blind spot as `chats(direct_only)`:
    a whole bucket of real applications invisible to the negotiation-derived
    view unless you diff against chats().

    Correction, verified 2026-08-31: the drop is not instant. A rejection that
    just landed sits in negotiations() with state DISCARD for a while (at least
    tens of minutes) before hh purges it -- this tool misses that window
    entirely, since it only catches ones already gone. An empty result here is
    NOT the same as "no rejections". Use `inbox` (which reads negotiations()
    state directly and catches DISCARD immediately) or `sent` with
    state=DISCARD for the freshest ones.
    """
    rows = _hh().lost_negotiations()
    if not rows:
        return ("nothing missing: every negotiation-linked chat is still listed. "
                "NOT the same as \"no rejections\" -- a fresh DISCARD sits in "
                "negotiations() for a while before hh drops it. Check `sent` with "
                "state=DISCARD too, or use `inbox` with include_rejected=true, "
                "which covers both cases.")
    out = [f"{(c['company'] or '?')[:24]:<26} {(c['vacancy'] or '')[:40]:<40} "
           f"chat={c['chat_id']}  {(c['last_time'] or '')[:10]}" for c in rows]
    return ("\n".join(out) + f"\n\n{len(rows)} negotiations gone; chat_read for the full thread. "
            "This misses a rejection that just landed and hasn't been dropped yet -- "
            "check `sent` with state=DISCARD too, or use `inbox` with include_rejected=true.")


def t_inbox(args):
    """Everything awaiting a reply. One cheap list; read a single chat for detail.

    Measured at 49 seconds before this rewrite, which is unusable as an agent's
    first tool call. The cost was a per-chat read to find out who spoke last, 22
    of them at 1.3s each. `chats()` already returns the last message, its author
    and its time for every chat in one paged call, so the loop was buying data it
    already had. Now: negotiations for state, chats for who-spoke-last, joined on
    chat_id. Two calls, about 9 seconds.

    The deeper fix is the shape, not the seconds. A list tool should be cheap and
    a detail tool expensive, so the agent pays for depth only where it decides
    depth is warranted. `chat_read` is the detail tool and costs 1.3s for the one
    conversation that turned out to matter.

    Classification (INTERVIEW/RESPONSE/DIRECT/UNLISTED) lives in
    `HH.inbox_view()`, shared with the CLI's `inbox` command -- kept here once
    so the two surfaces can't drift the way they did until 2026-08-30 (the CLI
    had no UNLISTED case at all and was silently blind to every rejection).
    """
    hh = _hh()
    out = []
    for c in hh.inbox_view():
        if c["state"] == "DISCARD" and not args.get("include_rejected"):
            continue
        flag = f"   <-- {c['flag']}" if c["flag"] else ""
        out.append(
            f"[{c['state']}] {c['company']} | {(c['vacancy'] or '')[:46]}  "
            f"[vacancy {c['vacancy_id']} | chat {c['chat_id']}]{flag}\n"
            f"  {c['last_author'] or 'работодатель'}"
            f"{' (bot)' if c['last_is_bot'] else ''}, {(c['last_time'] or '')[:16]}\n    "
            + c["last_text"][:700].replace("\n", "\n    "))
    return ("\n\n".join(out) + "\n\nOnly the latest message per chat is shown. "
            "Use chat_read for the full thread of one conversation."
            ) if out else "nothing awaiting a reply"


def t_chats(args):
    """Every messenger chat, including employer-initiated ones with no application.

    `sent`/`inbox` are built on /applicant/negotiations, which only knows chats of
    type NEGOTIATION. Employers writing to you first create type COMMON (subType
    GENAI = hh's AI recruiter), which those views cannot see at all.
    """
    rows = _hh().chats()
    if args.get("direct_only"):
        rows = [c for c in rows if c["is_direct"]]
    if not rows:
        return "no chats"
    out = []
    for c in rows:
        kind = f"DIRECT/{c['subtype'] or '?'}" if c["is_direct"] else "negotiation"
        who = "you" if c["last_mine"] else (c["last_author"] or "?")
        vac = f" [vacancy {c['vacancy_id']}]" if c.get("vacancy_id") else ""
        out.append(f"{kind:<18} unread={c['unread']} {(c['last_time'] or '')[:16]} "
                   f"{who[:16]:<17} {(c['company'] or '?')[:22]:<24} "
                   f"{(c['vacancy'] or '')[:40]}  [chat {c['chat_id']}]{vac}")
    direct = sum(1 for c in rows if c["is_direct"])
    return "\n".join(out) + f"\n\n{len(rows)} chats | {direct} employer-initiated"


def t_whoami(args):
    """Which hh account this session is authenticated as. Call it first when
    anything looks wrong: the usual cause is a stale or borrowed session."""
    return json.dumps(_hh().whoami(), ensure_ascii=False, indent=2)


def t_resume_scorecard(args):
    """hh's own verdict on every CV, next to how each one actually performs.

    Pulls together three things hh keeps in separate places: the funnel
    (impressions / opens / invitations), `fieldStatuses.leftToFillFields` (hh's
    literal checklist of what is still empty), and the canonical-vs-freetext
    split of the skills. Read this before rewriting a CV on instinct."""
    hh = _hh()
    stats = {s["hash"]: s for s in hh.resume_stats()}
    out = []
    for row in hh.resumes():
        if row.get("error"):
            continue
        r = hh.resume(row["hash"])
        st = stats.get(row["hash"]) or {}
        rate = f"{st.get('open_rate')}%" if st.get("open_rate") is not None else "-"
        out.append(f"=== {r.get('title')}")
        out.append(f"  funnel 7d:   shown {st.get('search_shows')}, opened {st.get('views')}, "
                   f"invited {st.get('invitations')}, open rate {rate}")
        out.append(f"  completeness {r.get('completeness_percent')}%  "
                   f"searchable={r.get('is_searchable')}  visibility={r.get('access_type')}")
        left = r.get("left_to_fill") or []
        out.append(f"  hh says still empty ({len(left)}): {', '.join(left) or 'nothing'}")
        out.append(f"  skills indexed by hh ({len(r.get('skills_canonical') or [])}): "
                   f"{', '.join((r.get('skills_canonical') or [])[:10])}")
        out.append(f"  skills as free text ({len(r.get('skills_freetext') or [])}): "
                   f"{', '.join((r.get('skills_freetext') or [])[:10])}")
        langs = ", ".join(f"{l['name']} {l['level']}" for l in (r.get("languages") or []))
        out.append(f"  languages:   {langs or 'none'}")
        exp = r.get("experience") or []
        out.append(f"  experience:  {len(exp)} entries, "
                   f"{sum(len(e.get('description') or '') for e in exp)} chars total")
        out.append("")
    return ("\n".join(out) +
            "\nskills hh indexes are its common vocabulary; free-text ones still carry "
            "ids, so this is a strong hint rather than proof of a search filter. "
            "Identical experience across CVs means employers read the same document "
            "whatever the title promised.")


def t_resume_bump(args):
    """Raise a CV in employer search by refreshing its date (hh's «Обновить дату»).

    There is no bump endpoint: an ordinary save IS the bump. Saving moves the
    CV's `updated`, pushes `nextTouchAt` to +4h and flips `canTouch` false,
    which is hh accounting the edit as having spent the touch. It does not
    re-moderate and the CV stays searchable.

    Skips by default when hh says it is too soon. Saves inside the 4h window
    still move `updated` but the ranking budget is already spent, so hammering
    buys nothing. Note a REAL CV edit spends the same budget: batch-edit a CV
    and this will correctly report the bump as unavailable.

    Omit `resume` to bump every CV that is currently eligible."""
    hh = _hh()
    targets = ([r for r in hh.resumes() if not r.get("error")]
               if not args.get("resume")
               else [{"hash": str(args["resume"]), "title": str(args["resume"])[:10]}])
    out, bumped = [], 0
    for r in targets:
        try:
            res = hh.resume_touch(r["hash"], force=bool(args.get("force")))
        except Exception as e:                        # noqa: BLE001
            out.append(f"  FAIL {r.get('title')}: {type(e).__name__}: {e}")
            continue
        if res.get("touched"):
            bumped += 1
            out.append(f"  BUMPED {r.get('title')}")
        else:
            out.append(f"  skip   {r.get('title')}: {res.get('reason')}")
    return "\n".join(out) + f"\n\n{bumped} of {len(targets)} bumped."


def t_view_vacancy(args):
    """Register an authenticated VIEW of a vacancy, which is what raises the
    account activity gauge (+2% per view, measured).

    `activity` reports a low score and previously offered no way to act on it.
    This is the action. It is a real interaction recorded on your account, not
    a read: hh counts it, and viewing many vacancies quickly is exactly the
    pattern that looks automated. Use it deliberately, a few at a time."""
    hh = _hh()
    ids = args.get("vacancy_ids") or ([args["vacancy_id"]] if args.get("vacancy_id") else [])
    if not ids:
        raise ToolError("bad_argument", "no vacancy ids given",
                        "Pass vacancy_id, or vacancy_ids as a list.")
    done = []
    for vid in [str(i) for i in ids][:20]:
        try:
            hh.view(vid)
            done.append(vid)
        except Exception as e:                        # noqa: BLE001
            done.append(f"{vid}(failed: {type(e).__name__})")
    a = hh.activity_score()
    return (f"viewed {len(done)}: {', '.join(done)}\n"
            f"activity now: {a.get('score')}%  (hh advises keeping it >= 80)")


def t_resume_advice(args):
    """hh's OWN AI critique of a CV: what to strengthen, what dilutes it.

    Two steps by design. Pass start=true once to create the task, wait a few
    seconds, then call again without it to read the result. A plain read never
    starts work on hh's side."""
    hh = _hh()
    rid = str(args.get("resume") or "")
    if not rid:
        raise ToolError("bad_argument", "resume hash required",
                        "Pass `resume` (38-char hash) from the resumes tool.")
    if args.get("start"):
        r = hh.resume_advice(rid, start=True)
        return (f"task {r.get('status', '?')} (taskId {r.get('taskId')}). "
                f"Wait ~5s then call again without start to read the advice.")
    d = hh.resume_advice(rid)
    if not d["advices"]:
        return d["hint"]
    out = []
    for a in d["advices"]:
        out.append(f"  [{a['section']}] {a['advice']}")
    return "\n".join(out) + f"\n\n{len(d['advices'])} points from hh's own model."


def t_resume_views(args):
    """WHICH employers opened a CV, by name and date. `resume_stats` gives the
    count; this gives the names, so a follow-up can be aimed at a company that
    has already read you. Pass a resume hash, or omit it for every CV."""
    hh = _hh()
    targets = ([r for r in hh.resumes() if not r.get("error")]
               if not args.get("resume")
               else [{"hash": str(args["resume"]), "title": str(args["resume"])[:10]}])
    out, total = [], 0
    for r in targets:
        rows = [v for v in hh.resume_views(r["hash"]) if v.get("opened")]
        total += len(rows)
        if not rows:
            continue
        out.append(f"{r['title']}  ({len(rows)} opens)")
        for v in rows[:int(args.get("limit") or 15)]:
            out.append(f"  {(v.get('at') or v.get('date') or '?'):18} "
                       f"{(v.get('company') or '?')[:40]:42} employer {v.get('employer_id')}")
    if not out:
        return "no employer has opened these CVs yet"
    return ("\n".join(out) + f"\n\n{total} genuine opens. Rows where hh marks the CV as "
            "only shown, not read, are filtered out.")


def t_suitable_vacancies(args):
    """hh's own matcher: vacancies it considers suited to your CVs. Distinct from
    `recommended`, which is the landing-page feed."""
    rows = _hh().suitable_vacancies()
    if not rows:
        return "hh returned no suitable vacancies"
    out = [f"  {(v['company'] or '?')[:24]:26} {(v['name'] or '')[:44]:46} "
           f"{'remote' if v['remote'] else '':7} {v['vacancy_id']}" for v in rows]
    return ("\n".join(out) +
            f"\n\n{len(rows)} shown of {rows[0].get('total_found')} hh matched.")



def t_favorites(args):
    """Vacancies saved with hh's star. hh serves only the first page of these."""
    rows = _hh().favorites()
    if not rows:
        return "nothing saved"
    total = rows[0].get("hh_total")
    out = [f"  {(v['company'] or '?')[:24]:26} {(v['name'] or '')[:44]:46} {v['vacancy_id']}"
           for v in rows]
    tail = ""
    if isinstance(total, int) and total > len(rows):
        tail = (f"\n\nhh reports {total} saved but serves only these {len(rows)}: "
                f"its ?page= parameter is ignored on this route, so the rest are "
                f"not reachable over HTTP.")
    return "\n".join(out) + f"\n\n{len(rows)} shown{tail}"


def t_resumes(args):
    """Every CV on the account. The hash is what every other call needs."""
    out = []
    for r in _hh().resumes():
        if r.get("error"):
            out.append(f"{r['hash']}  ERROR {r['error']}")
            continue
        sal = r["salary"] if isinstance(r["salary"], dict) else {}
        money = f"{sal.get('amount')} {sal.get('currency')}" if sal else "не указана"
        pct = r.get("completeness_percent")
        pctstr = f"заполнено={pct}%" if pct is not None else ""
        ren = " auto-renew" if r.get("auto_renewal") else ""
        exp = f" exp={r['experience_months']}м" if r.get("experience_months") else ""
        out.append(f"{r['hash']}  {(r['title'] or '?')[:34]:<36} {money:>13}  "
                   f"skills={r['skills']}{exp}  {pctstr}{ren}")
    return "\n".join(out)


def t_cv_push(args):
    """Write a CV variant to a live resume. Executes immediately on call -- this
    MCP layer has no confirm/dry-run gate; that's the calling agent's job."""
    hh = _hh()
    variant = args.get("variant") or {}
    if not variant:
        raise ToolError("bad_argument", "variant is empty",
                        "Pass a variant object with at least one writable field.")
    res = hh.resume_push(args["resume"], variant)
    return (f"HTTP {res['status']} verified={res['verified']}\n" +
            "\n".join(f"  {k}: {json.dumps(v, ensure_ascii=False)[:120]}"
                      for k, v in (res.get("fields") or {}).items()))


def t_resume_read(args):
    """One resume's full editable content as data, so you never script hh.resume() again."""
    r = _hh().resume(str(args["resume"]))
    r.pop("raw", None)
    return json.dumps(r, ensure_ascii=False, indent=2)


def t_resume_experience(args):
    """Find/replace inside a resume's experience, preserving entry structure.
    Executes immediately on call -- this MCP layer has no confirm gate."""
    hh = _hh()
    rid, find, replace = str(args["resume"]), args["find"], args["replace"]
    res = hh.experience_edit(rid, find, replace, dry_run=False)
    return json.dumps(res, ensure_ascii=False, indent=2)


def t_resume_exp_add(args):
    """Append new experience entries to a resume, preserving existing ones.
    Executes immediately on call -- this MCP layer has no confirm gate."""
    hh = _hh()
    rid = str(args["resume"])
    entries = args["entries"]
    if isinstance(entries, str):
        entries = json.loads(entries)
    res = hh.experience_add(rid, entries, dry_run=False)
    return json.dumps(res, ensure_ascii=False, indent=2)


def t_resume_exp_dates(args):
    """Set the start/end date of an existing experience entry, on one resume or
    on "all". Executes immediately on call -- this MCP layer has no confirm gate."""
    hh = _hh()
    pos = str(args["position"])
    start, end = args.get("start"), args.get("end")
    targets = ([r["hash"] for r in hh.resumes()] if str(args["resume"]) == "all"
               else [str(args["resume"])])
    out = []
    for rid in targets:
        try:
            out.append(hh.experience_dates(rid, pos, start=start, end=end,
                                           dry_run=False))
        except Exception as e:                       # noqa: BLE001
            out.append({"resume": rid, "error": str(e)})
    return json.dumps(out, ensure_ascii=False, indent=2)


def t_letter_get(args):
    cur = _hh().letter(str(args["vacancy_id"]))
    if not cur:
        return "no cover letter on that application"
    return (f"{cur['company']} | {cur['vacancy']}\n"
            f"state={cur['state']} read_by_employer={cur['read_by_employer']} "
            f"editable={cur['can_edit']}\n\n{cur['text']}")


def t_letter_set(args):
    """Rewrite an already-sent cover letter. Writes on call and does not vet the
    text; refusing once the employer has read it is hh's own rule, not a gate."""
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolError("bad_argument", "text is empty",
                        "Pass the replacement letter as `text`.")
    return str(_hh().set_letter(str(args["vacancy_id"]), text,
                                only_if_unread=not args.get("force")))


def t_chat_read(args):
    """One chat, newest last. This is the read you reach for constantly."""
    hh = _hh()
    c = hh.chat(str(args["chat_id"]))
    msgs = c["messages"]
    if args.get("last"):
        msgs = msgs[-int(args["last"]):]
    emp = f" | employer {c['employer_id']}" if c.get("employer_id") else ""
    head = (f"chat {args['chat_id']} | {len(c['messages'])} messages | "
            f"unread={c['unread']} | write_allowed={c['write_allowed']}{emp}")
    body = []
    for m in msgs:
        who = "you" if m["mine"] else (m["author"] or "?")
        body.append(f"[{(m['time'] or '')[:16]}] {who}{' (bot)' if m['is_bot'] else ''}\n"
                    + "  " + (m["text"] or "").replace("\n", "\n  "))
    return head + "\n\n" + "\n\n".join(body)


def t_search(args):
    """Vacancy search over HTTP. No Chrome, no auth."""
    hh = _hh()
    filters = {}
    if args.get("remote"):
        filters["schedule"] = "remote"
    if args.get("order_by"):
        filters["order_by"] = str(args["order_by"])
    if args.get("period"):
        filters["search_period"] = int(args["period"])
    # a city NAME or an hh area id; hh_client resolves names against the tree
    if args.get("area"):
        filters["area"] = str(args["area"])
    # hh's own filter vocabulary, harvested from searchClusters. `label` is a
    # list because hh accepts repeats: label=low_performance&label=accredited_it
    if args.get("label"):
        lab = args["label"]
        filters["label"] = [lab] if isinstance(lab, str) else list(lab)
    # search_field DOES work; the earlier note here said hh ignored it, which was
    # a bad test (a query whose words also appear in titles returns the same rows
    # either way). hh searches name + company_name + description by DEFAULT, so
    # this parameter can only ever narrow: `search_field=name` is title-only and
    # returns 0 for a phrase that lives in bodies. Re-verified 2026-09-04 against
    # searchClusters, which lists all three ids as selectedValues.
    if args.get("gph"):
        filters["accept_temporary"] = "true"
    for key in ("experience", "work_format", "education", "employment_form",
                "excluded_text", "salary_mode", "salary_frequency", "search_field"):
        if args.get(key):
            v = args[key]
            filters[key] = v if isinstance(v, (list, tuple)) else str(v)
    # salary_mode is a TRAP on its own: hh reads a bare salary_mode as "no salary
    # stated" and returns 150k noCompensation rows. With only_with_salary it filters
    # correctly (SERVICE -> 1361 genuine per-service postings). Verified 2026-09-04.
    if filters.get("salary_mode"):
        filters["only_with_salary"] = "true"
    if args.get("salary"):
        filters["salary"] = int(args["salary"])
        filters["only_with_salary"] = "true"
    rows = hh.search(str(args["text"]), pages=int(args.get("pages") or 2), **filters)
    if not rows:
        return "no results"
    applied = hh.applied_vacancy_ids()
    out = []
    for v in rows[:int(args.get("limit") or 40)]:
        vid = str(v.get("vacancy_id") or v.get("id") or "")
        # rows are FLAT (salary_from/salary_to); this used to read v["salary"] as a
        # dict, which never existed, so search printed no pay at all until 2026-09-04.
        lo, hi = v.get("salary_from"), v.get("salary_to")
        money = f"{lo or ''}-{hi or ''}".strip("-") or "не указана"
        # piece-rate roles: from/to are normalised to a month, so also show what the
        # employer actually wrote (9000-12000 за смену behind a 135000-257148 range)
        mode = v.get("pay_mode")
        if mode and mode != "MONTH":
            u_lo, u_hi = v.get("per_unit_from"), v.get("per_unit_to")
            unit = f"{u_lo or ''}-{u_hi or ''}".strip("-")
            if unit:
                money += f" ({unit}/{mode.lower()})"
        pub = (v.get("published") or "")[:16].replace("T", " ")
        mark = " [ALREADY APPLIED]" if vid in applied else ""
        out.append(f"{vid:>10}  {pub:<16}  {(v.get('company') or '?')[:24]:<26} "
                   f"{(v.get('name') or '')[:46]:<48} {money}{mark}")
    note = ("\n\nnote: with order_by=publication_time the first 2-3 rows may still be "
            "pinned/promoted postings out of date order; the rest is a true newest-first sort."
            if filters.get("order_by") == "publication_time" else "")
    return "\n".join(out) + f"\n\n{len(rows)} found, {len(out)} shown{note}"


def t_similar(args):
    """hh's "Похожие вакансии" block: recommended opportunities shown below a posting."""
    hh = _hh()
    res = hh.similar_vacancies(str(args["vacancy_id"]), limit=int(args.get("limit") or 20))
    rows = res["vacancies"]
    if not rows:
        return "no similar vacancies"
    applied = hh.applied_vacancy_ids()
    out = []
    for v in rows:
        money = (f"{v['salary_from'] or ''}-{v['salary_to'] or ''} {v['currency'] or ''}".strip()
                 if (v["salary_from"] or v["salary_to"]) else "")
        mark = "  [ALREADY APPLIED]" if v["id"] in applied else ""
        out.append(f"{v['id']:>10}  {(v['company'] or '?')[:24]:<26}"
                   f"{(v['name'] or '')[:46]:<48}{money}{mark}")
    return (f"{res['count']} recommended, similar to vacancy {res['source_vacancy']}:\n\n"
            + "\n".join(out))


def t_employer(args):
    """Who is actually behind a posting, and what hh did or did not verify."""
    hh = _hh()
    eid = args.get("employer_id")
    if not eid and args.get("vacancy_id"):
        eid = hh.employer_id_of(str(args["vacancy_id"]))
        if not eid:
            return f"no employer on vacancy {args['vacancy_id']}"
    if not eid:
        return "give employer_id, or vacancy_id to resolve one"
    d = hh.employer(eid)
    out = [f"{d['name']}  [{d['id']}]  {d['url']}",
           f"  address:    {d['address'] or '-'}",
           f"  site:       {d['site'] or '-'}",
           f"  industries: {', '.join(d['industries'] or []) or '-'}",
           f"  ESIA-verified: {d['esia_identified']}   IT-accredited: {d['accredited_it']}",
           f"  open vacancies: {d['active_vacancies']}"]
    if d["description"]:
        out.append(f"  description: {d['description'][:400]}")
    out += [f"  ! {f}" for f in d["flags"]]
    if args.get("vacancies"):
        out.append("")
        for v in hh.employer_vacancies(d["id"]):
            sal = f"{v['salary_from'] or ''}-{v['salary_to'] or ''}".strip("-") or "-"
            out.append(f"{v['id']:>10}  {(v['name'] or '')[:56]:<58} "
                       f"{(v['area'] or '')[:18]:<20} {sal}")
    return "\n".join(out)


def t_archive(args):
    """Freeze postings to disk before they get pulled. Local write, sends nothing."""
    hh = _hh()
    ids = [str(i) for i in (args.get("vacancy_ids") or [])]
    if args.get("employer_id"):
        ids += [v["id"] for v in hh.employer_vacancies(str(args["employer_id"]))]
    if not ids:
        return "nothing to archive: give vacancy_ids or employer_id"
    dest = Path(args.get("dest") or "archive")
    out = []
    for vid in dict.fromkeys(ids):
        try:
            r = hh.archive_vacancy(vid, dest)
            out.append(f"{r['id']:>10}  {r['bytes']:>7}b  sha256:{r['sha256'][:16]}  "
                       f"{(r['name'] or '')[:44]}")
        except Exception as e:
            out.append(f"{vid:>10}  FAILED: {e}")
    return "\n".join(out) + f"\n\n-> {dest}/"


def t_archive_index(args):
    """Citable index of an archive directory: title, url, capture time, hash."""
    from hh_client import HH
    rows = HH.archive_index(Path(args.get("dest") or "archive"))
    if not rows:
        return f"no archived postings in {args.get('dest') or 'archive'}/"
    out = ["| id | posting | employer | captured | sha256 |", "|---|---|---|---|---|"]
    out += [f"| [`{r['id']}`]({r['url']}) | {r['name'] or '?'} | {r['company'] or '?'} | "
            f"{(r['captured'] or '')[:10]} | `{r['sha256']}` |" for r in rows]
    return "\n".join(out)


def t_vacancy(args):
    """One vacancy in full, including the description. HTTP, no browser."""
    v = _hh().vacancy(str(args["vacancy_id"]))
    head = [f"{k}: {json.dumps(v[k], ensure_ascii=False)[:180]}"
            for k in ("name", "company", "salary", "area", "experience", "schedule",
                      "employment", "url", "archived") if k in v and v[k] is not None]
    desc = (v.get("description") or "").strip()
    return "\n".join(head) + f"\n\n--- description ({len(desc)} chars) ---\n{desc}"


def t_thread_read_state(args):
    """Which employers have READ your last message and still not replied.

    Silence after being read is a decision; silence before being read is a
    queue. The inbox shows both as "no reply". This separates them, which is
    what decides whether a follow-up is worth sending at all."""
    rows = _hh().thread_read_state()
    if not rows:
        return ("no live applications yet, so there is nothing to follow up. "
                "Apply to something first, or check `lost` if you have archived "
                "everything.")
    read = [r for r in rows if r["employer_read"] is True]
    unread = [r for r in rows if r["employer_read"] is False]
    def fmt(rs):
        return "\n".join(f"  [{r['state']:9}] {(r['company'] or '?')[:26]:28} "
                          f"{(r['vacancy'] or '')[:34]}  chat={r['chat_id']}" for r in rs)
    return (f"READ, no reply ({len(read)}) - they saw it and chose not to answer; a "
            f"follow-up adds little unless the thread is at INTERVIEW:\n{fmt(read)}\n\n"
            f"NOT READ ({len(unread)}) - your message is sitting unopened, which is a "
            f"queue problem, not a rejection. A nudge or a direct call actually adds "
            f"information here:\n{fmt(unread)}")



def t_contacts(args):
    """Recruiter name and direct phone. One vacancy, or every live application.

    Pass `vacancy_id` for one posting. OMIT it to sweep every live application
    at once, sorted real-numbers-first and interviews-first, which is what you
    want when applications are not converting and you need to route around the
    response queue. The sweep costs one call per application, so it is slow.

    `phone_state` matters and is not flattened: NONE is a real mobile,
    CREATED or PENDING is an hh call-tracking proxy that still connects but is
    logged and can expire. Many employers publish no contact at all; an empty
    result is normal rather than a failure.
    """
    hh = _hh()
    vid = args.get("vacancy_id")
    if vid:
        c = hh.contact_info(str(vid))
        if not (c.get("fio") or c.get("phone")):
            return f"vacancy {vid}: no contact published"
        ph = (c.get("phones") or [{}])[0]
        return (f"vacancy {vid}\n  {c.get('fio') or '?'}  "
                f"{c.get('phone') or '(no number)'}"
                f"  [{ph.get('virtual_phone_state')}]"
                + (f"\n  email: {c['email']}" if c.get("email") else ""))

    live = [n for n in hh.negotiations() if (n.get("state") or "") != "DISCARD"]
    rows, checked = [], 0
    for n in live:
        v = n.get("vacancy_id")
        if not v:
            continue
        checked += 1
        try:
            c = hh.contact_info(v)
        except Exception:                             # noqa: BLE001
            continue
        if not (c.get("fio") or c.get("phone")):
            continue
        ph = (c.get("phones") or [{}])[0]
        rows.append((ph.get("virtual_phone_state") != "NONE",
                     n.get("state") != "INTERVIEW", n, c, ph))
    if not rows:
        return f"none of {checked} live applications publish a recruiter contact"
    rows.sort(key=lambda r: (r[0], r[1]))
    out = []
    for _p, _i, n, c, ph in rows:
        out.append(f"[{n.get('state')}] {(n.get('company') or '?')[:26]} | "
                   f"{(n.get('name') or '')[:40]}")
        out.append(f"    {c.get('fio') or '?'}  {c.get('phone') or '(no number)'}  "
                   f"[{ph.get('virtual_phone_state')}]  vacancy {n.get('vacancy_id')}")
    return ("\n".join(out) +
            f"\n\n{len(rows)} of {checked} live applications publish a contact. "
            "phone_state NONE is a real mobile; CREATED/PENDING is an hh proxy.")



def t_apply(args):
    """Apply to a vacancy. Sends on call -- this MCP layer has no confirm gate.

    The checks kept here are about calling hh correctly, not about whether the
    application should be sent: dedupe against hh itself, and validate that the
    resume is a real hash on this account. Deciding an application is warranted,
    and reviewing the letter, belong to the calling agent.

    For applying in bulk, wrap this in your own rate-limited loop with a daily
    cap; this tool is a single deliberate call.
    """
    hh = _hh()
    vid = str(args["vacancy_id"])
    letter = (args.get("letter") or "").strip()

    if hh.applied_to(vid):
        return f"ALREADY APPLIED to {vid}. Nothing sent."

    resumes = {r["hash"]: r for r in hh.resumes() if not r.get("error")}
    rid = str(args.get("resume") or "")
    if rid not in resumes:
        listing = "\n".join(f"  {h}  {(r.get('title') or '?')[:48]}"
                            for h, r in resumes.items())
        # The identifier trap: negotiations report a numeric resumeId, every write
        # needs the 38-char hash, and the numeric one fails as an empty resume.
        return (f"resume {rid!r} is not a hash on this account. Use one of:\n{listing}")

    v = hh.vacancy(vid)
    head = (f"vacancy {vid}: {v.get('name')} @ {v.get('company')}\n"
            f"resume:  {resumes[rid].get('title')}\n"
            f"letter:  {letter[:400] if letter else '(none)'}")

    result = hh.apply(vid, rid, letter)
    if result["sent"]:
        return (f"{head}\n\nsent: HTTP {result['status']} | "
                f"verified={result.get('verified')}")
    return (f"{head}\n\nNOT SENT: HTTP {result['status']} | "
            f"{result.get('reason', '?')}\n{result.get('detail', '')}")


def t_hunt(args):
    """AI/LLM roles at small employers, ranked for a remote pitch. Mechanical: no
    LLM, just hh search filtered down to shops where remote is convincible."""
    hh = _hh()
    queries = args.get("queries") or []
    if isinstance(queries, str):
        queries = [queries]
    if not queries:
        return 'hunt needs queries, e.g. ["AI агент LLM", "RAG инженер"]'
    fmts = args.get("formats")
    if isinstance(fmts, str):
        fmts = [f for f in fmts.replace(",", " ").split() if f]
    fmts = [f.upper().replace("-", "_") for f in fmts] if fmts else None
    # stop_titles=[] must DISABLE filtering, so distinguish "absent" from "empty"
    stop = args.get("stop_titles")
    rows = hh.hunt(queries, pages=int(args.get("pages") or 2), formats=fmts,
                   limit=int(args.get("limit") or 15), size=not args.get("no_size"),
                   stop_titles=None if stop is None else list(stop))
    if not rows:
        return "no small-employer roles matched"
    FMT = {"REMOTE": "удал", "HYBRID": "гибрид", "ON_SITE": "офис"}
    out = []
    for v in rows:
        sal = f"{v.get('salary_from') or ''}-{v.get('salary_to') or ''}".strip("-") or "з/п н/у"
        fm = "/".join(FMT.get(x, x) for x in (v.get("work_format") or [])) or "?"
        resp = v.get("responses")
        openr = v.get("open_roles")
        out.append(f"{v['id']:>10}  {(v.get('name') or '')[:40]:<42} "
                   f"{(v.get('company') or '')[:18]:<20} {fm:<12} "
                   f"{(v.get('area') or '')[:12]:<14} {sal:<14} "
                   f"{(str(resp) + ' откл') if resp is not None else '':<9} "
                   f"{(str(openr) + ' ролей') if openr is not None else ''}")
    return "\n".join(out) + f"\n\n{len(rows)} roles at small employers (hh relevance order)"


def t_trash(args):
    """Archive (hide) negotiations by vacancy id or chat id. Moves them to hh's
    trash bucket, so it is reversible. Cleanup, never employer-facing."""
    hh = _hh()
    ids = args.get("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    if not ids:
        return "trash needs one or more vacancy ids or chat ids"
    out = []
    for key in ids:
        try:
            r = hh.trash_negotiation(str(key))
            out.append(f"  archived {key}: topic {r['topic']}, vacancy {r['vacancy_id']}")
        except Exception as e:
            out.append(f"  {key}: {e}")
    return "\n".join(out)


def t_activity(args):
    """Account activity gauge ('Ваша активность' on hh.ru/). hh's own advice: keep >= 80%."""
    a = _hh().activity_score()
    s = a.get("score")
    if s is None:
        return "could not read the activity score"
    chg = f" ({a['change']:+d} recently)" if a.get("change") else ""
    flag = "BELOW hh's 80% target" if s < 80 else "ok"
    return f"activity: {s}%{chg}  [{flag}]"


def t_recommended(args):
    """hh's personalized vacancy recommendations, the applicant landing-page feed."""
    rows = _hh().recommended()
    if not rows:
        return "no recommendations right now"
    FMT = {"REMOTE": "удал", "HYBRID": "гибрид", "ON_SITE": "офис"}
    out = []
    for v in rows:
        sal = f"{v['salary_from'] or ''}-{v['salary_to'] or ''}".strip("-") or "з/п н/у"
        fm = "/".join(FMT.get(x, x) for x in (v.get("work_format") or [])) or "?"
        resp = v.get("responses")
        comp = f"{resp} откл" if resp is not None else ""
        tag = " [APPLIED]" if v.get("applied") else (" [DM]" if v.get("inbox") else "")
        out.append(f"{v['id']}  {(v['name'] or '')[:40]:<42} "
                   f"{(v['company'] or '')[:18]:<20} {fm:<12} {sal:<14} {comp}{tag}")
    return "\n".join(out) + f"\n\n{len(rows)} personalized (based on your resume)"


def t_chat_send(args):
    """Post a new message into a chat. Sends on call -- this MCP layer has no
    confirm gate, and does not vet the text. Both belong to the calling agent."""
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolError("bad_argument", "text is empty",
                        "Pass the message to send as `text`.")
    hh = _hh()
    c = hh.chat(str(args["chat_id"]))
    if not c["write_allowed"]:
        raise ToolError("hh_refused",
                        "this chat does not accept messages (write_allowed is false)",
                        "hh has closed this thread. Nothing to do; pick another chat.")
    return str(hh.send_message(str(args["chat_id"]), text))


def t_chat_leave(args):
    """Leave chats, removing them from the messenger. Acts on call -- no gate,
    and NOT reversible. hh exposes no chat archive, so LEAVE_CHAT is the only way
    to clear a dead thread (a rejection) out of the chat list. Get ids from
    `chats`; do not point this at a live conversation."""
    ids = args.get("chat_ids") or args.get("chat_id") or []
    if isinstance(ids, (str, int)):
        ids = [ids]
    if not ids:
        raise ToolError("bad_argument", "no chat ids given",
                        "Pass one or more chat ids as `chat_ids` (from `chats`).")
    hh = _hh()
    out = []
    for cid in ids:
        try:
            r = hh.leave_chat(str(cid))
            out.append(f"  left chat {cid}: HTTP {r['status']}")
        except Exception as e:
            out.append(f"  {cid}: {e}")
    return "\n".join(out)


# ------------------------------------------------------------ failure contract
#
# One shape for every failure, because the caller is usually a model. Prose that
# varies per tool forces it to pattern-match English; a stable `kind` lets it
# branch, and `fix` tells a human exactly what to do. The expensive failure here
# is a dead session, so that one is deliberately loud and carries the command
# that repairs it rather than just naming the problem.

class ToolError(Exception):
    """A refusal a tool raises on purpose, with a machine-readable kind."""

    def __init__(self, kind: str, message: str, fix: str = ""):
        super().__init__(message)
        self.kind, self.message, self.fix = kind, message, fix


_SESSION_FIX = ("Get a fresh cookie: log into hh.ru in any browser, then "
                "DevTools -> Application -> Cookies -> https://hh.ru and copy "
                "`hhtoken` (plus `_xsrf` to write). Then run:\n"
                "  python3 hh_client.py session --cookie 'hhtoken=...; _xsrf=...'\n"
                "Confirm with: python3 hh_client.py whoami")


def describe_failure(exc: Exception) -> str:
    """Render any exception as the same structured, actionable error."""
    kind, fix = "error", ""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()

    if isinstance(exc, ToolError):
        kind, fix = exc.kind, exc.fix
    elif exc.__class__.__name__ == "SessionExpired":
        kind, fix = "session_expired", _SESSION_FIX
    elif isinstance(exc, KeyError):
        kind = "bad_argument"
        msg = f"missing required argument: {exc}"
        fix = "Check the tool's inputSchema and pass it."
    elif "no saved session" in low or "session" in low and "dead" in low:
        kind, fix = "session_missing", _SESSION_FIX
    elif "_xsrf" in low:
        kind = "no_write_permission"
        fix = ("This session can read but not write. Re-save it including the "
               "`_xsrf` cookie, then check `whoami` shows can_write: true.")
    elif "http 429" in low or "rate" in low and "limit" in low:
        kind = "rate_limited"
        fix = "Back off and retry later. hh throttles; one account at human pace."
    elif "not a hash on this account" in low or "not found" in low or "404" in low:
        kind, fix = "not_found", "Re-read the id from a list tool; ids are not interchangeable."
    elif "state blob not found" in low or "markup changed" in low:
        kind = "hh_changed"
        fix = ("hh changed its page shape. Run smoke.py to confirm, then the "
               "parser in hh_client.py needs updating.")
    elif isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        kind, fix = "network", "Check connectivity to hh.ru and retry."

    out = f"ERROR [{kind}]: {msg}"
    return f"{out}\n\nFIX: {fix}" if fix else out


# ------------------------------------------------------------- risk vocabulary
#
# MCP's tool annotations are how a server tells a client what a tool DOES to the
# world, so the client can gate it. Clients auto-approve readOnlyHint tools and
# put a confirmation in front of destructiveHint ones. That matters more here
# than in most servers: this layer has no gate of its own by design, so these
# hints are the only machine-readable warning a caller gets. A description
# saying "SENDS ON CALL" is for humans; this is for the client.
#
# They are hints, not enforcement. Nothing here stops a determined caller.
#
# WRITES that cannot be taken back once hh has them. `destructive` is read
# strictly as "may overwrite or destroy", so appending experience and archiving
# a negotiation (reversible, hh's own trash) are writes but not destructive.
_OVERWRITES = {"letter_set", "cv_push", "resume_experience", "resume_exp_dates"}
_SENDS_TO_EMPLOYER = {"apply", "chat_send"}
# Irreversible account changes that are not overwrites and reach no employer text,
# but destroy something on call: leaving a chat removes the thread from your
# messenger with no way back (hh offers no chat archive, only this).
_DESTROYS = {"chat_leave"}
_OTHER_WRITES = {"resume_exp_add", "archive_application", "snapshot_vacancy",
                 "resume_bump", "view_vacancy"} | _DESTROYS
WRITE_TOOLS = _OVERWRITES | _SENDS_TO_EMPLOYER | _OTHER_WRITES


def annotations_for(name: str) -> dict:
    """MCP tool annotations for one tool. See the note above."""
    read_only = name not in WRITE_TOOLS
    return {
        "title": name.replace("_", " "),
        "readOnlyHint": read_only,
        # Sending to an employer is not "overwriting" in the spec's sense, but it
        # is irreversible and reaches a third party, which is exactly the case a
        # confirmation step exists for. Flagged so clients treat it that way.
        "destructiveHint": (name in _OVERWRITES or name in _SENDS_TO_EMPLOYER
                            or name in _DESTROYS),
        # Same args twice leaves the same state: reads always, and `apply`
        # because it refuses a duplicate rather than applying again.
        "idempotentHint": read_only or name in ("apply", "cv_push", "letter_set",
                                                "resume_exp_dates", "archive_application"),
        # Everything here talks to hh.ru, an external system whose responses are
        # not under our control. `archive` also writes local files.
        "openWorldHint": True,
    }


# Which arguments a tool genuinely cannot run without. This used to be derived
# from a hardcoded whitelist of NAMES ("name", "text", "id", "vacancy_id"...),
# which got it wrong in both directions: chat_read and resume_read advertised no
# required arguments and then raised KeyError, chat_send demanded `text` but not
# `chat_id`, and `employer` claimed to require `vacancy_id` when employer_id
# alone is fine. An agent trusts this list; it has to match the function.
REQUIRED = {
    "apply":             ["vacancy_id", "resume"],
    "chat_read":         ["chat_id"],
    "chat_send":         ["chat_id", "text"],
    "chat_leave":        ["chat_ids"],
    "cv_push":           ["resume", "variant"],
    "letter_get":        ["vacancy_id"],
    "letter_set":        ["vacancy_id", "text"],
    "resume_advice":     ["resume"],
    "resume_exp_add":    ["resume", "entries"],
    "resume_exp_dates":  ["resume", "position"],
    "resume_experience": ["resume", "find", "replace"],
    "resume_read":       ["resume"],
    "search":            ["text"],
    "similar":           ["vacancy_id"],
    "snapshot_index":    [],
    "vacancy":           ["vacancy_id"],
    "archive_application": ["ids"],
    # employer accepts EITHER employer_id or vacancy_id, so neither is required
}

TOOLS = {
    "whoami":          (t_whoami, "WHICH HH ACCOUNT this server is authenticated as: name, email, hhid, resume count, and whether the session can write. Read-only and cheap. Call it first when anything looks wrong, and to confirm you are acting on the intended account.", {}),
    "resume_scorecard": (t_resume_scorecard, "ALL CVs AT ONCE: hh's verdict beside how each performs. The 7-day funnel "
                        "(impressions, opens, invitations, open rate), hh's own checklist of "
                        "still-empty fields, the canonical-vs-freetext skill split, languages and "
                        "body size. THE tool to read before rewriting anything. Use `resume_read` "
                        "for one CV's full editable content. COST: reads every CV, several seconds.", {}),
    "resume_bump":     (t_resume_bump, "RAISE A CV IN EMPLOYER SEARCH by refreshing its "
                                      "date (hh's «Обновить дату»). Skips when hh says the "
                                      "4-hour window is not up. Omit `resume` to bump every "
                                      "eligible CV. Note a real CV edit spends the same "
                                      "budget, so an edit and a bump compete. WRITES ON CALL.",
                        {"resume": {"type": "string"}, "force": {"type": "boolean"}}),
    "view_vacancy":    (t_view_vacancy, "REGISTER A VIEW of a vacancy, the action that "
                                       "raises the account activity gauge that `activity` "
                                       "reports (+2% per view). A real interaction recorded "
                                       "on your account, not a read, and viewing many at "
                                       "once looks automated. WRITES ON CALL.",
                        {"vacancy_id": {"type": "string"},
                         "vacancy_ids": {"type": "array", "items": {"type": "string"}}}),
    "resume_advice":   (t_resume_advice, "HH'S OWN AI CRITIQUE of one CV: which entries dilute it, what to strengthen. Free, and it is hh's opinion rather than ours, which makes it a good second view alongside `resume_scorecard`. Two steps: pass start=true once to run the analysis, wait a few seconds, then call again WITHOUT start to read it.",
                        {"resume": {"type": "string"}, "start": {"type": "boolean"}}),
    "resume_views":    (t_resume_views, "WHICH EMPLOYERS opened a CV, by company name and date. `resume_scorecard` "
                        "gives the COUNT of opens; this gives the NAMES, so a follow-up can target "
                        "a company that already read you. Omit `resume` to sweep every CV. COST: "
                        "one page fetch per CV when sweeping.",
                        {"resume": {"type": "string"}, "limit": {"type": "integer"}}),
    "suitable_vacancies": (t_suitable_vacancies, "HH'S CV MATCHER, no query needed: the dedicated matcher behind the "
                        "'подходящие вакансии' page, and it reports how many it found in total. "
                        "Broader than `recommended` (the small landing-page feed) and needs no "
                        "query unlike `search`. Start here when you want hh's opinion of what fits "
                        "the CV. COST: about 35 seconds.", {}),
    "favorites":       (t_favorites, "VACANCIES YOU STARRED on hh (Избранное): your own saved list, not a discovery tool like `search` or `suitable_vacancies`. hh serves only the first page and ignores paging, so the count says so when the list is truncated.", {}),
    "resumes":         (t_resumes, "LIST every CV with its 38-char hash, title and salary. The hash is what every other resume tool needs. Cheap. Use `resume_read` for one CV's full content, `resume_scorecard` for how they are performing.", {}),
    "cv_push":         (t_cv_push, "OVERWRITE CV HEADER FIELDS from a variant object: title, salary, about, skills, roles, employment_forms, work_formats, business_trips, travel_time. Does NOT touch experience entries: for those use `resume_experience` (text), `resume_exp_dates` (dates) or `resume_exp_add` (new entry). Writes on call.",
                        {"resume": {"type": "string"}, "variant": {"type": "object"}}),
    "resume_read":     (t_resume_read, "ONE CV'S FULL EDITABLE CONTENT as data: title, about, skills with levels, languages, roles, experience entries, plus hh's own left_to_fill checklist and its canonical-vs-freetext skill split. Read this before editing a CV. Use `resume_scorecard` instead for performance across all CVs.",
                        {"resume": {"type": "string"}}),
    "resume_experience": (t_resume_experience, "EDIT THE TEXT of existing experience entries by find/replace, preserving ids, dates, company and position. For DATES use `resume_exp_dates`; to ADD a new entry use `resume_exp_add`. Writes on call. Note each CV stores its own values, so a fix must be repeated per CV.",
                        {"resume": {"type": "string"}, "find": {"type": "string"},
                         "replace": {"type": "string"}}),
    "resume_exp_add":  (t_resume_exp_add, "ADD a NEW experience entry, preserving existing ones. To change text or dates on an entry that already exists use `resume_experience` or `resume_exp_dates` instead. Each entry: {company, position, start 'YYYY-MM-DD', end (or null for current), description}. Verified by read-back. Writes on call.",
                        {"resume": {"type": "string"}, "entries": {"type": "array"}}),
    "resume_exp_dates": (t_resume_exp_dates,
                        "CHANGE THE DATES of an existing experience entry, matched on a substring of the job title. For the DESCRIPTION text use `resume_experience`. resume='all' applies to every CV. Use this to close an open-ended job: an entry with no end date reads as a second current job. end='' reopens as current. Writes on call.",
                        {"resume": {"type": "string"}, "position": {"type": "string"},
                         "start": {"type": "string"}, "end": {"type": "string"}}),
    "sent":            (t_sent, "EVERY application you sent, with state (RESPONSE/INTERVIEW/DISCARD), "
                        "whether the employer opened it and whether it had a letter. The complete "
                        "list; use `inbox` for just the ones awaiting your reply. NOTE hh drops "
                        "resolved DISCARD topics from this entirely, so `lost` covers those. COST: "
                        "walks every page of negotiations, 20 per page.",
                        {"state": {"type": "string"}, "unread_only": {"type": "boolean"}}),
    "lost":            (t_lost, "THREADS MISSING FROM `sent` ENTIRELY, found by diffing the chat list "
                        "against every negotiation. TWO kinds land here and the name "
                        "undersells the second: rejections whose topic hh stopped listing, "
                        "AND applications YOU archived with `archive_application`. There is "
                        "no other way to read the archived bucket, because hh's "
                        "?filter=ARCHIVED is applied in the browser rather than the server. "
                        "So to answer 'which employers have I already approached?' you need "
                        "`sent` AND this, or your own archived applications look like fresh "
                        "leads. COST: diffs the whole chat list against every negotiation page.", {}),
    "inbox":           (t_inbox, "START HERE for 'what needs my attention': applications where the EMPLOYER "
                        "spoke last, plus employer-initiated chats, already classified. Use `sent` "
                        "for the full list of everything you sent regardless of who spoke last, "
                        "`lost` for ones hh dropped entirely, and `thread_read_state` to learn "
                        "whether your last message was even read. COST: about 10 seconds; it is "
                        "still the cheapest way to see the situation.",
                        {"include_rejected": {"type": "boolean"}}),
    "chats":           (t_chats, "EVERY messenger thread, including employer-initiated ones with NO application behind them (type COMMON), which are invisible to `sent` and `inbox`. Use direct_only=true for exactly those. This is the raw chat list; `inbox` is the curated to-do view over it.",
                        {"direct_only": {"type": "boolean"}}),
    "chat_read":       (t_chat_read, "Read one chat by id, newest last. Use last=N for the tail. chat_id comes from `inbox` or `chats` and is NOT a vacancy id: they are different numbers and passing a vacancy id fails.",
                        {"chat_id": {"type": "string"}, "last": {"type": "integer"}}),
    "chat_send":       (t_chat_send, "Post a new message into a chat. chat_id comes from `inbox` "
                                     "or `chats`, never a vacancy id. SENDS ON CALL: the message "
                                     "reaches the employer immediately, with no dry run and no "
                                     "vetting of the text here.",
                        {"chat_id": {"type": "string"}, "text": {"type": "string"}}),
    "search":          (t_search, "FIND VACANCIES BY QUERY, with hh's own filter vocabulary. `area` takes a "
                                  "city NAME (Москва, Алматы, Минск) or an id; hh.ru covers "
                                  "nine countries. label=[low_performance] is the COMPETITION "
                                  "filter, fewer than 10 applicants, which hh filters on even "
                                  "though it never shows the count. `salary_mode` is what the "
                                  "pay is PER (MONTH/HOUR/SHIFT/SERVICE), `salary_frequency` "
                                  "is how often. THE PIECE-RATE FILTERS: salary_frequency="
                                  "PER_PROJECT («За проект», ~2800 postings) works alone; "
                                  "salary_mode=SERVICE («За услугу», ~1360) is forced to pair "
                                  "with only_with_salary here because hh reads a bare "
                                  "salary_mode as 'no salary stated' and returns 150k empty "
                                  "rows. Results carry pay_mode + the per-unit amount. "
                                  "Plus experience, work_format, education, employment_form, "
                                  "gph (ГПХ contracts), search_field, excluded_text, "
                                  "a salary floor, and labels "
                                  "accredited_it / not_from_agency / with_salary. There is NO "
                                  "currency filter: hh ignores it. For hh's own suggestions "
                                  "use `recommended` or `suitable_vacancies`; for lookalikes "
                                  "of one posting use `similar`. NOTE hh's text matching is "
                                  "loose, so a query can return unrelated titles. "
                                  "COST: slow, roughly 30 to 40 seconds per page.",
                        {"text": {"type": "string"}, "remote": {"type": "boolean"},
                         "pages": {"type": "integer"}, "limit": {"type": "integer"},
                         "order_by": {"type": "string",
                                      "enum": ["relevance", "publication_time",
                                               "salary_desc", "salary_asc"]},
                         "area": {"type": "string",
                                  "description": "city or region: a NAME (Москва, Алматы, "
                                                 "Минск) or an hh area id"},
                         "label": {"type": "array", "items": {"type": "string"},
                                   "description": "hh flags. low_performance = FEWER THAN 10 "
                                                  "APPLICANTS, the competition filter. Also "
                                                  "accredited_it, not_from_agency, with_salary, "
                                                  "internship, with_address, night_shifts"},
                         "experience": {"type": "string",
                                        "enum": ["noExperience", "between1And3",
                                                 "between3And6", "moreThan6"]},
                         "work_format": {"type": "string",
                                         "enum": ["REMOTE", "HYBRID", "ON_SITE", "FIELD_WORK"]},
                         "education": {"type": "string",
                                       "enum": ["not_required_or_not_specified", "higher",
                                                "special_secondary"]},
                         "employment_form": {"type": "string",
                                             "enum": ["FULL", "PART", "PROJECT",
                                                      "FLY_IN_FLY_OUT"],
                                             "description": "PROJECT is hh's «Подработка». "
                                                            "These four are the ONLY filter "
                                                            "values; SIDE_JOB appears on "
                                                            "returned rows but is not an "
                                                            "input. hh also derives this from "
                                                            "your text, so searching "
                                                            "«подработка» selects PROJECT by "
                                                            "itself"},
                         "gph": {"type": "boolean",
                                 "description": "accept_temporary: «Оформление по ГПХ или по "
                                                "совместительству». The civil-law-contract "
                                                "gigs, which is where piece-rate work lives"},
                         "search_field": {"type": "string",
                                          "enum": ["name", "company_name", "description"],
                                          "description": "restrict matching to ONE field. All "
                                                         "three are searched by default, so "
                                                         "this only narrows: name = title-only. "
                                                         "To tighten a loose query prefer "
                                                         "QUOTING the phrase, which is what "
                                                         "actually cuts the noise"},
                         "excluded_text": {"type": "string",
                                           "description": "words to exclude, comma separated"},
                         "salary": {"type": "integer",
                                    "description": "minimum; also forces only_with_salary"},
                         "salary_mode": {"type": "string",
                                         "enum": ["MONTH", "HOUR", "SHIFT", "SERVICE",
                                                  "FLY_IN_FLY_OUT"],
                                         "description": "what the pay is PER: per month, per "
                                                        "hour, per shift, per service"},
                         "salary_frequency": {"type": "string",
                                              "enum": ["TWICE_PER_MONTH", "MONTHLY", "WEEKLY",
                                                       "DAILY", "PER_PROJECT"],
                                              "description": "how OFTEN it is paid"},
                         "period": {"type": "integer", "description": "search_period in days"}}),
    "vacancy":         (t_vacancy, "ONE VACANCY IN FULL including its description. Use after `search` / `recommended` / `suitable_vacancies` hand you an id and you want to judge the posting properly before applying. For the employer behind it use `employer`; for a recruiter phone use `contacts`.",
                        {"vacancy_id": {"type": "string"}}),
    "thread_read_state": (t_thread_read_state, "HAS THE EMPLOYER READ your last message, per live application. Silence after being read is a decision; silence before being read is a queue. `inbox` and `sent` cannot tell these apart. Use it to decide whether a follow-up is worth sending at all. Costs one call per thread, so it is a deliberate read.", {}),
    "contacts":        (t_contacts,
                        "RECRUITER NAME AND DIRECT PHONE. Pass vacancy_id for one posting, "
                        "or OMIT it to sweep every live application at once (SLOW: one call "
                        "per application), sorted real-numbers-first and interviews-first. "
                        "Reach for the sweep when applications are not converting and you "
                        "want to route around the response queue. phone_state NONE is a real "
                        "mobile; CREATED/PENDING is an hh call-tracking proxy. Many postings "
                        "publish nothing, so an empty result is normal.",
                        {"vacancy_id": {"type": "string"}}),
    "similar":         (t_similar, "LOOKALIKES OF ONE POSTING: hh's 'Похожие вакансии' block, ranked by "
                        "similarity to a vacancy you name. Requires a vacancy_id, which is what "
                        "separates it from `search` (your query), `recommended` (hh's feed) and "
                        "`suitable_vacancies` (CV match). Use it to widen out from a posting you "
                        "already like. COST: about 40 seconds.",
                        {"vacancy_id": {"type": "string"}, "limit": {"type": "integer"}}),
    "employer":        (t_employer, "WHO IS BEHIND A POSTING: identity, address, industries, open roles, and what hh did NOT verify. Pass vacancy_id to resolve the employer from a posting, or employer_id directly. Use `vacancy` for the posting itself.",
                        {"employer_id": {"type": "string"},
                         "vacancy_id": {"type": "string"},
                         "vacancies": {"type": "boolean"}}),
    "snapshot_vacancy": (t_archive, "SAVE POSTINGS TO LOCAL DISK (raw html + hh's json + readable text + sha256) so a citation survives the posting being pulled. Writes FILES, touches nothing on hh. Do not confuse with `archive_application`, which hides an application on hh itself.",
                        {"vacancy_ids": {"type": "array", "items": {"type": "string"}},
                         "employer_id": {"type": "string"}, "dest": {"type": "string"}}),
    "snapshot_index":  (t_archive_index, "CITABLE MARKDOWN INDEX of a local snapshot directory made by `snapshot_vacancy`: every frozen posting with its live url, capture time and sha256. Reads local files only.",
                        {"dest": {"type": "string"}}),
    "apply":           (t_apply, "APPLY to a vacancy with a chosen CV and cover letter. SENDS ON CALL: no dry run, no vetting of the letter. Refuses a duplicate application and an unknown resume hash, which are call errors rather than policy. Get the resume hash from `resumes`. Rewrite the letter afterwards with `letter_set` if needed.",
                        {"vacancy_id": {"type": "string"}, "resume": {"type": "string"},
                         "letter": {"type": "string"}}),
    "letter_get":      (t_letter_get, "READ the cover letter already sent with one application. Use `letter_set` to rewrite it, which is possible until the employer opens it.",
                        {"vacancy_id": {"type": "string"}}),
    "letter_set":      (t_letter_set, "REWRITE the cover letter of an application already sent, in place. Read the current one first with `letter_get`. WRITES ON CALL: the employer sees the new text immediately and it is not vetted here. Refuses once the employer has opened it unless force=true.",
                        {"vacancy_id": {"type": "string"}, "text": {"type": "string"},
                         "force": {"type": "boolean"}}),
    "hunt":            (t_hunt, "OPINIONATED SEARCH for small employers with a remote-work bias. A filtered wrapper over `search`, not a different index. IT DISCARDS TITLES BY DEFAULT: sales, support, recruiting, testing, design, product and project management, accounting and legal. That default is one job-seeker's saved search, so pass `stop_titles` to replace it (empty list disables filtering) if those are the roles you want. Use plain `search` for no opinions at all. COST: runs several searches, expect minutes.",
                        {"queries": {"type": "array", "items": {"type": "string"}},
                         "formats": {"type": "array", "items": {"type": "string"}},
                         "stop_titles": {"type": "array", "items": {"type": "string"}},
                         "pages": {"type": "integer"}, "limit": {"type": "integer"},
                         "no_size": {"type": "boolean"}}),
    "archive_application": (t_trash, "HIDE AN APPLICATION ON HH by vacancy id or chat id, moving it to hh's own trash bucket. Reversible, and it changes YOUR ACCOUNT. Do not confuse with `snapshot_vacancy`, which only writes files to disk. Cleanup, never employer-facing.",
                        {"ids": {"type": "array", "items": {"type": "string"}}}),
    "chat_leave":      (t_chat_leave, "LEAVE CHATS, removing them from your messenger, by chat id (from `chats`). NOT reversible and NOT the same as `archive_application`: that hides the application but leaves the chat in your messenger, because hh has no chat archive. LEAVE_CHAT is the only action that clears the thread out, so use it on dead threads (a rejection) and never on a live conversation. ACTS ON CALL.",
                        {"chat_ids": {"type": "array", "items": {"type": "string"}}}),
    "recommended":     (t_recommended, "HH'S PERSONALISED FEED, no query needed: what hh puts on the applicant "
                        "landing page based on your CVs and behaviour. About 6 at a time, high "
                        "signal. Use `suitable_vacancies` for hh's dedicated CV matcher (far more "
                        "results), `search` when you have your own query, `similar` for lookalikes "
                        "of one posting. COST: about 35 seconds.", {}),
    "activity":        (t_activity, "ACCOUNT ACTIVITY GAUGE ('Ваша активность' on hh.ru), one number for the whole account, which hh decays when idle and advises keeping >= 80%. NOT per-CV performance: for that use `resume_scorecard`. NOT resume completeness either.", {}),
}


# ---------------------------------------------------------------- jsonrpc

def respond(rid, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": rid}
    if error:
        m["error"] = error
    else:
        m["result"] = result
    sys.stdout.write(json.dumps(m, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}

        if method == "initialize":
            respond(rid, {"protocolVersion": "2024-11-05",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "hh-cv", "version": "1.0.0"}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            respond(rid, {"tools": [
                {"name": n, "description": d,
                 "annotations": annotations_for(n),
                 "inputSchema": {"type": "object", "properties": p,
                                 "required": REQUIRED.get(n, [])}}
                for n, (_, d, p) in TOOLS.items()]})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in TOOLS:
                respond(rid, error={"code": -32601, "message": f"unknown tool {name}"})
                continue
            try:
                text = TOOLS[name][0](args)
                respond(rid, {"content": [{"type": "text", "text": str(text)}]})
            except Exception as e:                    # noqa: BLE001
                respond(rid, {"content": [{"type": "text", "text": describe_failure(e)}],
                              "isError": True})
        elif rid is not None:
            respond(rid, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    main()
