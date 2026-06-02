#!/bin/bash
#
# run-backlog.sh — unattended driver for the PR-review backlog run.
#
# Wraps `.venv/bin/review-backlog` so a scheduler (macOS launchd now; Linux
# systemd later) can fire the morning run safely. The tool itself NEVER submits
# a review — it only stages PENDING reviews for a human to submit by hand — and
# this wrapper does not change that; it just runs the command and surfaces
# failures.
#
# PORTABILITY: the core (cd, source .env, lock, run, classify, log) is
# OS-agnostic. The ONLY OS-specific seam is notify_failure() below (osascript on
# macOS, notify-send on Linux). Run this script directly to test outside the
# scheduler — every behaviour below is exercised the same way.
#
# SECURITY: tokens are sourced from a git-ignored .env and never written to the
# log. Do NOT add `set -x` (it would echo the secrets).
#
# Behaviour is driven entirely by environment variables (all optional):
#   LIMIT     cap to the first N PRs (default 10; "none"/"0"/"" => full backlog)
#   PRS       explicit comma-separated PR numbers (overrides LIMIT)
#   REPO      owner/name override (default: config.yaml's repo)
#   CONFIG    config path           (default: <project>/config.yaml)
#   ENV_FILE  env file with tokens   (default: <project>/.env)
#   DRY_RUN   "1" => add --dry-run and skip failure classification
#
# Exit status: 0 on success / nothing-to-do / overlap-skip; 1 on any failure
# (bad HOME, missing tokens, config/preflight error, dead run, crash). Every
# exit-1 path also fires notify_failure().

set -euo pipefail

# Owner-only logs/files: the dated log + generated tree carry PR diff content,
# which must not be world-readable on a shared host (finding F51). The Python
# subprocess inherits this umask, so its outputs are 0600/0700 too.
umask 077

# --- OS-specific notification seam (the ONLY part a new platform touches). ----
notify_failure() { # $1 = title, $2 = message
  local title="$1"
  local msg="$2"
  case "$(uname -s)" in
    Darwin)
      # Strip backslashes and double-quotes so they cannot break the AppleScript
      # string literal we hand to osascript.
      msg="${msg//\\/}"
      msg="${msg//\"/}"
      title="${title//\\/}"
      title="${title//\"/}"
      osascript -e "display notification \"${msg}\" with title \"${title}\"" \
        >/dev/null 2>&1 || true
      ;;
    Linux)
      if command -v notify-send >/dev/null 2>&1; then
        notify-send -- "$title" "$msg" >/dev/null 2>&1 || true
      fi
      ;;
  esac
}

log() { # append a timestamped line to the dated log and stderr
  printf '%s %s\n' "$(date '+%F %T')" "$1" | tee -a "$LOG" >&2
}

# shellcheck disable=SC2329  # invoked indirectly via `trap cleanup EXIT` below.
cleanup() {
  rm -f "$RUN_OUT"
  if [[ "$LOCK_HELD" == "1" ]]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}

