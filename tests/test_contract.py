#!/usr/bin/env python3
"""Contract tests for the MCP surface. No network, no session, no fixtures.

These answer "is this server still a coherent MCP server", which is the question
CI can ask on every push without holding anyone's credentials. They deliberately
run with NO session present, which also pins a real property: every tool that
needs auth must fail fast with a clear message rather than hang or traceback.

Run: python3 tests/test_contract.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

MCP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MCP))

import server  # noqa: E402

# Tools that reach an employer or change a live CV. The published server runs
# them on call; the contract is that they SAY so, loudly, in the description an
# agent author will read.
WRITES = {"apply", "chat_send", "letter_set", "cv_push", "resume_experience",
          "resume_exp_add", "resume_exp_dates"}


def test_tool_table_is_well_formed():
    assert server.TOOLS, "no tools at all"
    for name, entry in server.TOOLS.items():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), f"{name}: bad tool name"
        assert len(entry) == 3, f"{name}: expected (fn, description, schema)"
        fn, desc, schema = entry
        assert callable(fn), f"{name}: handler is not callable"
        assert isinstance(desc, str) and len(desc) > 20, f"{name}: thin description"
        assert isinstance(schema, dict), f"{name}: schema is not a dict"


def test_schemas_are_portable_across_providers():
    """Every property needs an explicit type or an anyOf union.

    Untyped `{}` is accepted by Anthropic and REJECTED by OpenAI, whose
    function-schema validation requires a type key. A tool added with a bare
    `{}` works in one client and breaks in another, which is exactly the kind of
    bug that only shows up in somebody else's stack.
    """
    for name, (_fn, _desc, schema) in server.TOOLS.items():
        for prop, spec in schema.items():
            assert isinstance(spec, dict), f"{name}.{prop}: not a schema object"
            assert "type" in spec or "anyOf" in spec, \
                f"{name}.{prop}: no type/anyOf, breaks OpenAI-style validation"
            if spec.get("type") == "array" and "items" in spec:
                assert isinstance(spec["items"], dict), f"{name}.{prop}: bad items"


def test_no_confirm_gate_in_the_published_surface():
    """mcp/ is the capability layer: gating lives in the calling agent. A confirm
    or dry_run argument reappearing here means the gate got duplicated back in."""
    for name, (_fn, _desc, schema) in server.TOOLS.items():
        assert "confirm" not in schema, f"{name} takes confirm again"
        assert "dry_run" not in schema, f"{name} takes dry_run again"


def test_writes_announce_themselves():
    """Someone wiring an agent to this server reads the description and nothing
    else. If a write stops announcing that it fires immediately, they will not
    know to put an approval gate in front of it."""
    for name in WRITES:
        assert name in server.TOOLS, f"{name} vanished from the tool table"
        desc = server.TOOLS[name][1].lower()
        assert ("sends on call" in desc or "writes on call" in desc
                or "executes on call" in desc), \
            f"{name}: description no longer says it fires immediately: {desc[:90]}"


def test_every_tool_declares_its_risk():
    """Annotations are the only machine-readable warning this server gives.

    The layer has no gate of its own, so a client deciding whether to
    auto-approve has nothing else to go on. A write that forgets to say it
    writes is the bug this catches, and it is the same bug in a different
    place as a write missing from the agent's WRITE_TOOLS.
    """
    listed = {t["name"]: t["annotations"] for t in _list_over_stdio()}
    for name, ann in listed.items():
        for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert isinstance(ann.get(key), bool), f"{name}: {key} missing or not a bool"
        assert ann["readOnlyHint"] is (name not in WRITES | {"archive_application",
                                                            "snapshot_vacancy",
                                                            "resume_bump",
                                                            "view_vacancy",
                                                            "chat_leave"}), \
            f"{name}: readOnlyHint disagrees with whether it writes"
    for name in WRITES:
        assert listed[name]["readOnlyHint"] is False, f"{name} claims to be read-only"
    # chat_leave sends no employer text but is irreversible, so it must warn too.
    for name in ("apply", "chat_send", "letter_set", "cv_push", "chat_leave"):
        assert listed[name]["destructiveHint"] is True, \
            f"{name} does not warn clients to confirm before calling it"


def test_failures_are_one_shape():
    """Every failure carries a machine-readable kind, so a model can branch on
    it instead of pattern-matching English prose that differs per tool."""
    out, _p = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "vacancy", "arguments": {}}}])
    text = out[2]["result"]["content"][0]["text"]
    assert out[2]["result"].get("isError"), "a failure was not flagged isError"
    assert re.match(r'^ERROR \[[a-z_]+\]: ', text), f"unstructured failure: {text[:80]}"


def test_session_failure_says_how_to_fix_it():
    """The expensive failure is a dead cookie. It must hand back the command
    that repairs it, not just name the problem."""
    out, _p = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "resumes", "arguments": {}}}],
                   env={"HH_SESSION": "/nonexistent/no_session_here.json"}, timeout=45)
    text = out[2]["result"]["content"][0]["text"]
    assert "session" in text.lower(), "no mention of the session"
    assert "FIX:" in text, "no fix offered for the one failure users will actually hit"
    assert "hhtoken" in text, "the fix does not name the cookie needed"


# Tools whose neighbours are genuinely unambiguous: nothing to disambiguate from.
_NO_NEIGHBOUR = {"activity", "hunt", "whoami", "contacts"}

CONFUSABLE = [
    # a set of tools an agent must choose BETWEEN. Each has to name at least one
    # sibling, or the model is picking from descriptions written in isolation.
    ("discovery", {"search", "recommended", "suitable_vacancies", "similar", "hunt"}),
    ("status",    {"inbox", "sent", "lost", "thread_read_state", "chats"}),
    ("cv reads",  {"resumes", "resume_read", "resume_scorecard"}),
    ("cv writes", {"cv_push", "resume_experience", "resume_exp_dates", "resume_exp_add"}),
    ("disk vs account", {"snapshot_vacancy", "snapshot_index", "archive_application"}),
]


def test_confusable_tools_name_their_siblings():
    """A description that is fine alone is useless in a set of 35.

    These clusters are the ones an agent must choose BETWEEN, and picking wrong
    is expensive: snapshot_vacancy writes files, archive_application changes the
    account. Every member has to point at a sibling so the model can tell them
    apart from the descriptions alone, which is all it gets.
    """
    for label, cluster in CONFUSABLE:
        for name in cluster:
            assert name in server.TOOLS, f"{label}: {name} is gone; update this test"
            desc = server.TOOLS[name][1]
            siblings = {s for s in cluster if s != name and f"`{s}`" in desc}
            assert siblings or name in _NO_NEIGHBOUR, (
                f"{label}: `{name}` names none of its siblings {cluster - {name}}, "
                f"so an agent choosing between them has nothing to go on")


def test_ships_no_personal_tools():
    """The grounding/portfolio/doctor tools read one person's files. They live in
    not part of this package; a published server must not carry them."""
    personal = {"ledger_search", "ledger_rules", "ledger_check",
                "portfolio_search", "doctor"}
    leaked = personal & set(server.TOOLS)
    assert not leaked, f"personal tools in the published surface: {leaked}"


# Tools that cost real seconds or many round trips. hh is slow: a search page is
# 30-40s, and several of these fan out one call per application or per CV.
EXPENSIVE = {"contacts", "search", "hunt", "similar", "recommended",
             "suitable_vacancies", "resume_views", "thread_read_state",
             "inbox", "sent", "lost", "resume_scorecard"}


def test_expensive_tools_say_so_on_the_wire():
    """An agent budgets from the DESCRIPTION, which is all it receives.

    The cost notes used to live in the Python docstrings, which never leave the
    process, while the model saw only the description string. It would happily
    chain four 40-second calls with nothing warning it."""
    for name in EXPENSIVE:
        assert name in server.TOOLS, f"{name} is gone; update EXPENSIVE"
        d = server.TOOLS[name][1].lower()
        assert any(w in d for w in ("cost:", "slow", "one call per", "seconds")), \
            f"`{name}` is expensive but its wire description gives no cost signal"


def test_required_args_match_the_functions():
    """`required` is a promise to the agent, and it used to be a guess.

    It was derived from a whitelist of argument NAMES, so chat_read and
    resume_read advertised nothing required and then raised KeyError on the
    call, while `employer` claimed to need vacancy_id when employer_id alone
    works. An agent plans against this list."""
    for name, req in server.REQUIRED.items():
        assert name in server.TOOLS, f"REQUIRED names {name}, which is not a tool"
        props = server.TOOLS[name][2]
        for arg in req:
            assert arg in props, f"{name} requires {arg!r} but does not declare it"
    listed = {t["name"]: t["inputSchema"].get("required", []) for t in _list_over_stdio()}
    for name, req in server.REQUIRED.items():
        assert listed.get(name) == req, f"{name}: tools/list says {listed.get(name)}, table says {req}"
    # tools that cannot run without an id must say so. `contacts` is deliberately
    # NOT here: omitting vacancy_id is its sweep-every-application mode.
    for name in ("chat_read", "resume_read", "vacancy", "similar"):
        assert listed.get(name), f"{name} advertises no required args but cannot run without one"


def test_no_personal_data_anywhere_in_the_package():
    """Scan the SHIPPED FILES for generic personal-data shapes, not just tool names.

    This package is meant to be published, so nothing in it should carry a real
    person's contact details or account identifiers. The patterns below are
    generic on purpose: an email, a phone, a resume-hash-shaped id, a revenue
    figure. If you fork this for your own account, keep your name, email and
    employer out of committed files -- the point of the split is that this layer
    holds only hh capability, no personal data.
    """
    patterns = {
        "email address":  re.compile(r'[\w.+-]+@[\w-]+\.[a-z]{2,}'),
        "russian phone":  re.compile(r'(?:\+7|\b8)\d{10}\b'),
        "38-char id":     re.compile(r'\b[0-9a-f]{38}\b'),
        "revenue figure": re.compile(r'\bMRR\b|\bARR\b'),
    }
    for f in sorted(MCP.rglob("*.py")) + sorted(MCP.rglob("*.md")):
        if "fixtures" in f.parts or f.name.startswith("test_"):
            continue                       # fixtures have their own cleanliness test
        text = f.read_text(encoding="utf-8")
        for label, pat in patterns.items():
            hit = pat.search(text)
            assert not hit, (f"{f.relative_to(MCP)} leaks {label}: {hit.group(0)!r}. "
                             f"This package is meant to be publishable.")


def test_imports_nothing_outside_this_folder():
    """`cp -R mcp/ elsewhere` must run. Module-level imports decide that."""
    local = {f[:-3] for f in os.listdir(MCP) if f.endswith(".py")}
    stdlib = set(sys.stdlib_module_names)
    for fname in sorted(f for f in os.listdir(MCP) if f.endswith(".py")):
        src = (MCP / fname).read_text(encoding="utf-8")
        for mod in re.findall(r'^(?:from|import)\s+([a-zA-Z_][\w.]*)', src, re.M):
            top = mod.split(".")[0]
            assert top in local or top in stdlib or top == "__future__", \
                f"{fname} imports {top!r} at module level; breaks lift-and-run"


# ----------------------------------------------------------- over real stdio

def _rpc(requests, env=None, timeout=60):
    """Drive the server as a real subprocess over stdio, like a client would."""
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    e = dict(os.environ)
    e.pop("HH_SESSION", None)
    e.update(env or {})
    p = subprocess.run([sys.executable, str(MCP / "server.py")], input=payload,
                       capture_output=True, text=True, timeout=timeout, env=e)
    out = {}
    for line in p.stdout.splitlines():
        try:
            m = json.loads(line)
        except Exception:
            continue
        if "id" in m:
            out[m["id"]] = m
    return out, p


def _list_over_stdio():
    out, p = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
    assert 2 in out, f"no tools/list response. stderr: {p.stderr[:300]}"
    return out[2]["result"]["tools"]


def test_initialize_and_tools_list_over_stdio():
    out, p = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
    assert 1 in out, f"no initialize response. stderr: {p.stderr[:300]}"
    assert out[1]["result"]["serverInfo"]["name"], "no server name"
    listed = out[2]["result"]["tools"]
    assert len(listed) == len(server.TOOLS), \
        f"tools/list says {len(listed)}, table has {len(server.TOOLS)}"
    for t in listed:
        assert t["name"] in server.TOOLS
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_unknown_tool_is_an_error_not_a_crash():
    out, _p = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "no_such_tool", "arguments": {}}}])
    m = out[2]
    assert "error" in m or (m.get("result", {}).get("isError")), \
        "unknown tool did not report an error"


def test_auth_tools_fail_fast_without_a_session():
    """With no session anywhere, an authenticated tool must say so immediately.

    This is what CI actually exercises: no credentials present. A tool that
    hangs or tracebacks here would do the same on a fresh install, which is the
    first thing a new user of the published server sees.
    """
    out, _p = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "resumes", "arguments": {}}}],
                   env={"HH_SESSION": "/nonexistent/no_session_here.json"},
                   timeout=45)
    m = out[2]
    txt = json.dumps(m, ensure_ascii=False).lower()
    assert "session" in txt, f"unhelpful no-session failure: {txt[:200]}"
    assert "traceback" not in txt, "leaked a traceback instead of a message"


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
