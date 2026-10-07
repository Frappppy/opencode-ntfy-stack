#!/usr/bin/env python3
"""
Regression tests for ntfy-notify — the sender every other tool depends on.

Each test runs the REAL script against a local HTTP listener under a
throwaway $HOME, so the suite never reads your config, never touches your
state, and never reaches your phone.

    python3 tests/test_notify.py          # run everything
    python3 tests/test_notify.py dedup    # run tests whose name contains "dedup"
"""
from __future__ import annotations

import concurrent.futures
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NOTIFY = os.path.normpath(os.path.join(HERE, "..", "bin", "ntfy-notify"))
OUTBOX = os.path.normpath(os.path.join(HERE, "..", "bin", "ntfy-outbox"))
TOPIC = "unit-test-topic"


# ── local HTTP listener ───────────────────────────────────────────────────
class Listener:
    """Records every request and answers with a standing response.

    `reply` is a (status, body) tuple you can flip mid-test, which is how the
    suite proves "the server recovered and the retry went through".
    """

    def __init__(self, reply=(200, '{"id":"ok"}'), delay=0.0):
        self.reply = reply
        self.delay = delay
        self.requests = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self._sock.settimeout(0.2)
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > 1 << 16:
                    break
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode(errors="replace").split("\r\n")
            clen = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    clen = int(line.split(":", 1)[1].strip() or 0)
            while len(rest) < clen:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                rest += chunk

            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            request = {
                "path": lines[0] if lines else "",
                "headers": headers,
                "body": rest.decode(errors="replace"),
            }
            self.requests.append(request)

            if self.delay:
                time.sleep(self.delay)
            response = self.reply(request) if callable(self.reply) else self.reply
            status, body = response
            payload = body.encode()
            conn.sendall(
                b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % (status, len(payload)) + payload
            )
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass


# ── sandbox + invocation ──────────────────────────────────────────────────
def sandbox(url, config="real", topic=TOPIC):
    """A throwaway HOME with its own conf, state, and a mute notify-send."""
    root = tempfile.mkdtemp(prefix="ntfy-notify-")
    os.makedirs(os.path.join(root, ".config"), exist_ok=True)
    os.makedirs(os.path.join(root, "bin"), exist_ok=True)
    # ntfy-notify mkdir's this on start, but several tests seed the marker or
    # the circuit BEFORE the first run — create it up front so they can.
    os.makedirs(os.path.join(root, ".state", "ntfy-notify"), exist_ok=True)

    # Any failure in these tests fires desktop_fallback. Mute it, or a passing
    # suite still sprays popups across the desktop.
    stub = os.path.join(root, "bin", "notify-send")
    with open(stub, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(stub, 0o755)

    if config == "real":
        with open(os.path.join(root, ".config", "ntfy-notify.conf"), "w") as fh:
            fh.write(f'NTFY_TOPIC="${{NTFY_TOPIC:-{topic}}}"\n')
    elif config == "placeholder":
        with open(os.path.join(root, ".config", "ntfy-notify.conf"), "w") as fh:
            fh.write('NTFY_TOPIC="${NTFY_TOPIC:-REPLACE_WITH_YOUR_TOPIC}"\n')

    env = dict(os.environ)
    env["HOME"] = root
    env["PATH"] = os.path.join(root, "bin") + os.pathsep + env.get("PATH", "")
    env["XDG_CONFIG_HOME"] = os.path.join(root, ".config")
    env["XDG_STATE_HOME"] = os.path.join(root, ".state")
    env["NTFY_SERVER"] = url
    for key in ("NTFY_TOPIC", "NTFY_AUTH", "NTFY_DEDUP_WINDOW"):
        env.pop(key, None)
    return root, env


def run(env, *args, timeout=90):
    return subprocess.run([NOTIFY, *args], env=env,
                          capture_output=True, text=True, timeout=timeout)


def state(env, name):
    """Path of a state file inside the sandbox."""
    return os.path.join(env["XDG_STATE_HOME"], "ntfy-notify", name)


def read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def requests_to(lst, env, *args):
    """Run the sender, return (result, requests it actually made)."""
    before = len(lst.requests)
    res = run(env, *args)
    return res, lst.requests[before:]


def closed_port():
    """A port nothing is listening on (bind then release)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── CLI / configuration ───────────────────────────────────────────────────
def test_help_exits_zero():
    root, env = sandbox("http://127.0.0.1:1")
    try:
        res = run(env, "-h")
        assert res.returncode == 0, f"-h must exit 0, got {res.returncode}"
        assert "-p" in res.stdout and "-t" in res.stdout, "usage must list flags"
    finally:
        pass


def test_unknown_flag_exits_two():
    _, env = sandbox("http://127.0.0.1:1")
    res = run(env, "-z", "nope")
    assert res.returncode == 2, f"bad flag must exit 2, got {res.returncode}"
    assert "Invalid option" in res.stderr


def test_missing_message_exits_two():
    _, env = sandbox("http://127.0.0.1:1")
    res = run(env)
    assert res.returncode == 2, f"no message must exit 2, got {res.returncode}"


def test_missing_config_exits_two_and_sends_nothing():
    lst = Listener()
    try:
        _, env = sandbox(lst.url, config="none")
        res, sent = requests_to(lst, env, "should never leave the box")
        assert res.returncode == 2, f"unconfigured must exit 2, got {res.returncode}"
        assert "no topic configured" in res.stderr
        assert "ntfy-notify.conf" in res.stderr, "error must name the file to fix"
        assert sent == [], f"must send nothing without a topic, sent {len(sent)}"
    finally:
        lst.close()


def test_help_works_without_config():
    # -h must not require configuration, or a fresh machine cannot even
    # discover the flags it needs.
    _, env = sandbox("http://127.0.0.1:1", config="none")
    res = run(env, "-h")
    assert res.returncode == 0, f"-h without config must exit 0, got {res.returncode}"


def test_placeholder_topic_rejected():
    lst = Listener()
    try:
        _, env = sandbox(lst.url, config="placeholder")
        res, sent = requests_to(lst, env, "placeholder probe")
        assert res.returncode == 2, f"placeholder must be rejected, got {res.returncode}"
        assert sent == [], "an unedited example conf must never publish"
    finally:
        lst.close()


# ── delivery ──────────────────────────────────────────────────────────────
def test_success_sends_to_configured_topic():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        res, sent = requests_to(lst, env, "hello world")
        assert res.returncode == 0, f"want 0, got {res.returncode}: {res.stderr}"
        assert len(sent) == 1, f"want 1 request, got {len(sent)}"
        assert f"/{TOPIC}" in sent[0]["path"], f"wrong topic: {sent[0]['path']}"
        assert "notified" in res.stdout
    finally:
        lst.close()


def test_headers_priority_title_tag():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        res, sent = requests_to(
            lst, env, "-p", "high", "the body", "Some Title", "warning")
        assert res.returncode == 0, res.stderr
        h = sent[0]["headers"]
        assert h.get("priority") == "high", h.get("priority")
        assert h.get("title") == "Some Title", h.get("title")
        assert h.get("tags") == "warning", h.get("tags")
        assert sent[0]["body"] == "the body"
    finally:
        lst.close()


def test_click_and_group_headers():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        res, sent = requests_to(
            lst, env, "-c", "https://example.test/pr/5", "-g", "pr-5", "review")
        assert res.returncode == 0, res.stderr
        h = sent[0]["headers"]
        assert h.get("click") == "https://example.test/pr/5", h.get("click")
        assert h.get("group") == "pr-5", h.get("group")
    finally:
        lst.close()


def test_http_500_retries_then_fails():
    lst = Listener(reply=(500, '{"error":"boom"}'))
    try:
        _, env = sandbox(lst.url)
        res, sent = requests_to(lst, env, "will fail")
        assert res.returncode == 1, f"undelivered must exit 1, got {res.returncode}"
        assert len(sent) == 3, f"routine send must try 3 times, tried {len(sent)}"
        assert "FAILED after 3 attempt(s)" in res.stderr, res.stderr
        # honest count: exactly what it tried
        assert "FAILED after 1" not in res.stderr
    finally:
        lst.close()


def test_unreachable_server_fails():
    port = closed_port()
    _, env = sandbox(f"http://127.0.0.1:{port}")
    res = run(env, "nobody is listening")
    assert res.returncode == 1, f"want 1 on dead endpoint, got {res.returncode}"
    assert "FAILED after" in res.stderr


def test_success_writes_metrics():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        run(env, "metric me")
        metrics = read(state(env, "metrics.log"))
        assert "\tsuccess\t1\t200\t" in metrics, metrics
        assert TOPIC in metrics, "metrics must record which topic was used"
    finally:
        lst.close()


def test_failure_writes_metrics():
    lst = Listener(reply=(500, '{"error":"nope"}'))
    try:
        _, env = sandbox(lst.url)
        run(env, "metric me")
        metrics = read(state(env, "metrics.log"))
        assert metrics.count("\tfail\t") == 3, f"want 3 fail lines:\n{metrics}"
        assert "\texhausted\t" in metrics, "must record the final exhaustion"
    finally:
        lst.close()


# ── deduplication ─────────────────────────────────────────────────────────
def test_duplicate_suppressed():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        first, _ = requests_to(lst, env, "same message")
        assert first.returncode == 0
        second, sent = requests_to(lst, env, "same message")
        assert second.returncode == 0, "a suppressed duplicate is not an error"
        assert "Suppressed duplicate" in second.stderr, second.stderr
        assert sent == [], f"duplicate reached the server ({len(sent)} requests)"
    finally:
        lst.close()


def test_different_message_sends():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        requests_to(lst, env, "message one")
        res, sent = requests_to(lst, env, "message two")
        assert res.returncode == 0
        assert len(sent) == 1, "a different message must still send"
    finally:
        lst.close()


def test_failed_send_is_not_recorded_as_sent():
    # THE bug this guards: recording the hash before the send meant a message
    # that failed every attempt was marked sent, and the retry that mattered
    # was swallowed as a duplicate.
    lst = Listener(reply=(500, '{"error":"down"}'))
    try:
        _, env = sandbox(lst.url)
        failed, _ = requests_to(lst, env, "flaky message")
        assert failed.returncode == 1, "first try must fail"

        lst.reply = (200, '{"id":"ok"}')          # server recovers
        retry, sent = requests_to(lst, env, "flaky message")
        assert retry.returncode == 0, f"retry must deliver: {retry.stderr}"
        assert len(sent) >= 1, "the retry must actually reach the server"
    finally:
        lst.close()


# ── refusal marker (daily quota) ──────────────────────────────────────────
def test_429_writes_publish_blocked_marker():
    lst = Listener(reply=(429, '{"code":429,"error":"daily message quota reached"}'))
    try:
        _, env = sandbox(lst.url)
        res = run(env, "quota probe")
        assert res.returncode == 1
        marker = read(state(env, "publish_blocked"))
        assert marker, "a 429 must leave a marker behind"
        ts, _, body = marker.partition("\n")
        assert ts.isdigit(), f"first line must be an epoch, got {ts!r}"
        assert "quota" in body, "marker must keep the reason ntfy gave us"
    finally:
        lst.close()


def test_marker_forces_single_attempt():
    # Looping at a counter that resets once a day only burns the caller's
    # 120s timeout — one attempt still proves it and still wins if it rolled.
    lst = Listener(reply=(500, '{"error":"still refused"}'))
    try:
        _, env = sandbox(lst.url)
        with open(state(env, "publish_blocked"), "w") as fh:
            fh.write(f"{int(time.time())}\n" + '{"error":"daily message quota reached"}\n')
        res, sent = requests_to(lst, env, "after refusal")
        assert res.returncode == 1
        assert len(sent) == 1, f"a fresh marker means ONE attempt, got {len(sent)}"
        assert "single attempt" in res.stderr, res.stderr
    finally:
        lst.close()


def test_success_clears_marker():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        marker_path = state(env, "publish_blocked")
        with open(marker_path, "w") as fh:
            fh.write(f"{int(time.time())}\n" + '{"error":"quota"}\n')
        res, _ = requests_to(lst, env, "recovery")
        assert res.returncode == 0, res.stderr
        assert not os.path.exists(marker_path), "a delivered message must clear it"
    finally:
        lst.close()


def test_stale_marker_ignored():
    lst = Listener(reply=(500, '{"error":"nope"}'))
    try:
        _, env = sandbox(lst.url)
        with open(state(env, "publish_blocked"), "w") as fh:
            fh.write(f"{int(time.time()) - 7200}\n" + '{"error":"old"}\n')
        res, sent = requests_to(lst, env, "stale marker")
        assert len(sent) == 3, f"a >1h marker must not degrade the send, got {len(sent)}"
        assert "single attempt" not in res.stderr
    finally:
        lst.close()


# ── circuit breaker ───────────────────────────────────────────────────────
def test_exhausted_send_trips_circuit():
    lst = Listener(reply=(500, '{"error":"down"}'))
    try:
        _, env = sandbox(lst.url)
        run(env, "trip the circuit")
        assert os.path.exists(state(env, "circuit")), "an exhausted send must trip it"
    finally:
        lst.close()


def test_open_circuit_degrades_to_one_attempt():
    lst = Listener(reply=(500, '{"error":"down"}'))
    try:
        _, env = sandbox(lst.url)
        with open(state(env, "circuit"), "w") as fh:
            fh.write(str(int(time.time())))
        res, sent = requests_to(lst, env, "routine while open")
        assert len(sent) == 1, f"open circuit must not hammer, sent {len(sent)}"
        assert "Circuit open" in res.stderr, res.stderr
        # ...but it must still try. Skipping + exit 0 was the silent-loss bug.
        assert res.returncode == 1, "degraded still reports honestly"
    finally:
        lst.close()


def test_critical_bypasses_circuit():
    lst = Listener(reply=(500, '{"error":"down"}'))
    try:
        _, env = sandbox(lst.url)
        with open(state(env, "circuit"), "w") as fh:
            fh.write(str(int(time.time())))
        res, _ = requests_to(lst, env, "-p", "high", "urgent alert")
        assert "Circuit open" not in res.stderr, \
            "a high-priority message must ignore the breaker"
        # 8 attempts would take far too long; the marker above proves intent.
    finally:
        lst.close()


def test_success_clears_circuit():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        circuit = state(env, "circuit")
        with open(circuit, "w") as fh:
            fh.write(str(int(time.time())))
        res, _ = requests_to(lst, env, "delivery resets it")
        assert res.returncode == 0, res.stderr
        assert not os.path.exists(circuit), "success must close the breaker"
    finally:
        lst.close()


# ── templates ─────────────────────────────────────────────────────────────
def test_template_expansion():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        res, sent = requests_to(lst, env, "-t", "build_failed", "unit tests broke")
        assert res.returncode == 0, res.stderr
        body = sent[0]["body"]
        assert "Build failed" in body and "unit tests broke" in body, body
        assert "{" not in body and "}" not in body, f"unsubstituted placeholder: {body}"
        assert sent[0]["headers"]["title"] == "Build_failed"
        assert sent[0]["headers"]["tags"] == "build_failed"
    finally:
        lst.close()


def test_unknown_template_exits_two():
    _, env = sandbox("http://127.0.0.1:1")
    res = run(env, "-t", "nonsense", "hello")
    assert res.returncode == 2, f"unknown template must exit 2, got {res.returncode}"
    assert "Unknown template" in res.stderr


def test_template_title_override():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        res, sent = requests_to(lst, env, "-t", "info", "heads up", "My Own Title")
        assert res.returncode == 0, res.stderr
        assert sent[0]["headers"]["title"] == "My Own Title"
        assert "Info" in sent[0]["body"]
    finally:
        lst.close()


def test_template_preserves_ampersand_and_backslash():
    """Regression: bash >= 5.2 enables patsub_replacement by default, so an
    unquoted '&' in the replacement of ${var//pat/repl} expands to the matched
    text. The template substitution used to put the message in the replacement,
    which published a literal "{msg}" wherever the message contained '&'."""
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        msg = 'verified & hardened C:\\temp "quoted" $HOME `cmd`'
        res, sent = requests_to(lst, env, "-t", "info", msg)
        assert res.returncode == 0, res.stderr
        body = sent[0]["body"]
        assert "{msg}" not in body, f"placeholder leaked/corrupted: {body!r}"
        assert body.endswith(msg), f"message mangled: {body!r}"
        assert "&" in body and "C:\\temp" in body, f"special chars lost: {body!r}"
    finally:
        lst.close()


# ── daily budget ───────────────────────────────────────────────────────────
def _seed_budget(env, n_success):
    """Record `n_success` delivered messages for TODAY in the metrics file."""
    today = time.strftime("%Y-%m-%d")
    with open(state(env, "metrics.log"), "w") as fh:
        for _ in range(n_success):
            fh.write(f"{today}T10:00:00-04:00\t{TOPIC}\tsuccess\t1\t200\t5\n")


def test_routine_message_gated_at_budget():
    # Past the cap the message must be refused LOCALLY: no request reaches
    # the listener, and the exit code must be 3 — never 0, because callers
    # read 0 as "delivered" and a skip is not a delivery.
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        _seed_budget(env, 200)
        res, reqs = requests_to(lst, env, "gated", "budget")
        assert res.returncode == 3, f"want exit 3, got {res.returncode}"
        assert reqs == [], "a gated message must not reach the network"
        assert "budget" in res.stderr.lower(), res.stderr
        assert "budget_skip" in read(state(env, "metrics.log"))
    finally:
        lst.close()


def test_high_priority_bypasses_budget():
    # Urgent traffic must still attempt past the routine cap — the only
    # wall that matters for it is ntfy's own 429, handled separately.
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        _seed_budget(env, 200)
        res, reqs = requests_to(lst, env, "-p", "high", "urgent", "budget")
        assert res.returncode == 0, f"high must send, got {res.returncode}"
        assert len(reqs) == 1, f"want 1 request, got {len(reqs)}"
    finally:
        lst.close()


def test_routine_high_priority_is_still_budget_gated():
    # The heartbeat sends high priority so Android's Doze delivers it
    # promptly, but marks itself routine so the daily budget can still skip it
    # before the quota wall. High priority alone must NOT lift that.
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        _seed_budget(env, 200)
        res, reqs = requests_to(lst, env, "-p", "high", "-r", "routine", "hb")
        assert res.returncode == 3, f"want exit 3, got {res.returncode}"
        assert reqs == [], "a routine message must not reach the network at cap"
    finally:
        lst.close()


def test_routine_flag_accepted_and_sent_high_below_budget():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        res, reqs = requests_to(lst, env, "-p", "high", "-r", "roster", "hb")
        assert res.returncode == 0, res.returncode
        assert len(reqs) == 1, reqs
        assert reqs[0]["headers"].get("priority") == "high", reqs[0]["headers"]
    finally:
        lst.close()


def test_routine_message_sends_below_budget():
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        _seed_budget(env, 199)
        res, reqs = requests_to(lst, env, "fits", "budget")
        assert res.returncode == 0, f"want delivery, got {res.returncode}"
        assert len(reqs) == 1, f"want 1 request, got {len(reqs)}"
    finally:
        lst.close()


def test_success_records_sent_message_id():
    # The reply bridge recognises our own notifications by the id ntfy
    # assigned, so a custom-titled notice ("Candid submitted") can no longer
    # be routed back into a session as if the human had typed it.
    lst = Listener(reply=(200, '{"id":"msg_abc","time":1,"event":"message"}'))
    try:
        _, env = sandbox(lst.url)
        res, _ = requests_to(lst, env, "notice", "Candid submitted")
        assert res.returncode == 0, res.returncode
        assert "msg_abc" in read(state(env, "sent_ids")), read(state(env, "sent_ids"))
    finally:
        lst.close()


def test_failed_send_records_no_id():
    # Nothing may be recorded when the send failed — a recorded id would tell
    # the bridge a message is ours when it never went out.
    lst = Listener()
    try:
        _, env = sandbox(lst.url)
        port = closed_port()
        env["NTFY_SERVER"] = f"http://127.0.0.1:{port}"
        res, _ = requests_to(lst, env, "nope", "nowhere")
        assert res.returncode != 0, res.returncode
        assert read(state(env, "sent_ids")) == ""
    finally:
        lst.close()


# ── outbox: a failed send is retried, never silently dropped ───────────────
def _outbox_files(env):
    d = state(env, "outbox")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def _dead_env():
    return sandbox(f"http://127.0.0.1:{closed_port()}")


def test_exhausted_send_is_spooled():
    _, env = _dead_env()
    res = run(env, "cannot deliver this", "Spool me", "warning")
    assert res.returncode != 0, res.returncode
    files = _outbox_files(env)
    assert len(files) == 1, files
    raw = open(os.path.join(state(env, "outbox"), files[0]), "rb").read()
    assert b"cannot deliver this" in raw, raw


def test_flusher_failure_does_not_spool_a_duplicate():
    # The outbox retries by calling ntfy-notify with NTFY_NO_SPOOL=1, so a
    # still-failing retry leaves the one file rather than making another.
    _, env = _dead_env()
    run(env, "still stuck", "Stuck", "warning")
    env["NTFY_NO_SPOOL"] = "1"
    res = run(env, "still stuck", "Stuck", "warning")
    assert res.returncode != 0
    assert len(_outbox_files(env)) == 1, _outbox_files(env)


def test_outbox_retries_and_clears_once_delivered():
    lst = Listener()
    try:
        _, env = _dead_env()
        assert run(env, "queued body", "Queued", "warning").returncode != 0
        assert len(_outbox_files(env)) == 1
        env["NTFY_SERVER"] = lst.url  # route recovers
        res = subprocess.run([OUTBOX], env=env, capture_output=True,
                             text=True, timeout=120)
        assert res.returncode == 0, res.stderr
        assert len(lst.requests) == 1, lst.requests
        assert _outbox_files(env) == [], _outbox_files(env)
    finally:
        lst.close()


def test_outbox_keeps_message_while_route_is_down():
    _, env = _dead_env()
    assert run(env, "still stuck", "Stuck", "warning").returncode != 0
    res = subprocess.run([OUTBOX], env=env, capture_output=True,
                         text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    assert len(_outbox_files(env)) == 1, _outbox_files(env)


def test_concurrent_sends_record_their_own_ids():
    # Heartbeat and events can publish simultaneously. Each response has a
    # distinct id, so this catches any regression to a shared response file
    # where one process can record the other process's ntfy id.
    def response_for(request):
        title = request["headers"].get("title", "")
        token = title.rsplit(" ", 1)[-1]
        return 200, '{"id":"id_' + token + '"}'

    lst = Listener(reply=response_for, delay=0.05)
    try:
        _, env = sandbox(lst.url)
        titles = [f"parallel notice {i:02d}" for i in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(run, env, f"body {i}", title)
                       for i, title in enumerate(titles)]
            results = [f.result(timeout=60) for f in futures]
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        recorded = read(state(env, "sent_ids"))
        missing = [f"id_{i:02d}" for i in range(12) if f"id_{i:02d}\t" not in recorded]
        assert not missing, f"concurrent response IDs were lost/mismatched: {missing}"
    finally:
        lst.close()


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
