#!/usr/bin/env python3
"""
Static checks that catch the boring drift by hand-testing never does:

  1. every flag a shell script parses is documented in its usage text
  2. every argparse argument has a help string
  3. every script has a shebang and is executable
  4. nothing hardcodes /home/<someone> — a new machine has a new username

    python3 tests/argcheck.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.normpath(os.path.join(HERE, "..", "bin"))

problems: list[str] = []
checked = 0


def fail(msg: str) -> None:
    problems.append(msg)


# ── shell ─────────────────────────────────────────────────────────────────
def getopts_flags(text: str) -> set[str]:
    """Letters declared in `getopts "<optstring>"`."""
    m = re.search(r'getopts\s+"([^"]+)"', text)
    if not m:
        return set()
    spec = m.group(1).lstrip(":")
    flags = set()
    for ch in spec:
        if ch.isalpha():
            flags.add(ch)
    return flags


def usage_body(text: str) -> str:
    """The heredoc inside usage() — where flags are documented."""
    start = text.find("usage()")
    if start < 0:
        return ""
    op = text.find("<<'EOF'", start)
    if op < 0:
        return text[start:start + 2000]
    end = text.find("\nEOF", op)
    return text[op:end if end > 0 else op + 2000]


def check_shell(path: str) -> None:
    global checked
    name = os.path.basename(path)
    with open(path) as fh:
        text = fh.read()

    flags = getopts_flags(text)
    if not flags:
        return
    body = usage_body(text)
    if not body:
        fail(f"{name}: parses flags but usage() has no documented text")
        return
    for flag in sorted(flags):
        checked += 1
        if not re.search(rf"(^|[\s|])-{flag}(\s|$|[\s|])", body, re.M):
            fail(f"{name}: flag -{flag} is parsed but never documented in usage")


# ── python ────────────────────────────────────────────────────────────────
def check_python(path: str) -> None:
    global checked
    name = os.path.basename(path)
    with open(path) as fh:
        text = fh.read()

    if "add_argument(" not in text:
        return
    chunks = text.split("add_argument(")[1:]
    for i, chunk in enumerate(chunks, 1):
        checked += 1
        if "help=" not in chunk[:400]:
            # name the argument so the message is actionable
            m = re.search(r'["\'](-{1,2}[\w-]+)["\']', chunk[:200])
            what = m.group(1) if m else f"#{i}"
            fail(f"{name}: {what} has no help=")


# ── both ──────────────────────────────────────────────────────────────────
def is_library(path: str, text: str) -> bool:
    """A .py with no __main__ guard is imported, never invoked.

    Libraries are not entry points: they need neither a shebang nor the
    executable bit, and demanding both would just push people into adding
    meaningless boilerplate to working modules.
    """
    return path.endswith(".py") and '__name__ == "__main__"' not in text


def check_portable_and_exec(path: str) -> None:
    global checked
    name = os.path.basename(path)
    with open(path) as fh:
        text = fh.read()
    library = is_library(path, text)

    checked += 1
    if not text.startswith("#!") and not library:
        fail(f"{name}: missing shebang")

    checked += 1
    if not library and not os.access(path, os.X_OK):
        fail(f"{name}: not executable (chmod +x)")

    checked += 1
    hardcoded = re.findall(r"/home/[A-Za-z0-9._-]+", text)
    if hardcoded:
        fail(f"{name}: hardcoded home path {sorted(set(hardcoded))} breaks new machines")


def main() -> int:
    files = sorted(
        os.path.join(BIN, f) for f in os.listdir(BIN)
        if os.path.isfile(os.path.join(BIN, f)) and not f.endswith(".pyc")
    )
    if not files:
        print(f"  no scripts found under {BIN}", file=sys.stderr)
        return 2

    for path in files:
        if path.endswith(".py"):
            check_python(path)
        else:
            check_shell(path)
        check_portable_and_exec(path)

    print(f"  checked {len(files)} scripts, {checked} assertions")
    if problems:
        print(f"\n  {len(problems)} problem(s):")
        for p in problems:
            print(f"    \033[31m✗\033[0m {p}")
        return 1
    print("  \033[32m✓ all clean\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
