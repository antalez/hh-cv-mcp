# hh-cv-mcp

An MCP server for **hh.ru**, the Russian-language job board (it also serves
Kazakhstan, Belarus, Azerbaijan, Uzbekistan and more). It gives an LLM agent — or
a person at a CLI — the full applicant surface of one hh account: search,
resumes, applications, cover letters, employer chat, and the analytics hh keeps
mostly hidden.

Two files, no third-party dependencies, stdio transport. Copy the folder, point
it at a login cookie, and every tool works.

---

## Why this exists

hh discontinued its public applicant API on 2025-12-15. `api.hh.ru/vacancies` now
returns `forbidden` from every origin. But hh's own website content-negotiates to
JSON for a client holding a login session, so **everything the site does, this
does too, over plain HTTP** — no browser, no scraping of rendered HTML, no token.
The protocol was reverse-engineered by measurement, not read from docs (there are none).

---

## Install

Copy this folder. That is the install. Python 3.10+, standard library only.

### Get a session

Every tool needs a logged-in hh.ru cookie. **No browser automation required** —
one cookie authenticates. In any browser logged into hh.ru:
DevTools → Application → Cookies → `https://hh.ru`, copy the value of `hhtoken`
(and `_xsrf` too if you want to write), then:

```bash
python3 hh_client.py session --cookie 'hhtoken=...; _xsrf=...'
python3 hh_client.py whoami        # confirm which account you just saved
```

`session` with no arguments reads the same from stdin, and a whole `Cookie:`
request header pasted verbatim also works. Cookies expire every few weeks; re-run
`session` when calls start returning login pages.

> Optional `--from-chrome` lifts cookies out of a running Chrome over CDP; it
> needs `pip install playwright`. It is the fallback, not the path.

### Mount it in a client

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS):

```jsonc
{
  "mcpServers": {
    "hh": {
      "command": "python3",
      "args": ["/abs/path/to/mcp/server.py"],
      "env": { "HH_SESSION": "/abs/path/to/.hh_session.json" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`) — note it uses `servers`, not `mcpServers`, and
wants an explicit `type`:

```jsonc
{
  "servers": {
    "hh": {
      "type": "stdio",
      "command": "python3",
      "args": ["/abs/path/to/mcp/server.py"],
      "env": { "HH_SESSION": "/abs/path/to/.hh_session.json" }
    }
  }
}
```

Set `HH_SESSION` explicitly rather than relying on discovery, so the server works
whatever directory the client launches it from. Restart the client, then ask it
`whoami` to confirm the mount.

### Environment

