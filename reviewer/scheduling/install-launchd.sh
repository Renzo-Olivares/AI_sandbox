#!/bin/bash
#
# install-launchd.sh — generate + install the launchd LaunchAgent for THIS
# machine. launchd cannot expand ~, $HOME, or env vars in a plist's
# ProgramArguments / path / EnvironmentVariables fields, so those must be
# absolute literals; this fills the template with values computed from the
# current checkout + user, validates the plist, and bootstraps it.
#
# Usage (run from anywhere — the script locates the project itself):
#   scheduling/install-launchd.sh             # PRODUCTION: weekday 06:00, LIMIT=10
#   scheduling/install-launchd.sh --test      # TEST: kickstart-only, LIMIT=2
#   scheduling/install-launchd.sh --print     # print the generated plist, install nothing
#   scheduling/install-launchd.sh --uninstall # bootout + remove (add --test for the test agent)
#
# Running this IS the "go live" step: the repo ships dormant (only the template
# is committed; nothing is in ~/Library/LaunchAgents until you run this).
# RunAtLoad is false, so production fires only on the 06:00 calendar trigger (or
# `launchctl kickstart`).
#
# The home/project paths must contain no '&' or backslash (the only characters
# special to the awk substitution below); this is validated and aborts loudly
# rather than silently corrupting the plist (finding F53).
set -euo pipefail

# Owner-only plist + logs: they sit under the user's home and can carry PR
# content paths; do not make them world-readable (finding F51).
umask 077

MODE="prod"
ACTION="install"
for arg in "$@"; do
  case "$arg" in
    --test) MODE="test" ;;
    --print) ACTION="print" ;;
    --uninstall) ACTION="uninstall" ;;
    -h | --help)
      sed -n '9,13p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/launchd/pr-review-backlog.plist.template"
WRAPPER="$SCRIPT_DIR/run-backlog.sh"

if [[ "$MODE" == "test" ]]; then
  LABEL="local.pr-review-backlog.test"
  LIMIT="2"
  LOG_TAG="test." # -> launchd.test.out.log / launchd.test.err.log
else
  LABEL="local.pr-review-backlog"
  LIMIT="10"
  LOG_TAG=""
fi
DOMAIN="gui/$(id -u)"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

