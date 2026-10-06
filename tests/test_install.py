#!/usr/bin/env python3
"""
Regression tests for install.sh.

These run against a throwaway $HOME — and that is the whole point. systemd
resolves the user by D-Bus, NOT by $HOME, so a sandboxed install used to
redirect the *files* while `systemctl --user` still aimed at the LIVE session.
`--uninstall` in a test therefore disabled the real ntfy-events / ntfy-reply /
ntfy-keepawake and all three timers, and the stack had to be re-enabled by hand.

`test_sandbox_uninstall_leaves_live_systemd_alone` is the test that catches it.

    python3 tests/test_install.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL = os.path.normpath(os.path.join(HERE, "..", "install.sh"))

# The units install.sh is allowed to switch on or off.
LIVE_UNITS = [
    "ntfy-events.service",
    "ntfy-reply.service",
    "ntfy-keepawake.service",
    "ntfy-heartbeat.timer",
    "ntfy-stuck.timer",
    "ntfy-health.timer",
    "ntfy-outbox.timer",
]


def sandbox():
    root = tempfile.mkdtemp(prefix="ntfy-install-")
    return root, os.path.join(root, ".config")


def run(root, conf_home, *args, timeout=120):
    env = dict(os.environ)
    env["HOME"] = root
    env["XDG_CONFIG_HOME"] = conf_home
    env.pop("NTFY_TOPIC", None)
    return subprocess.run([INSTALL, *args], env=env,
                          capture_output=True, text=True, timeout=timeout)


def live_state() -> list[str]:
    """active/enabled state of the real units — the thing we must not touch."""
    out = []
    for u in LIVE_UNITS:
        r = subprocess.run(["systemctl", "--user", "is-active", u],
                           capture_output=True, text=True)
        out.append(f"{u}={r.stdout.strip()}")
    return out


def count(path):
    try:
        return len([f for f in os.listdir(path)])
    except OSError:
        return 0


def units_dir(conf_home: str) -> str:
    """Where install.sh puts the units: $XDG_CONFIG_HOME/systemd/user."""
    return os.path.join(conf_home, "systemd", "user")


# ── CLI ───────────────────────────────────────────────────────────────────
def test_help_exits_zero_and_is_clean():
    root, conf = sandbox()
    res = run(root, conf, "--help")
    assert res.returncode == 0, f"want 0, got {res.returncode}"
    assert "install.sh" in res.stdout, res.stdout
    # an earlier version sliced by line number and printed `set -euo pipefail`
    assert "set -euo" not in res.stdout, "help leaks shell source lines"


def test_unknown_option_exits_two():
    root, conf = sandbox()
    res = run(root, conf, "--nonsense")
    assert res.returncode == 2, f"want 2, got {res.returncode}"
    assert "unknown option" in res.stderr


def test_topic_without_a_value_fails_loudly():
    root, conf = sandbox()
    res = run(root, conf, "--topic")
    assert res.returncode != 0, "a value-less --topic must not succeed"
    assert "needs a value" in (res.stdout + res.stderr)


# ── install ───────────────────────────────────────────────────────────────
def test_fresh_install_lays_down_everything():
    root, conf = sandbox()
    res = run(root, conf, "--no-enable", "--topic", "opencode-aaaabbbbccccddddeeeeffff")
    assert res.returncode == 0, res.stderr
    assert count(os.path.join(root, ".local", "bin")) == 11, "want 11 scripts"
    assert count(units_dir(conf)) == 11, f"want 11 units, got {count(units_dir(conf))}"


def test_installed_scripts_are_executable():
    root, conf = sandbox()
    run(root, conf, "--no-enable", "--topic", "opencode-aaaabbbbccccddddeeeeffff")
    notify = os.path.join(root, ".local", "bin", "ntfy-notify")
    assert os.access(notify, os.X_OK), "ntfy-notify must be executable"
    assert os.access(INSTALL, os.X_OK), "install.sh must be executable"


def test_config_written_with_0600():
    root, conf = sandbox()
    run(root, conf, "--no-enable", "--topic", "opencode-aaaabbbbccccddddeeeeffff")
    path = os.path.join(conf, "ntfy-notify.conf")
    assert os.path.exists(path), "config must be created"
    mode = oct(os.stat(path).st_mode & 0o777)
    assert mode == "0o600", f"credential file must be 0600, got {mode}"
    with open(path) as fh:
        assert "opencode-aaaabbbbccccddddeeeeffff" in fh.read()


def test_rerun_never_clobbers_an_existing_config():
    # Updating the stack must not destroy the credential.
    root, conf = sandbox()
    run(root, conf, "--no-enable", "--topic", "opencode-original-topic-here")
    path = os.path.join(conf, "ntfy-notify.conf")
    with open(path, "w") as fh:
        fh.write('NTFY_TOPIC="${NTFY_TOPIC:-opencode-my-secret}"\n')

    res = run(root, conf, "--no-enable")
    assert res.returncode == 0, res.stderr
    with open(path) as fh:
        body = fh.read()
    assert "opencode-my-secret" in body, "config was clobbered by a re-run"
    assert "keeping existing" in res.stdout, res.stdout


# ── the regression this file exists for ───────────────────────────────────
def test_sandbox_install_does_not_touch_systemd():
    root, conf = sandbox()
    before = live_state()
    res = run(root, conf, "--no-enable", "--topic", "opencode-aaaabbbbccccddddeeeeffff")
    after = live_state()
    assert res.returncode == 0, res.stderr
    assert before == after, (
        "a sandboxed install changed the LIVE systemd state!\n"
        f"  before: {before}\n  after : {after}\n"
        "  restore with: systemctl --user enable --now ntfy-events.service "
        "ntfy-reply.service ntfy-keepawake.service "
        "ntfy-heartbeat.timer ntfy-stuck.timer ntfy-health.timer "
        "ntfy-outbox.timer"
    )


def test_sandbox_uninstall_leaves_live_systemd_alone():
    """The bug that took the real stack down on 2026-10-01."""
    root, conf = sandbox()
    run(root, conf, "--no-enable", "--topic", "opencode-aaaabbbbccccddddeeeeffff")

    before = live_state()
    res = run(root, conf, "--uninstall")
    after = live_state()

    assert res.returncode == 0, res.stderr
    # it must still clean up its own files...
    assert count(os.path.join(root, ".local", "bin")) == 0, "scripts not removed"
    assert count(units_dir(conf)) == 0, f"units not removed: {os.listdir(units_dir(conf))}"
    # ...but must say it declined to touch systemd
    assert "leaving systemd alone" in res.stdout, res.stdout
    assert before == after, (
        "SANDBOX --uninstall DISABLED THE LIVE STACK — the guard is gone.\n"
        f"  before: {before}\n  after : {after}\n"
        "  restore with: systemctl --user enable --now ntfy-events.service "
        "ntfy-reply.service ntfy-keepawake.service "
        "ntfy-heartbeat.timer ntfy-stuck.timer ntfy-health.timer "
        "ntfy-outbox.timer"
    )


def test_uninstall_keeps_the_config_but_purge_removes_it():
    root, conf = sandbox()
    run(root, conf, "--no-enable", "--topic", "opencode-aaaabbbbccccddddeeeeffff")
    path = os.path.join(conf, "ntfy-notify.conf")

    before = live_state()
    run(root, conf, "--uninstall")
    assert os.path.exists(path), "--uninstall must keep the credential"

    res = run(root, conf, "--purge")
    after = live_state()
    assert res.returncode == 0, res.stderr
    assert not os.path.exists(path), "--purge must delete the credential"
    assert before == after, "purge touched the LIVE systemd state"
    assert "credential destroyed" in res.stdout, res.stdout


def test_timer_driven_service_files_are_removed_too():
    # Enumerating what to delete from the *enable* list left the three
    # timer-driven .service companions behind on disk.
    root, conf = sandbox()
    run(root, conf, "--no-enable", "--topic", "opencode-aaaabbbbccccddddeeeeffff")
    units = units_dir(conf)
    assert os.path.exists(os.path.join(units, "ntfy-heartbeat.service")), \
        "install must copy the timer-driven .service companions"
    run(root, conf, "--uninstall")
    assert count(units) == 0, "left orphaned unit files behind"


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
