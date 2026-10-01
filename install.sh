#!/usr/bin/env bash
# install.sh — put (or update) the ntfy stack on this machine.
#
#   ./install.sh                       # prompts for the topic the first time
#   ./install.sh --topic <your-topic>  # non-interactive
#   ./install.sh                       # later runs just update the files
#   ./install.sh --uninstall           # remove units + scripts (keeps config)
#   ./install.sh --purge               # remove config too (destroys the credential)
#
# Safe to re-run: scripts are overwritten (that's the point of updating) but
# ~/.config/ntfy-notify.conf is NEVER touched once it exists.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
CONF="$CONF_DIR/ntfy-notify.conf"
EXAMPLE="$REPO/config/ntfy-notify.conf.example"

UNITS=(
  ntfy-events.service
  ntfy-reply.service
  ntfy-keepawake.service
  ntfy-heartbeat.timer
  ntfy-stuck.timer
  ntfy-health.timer
)

usage() {
  cat <<'USAGE'
install.sh — put (or update) the ntfy stack on this machine.

  ./install.sh                       # prompts for the topic the first time
  ./install.sh --topic <your-topic>  # non-interactive
  ./install.sh                       # later runs just update the files
  ./install.sh --no-enable           # copy files but don't enable units
  ./install.sh --uninstall           # remove units + scripts (keeps config)
  ./install.sh --purge               # remove config too (destroys the credential)

Safe to re-run: scripts are overwritten (that's the point of updating) but
~/.config/ntfy-notify.conf is NEVER touched once it exists.
USAGE
}

TOPIC_ARG=""
DO_ENABLE=1
MODE="install"

while (($#)); do
  case "$1" in
    --topic)      TOPIC_ARG="${2:?--topic needs a value}"; shift 2 ;;
    --topic=*)    TOPIC_ARG="${1#*=}"; shift ;;
    --no-enable)  DO_ENABLE=0; shift ;;
    --uninstall)  MODE="uninstall"; shift ;;
    --purge)      MODE="uninstall"; DO_ENABLE=0; CONF_DIR="__PURGE__"; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            echo "install.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
info() { printf '  \033[36m•\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ── uninstall ─────────────────────────────────────────────────────────────
if [[ "$MODE" == "uninstall" ]]; then
  echo "Removing the ntfy stack from this machine..."
  # Disable only the units we actually enable (the timers + services that are
  # meant to run on their own); then delete EVERY ntfy-* file. Enumerating
  # what to delete from the enable list left the three timer-driven
  # .service companions behind on disk.
  for u in "${UNITS[@]}"; do
    systemctl --user disable --now "$u" >/dev/null 2>&1 || true
  done
  removed=0
  for f in "$UNIT_DIR"/ntfy-*; do
    [[ -f "$f" ]] || continue
    rm -f "$f"
    ok "removed $(basename "$f")"
    removed=$((removed + 1))
  done
  (( removed == 0 )) && info "no units found under $UNIT_DIR"
  for f in ntfy-notify ntfy_oc.py ntfy_activity.py ntfy-events ntfy-reply \
           ntfy-heartbeat ntfy-stuck ntfy-health ntfy-finding ntfy-ask; do
    [[ -f "$BIN_DIR/$f" ]] && { rm -f "$BIN_DIR/$f"; ok "removed $BIN_DIR/$f"; }
  done
  systemctl --user daemon-reload >/dev/null 2>&1 || true

  if [[ "$CONF_DIR" == "__PURGE__" ]]; then
    [[ -f "$CONF" ]] && rm -f "$CONF" && ok "deleted $CONF (credential destroyed)"
  else
    [[ -f "$CONF" ]] && info "kept $CONF — it holds your topic"
  fi
  info "State under ~/.local/state/ntfy-* was left alone."
  exit 0
fi

# ── scripts ───────────────────────────────────────────────────────────────
echo "Installing scripts → $BIN_DIR"
mkdir -p "$BIN_DIR"
for f in "$REPO"/bin/*; do
  # -f, not -x: bin/ can hold __pycache__ from running the tests, and handing
  # a directory to `install` aborts the whole run with set -e.
  [[ -f "$f" ]] || continue
  base="$(basename "$f")"
  if [[ "$BIN_DIR/$base" -ef "$f" ]]; then continue; fi
  install -m 0755 "$f" "$BIN_DIR/$base"
  ok "$base"
done

# ── units ─────────────────────────────────────────────────────────────────
echo "Installing units → $UNIT_DIR"
mkdir -p "$UNIT_DIR"
for f in "$REPO"/systemd/*; do
  [[ -f "$f" ]] || continue
  install -m 0644 "$f" "$UNIT_DIR/$(basename "$f")"
  ok "$(basename "$f")"
done

# ── config (never overwrite an existing one) ──────────────────────────────
if [[ -f "$CONF" ]] && ! grep -q 'REPLACE_WITH_YOUR_TOPIC' "$CONF"; then
  info "keeping existing $CONF"
else
  if [[ -z "$TOPIC_ARG" ]]; then
    if [[ -t 0 ]]; then
      printf 'Your ntfy topic [press Enter to generate a new one]: '
      read -r TOPIC_ARG
    fi
  fi
  if [[ -z "$TOPIC_ARG" ]]; then
    TOPIC_ARG="opencode-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    GENERATED=1
  else
    GENERATED=0
  fi
  mkdir -p "$CONF_DIR"
  {
    echo "# Written by install.sh — this file holds the stack's only credential."
    echo "# It is gitignored. Anyone who knows the topic can read and post to it."
    echo
    echo "NTFY_TOPIC=\"\${NTFY_TOPIC:-$TOPIC_ARG}\""
  } > "$CONF"
  chmod 0600 "$CONF"
  ok "wrote $CONF"
  if [[ "${GENERATED:-0}" == "1" ]]; then
    info "generated topic: $TOPIC_ARG"
  fi
fi

# ── enable ────────────────────────────────────────────────────────────────
if [[ "$DO_ENABLE" == "1" ]]; then
  echo "Enabling units"
  systemctl --user daemon-reload
  for u in "${UNITS[@]}"; do
    if systemctl --user enable --now "$u" >/dev/null 2>&1; then
      ok "$u enabled"
    else
      printf '  \033[33m!\033[0m could not enable %s (systemd unavailable?)\n' "$u"
    fi
  done
fi

# ── summary ───────────────────────────────────────────────────────────────
echo
echo "Done. Next steps:"
if [[ "${GENERATED:-0}" == "1" ]]; then
  cat <<EOF
  1. On your phone, install the ntfy app and subscribe to topic:
       $TOPIC_ARG
  2. Set NTFY_TOPIC in $CONF if you ever change it.
EOF
else
  cat <<EOF
  1. Make sure the phone is subscribed to the same topic as $CONF
  2. Check the stack:  ntfy-health -s
EOF
fi
cat <<EOF

  Useful:
    ntfy-health -s            health now
    ntfy-heartbeat -n         preview a heartbeat without sending
    journalctl --user -u ntfy-events -f    tail the event stream
    ntfy-notify "hello"       send a test message
EOF

if [[ -f "$CONF" ]]; then
  if grep -q 'REPLACE_WITH_YOUR_TOPIC' "$CONF"; then
    die "config still holds the placeholder — set NTFY_TOPIC in $CONF"
  fi
fi
