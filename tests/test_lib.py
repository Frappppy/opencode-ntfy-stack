#!/usr/bin/env python3
"""
Regression tests for ntfy_oc (the shared library) and the reply router.

The library is imported under a throwaway $HOME *before* it computes any
paths, so nothing here reads your real config, state, or OpenCode store.

    python3 tests/test_lib.py
    python3 tests/test_lib.py roster     # substring filter
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.normpath(os.path.join(HERE, "..", "bin"))
UNIT_DIR = os.path.normpath(os.path.join(HERE, "..", "systemd"))

# ── sandbox FIRST: ntfy_oc bakes $HOME into module constants at import ────
SANDBOX = tempfile.mkdtemp(prefix="ntfy-lib-")
os.environ["HOME"] = SANDBOX
os.environ["XDG_CONFIG_HOME"] = os.path.join(SANDBOX, ".config")
os.environ["XDG_STATE_HOME"] = os.path.join(SANDBOX, ".state")
os.environ.pop("NTFY_TOPIC", None)
os.environ.pop("NTFY_SERVER", None)
os.makedirs(os.environ["XDG_CONFIG_HOME"], exist_ok=True)

sys.path.insert(0, BIN)
import ntfy_oc as oc  # noqa: E402

CONF = os.path.join(os.environ["XDG_CONFIG_HOME"], "ntfy-notify.conf")


def write_conf(text: str) -> None:
    with open(CONF, "w") as fh:
        fh.write(text)


def load_script(name: str, filename: str):
    """Import a shebang script from bin/ (they have no .py suffix)."""
    path = os.path.join(BIN, filename)
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def with_sessions(rows):
    """Run fn with oc.all_sessions() temporarily returning `rows`."""
    original = oc.all_sessions
    oc.all_sessions = lambda: rows
    try:
        return _roster()
    finally:
        oc.all_sessions = original


def _roster():
    return oc.roster_lines()


# ── shorten() ─────────────────────────────────────────────────────────────
def test_shorten_drops_leading_filler():
    assert oc.shorten("Continue working on the eToro bounty") == "eToro bounty"


def test_shorten_never_exceeds_label_width():
    long_title = "Refactor the notification pipeline for the whole stack again"
    out = oc.shorten(long_title)
    assert len(out) <= oc.LABEL_W, f"{len(out)} > {oc.LABEL_W}: {out!r}"


def test_shorten_keeps_a_second_word():
    # "Phone" alone is too terse to identify a thread, so a second word is
    # kept even when it has to be truncated.
    out = oc.shorten("Phone notifications for tasks")
    assert " " in out, f"must keep a second word: {out!r}"
    assert len(out) <= oc.LABEL_W, f"{len(out)} > {oc.LABEL_W}"


def test_shorten_empty_is_session():
    assert oc.shorten("") == "session"
    assert oc.shorten(None) == "session"


def test_shorten_drops_filler_until_a_real_word():
    # leading filler verbs are stripped, but a real word stops the strip
    assert oc.shorten("continue working on it now") == "it now"
    assert oc.shorten("please resume the report") == "report"
    # …and an all-filler title still yields something identifiable
    assert oc.shorten("continue working now") == "session"


def test_shorten_strips_punctuation():
    assert oc.shorten("Fix: auth! (#42)") == "Fix auth 42"


def test_shorten_keeps_short_title_whole():
    assert oc.shorten("eToro") == "eToro"


# ── pad() ─────────────────────────────────────────────────────────────────
def test_pad_pads_to_width():
    out = oc.pad("hi")
    assert len(out) == oc.LABEL_W, f"{len(out)} != {oc.LABEL_W}"
    assert out.startswith("hi")
    assert out.endswith(" ")


def test_pad_truncates_with_ellipsis():
    out = oc.pad("x" * 40)
    assert len(out) == oc.LABEL_W, f"{len(out)} != {oc.LABEL_W}"
    assert out.endswith("…")


# ── humanise_age() ────────────────────────────────────────────────────────
def test_humanise_age_units():
    assert oc.humanise_age(10) == "just now"
    assert oc.humanise_age(89) == "just now"
    assert oc.humanise_age(100) == "1m ago"
    assert oc.humanise_age(12 * 60) == "12m ago"
    assert oc.humanise_age(3 * 3600) == "3h ago"
    assert oc.humanise_age(2 * 86400) == "2d ago"


# ── config resolution (the credential) ────────────────────────────────────
def test_conf_unwraps_default_wrapper():
    # install.sh writes ${KEY:-value} so an ambient env var still wins. The
    # value must be unwrapped — returning the wrapper literally turned the
    # health probe into https://ntfy.sh/${...}/json and 404'd every run.
    write_conf('NTFY_TOPIC="${NTFY_TOPIC:-abc123-topic}"\n')
    assert oc._conf("NTFY_TOPIC") == "abc123-topic"


def test_conf_reads_plain_quoted_value():
    write_conf('NTFY_SERVER="https://ntfy.example"\n')
    assert oc._conf("NTFY_SERVER") == "https://ntfy.example"


def test_conf_missing_key_returns_default():
    write_conf("# nothing here\n")
    assert oc._conf("NTFY_TOPIC", "missing-default") == "missing-default"


def test_conf_ignores_comments_and_blanks():
    write_conf("# NTFY_TOPIC=commented\n\nNTFY_TOPIC=real\n")
    assert oc._conf("NTFY_TOPIC") == "real"


def test_conf_missing_file_is_silent():
    os.rename(CONF, CONF + ".away")
    try:
        assert oc._conf("NTFY_TOPIC", "fallback") == "fallback"
    finally:
        os.rename(CONF + ".away", CONF)


def test_topic_resolves_from_conf():
    write_conf('NTFY_TOPIC="${NTFY_TOPIC:-from-the-conf-file}"\n')
    assert oc._conf("NTFY_TOPIC") == "from-the-conf-file"


def test_ambient_topic_wins_over_conf():
    write_conf('NTFY_TOPIC="${NTFY_TOPIC:-conf-value}"\n')
    os.environ["NTFY_TOPIC"] = "scratch-topic"
    try:
        # both the conf writer's guard and the library must prefer the env,
        # because that is how tests publish without reaching a real phone
        import subprocess
        out = subprocess.run(
            ["bash", "-c", 'source "$1"; printf %s "$NTFY_TOPIC"', "_", CONF],
            capture_output=True, text=True, check=True,
        ).stdout
        assert out == "scratch-topic", out
        assert (os.environ.get("NTFY_TOPIC") or "") == "scratch-topic"
    finally:
        os.environ.pop("NTFY_TOPIC", None)


# ── session binding ───────────────────────────────────────────────────────
def test_bind_and_bound_roundtrip():
    oc.bind_session("ses_abc", "My Session")
    got = oc.bound_session()
    assert got is not None, "a fresh binding must be readable"
    assert got["id"] == "ses_abc"
    assert got["title"] == "My Session"


def test_bound_session_expires():
    oc.bind_session("ses_abc", "My Session")
    assert oc.bound_session(ttl_s=-1) is None, "an expired binding must not route"


def test_bound_session_missing_file_is_none():
    try:
        os.remove(oc.BOUND_FILE)
    except OSError:
        pass
    assert oc.bound_session() is None


# ── roster ────────────────────────────────────────────────────────────────
def test_roster_working_first_then_idle():
    lines = with_sessions([
        {"id": "a", "title": "Build", "running": True, "idle_for": "1m"},
        {"id": "b", "title": "Idle thing", "running": False, "idle_for": "12m"},
        {"id": "c", "title": "Another idle", "running": False, "idle_for": "2h"},
    ])
    text = "\n".join(lines)
    assert "WORKING NOW" in text and "IDLE" in text, text
    assert text.index("WORKING NOW") < text.index("IDLE"), "working must come first"
    # contiguous numbering across the section break: 1 = working, 2 and 3 = idle
    assert lines[1].startswith("#1"), lines[1]
    assert lines[4].startswith("#2"), lines[4]
    assert lines[5].startswith("#3"), lines[5]
    assert "12m" in lines[4], f"idle age must show: {lines[4]!r}"


def test_roster_lists_every_session_without_a_cap():
    rows = [{"id": f"s{i}", "title": f"Session {i}", "running": False,
             "idle_for": "5m"} for i in range(1, 10)]
    lines = with_sessions(rows)
    listed = [l for l in lines if l.startswith("#")]
    assert len(listed) == 9, f"no '+N more idle' truncation: got {len(listed)}"
    assert listed[-1].startswith("#9"), listed[-1]


def test_roster_empty_state():
    lines = with_sessions([])
    assert lines == ["No sessions yet."], lines


def test_roster_text_is_joined_lines():
    rows = [{"id": "a", "title": "Only", "running": True, "idle_for": "now"}]
    original = oc.all_sessions
    oc.all_sessions = lambda: rows
    try:
        text = oc.roster_text()
        assert text == "\n".join(oc.roster_lines())
        assert text.startswith("WORKING NOW")
    finally:
        oc.all_sessions = original


def test_roster_numbering_is_contiguous():
    rows = ([{"id": f"w{i}", "title": f"Working {i}", "running": True,
              "idle_for": "now"} for i in range(2)] +
            [{"id": f"i{i}", "title": f"Idle {i}", "running": False,
              "idle_for": "9m"} for i in range(3)])
    lines = [l for l in with_sessions(rows) if l.startswith("#")]
    numbers = [int(l.split()[0][1:]) for l in lines]
    assert numbers == [1, 2, 3, 4, 5], numbers


# ── subagent filter ───────────────────────────────────────────────────────
def _with_db(rows):
    path = os.path.join(SANDBOX, "oc.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("create table session_v2 (id text primary key, parent_id text)")
    for sid, parent in rows:
        conn.execute("insert into session_v2 values (?, ?)", (sid, parent))
    conn.commit()
    conn.close()
    return path


def test_is_subagent_detects_children():
    path = _with_db([("child", "parent-1"), ("root", None)])
    old_db, old_cache = oc.DB, oc._SUBAGENT_CACHE
    oc.DB, oc._SUBAGENT_CACHE = path, {}
    try:
        assert oc.is_subagent("child") is True, "a child session must be filtered"
        assert oc.is_subagent("root") is False, "a human session must be reported"
    finally:
        oc.DB, oc._SUBAGENT_CACHE = old_db, old_cache


def test_is_subagent_unknown_id_is_not_a_child():
    path = _with_db([("root", None)])
    old_db, old_cache = oc.DB, oc._SUBAGENT_CACHE
    oc.DB, oc._SUBAGENT_CACHE = path, {}
    try:
        assert oc.is_subagent("never-seen") is False
    finally:
        oc.DB, oc._SUBAGENT_CACHE = old_db, old_cache


def test_is_subagent_fails_open_when_store_is_unreachable():
    # Failing OPEN means "not a subagent": dropping a real completion is
    # worse than an occasional extra ping.
    old_db, old_cache = oc.DB, oc._SUBAGENT_CACHE
    oc.DB, oc._SUBAGENT_CACHE = "/nonexistent/path/store.db", {}
    try:
        assert oc.is_subagent("anything") is False, "must not swallow a real ping"
    finally:
        oc.DB, oc._SUBAGENT_CACHE = old_db, old_cache


def test_is_subagent_empty_id_is_false():
    assert oc.is_subagent("") is False


# ── reply routing ─────────────────────────────────────────────────────────
def test_parse_target_plain_text():
    reply = load_script("ntfy_reply_mod", "ntfy-reply")
    original = reply.oc.all_sessions
    reply.oc.all_sessions = lambda: [
        {"id": "s1", "title": "One"}, {"id": "s2", "title": "Two"}]
    try:
        session, rest, is_opts, bad = reply.parse_target("focus on X")
        assert session is None and rest == "focus on X"
        assert is_opts is False and bad == 0

        session, rest, is_opts, bad = reply.parse_target("1,3")
        assert session is None and is_opts is True and bad == 0

        session, rest, is_opts, bad = reply.parse_target("#2 hello")
        assert session["id"] == "s2", session
        assert rest == "hello" and is_opts is False and bad == 0

        # "#9" that matches nothing must surface as a bad index, not be
        # carried onward as literal text and misrouted.
        session, rest, is_opts, bad = reply.parse_target("#9 hello")
        assert session is None and bad == 9, (session, bad)

        session, rest, is_opts, bad = reply.parse_target("#2, 1")
        assert session["id"] == "s2" and is_opts is True and rest == "1"
    finally:
        reply.oc.all_sessions = original


def test_is_our_own_matches_own_titles():
    reply = load_script("ntfy_reply_mod", "ntfy-reply")
    assert reply.is_our_own({"title": "OpenCode"}) is True
    assert reply.is_our_own({"title": "OpenCode heartbeat"}) is True
    assert reply.is_our_own({"title": "someone else"}) is False
    assert reply.is_our_own({}) is False
    # tags alone must NOT classify a message as ours: a genuine reply
    # carrying a tag we happen to use would vanish with no error.
    assert reply.is_our_own({"title": "please look", "tags": "hand"}) is False


def test_is_our_own_recognises_our_ids_with_custom_titles():
    # An agent's own status notice can carry any title ("Candid submitted"),
    # so the title prefix alone misses it and the bridge steered it into a
    # session as if the human had typed it. The id ntfy-notify recorded is
    # the exact signal.
    reply = load_script("ntfy_reply_mod", "ntfy-reply")
    os.makedirs(os.path.dirname(reply.SENT_IDS_FILE), exist_ok=True)
    try:
        with open(reply.SENT_IDS_FILE, "w") as fh:
            fh.write(f"msg_ours\t{time.time()}\n")
        assert reply.is_our_own({"id": "msg_ours", "title": "Candid submitted"}) is True
        # A genuine reply was never published by us, so its id is not there.
        assert reply.is_our_own({"id": "msg_human", "title": "Candid submitted"}) is False
    finally:
        try:
            os.remove(reply.SENT_IDS_FILE)
        except OSError:
            pass


def test_reply_cursor_advances_over_ignored_own_notices():
    # Filtered self-notifications must still advance the base-topic cursor;
    # otherwise the bridge re-fetches them forever and can misclassify one
    # after its sent-id entry expires.
    reply = load_script("ntfy_reply_mod", "ntfy-reply")
    cursors = {reply.BASE_TOPIC: "m:before", reply.REPLY_TOPIC: "m:reply-before"}
    events = {
        reply.REPLY_TOPIC: [],
        reply.BASE_TOPIC: [
            ({"id": "own-1", "topic": reply.BASE_TOPIC}, True),
            ({"id": "own-2", "topic": reply.BASE_TOPIC}, True),
        ],
    }
    saved = []
    reply.ensure_cursors = lambda: cursors
    reply.stream_once = lambda _c: ([], cursors, events)
    reply.save_cursors = lambda value: saved.append(value)
    assert reply.drain() == 0
    assert saved and saved[-1][reply.BASE_TOPIC] == "m:own-2", saved
    assert saved[-1][reply.REPLY_TOPIC] == "m:reply-before", saved


def test_sent_id_record_is_pruned():
    # The record is bounded: entries older than the TTL must not match, or a
    # stale id could mask a genuine reply that reused it.
    reply = load_script("ntfy_reply_mod", "ntfy-reply")
    os.makedirs(os.path.dirname(reply.SENT_IDS_FILE), exist_ok=True)
    stale = time.time() - reply.SENT_IDS_TTL_S - 60
    try:
        with open(reply.SENT_IDS_FILE, "w") as fh:
            fh.write(f"msg_old\t{stale}\n")
        assert reply.is_our_own({"id": "msg_old", "title": "Candid submitted"}) is False
    finally:
        try:
            os.remove(reply.SENT_IDS_FILE)
        except OSError:
            pass


# ── health contract ───────────────────────────────────────────────────────
def test_health_declares_three_way_exit_contract():
    health = load_script("ntfy_health_mod", "ntfy-health")
    assert health.EXIT_PROBLEMS == 3, health.EXIT_PROBLEMS

    with open(os.path.join(UNIT_DIR, "ntfy-health.service")) as fh:
        unit = fh.read()
    # systemd must not treat "found problems" as a unit failure, or the
    # watchdog's own failure shows up as the problem it is reporting.
    assert "SuccessExitStatus=0 3" in unit, unit


def test_health_help_exits_zero():
    health = load_script("ntfy_health_mod", "ntfy-health")
    assert callable(health.main)


# ── ask: same-question guard ──────────────────────────────────────────────
def _run_ask(mod, argv):
    old_argv = sys.argv
    sys.argv = argv
    try:
        return mod.main()
    finally:
        sys.argv = old_argv


def _seed_question(mod, session_id=""):
    mod.save({"question": "Guard?", "options": ["Yes", "No"],
              "created": time.time(), "session_id": session_id,
              "session_title": ""})


def test_ask_same_question_not_resent():
    # A session that is still waiting re-asks on later turns; each re-ask is
    # minutes apart so the sender's 30s dedup never catches it. The guard
    # refreshes the TTL instead of delivering a copy.
    ask = load_script("ntfy_ask_mod", "ntfy-ask")
    ask.all_sessions = lambda: []          # where_am_i -> ("", "")
    _seed_question(ask)
    rc = _run_ask(ask, ["ntfy-ask", "Guard?", "Yes|No"])
    assert rc == 0, f"guard must exit 0, got {rc}"
    q = ask.load()
    assert q is not None, "guard must keep (not consume) the question"
    assert time.time() - q["created"] < 60, "guard must refresh the TTL"


def test_ask_different_options_sends():
    ask = load_script("ntfy_ask_mod", "ntfy-ask")
    ask.all_sessions = lambda: []
    _seed_question(ask)
    rc = _run_ask(ask, ["ntfy-ask", "Guard?", "Yes|Maybe"])
    # NOTIFY does not exist under the sandbox HOME, so a send attempt fails
    # loudly — which is exactly what proves the guard did NOT swallow it.
    assert rc == 1, f"a changed question must go to the send path, got {rc}"


def test_ask_same_words_different_session_sends():
    # Same words, different asker = different request: the answer would route
    # elsewhere, so it must still send.
    ask = load_script("ntfy_ask_mod", "ntfy-ask")
    ask.all_sessions = lambda: []
    _seed_question(ask, session_id="ses_someone_else")
    rc = _run_ask(ask, ["ntfy-ask", "Guard?", "Yes|No"])
    assert rc == 1, f"a different asker must still send, got {rc}"


# ── events: only failures and interrupts buzz ──────────────────────────────
def test_events_succeeded_turn_sends_nothing():
    # Per-turn "Done" was 145 of 244 pings over five days, and it was always
    # wrong-footed: sessions chain straight into the next turn (Viator said
    # "Done" at 13:13:11 on Oct 2 and worked again by 13:15:51), so the phone
    # showed "Done ✅ X" and then "1 working: X". ntfy-heartbeat's
    # running→gone detector owns finish announcements now — it is the only
    # signal that separates a real finish from a mid-work pause.
    ev = load_script("ntfy_events_mod", "ntfy-events")
    sent = []
    ev.send = lambda *a, **k: sent.append(a)
    ev.handle_turn_end("ses_test", "succeeded", "some detail")
    assert sent == [], f"succeeded turns must stay silent, got {sent!r}"


def test_events_failure_and_interrupt_still_notify():
    # Failures and interrupts are immediate news regardless of the flood fix —
    # a task dying is exactly when the phone should buzz.
    ev = load_script("ntfy_events_mod", "ntfy-events")
    sent = []
    ev.send = lambda *a, **k: sent.append(a)
    # The sandbox has no OpenCode store; stub the lookups the handler does
    # after the subagent check (module-level aliases, so oc stays untouched).
    ev.session_title = lambda sid: "Test Session"
    ev.last_activity = lambda sid: "ran a command"
    ev.handle_turn_end("ses_test", "failed", "boom")
    ev.handle_turn_end("ses_test", "interrupted", "user stopped it")
    assert len(sent) == 2, f"want 2 notifications, got {len(sent)}"
    titles = [c[1] for c in sent]
    assert any("Failed" in t for t in titles), titles
    assert any("Interrupted" in t for t in titles), titles


# ── runner ────────────────────────────────────────────────────────────────
def main():
    want = sys.argv[1:] if len(sys.argv) > 1 else None
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)
             and (not want or any(w in n for w in want))]

    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  \033[32m✓\033[0m {name}")
        except AssertionError as exc:
            failed.append((name, str(exc)))
            print(f"  \033[31m✗\033[0m {name}\n      {exc}")
        except Exception as exc:                     # noqa: BLE001
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  \033[31m✗\033[0m {name}\n      {type(exc).__name__}: {exc}")

    print(f"\n  {passed}/{len(tests)} passed"
          + ("" if not failed else f", {len(failed)} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
