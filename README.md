<div align="center">

# 📱 opencode-ntfy-stack

**Your AI agents text your phone — when they finish, when they fail,
when they need a decision, and when they're stuck.**

*And you text back to keep working from anywhere.*

![python3](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![bash](https://img.shields.io/badge/shell-bash-green?logo=gnubash&logoColor=white)
![systemd](https://img.shields.io/badge/systemd-user%20units-orange?logo=systemd&logoColor=white)
![deps](https://img.shields.io/badge/dependencies-none-brightgreen)
![license](https://img.shields.io/badge/license-see%20repo-lightgrey)

</div>

---

## Why this exists

An agent that works unattended is only useful if you can **leave**. This stack
turns OpenCode's session events into plain-English phone notifications, and
turns your phone replies back into real input — so you can approve a step, redirect
a session, or notice a stall while you're away from the desk.

It is built around one rule:

> **Nothing is ever silently lost, and nothing is ever falsely reported.**

If a message could not be delivered, you are told. If a notification was queued
while the machine was down, it is reported when it comes back — labelled
`(while away)`. If delivery is refused, the reason ntfy gave us is kept, not
swallowed.

---

## What you get

| Tool | Runs | What it does |
|------|------|--------------|
| **`ntfy-notify`** | on demand | The sender. Every other tool talks to the phone through it. |
| **`ntfy-events`** | service | Listens to OpenCode's event stream → "task done", "it failed", "approval needed". |
| **`ntfy-heartbeat`** | every 15 min | "Here's what's working, here's what's idle" — one scannable roster. |
| **`ntfy-stuck`** | every 3 min | Catches the three ways work stalls: repeats, consecutive failures, dead air. |
| **`ntfy-health`** | every 5 min | Watches the stack itself. Speaks only on **state changes**. |
| **`ntfy-reply`** | service | Turns your phone texts into real input in a session. |
| **`ntfy-ask`** | on demand | Multiple-choice questions with the session roster attached. |
| **`ntfy-finding`** | on demand | Logs a finding in a greppable shape and pings you. |
| **`ntfy-outbox`** | every 2 min | Retries a notification a failed send spooled — nothing is silently dropped. |
| **`ntfy-keepawake`** | service | Stops the machine suspending out from under you. |
| **`ntfy_oc.py`** | library | The single source of truth every tool imports. |

---

## How it fits together

```
              OpenCode  ── SSE stream ──►  ntfy-events
                 ▲                            │  filters subagents
                 │                            ▼
                 │                      ┌─────────────┐
   ntfy-heartbeat (15m) ──────────────► │ ntfy-notify │ ──► ntfy.sh ──► 📱
   ntfy-stuck     (3m)  ──────────────► │   (sender)  │
   ntfy-health    (5m)  ──────────────► └─────────────┘
   ntfy-finding         ──────────────►        ▲  honest retries, dedup,
                                               │  quota marker, circuit breaker
                 📱 ── reply ──► ntfy-reply ───┘
                                     │
                                     └──► OpenCode session  (you just kept working)
```

Every arrow into `ntfy-notify` means one code path decides retries, dedup,
priorities and failure reporting — so every notification behaves the same way.

---

## Quick start

```bash
git clone https://github.com/<you>/opencode-ntfy-stack.git
cd opencode-ntfy-stack
./install.sh --topic "opencode-XXXXXXXXXXXXXXXXXXXXXXXX"
```

Then install the **ntfy** app on your phone and subscribe to the same topic.

That's it. `install.sh` copies the scripts to `~/.local/bin`, the units to
`~/.config/systemd/user`, writes `~/.config/ntfy-notify.conf`, and enables
everything.

```bash
ntfy-notify "hello"        # prove the pipeline end to end
ntfy-health -s             # is the stack healthy?
```

No `--topic`? It prompts, or generates a fresh one for you and prints the
value to subscribe with.

Re-running `./install.sh` later just updates the files — **it never overwrites
an existing config.**

---

## On a new computer

This is what the repo is for. Nothing that matters lives in `/tmp` or in one
machine's home directory.

```bash
git clone https://github.com/<you>/opencode-ntfy-stack.git
cd opencode-ntfy-stack
./install.sh --topic "<your topic>"     # same topic as your phone
```

Your scripts, your units, your tests and this README arrive together. Machine
local state (cursors, counters, dedup history) is deliberately *not* carried
over — it is rebuilt as the stack runs.

> ⚠️ **The topic is the credential.** Store it in your password manager, not in
> this repo. If you lose it, run `./install.sh` without `--topic` to generate a
> new one, then re-subscribe the phone.

---

## Using it from your phone

Notifications are **plain English only** — never a command, a file path, or a
tool argument. You should be able to read them at a glance:

```
✅ Audit 6 smaller ntfy scripts (while you were away)
🔴 Session stuck: same command repeated 4 times
❓ Need input — Submit capture_13.png to the bounty?
```

The heartbeat lists **every** session, working first, then idle ones with how
long they've been idle — so you can pick a thread to answer:

```
WORKING NOW
#1  Phone notificatio…

IDLE
#2  eToro MBB bounty    12m ago
#3  Audit 6 smaller      3h ago
```

### Replying

Reply to any notification. Prefix with `#N` to choose the session — sessions
are numbered running-first, then alphabetically, so `#N` always means the same
thing:

| You send | Meaning |
|---|---|
| `#2 do the thing` | send text to session 2 |
| `1` / `1,3` | answer options in the question's session |
| `#2, 1` | session 2 **and** option 1 — both at once |
| `0` | skip the question |
| `S` | "let me type my own" — your next message is the answer |
| `?` | show the routing map (active + parked sessions) |
| `yes` / `no` | answer a pending permission |

With one session running, a bare reply goes there. With several running and no
recent question, the bridge **asks which one** rather than guessing — a
mistyped number is surfaced, never silently misrouted.

The bridge ignores the stack's *own* notifications even when their title isn't
`OpenCode · …`: `ntfy-notify` records the id ntfy assigns to every message it
publishes, and the bridge skips any base-topic message carrying one. Without
that, an agent's status notice sent with a custom title ("Candid submitted")
was treated as your reply and fed back into a session — the bot answering
itself.

### Asking the human

```bash
ntfy-ask "Which should I chase first?" "Deeplinks|Auth tokens|TLS pinning"
ntfy-ask --pending      # what's still waiting
ntfy-ask --cancel       # forget it
```

Questions carry the session roster and the origin session, expire after
45 minutes, and can be answered from a parked session — which wakes it up.

---

## Configuration

Everything lives in **`~/.config/ntfy-notify.conf`**:

```bash
NTFY_TOPIC="${NTFY_TOPIC:-your-topic-here}"   # the only credential
#NTFY_SERVER="https://ntfy.sh"
#NTFY_AUTH=""
#NTFY_DEDUP_WINDOW="30"
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `NTFY_TOPIC` | *none* | Your topic. **No default on purpose** — see [Security](#security). |
| `NTFY_SERVER` | `https://ntfy.sh` | Any ntfy server. |
| `NTFY_AUTH` | unset | Bearer token, for private topics. |
| `NTFY_DEDUP_WINDOW` | `30` | Seconds before an identical message may repeat. |

The file is gitignored. Environment variables override it, which is how the
test suite publishes to scratch topics without ever reaching your phone.

### Sending

```bash
ntfy-notify "message"                          # routine
ntfy-notify -p high "tests failed"             # urgent: retries harder, ignores the breaker
ntfy-notify -c "https://pr/5" "visual diff"    # tap opens a URL
ntfy-notify -g pr-5 "Review needed"            # related messages stack
ntfy-notify -t build_failed "oops"             # templates
```

---

## Behaviour worth knowing

These are not preferences. Each one is written in code because the opposite
behaviour caused a real, silent failure.

**Honest exit codes** — `ntfy-notify` exits `0` delivered, `1` not delivered,
`2` bad usage or missing config, `3` skipped by the daily routine budget. It
never exits `0` on a message that didn't go out. A circuit that was "open" used
to skip the send and exit `0`; the heartbeat read that as delivered and cleared
its queue — 15 notifications vanished that way.

**Never skip, only degrade.** An open circuit or a fresh quota refusal drops
the sender to *one* attempt — it does not skip the attempt. One try still
proves the channel and still wins if the counter just rolled over.

**Quota is read from the response, not the status.** ntfy's GET keeps
returning 200 while its POSTs are refused with `429`, so a naive probe rates a
channel that can deliver nothing as "reachable". On a `429` the sender stores
the timestamp and ntfy's own reason text, and the health watchdog reads that
marker.

**Daily budget.** ntfy.sh allows 250 publishes per IP per day; the first
heavy day hit the wall at noon and *everything* — permissions, questions,
failures — was refused until midnight. Past 200 delivered messages, routine
(default/low) sends are refused **locally** with exit `3`, before spending a
network attempt, leaving the last 50 slots for high/urgent traffic. Callers
treat `3` like any failure: the heartbeat queues the message and re-renders
it next tick, so nothing is lost — it goes out once the counter rolls over.

**A failed send is retried, not dropped.** When every attempt fails, the
sender spools the message (title, body, tag, priority) and the `ntfy-outbox`
timer retries it every 2 minutes, deleting it **only** on a real delivery. The
queue is bounded — 24h or 300 messages — and anything dropped past a bound is
logged, never discarded in silence.

**Send budget.** Every caller runs the sender under `timeout=120`, so the
sender bounds its *whole* retry sequence — 90s critical, 30s routine. A retry
loop that outlives its caller is worse than no retry loop.

**Dedup records on delivery, never on attempt.** Writing the hash before
sending meant a message that failed every attempt was marked sent, and the
retry that mattered was swallowed as a duplicate.

**A session finish pings promptly; a mid-work turn doesn't.** `ntfy-events`
buzzes immediately for failed turns and permission prompts (~8s). A
*successful* turn pings only once the session actually goes **idle** — the
events daemon re-checks the running list a couple of seconds after the turn
ends, so a session that chains straight into its next turn stays quiet (buzzing
every turn was 145 of 244 pings over five days, and "Done ✅ X" kept arriving
while X worked on). An **interrupted** turn gets the same check: internal
supersedes (a tool reloading or retrying) leave the session running and stay
quiet; only a stop that parks the session pings. The 15-minute heartbeat is the
fallback and skips anything events already reported.

**“Needs your input” means exactly that.** A form or inbox item is reported
only for a session that is **not** running — a working session's leftovers are
not a wait on you. (Reporting them produced "2 sessions working" and "1 inbox
waiting" in the same message.)

**Subagents don't ping you.** Background agents fire the same events as real
sessions; `is_subagent()` filters them — and *fails open*, because dropping a
real completion is worse than an occasional extra ping.

**The roster shows everything.** Working sessions first, then every idle one
with how long it's been idle. No "+N more idle" truncation — you use that list
to pick a thread.

**Heartbeat every 15 minutes**, because [ntfy.sh's free tier is 250 messages
per IP per day](https://docs.ntfy.sh/faq/#are-there-any-limits) and a chatty
stack will hit it by lunchtime.

---

## Security

The **topic is the only credential** for the entire stack. Anyone who knows it
can read your notifications *and* publish to them.

That is why:

- there is **no default topic anywhere in the code** — `ntfy-notify` refuses to
  send without one, naming the config file to fix;
- `~/.config/ntfy-notify.conf` is **gitignored** and written `0600`;
- an unedited `ntfy-notify.conf.example` is **rejected**, not published to;
- nothing in this repo needs a token, a password, or a key.

If the repo is ever made private, nothing changes — it contains no secrets
either way. If you suspect the topic leaked, rotate it: change `NTFY_TOPIC`
and re-subscribe the phone.

---

## Testing

```bash
python3 tests/test_notify.py     # 38 tests — the sender, end to end
python3 tests/test_lib.py        # 46 tests — the library, reply routing, ask/events guards
python3 tests/test_install.py    # 11 tests — install/update/uninstall
python3 tests/argcheck.py        # 38 assertions — flag/doc/portability drift
```

**Everything is sandboxed.** The suite runs the *real* scripts against a local
HTTP listener under a throwaway `$HOME`, so it never reads your config, never
touches your state, and **never reaches your phone**.

The listener can be told to answer `500`, `429`, or to recover mid-test —
which is how the retry, refusal-marker and circuit-breaker rules are proven
rather than asserted.

> **One trap the install tests exist for:** `systemctl --user` resolves the
> user by D-Bus, **not** by `$HOME`. A throwaway `$HOME` redirects every file
> path but leaves systemd pointed at your live session — so an uninstall run
> inside a sandbox happily disabled the real units. `install.sh` now refuses to
> touch systemd unless it is operating on your actual unit directory, and
> `test_sandbox_uninstall_leaves_live_systemd_alone` fails loudly if that guard
> ever disappears.

---

## Troubleshooting

```bash
ntfy-health -s                              # what does the watchdog think?
journalctl --user -u ntfy-events -f         # tail the event stream
journalctl --user -u ntfy-reply -n 50       # why didn't my reply land?
ntfy-heartbeat -n                           # preview a heartbeat, don't send
ls ~/.local/state/ntfy-notify/              # metrics, dedup, markers, circuit
```

| Symptom | Likely cause |
|---------|--------------|
| Nothing arrives | Phone not subscribed to the topic, or `ntfy-health` says `Phone push: unreachable` |
| `no topic configured` | `NTFY_TOPIC` missing — run `./install.sh` or edit the conf |
| Arrives, then stops | `publish_blocked` marker in `~/.local/state/ntfy-notify/` — daily quota or IP rotation |
| Duplicates | Look for `Suppressed duplicate` in the logs; a missing dedup file means state was wiped |
| `permission denied (publickey)` | Unrelated to this stack — check your own SSH setup |
| A unit won't start | `systemctl --user status <unit>` then `journalctl --user -u <unit>` |

Exit codes you can script against:

| Tool | `0` | `1` | `2` | `3` |
|------|-----|-----|-----|-----|
| `ntfy-notify` | delivered | **not** delivered | bad usage / no config | — |
| `ntfy-health` | healthy | crashed | crashed | **problems found** |
| `install.sh` | success | failure | bad flag | — |

`ntfy-health` declares `SuccessExitStatus=0 3`, so "found a problem" is a
*result*, not a unit failure — otherwise the watchdog's own failure shows up as
the problem it is reporting.

---

## Layout

```
opencode-ntfy-stack/
├── bin/                    # the 11 scripts — installed to ~/.local/bin
│   ├── ntfy-notify         # bash sender: retries, dedup, markers, budget
│   ├── ntfy_oc.py          # shared library (paths, API, roster, helpers)
│   ├── ntfy_activity.py    # tool-call → plain-English phrases
│   ├── ntfy-events         # SSE listener
│   ├── ntfy-reply          # phone → session input
│   ├── ntfy-heartbeat      # periodic roster
│   ├── ntfy-stuck          # stall detector
│   ├── ntfy-health         # stack watchdog
│   ├── ntfy-finding        # greppable findings
│   ├── ntfy-ask            # multiple-choice questions
│   └── ntfy-outbox         # retry notifications a failed send had spooled
├── systemd/                # user units → ~/.config/systemd/user
├── config/
│   └── ntfy-notify.conf.example   # copied by install.sh (never committed filled in)
├── tests/                  # sandboxed regression suite + static checks
│                           # (test_install guards the systemd/$HOME trap)
├── install.sh              # install / update / --uninstall / --purge
└── README.md
```

State is written to `~/.local/state/ntfy-*` and is intentionally not tracked.

---

## Uninstall

```bash
./install.sh --uninstall        # removes units + scripts, keeps your config
./install.sh --purge            # also deletes ~/.config/ntfy-notify.conf
```

`--purge` destroys the credential. Your phone will stop receiving anything.

---

<div align="center">

*Built so you can close the laptop and still know what happened.*

</div>
