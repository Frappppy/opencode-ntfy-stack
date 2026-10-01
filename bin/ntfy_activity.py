"""
ntfy_activity — turn OpenCode's internal activity into short plain English.

The notifications used to show raw tool calls and shell commands
("shell — curl -sS -m 6 https://..."). That is machine detail, not status.
This module maps what a session is doing onto a human phrase, and never
exposes commands, file contents, or arguments.

Shared by ntfy-events and ntfy-heartbeat.
"""

from __future__ import annotations

import re

# ── tool name → plain English ────────────────────────────────────────────────
# describe_tool() answers for read/write/edit/patch and bash/shell/execute
# BEFORE this table is consulted, so entries for those names could never be
# reached — and where they could be reached they disagreed with the branch
# that wins ("Writing a file" vs "Editing files"). Only names that actually
# fall through to _tool_phrase belong here.
TOOL_PHRASES = {
    "grep": "Searching the code",
    "glob": "Looking for files",
    "list": "Listing files",
    "ls": "Listing files",
    "webfetch": "Reading a web page",
    "websearch": "Searching online",
    "todowrite": "Updating the task list",
    "todoread": "Checking the task list",
    "question": "Asking you a question",
    "task": "Handing work to a helper",
    "agent": "Handing work to a helper",
    "skill": "Using a skill",
    "python": "Running a script",
}


def _tool_phrase(name: str) -> str:
    n = (name or "").lower()
    if n in TOOL_PHRASES:
        return TOOL_PHRASES[n]
    if n.startswith("browser"):
        return "Using the browser"
    if "search" in n:
        return "Searching for something"
    if n.startswith("session"):
        return "Managing a session"
    if n.startswith("permission"):
        return "Asking for permission"
    return "Working"


# ── shell command → intent (never the command itself) ───────────────────────
def _shell_phrase(cmd: str) -> str:
    """Infer what a shell command is FOR, without revealing the command."""
    c = " ".join((cmd or "").split()).lower()

    plain_word = re.compile(r"^[A-Za-z0-9_]+$")

    def has(*words: str) -> bool:
        """
        Whole-word match for ordinary words, substring match otherwise.

        Bare `w in c` was how "results" became Looking at the disk (it
        contains "ls"), "desktop" became Checking running processes ("top"),
        "update" became Checking the machine ("date"), and "--flag" became
        Searching the code ("ag "). A trailing digit is still accepted so
        python3 / node18 match python / node.
        """
        for w in words:
            core = w.strip()
            if not core:
                continue
            if plain_word.match(core):
                if re.search(r"(?<![\w-])" + re.escape(core) + r"(?![A-Za-z_-])", c):
                    return True
            elif w in c:
                return True
        return False

    # order matters: most specific first
    if has("sqlite3", "select ", "insert ", "create table"):
        return "Checking a database"
    if has("curl", "wget", "http://", "https://"):
        return "Checking a website"
    if has("grep ", "rg ", "ripgrep", "ack ", "ag "):
        return "Searching the code"
    if has("find ", "locate ", "fd "):
        return "Looking for files"
    if has("cat ", "head ", "tail ", "less ", "sed -n", "jq "):
        return "Reading something"
    if has("python", "node ", "deno ", "bun "):
        return "Running a script"
    if has("git "):
        return "Working with git"
    # tests BEFORE builds: the comment above says "most specific first", but
    # "npm " and "cargo " on the build line swallowed "npm test" and
    # "cargo test", so running the tests was reported as Building the project.
    if has("pytest", "jest", "vitest", "npm test", "go test", "cargo test"):
        return "Running tests"
    if has("npm ", "make", "cargo ", "go build", "tsc", "webpack", "vite", "pnpm", "yarn"):
        return "Building the project"
    if has("chmod", "chown", "mount", "systemctl", "journalctl", "service "):
        return "Checking the system"
    if has("sed -i", "tee ", ">", ">>", "patch "):
        return "Changing a file"
    if has("rm ", "mv ", "cp ", "mkdir", "rmdir"):
        return "Organising files"
    if has("ls", "tree", "du ", "df "):
        return "Looking at the disk"
    if has("ps ", "top", "htop", "kill ", "pgrep"):
        return "Checking running processes"
    if has("date", "uptime", "whoami", "uname", "hostname"):
        return "Checking the machine"
    if has("sleep"):
        return "Waiting"
    if has("opencode", "ntfy"):
        return "Setting up notifications"
    return "Running a command"


def describe_tool(name: str, state: dict | None = None) -> str:
    """Plain-English phrase for a tool call. Never returns the input."""
    name = name or ""
    n = name.lower()
    if n in ("shell", "bash", "execute"):
        inp = (state or {}).get("input") or {}
        cmd = inp.get("command") if isinstance(inp, dict) else ""
        if isinstance(cmd, str) and cmd.strip():
            return _shell_phrase(cmd)
        return "Running a command"
    if n.startswith("browser"):
        return "Using the browser"
    if n in ("read",):
        return "Reading a file"
    if n in ("write", "edit", "multiedit", "patch"):
        return "Editing files"
    # both branches were identical
    return _tool_phrase(n)


# ── short, human conclusion from an assistant message ───────────────────────
def plain_conclusion(items: list[dict], limit: int = 90) -> str:
    """
    The assistant's own closing words, lightly trimmed.
    Prefers what it SAID (natural language) over what it ran (technical).
    """
    for it in reversed(items or []):
        if it.get("type") != "text":
            continue
        txt = (it.get("text") or "").strip()
        if not txt:
            continue
        # first sentence-ish chunk, so it stays short
        txt = " ".join(txt.split())
        for stop in (". ", "? ", "! ", "\n"):
            i = txt.find(stop)
            if 0 < i < limit:
                txt = txt[: i + 1]
                break
        if len(txt) > limit:
            txt = txt[: limit - 1].rstrip() + "…"
        return txt
    return ""


def plain_working(items: list[dict]) -> str:
    """What is happening right now, in a few words."""
    # 1) something actively in flight is the most accurate
    for it in reversed(items or []):
        if it.get("type") != "tool":
            continue
        st = it.get("state") or {}
        if st.get("status") in ("running", "streaming", "pending"):
            return describe_tool(it.get("name"), st)

    # 2) otherwise what it most recently said
    said = plain_conclusion(items)
    if said:
        return said

    # 3) last tool it finished
    for it in reversed(items or []):
        if it.get("type") == "tool":
            return describe_tool(it.get("name"), it.get("state") or {})

    return "Thinking it through"
