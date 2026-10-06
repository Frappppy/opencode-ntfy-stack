"""
ntfy_oc — shared access to the local OpenCode server and its message store.

These helpers were duplicated across five scripts (service_info alone existed
four times). They live here so the ntfy tools have one implementation of
"how do I talk to OpenCode", which matters because a subtle divergence is
exactly how the earlier silent-failure bugs happened.

Everything here is read-only against the app: the HTTP client is GET/POST
against a local socket, and the DB is opened with mode=ro.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import urllib.request

DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
SERVICE = os.path.expanduser("~/.local/state/opencode/service.json")

# Shared paths/config. Every ntfy tool used to declare these itself, which
# meant changing one (e.g. moving state) meant editing five files.
STATE_DIR = os.path.expanduser("~/.local/state/ntfy-heartbeat")
NOTIFY = os.path.expanduser("~/.local/bin/ntfy-notify")

# ntfy-notify keeps its own state here (metrics, markers, circuit). The reply
# bridge reads the sent-id record from this dir to prove a base-topic message
# is one of ours instead of guessing from the title.
NOTIFY_STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "ntfy-notify")
SENT_IDS_FILE = os.path.join(NOTIFY_STATE_DIR, "sent_ids")


def exec_announced_recently(session_id: str, window_s: int = 1800) -> bool:
    """
    True when ntfy-events announced this session's turn outcome recently.

    ntfy-events writes exec:{sid}:{outcome} into events_seen.json (pruned to
    the last hour) as a dedup key. The heartbeat reads it here so the same
    finish is not announced a second time up to 15 minutes later.
    """
    try:
        seen = json.load(open(os.path.join(STATE_DIR, "events_seen.json")))
    except Exception:
        return False
    now = time.time()
    for outcome in ("succeeded", "failed", "interrupted"):
        ts = seen.get(f"exec:{session_id}:{outcome}")
        if ts and (now - ts) < window_s:
            return True
    return False

# ~/.config/ntfy-notify.conf is the same file ntfy-notify sources, so both
# sides resolve the topic one way. The topic is the stack's only credential:
# anyone who knows it can read and post to it, so it lives only in that
# file — which is gitignored — and never as a literal in code.
CONFIG_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "ntfy-notify.conf")


def _conf(key: str, default: str = "") -> str:
    """Read KEY from ntfy-notify.conf.

    install.sh writes values as ${KEY:-value} so an ambient environment
    variable still wins (that is how the tests publish to scratch topics
    without ever reaching a real phone); the default is unwrapped here.
    """
    try:
        with open(CONFIG_FILE) as fh:
            lines = fh.readlines()
    except OSError:
        return default
    for line in lines:
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != key:
            continue
        # Strip the quotes BEFORE looking for the ${VAR:-default} wrapper, or
        # the leading quote hides the "$" and the wrapper is returned whole —
        # which sent the health probe to https://ntfy.sh/${...}/json and 404s.
        value = value.strip().strip('"').strip("'")
        if value.startswith("${") and ":-" in value:
            value = value[value.index(":-") + 2: value.rindex("}")]
        return value.strip() or default
    return default


TOPIC = os.environ.get("NTFY_TOPIC") or _conf("NTFY_TOPIC")
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or _conf("NTFY_SERVER")
               or "https://ntfy.sh")

# Units the watchdog treats as part of the stack.
WATCHED_UNITS = (
    "ntfy-events.service",
    "ntfy-reply.service",
    "ntfy-keepawake.service",
)
WATCHED_TIMERS = (
    "ntfy-heartbeat.timer",
    "ntfy-stuck.timer",
    "ntfy-health.timer",
    "ntfy-outbox.timer",
)

# Column width for the short label, so rosters line up and scan on a phone.
LABEL_W = 18

_FILLER = {"continue", "work", "working", "on", "the", "a", "an", "and",
           "please", "now", "resume", "start", "keep", "go"}


def shorten(title: str) -> str:
    """Condense a session title to something scannable on a phone."""
    cleaned = "".join(c if c.isalnum() or c in " -_" else " " for c in (title or ""))
    words = [w for w in cleaned.split() if w]
    # drop leading filler verbs so "Continue eToro" reads as "eToro"
    while words and words[0].lower() in _FILLER:
        words.pop(0)
    if not words:
        return "session"
    out, used = [], 0
    for w in words:
        add = len(w) + (1 if out else 0)
        if out and used + add > LABEL_W:
            # Always try to keep a second word — a single word ("Phone" from
            # "Phone notifications for tasks") is too terse to identify a
            # thread. If it will not fit, truncate it rather than drop it.
            if len(out) == 1:
                room = LABEL_W - used - 1
                if room >= 4:
                    out.append(w[: room - 1].rstrip() + "…")
            break
        out.append(w)
        used += add
    return " ".join(out)


def pad(label_text: str) -> str:
    """Left-align a label into a fixed-width column."""
    t = label_text if len(label_text) <= LABEL_W else label_text[:LABEL_W - 1] + "…"
    return t.ljust(LABEL_W)


BOUND_FILE = os.path.expanduser("~/.local/state/ntfy-heartbeat/reply_target.json")


def bind_session(session_id: str, title: str) -> None:
    """
    Record that WE asked this session something, so the next phone reply is
    routed to it.

    Only call this when a question/approval was actually put to the human.
    Calling it on delivery is wrong: it turns the binding into "last session
    we wrote to", which misroutes a later, unrelated reply. That bug sent a
    message meant for the Viator session into the notifications session.
    """
    try:
        os.makedirs(os.path.dirname(BOUND_FILE), exist_ok=True)
        with open(BOUND_FILE, "w") as fh:
            json.dump({"id": session_id, "title": title, "ts": time.time()}, fh)
    except Exception:
        pass


def bound_session(ttl_s: int = 15 * 60) -> dict | None:
    """The session we last asked something of, if the binding is still fresh."""
    try:
        with open(BOUND_FILE) as fh:
            b = json.load(fh)
    except Exception:
        return None
    if time.time() - float(b.get("ts", 0)) > ttl_s:
        return None
    return {"id": b.get("id"), "title": b.get("title") or ""}


# ── server ──────────────────────────────────────────────────────────────────
def service_info() -> tuple[str, str] | None:
    """(base_url, password) for the running OpenCode server, or None.

    Returns None if the app is not running — callers treat that as "no
    sessions", never as an error, so a closed app means silence, not alerts.
    """
    try:
        with open(SERVICE) as fh:
            meta = json.load(fh)
        pid = meta.get("pid")
        if pid and not os.path.exists(f"/proc/{pid}"):
            return None
        return meta["url"].rstrip("/"), meta["password"]
    except Exception:
        return None

def api(base: str, pw: str, path: str, payload=None, method=None,
        timeout: float = 15.0):
    """Authenticated call to the OpenCode HTTP API."""
    token = base64.b64encode(f"opencode:{pw}".encode()).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{base}{path}", data=data, method=method,
        headers={"Authorization": f"Basic {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
        return json.loads(body) if body.strip() else {}

def get(path: str, timeout: float = 10.0):
    """GET a path on the running server. Returns None if unreachable."""
    svc = service_info()
    if not svc:
        return None
    try:
        return api(svc[0], svc[1], path, timeout=timeout)
    except Exception:
        return None

# ── sessions ────────────────────────────────────────────────────────────────
def running_sessions() -> list[dict]:
    """
    Sessions the server currently reports as running, as {id, title}.

    Sorted alphabetically by title so any "#N" shown to the user is stable
    between calls. The server's own dict order is NOT stable, and using it
    for numbering would silently change what "#1" refers to.
    """
    active = (get("/api/session/active") or {}).get("data") or {}
    if not active:
        return []
    sessions = (get("/api/session") or {}).get("data") or []
    by_id = {s["id"]: s for s in sessions}
    out = []
    for sid, st in active.items():
        if isinstance(st, dict) and st.get("type") == "running":
            out.append({"id": sid, "title": by_id.get(sid, {}).get("title") or sid})
    out.sort(key=lambda s: s["title"].lower())
    return out


def all_sessions() -> list[dict]:
    """
    Every session, numbered for phone replies.

    Active sessions come first (1..n), then parked ones (n+1..m), each group
    sorted alphabetically so a "#N" keeps meaning the same thing while the
    set is unchanged.

    Each entry: {id, title, running: bool, idle_for: str}
    """
    sessions = (get("/api/session") or {}).get("data") or []
    active = (get("/api/session/active") or {}).get("data") or {}
    now = time.time()

    live, parked = [], []
    for s in sessions:
        sid = s.get("id")
        st = active.get(sid)
        is_live = isinstance(st, dict) and st.get("type") == "running"
        updated = (s.get("time") or {}).get("updated") or 0
        entry = {
            "id": sid,
            "title": s.get("title") or sid,
            "running": bool(is_live),
            "updated_ms": updated,
            "idle_for": "" if is_live else humanise_age(now - updated / 1000),
        }
        (live if is_live else parked).append(entry)

    live.sort(key=lambda s: s["title"].lower())
    # Parked sessions: most recently touched first, so "what did I just stop
    # doing" is at the top rather than buried alphabetically.
    parked.sort(key=lambda s: s.get("updated_ms", 0), reverse=True)
    return live + parked


_SUBAGENT_CACHE: dict[str, bool] = {}


def is_subagent(session_id: str) -> bool:
    """
    True when a session was spawned as a child (subagent), not by a human.

    Subagents are an implementation detail of the harness: their turns fire
    the same session.execution.* events as real ones, so without this the
    phone gets a "task done" ping for every background agent. ntfy-events
    promised this filter in a comment while doing nothing — this is it.

    Fails OPEN: if the store is unreachable we cannot tell, and dropping a
    real completion is worse than an occasional extra ping.
    """
    if not session_id:
        return False
    if session_id in _SUBAGENT_CACHE:
        return _SUBAGENT_CACHE[session_id]
    try:
        row = db().execute(
            "select parent_id from session_v2 where id=?", (session_id,)
        ).fetchone()
        result = bool(row and (row[0] or "").strip())
    except Exception:
        return False
    _SUBAGENT_CACHE[session_id] = result
    return result


def roster_lines() -> list[str]:
    """
    One canonical, phone-shaped view of EVERY session.

    Working sessions first, then idle ones with how long they have been idle.
    Past, present and future are all visible at a glance — a session that has
    not started yet simply sits in the idle group.

    No cap: every session is listed. Truncating a list Frappppy uses to pick a
    thread is worse than a long message he can scroll.
    """
    sessions = all_sessions()
    live = [s for s in sessions if s["running"]]
    parked = [s for s in sessions if not s["running"]]

    lines: list[str] = []
    if live:
        lines.append("WORKING NOW")
        for i, s in enumerate(live, 1):
            lines.append(f"#{i}  {pad(shorten(s['title']))}")
    if parked:
        if lines:
            lines.append("")
        lines.append("IDLE")
        for i, s in enumerate(parked, len(live) + 1):
            lines.append(f"#{i}  {pad(shorten(s['title']))}  {s['idle_for']}")
    if not lines:
        lines.append("No sessions yet.")
    return lines


def roster_text() -> str:
    """The roster as one string. The only renderer — every script uses this."""
    return "\n".join(roster_lines())


def humanise_age(seconds: float) -> str:
    """Compact age: 'just now', '12m ago', '3h ago', '2d ago'."""
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"

def session_title(session_id: str) -> str:
    """Best-effort title for a session id (API first, then the store)."""
    for s in (get("/api/session") or {}).get("data") or []:
        if s["id"] == session_id:
            return s.get("title") or session_id
    row = db().execute(
        "select title from session_v2 where id=?", (session_id,)
    ).fetchone()
    return (row and row[0]) or session_id

# ── message store ───────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    """Read-only connection to OpenCode's SQLite store.

    Safe to open while the app is running: SQLite in WAL mode allows
    concurrent readers.
    """
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