main() {
  # --- Locate the project: this script lives in <project>/scheduling/. --------
  SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"

  LIMIT="${LIMIT:-10}"
  PRS="${PRS:-}"
  REPO="${REPO:-}"
  CONFIG="${CONFIG:-$PROJECT_DIR/config.yaml}"
  ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env}"
  DRY_RUN="${DRY_RUN:-0}"

  # --- Build a complete PATH up front (tolerant of an as-yet-unvalidated HOME).
  # A scheduler hands us a tiny PATH; resolve agy/gh/git/flutter/dart/osascript
  # and the venv explicitly. Non-existent candidates are skipped, so this works
  # unchanged across machines and across macOS/Linux. (gotcha #2)
  #
  # SECURITY (F52): system dirs come FIRST so a malicious git/gh/date/grep
  # planted in a user-writable dir ($HOME/.local/bin, /opt/homebrew, the venv)
  # can't shadow the system tools for this credential-bearing run. The
  # specialized tools (gh, flutter, dart, agy) live only in the user/brew dirs,
  # so those still resolve — just after the system ones.
  _path_suffix=""
  for _d in \
    "${HOME:-}/.local/bin" \
    /opt/homebrew/bin \
    /usr/local/bin \
    "${HOME:-}/flutter/bin" \
    "$PROJECT_DIR/.venv/bin"; do
    [[ -d "$_d" ]] && _path_suffix="${_path_suffix:+$_path_suffix:}$_d"
  done
  export PATH="/usr/bin:/bin:/usr/sbin:/sbin${_path_suffix:+:$_path_suffix}"
  unset _d _path_suffix

  # --- HOME must be the real home: agy authenticates HOME-relative, and a blank
  #     or wrong HOME silently fails every review (gotcha #1). Fail loud. ------
  if [[ -z "${HOME:-}" ]] || [[ ! -d "$HOME" ]]; then
    echo "FATAL: HOME unset or not a directory ('${HOME:-}') —" \
      "agy auth needs the real home." >&2
    notify_failure "PR review (scheduled) FAILED" \
      "HOME unset/invalid — agy auth would fail; see scheduler logs."
    exit 1
  fi

  # --- Logging: a dated human log + a this-run-only capture for classification.
  LOG_DIR="$PROJECT_DIR/pr-reviews-generated/logs"
  mkdir -p "$LOG_DIR"
  LOG="$LOG_DIR/backlog-$(date +%F).log"
  RUN_OUT="$(mktemp "${TMPDIR:-/tmp}/pr-review-run.XXXXXX")"

  # --- Single-run lock: mkdir is atomic and portable (macOS has no flock). ----
  LOCK_DIR="$PROJECT_DIR/pr-reviews-generated/locks/backlog.lock"
  mkdir -p "$(dirname "$LOCK_DIR")"
  LOCK_HELD=0
  trap cleanup EXIT

  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    stale_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    # `kill -0` alone is fooled by PID reuse: a recycled pid belonging to some
    # unrelated process would look like a live holder and silently skip the run
    # (finding F49). Also confirm the process is actually a run-backlog before
    # treating the lock as held.
    if [[ -n "$stale_pid" ]] && kill -0 "$stale_pid" 2>/dev/null &&
      ps -ww -p "$stale_pid" -o command= 2>/dev/null | grep -q "run-backlog"; then
      log "another run (pid $stale_pid) holds the lock; exiting without action."
      exit 0
    fi
    log "removing stale lock (pid '${stale_pid:-?}' not a live run) and reclaiming."
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
      log "could not acquire lock after reclaim; exiting."
      exit 0
    fi
  fi
  LOCK_HELD=1
  echo "$$" >"$LOCK_DIR/pid"

  # --- Working directory + credentials (.env is NOT auto-loaded for us). ------
  cd "$PROJECT_DIR" || {
    log "STATUS: FAILED (cannot cd to project dir: $PROJECT_DIR)"
    notify_failure "PR review (scheduled) FAILED" "cannot cd to $PROJECT_DIR"
    exit 1
  }

  if [[ ! -f "$ENV_FILE" ]]; then
    log "STATUS: FAILED (env file not found: $ENV_FILE)"
    notify_failure "PR review (scheduled) FAILED" "env file missing: $ENV_FILE"
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090  # ENV_FILE is a configurable path, not a constant.
  . "$ENV_FILE"
  set +a

  if [[ -z "${GH_TOKEN_READONLY:-}" ]] || [[ -z "${GH_TOKEN_WRITE:-}" ]]; then
    log "STATUS: FAILED (GH_TOKEN_READONLY / GH_TOKEN_WRITE missing from $ENV_FILE)"
    notify_failure "PR review (scheduled) FAILED" \
      "GH tokens missing from $ENV_FILE"
    exit 1
  fi

  REVIEW_BACKLOG="$PROJECT_DIR/.venv/bin/review-backlog"
  if [[ ! -x "$REVIEW_BACKLOG" ]]; then
    log "STATUS: FAILED (review-backlog not executable at $REVIEW_BACKLOG)"
    notify_failure "PR review (scheduled) FAILED" \
      "review-backlog missing at $REVIEW_BACKLOG"
    exit 1
  fi

  # --- Assemble the review-backlog invocation from the knobs. -----------------
  ARGS=(--config "$CONFIG")
  [[ -n "$REPO" ]] && ARGS+=(--repo "$REPO")
  if [[ -n "$PRS" ]]; then
    ARGS+=(--prs "$PRS")
  elif [[ -n "$LIMIT" ]] && [[ "$LIMIT" != "none" ]] && [[ "$LIMIT" != "0" ]]; then
    ARGS+=(--limit "$LIMIT")
  fi
  [[ "$DRY_RUN" == "1" ]] && ARGS+=(--dry-run)

  log "start: review-backlog ${ARGS[*]} (cwd=$PROJECT_DIR)"

  # --- Run, teeing to the dated log AND a this-run-only file for classification.
  # review-backlog exits 0 even on partial/whole failure, so we capture rc AND
  # inspect the output (the failure surface, gotcha #4).
  set +e
  "$REVIEW_BACKLOG" "${ARGS[@]}" 2>&1 | tee -a "$LOG" | tee "$RUN_OUT"
  rc=${PIPESTATUS[0]}
  set -e

  # --dry-run only lists candidates; there is no run outcome to classify.
  if [[ "$DRY_RUN" == "1" ]]; then
    log "STATUS: dry-run (rc=$rc; no classification)"
    exit "$rc"
  fi

  # --- Classify the outcome (the failure surface). ---------------------------
  # Key off the CLI's MACHINE-READABLE status line (a stable token), never its
  # human wording — so rewording the summary can't break dead-run detection
  # (F47). review-backlog exits 0 even on partial/whole failure, so we inspect
  # rc too.
  status_line="$(grep -m1 -E "^PR-REVIEW-STATUS " "$RUN_OUT" || true)"
  status=""
  reason=""
  if (( rc != 0 )); then
    status="FAILED"
    reason="review-backlog exited $rc (config/preflight error)"
  elif [[ -z "$status_line" ]]; then
    status="FAILED"
    reason="no status line — review-backlog crashed or was killed"
  elif printf '%s' "$status_line" | grep -q "nothing-to-review"; then
    status="ok-empty"
    reason="nothing to review — all candidates already staged."
  else
    reviewed="$(printf '%s' "$status_line" | sed -n 's/.*reviewed=\([0-9]*\).*/\1/p')"
    failed="$(printf '%s' "$status_line" | sed -n 's/.*failed=\([0-9]*\).*/\1/p')"
    reviewed="${reviewed:-0}"
    failed="${failed:-0}"
    # 10# forces base-10: these are sed-extracted strings, and (( )) reads a
    # leading-zero value (e.g. "09") as invalid octal — unlike the old [ -eq ]
    # decimal test — which would silently misclassify this dead-run/partial
    # safety branch. The producer emits plain ints today; this keeps us robust.
    if (( 10#$reviewed == 0 && 10#$failed > 0 )); then
      status="FAILED"
      reason="dead run — $status_line (quota/auth/HOME?)"
    elif (( 10#$failed > 0 )); then
      status="ok-partial"
      reason="$status_line"
    else
      status="ok"
      reason="$status_line"
    fi
  fi

  log "STATUS: $status ($reason)"

  case "$status" in
    FAILED)
      notify_failure "PR review (scheduled) FAILED" "$reason — log: $LOG"
      exit 1
      ;;
    ok-partial)
      # Not fatal (partial success is exit 0 by design), but worth a heads-up.
      notify_failure "PR review: some PRs failed" "$reason — log: $LOG"
      exit 0
      ;;
    *)
      exit 0
      ;;
  esac
}

main "$@"