| variable | effect |
|---|---|
| `HH_SESSION` | path to the cookie jar (else looked for beside the server, then one dir up) |
| `HH_CURRENCY` | default currency for salary writes (`RUR` if unset; hh serves several countries) |
| `HH_REFUSE_DASHES` | set to `1` to reject em/en dashes in anything you write (one user's house style, off by default) |

---

## The one rule that matters

**This layer has no gate.** Every write executes the moment it is called: no
confirm argument, no dry run, no vetting of text. `apply` applies. `chat_send`
sends. `cv_push` overwrites a live CV. This is deliberate — deciding *whether* a
write should happen belongs to the agent or human driving it, not to the tool.

The server's only defence is telling you the truth about what each tool does,
through MCP annotations (`readOnlyHint` / `destructiveHint`). A good client reads
those and asks before a destructive call.

**If you point an unattended LLM at this, put an approval gate in front of the 11
write tools.** Suspend every write for a human to confirm before it executes
(pydantic-ai's `ApprovalRequiredToolset` is one way). An LLM with `apply` and no
gate will apply to things.

---

## What it covers

Every workflow a job-seeker runs, end to end.

| workflow | tools |
|---|---|
| **auth** | `session` (CLI), `whoami` |
| **find work** | `search`, `hunt`, `recommended`, `suitable_vacancies`, `similar` |
| **judge a vacancy** | `vacancy`, `employer`, `contacts` |
| **apply** | `apply`, `letter_get`, `letter_set`, `cv_push` |
| **track** | `inbox`, `sent`, `lost`, `thread_read_state` |
| **chat** | `chats`, `chat_read`, `chat_send` |
| **diagnose the CV** | `resume_scorecard`, `resume_views`, `resume_advice` |
| **maintain the CV** | `resumes`, `resume_read`, `resume_experience`, `resume_exp_dates`, `resume_exp_add`, `resume_bump` |
| **housekeeping** | `archive_application`, `snapshot_vacancy`, `snapshot_index`, `activity`, `view_vacancy` |

The tools most worth knowing exist because hh hides them:

- **`resume_scorecard`** — hh's own verdict on every CV beside how it performs:
  the 7-day funnel (impressions / opens / invitations), hh's checklist of empty
  fields, the canonical-vs-free-text skill split.
- **`resume_views`** — which *employers* opened your CV, by name and date.
- **`resume_advice`** — hh's own LLM critique of a CV (free).
- **`thread_read_state`** — whether an employer has actually *read* your last
  message. Silence after being read is a decision; before, a queue.
- **`contacts`** — a recruiter's direct phone, where the posting publishes one.
- **`search`** filters on hh's own vocabulary, including `label=low_performance`
  (**fewer than 10 applicants** — the competition signal hh never shows as a
  number) and `salary_mode` (per month / hour / shift / service).

## Writes execute immediately

The 11 write tools: `apply`, `chat_send`, `letter_set`, `cv_push`,
`resume_experience`, `resume_exp_dates`, `resume_exp_add`, `resume_bump`,
`archive_application`, `snapshot_vacancy`, `view_vacancy`. Of these,
`snapshot_vacancy` only writes local files and `archive_application` is reversible
(hh's own trash). The rest change your live account or reach an employer.

## Errors

Every failure comes back one shape, so an agent can branch on it:

```
ERROR [session_expired]: HTTP 401 on an authenticated call...

FIX: Get a fresh cookie: ... python3 hh_client.py session --cookie '...'
```

`kind` is machine-readable (`session_expired`, `session_missing`,
`no_write_permission`, `rate_limited`, `not_found`, `hh_changed`, `bad_argument`,
`network`); the `FIX:` line tells a human what to do.

## Tests

```bash
python3 tests/test_contract.py          # is this a valid MCP server (no session)
python3 tests/test_parsers.py           # replay recorded hh responses (no session)
python3 tests/test_fixtures_are_clean.py
python3 smoke.py                        # LIVE, read-only, needs a session
```

The first three need no session, network or account: `tests/fixtures/` holds real
responses reduced to a skeleton, so the parsers are testable by anyone who clones
this. `smoke.py` is the only thing that notices hh changing a payload; run it on a
schedule wherever the session lives. Re-record fixtures with
`python3 tests/record_fixtures.py` (needs a session); it scrubs by allowlist and
`test_fixtures_are_clean.py` fails if anything personal survives.

The contract tests enforce the design rules — no confirm/dry_run arguments, every
write annotated, confusable tools cross-referencing, expensive tools carrying a
cost signal, `required` matching the functions, and **no personal data anywhere
in the shipped files** (that last one exists because a leak once shipped here).

## Things the protocol will teach you the hard way

- **A chat is not an application.** `/applicant/negotiations` only knows
  NEGOTIATION chats. Employers who message you first create type COMMON, invisible
  to `sent` — that is what `chats direct_only=true` is for.
- **A resume has two identifiers.** Negotiations report a numeric `resumeId`;
  every URL and write needs the 38-character hash. The numeric one returns an
  empty resume that reads like a dead session.
- **Everything pages at 20** — except `chats` (cursor) and `favorites` (no paging
  at all). `resume_advice` has a hidden **10-tasks-per-day** quota.
- **A 200 does not mean a parameter worked.** `?filter=ARCHIVED`, `?page=` on
  favourites, and `search_field=name` all return 200 and are ignored. Test
  filters against `searchCounts.value`, not the returned row count.
- **`/search/vacancy` is throttled to a hang.** Keep it off any hot path.

One account, at human pace. hh notices bulk behaviour.

## License

Source-available under the **PolyForm Noncommercial License 1.0.0** (see [LICENSE.md](LICENSE.md)): free to use, run, and modify for **non-commercial** purposes. Commercial use needs a separate license — open an issue to ask.