# --- uninstall: bootout + remove, then stop. ----------------------------------
if [[ "$ACTION" == "uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$DEST"
  echo "uninstalled $LABEL (removed $DEST)"
  exit 0
fi

# --- sanity. ------------------------------------------------------------------
[[ -f "$TEMPLATE" ]] || {
  echo "FATAL: template not found: $TEMPLATE" >&2
  exit 1
}
[[ -f "$WRAPPER" ]] || {
  echo "FATAL: wrapper not found: $WRAPPER" >&2
  exit 1
}
if [[ -z "${HOME:-}" ]] || [[ ! -d "$HOME" ]]; then
  echo "FATAL: HOME unset/invalid ('${HOME:-}') — agy auth is HOME-relative." >&2
  exit 1
fi

# --- compute the absolute-literal values launchd needs. -----------------------
LOG_DIR="$PROJECT_DIR/pr-reviews-generated/logs"
OUT_LOG="$LOG_DIR/launchd.${LOG_TAG}out.log"
ERR_LOG="$LOG_DIR/launchd.${LOG_TAG}err.log"
# A complete PATH for launchd's initial environment. (The wrapper rebuilds PATH
# itself too; non-existent dirs in PATH are harmless, so this is safe across
# Apple Silicon /opt/homebrew and Intel /usr/local.) System dirs come FIRST so a
# planted binary in a user-writable dir can't shadow git/date/grep etc. (F52).
PATH_LINE="/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/flutter/bin:$PROJECT_DIR/.venv/bin"

# Production fires weekday (Mon–Fri) 06:00; the test agent has no schedule (it
# only runs on `launchctl kickstart`).
if [[ "$MODE" == "prod" ]]; then
  SCHEDULE_BLOCK=$'  <key>StartCalendarInterval</key>\n  <array>'
  for wd in 1 2 3 4 5; do
    SCHEDULE_BLOCK+=$'\n    <dict><key>Weekday</key><integer>'"$wd"$'</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>'
  done
  SCHEDULE_BLOCK+=$'\n  </array>'
else
  SCHEDULE_BLOCK=""
fi

# --- guard: gsub's replacement still interprets & (matched text) and \ (escape)
# --- even via ENVIRON, so a path containing either would silently corrupt the
# --- plist. Abort loudly instead (finding F53). ------------------------------
for _v in "$WRAPPER" "$HOME" "$PATH_LINE" "$OUT_LOG" "$ERR_LOG"; do
  case "$_v" in
    *"&"* | *"\\"*)
      echo "FATAL: path contains '&' or backslash, which would corrupt the" \
        "generated plist: $_v" >&2
      exit 1
      ;;
  esac
done

# --- fill the template. Values go through ENVIRON (not -v) so awk does no
# --- escape-processing on them; gsub's only special chars are & and \, which we
# --- validated above are absent. The multi-line schedule substitutes cleanly. -
plist="$(
  P_LABEL="$LABEL" P_SCRIPT="$WRAPPER" P_HOME="$HOME" P_PATH="$PATH_LINE" \
    P_OUT="$OUT_LOG" P_ERR="$ERR_LOG" P_LIMIT="$LIMIT" P_SCHED="$SCHEDULE_BLOCK" \
    awk '
    { gsub(/__LABEL__/, ENVIRON["P_LABEL"]);
      gsub(/__SCRIPT__/, ENVIRON["P_SCRIPT"]);
      gsub(/__HOME__/, ENVIRON["P_HOME"]);
      gsub(/__PATH__/, ENVIRON["P_PATH"]);
      gsub(/__OUT_LOG__/, ENVIRON["P_OUT"]);
      gsub(/__ERR_LOG__/, ENVIRON["P_ERR"]);
      gsub(/__LIMIT__/, ENVIRON["P_LIMIT"]);
      gsub(/__SCHEDULE_BLOCK__/, ENVIRON["P_SCHED"]);
      print }
  ' "$TEMPLATE"
)"

if [[ "$ACTION" == "print" ]]; then
  printf '%s\n' "$plist"
  exit 0
fi

# --- install: write, lint, (re)bootstrap. -------------------------------------
mkdir -p "$LOG_DIR" "$(dirname "$DEST")"
# umask only governs NEW dirs; tighten an existing log dir too so upgrading from
# a pre-umask install doesn't leave it world-listable (finding L5).
chmod 700 "$LOG_DIR"
# Pre-create the launchd stdout/err logs 0600. launchd opens these itself at run
# time with ITS umask (commonly world-readable) — the wrapper's umask cannot
# govern files launchd creates — so pre-create + chmod and launchd then appends
# to an already-private file. These logs capture full review output incl. PR
# diff content (finding F51).
touch "$OUT_LOG" "$ERR_LOG"
chmod 600 "$OUT_LOG" "$ERR_LOG"
printf '%s\n' "$plist" >"$DEST"
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$DEST" >/dev/null || {
    echo "FATAL: generated plist failed plutil -lint: $DEST" >&2
    exit 1
  }
fi
# bootout first so re-running is idempotent (ignore "not loaded").
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$DEST"

UNINSTALL_HINT="$0 --uninstall"
[[ "$MODE" == "test" ]] && UNINSTALL_HINT="$0 --test --uninstall"
echo "installed $LABEL -> $DEST"
echo "  status:    launchctl print $DOMAIN/$LABEL"
echo "  fire now:  launchctl kickstart -k $DOMAIN/$LABEL"
[[ "$MODE" == "prod" ]] && echo "             (otherwise it waits for weekday 06:00)"
echo "  uninstall: $UNINSTALL_HINT"
