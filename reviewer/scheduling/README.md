# Scheduling the PR-review backlog run

This wraps the finished `review-backlog` command in an unattended scheduler. It
sits **on top of** the tool — it changes nothing under `pr_review/`. The tool
**never submits** a review; it only stages **PENDING** reviews for you to submit
by hand in the GitHub UI. The scheduler preserves that: it just runs the
command, so there is no submit path to trigger.

```
scheduling/
├── run-backlog.sh                              # portable wrapper (macOS + Linux)
├── install-launchd.sh                          # macOS: generate + (un)install the LaunchAgent for THIS machine
├── launchd/
│   └── pr-review-backlog.plist.template        #   plist template the installer fills (per machine)
├── systemd/                                    # Linux scheduler (future; templates)
│   ├── pr-review-backlog.service
│   └── pr-review-backlog.timer
└── README.md
```

## Operating model: catch up first, then maintain

- **Phase 1 — catch-up (manual, attended).** While the backlog is large, review
  hand-picked PRs yourself so you only stage as many pending reviews as you can
  read and submit in a day. **This needs nothing here** — just run the tool:

  ```sh
  cd /path/to/review_2
  .venv/bin/review-backlog --prs 123,456,789 --config config.yaml
  # or, to walk the live backlog in chunks (idempotent: re-runs skip what is
  # already staged, so each run grabs the next N):
  .venv/bin/review-backlog --limit 8 --config config.yaml
  ```

- **Phase 2 — maintenance (scheduled).** Once the backlog is a daily trickle,
  turn on the schedule below. The 6am run uses the **query** backlog
  (requested-review + silent-update), capped at `LIMIT=10` as a safety valve.

**The scheduler ships dormant.** Only the plist *template* is committed; nothing
is installed in `~/Library/LaunchAgents/` until you run the installer below, so
launchd never fires it on its own. Do that only when you switch from Phase 1 to
Phase 2.

## macOS (launchd)

`install-launchd.sh` generates the plist for this machine (launchd can't expand
`~`/`$HOME`, so the paths must be absolute literals), validates it, writes it to
`~/Library/LaunchAgents/`, and loads it. Run it from anywhere — it locates the
project itself.

```sh
# GO LIVE — generate + load the weekday-06:00 agent:
scheduling/install-launchd.sh

# STATUS — is it loaded? next fire time? last exit code?
launchctl print gui/$(id -u)/local.pr-review-backlog

# FIRE NOW — run immediately without waiting for the clock:
launchctl kickstart -k gui/$(id -u)/local.pr-review-backlog

# PAUSE — back to dormant (bootout + remove the plist):
scheduling/install-launchd.sh --uninstall

# INSPECT — print the generated plist without installing anything:
scheduling/install-launchd.sh --print
```

Once loaded, the agent persists across reboots/logins until you `--uninstall`.
`RunAtLoad` is false, so loading does **not** run it — only the 06:00 calendar
trigger (or `kickstart`) does. launchd fires it **on wake** if the Mac was
asleep at 06:00.

## Wrapper knobs (env vars)

The wrapper is driven entirely by environment variables, so the same script
serves production, the test agent, and direct runs. The generated plist sets
these via `EnvironmentVariables`; you can also set them inline when running by
hand.

| Var        | Default               | Meaning                                                        |
|------------|-----------------------|----------------------------------------------------------------|
| `LIMIT`    | `10`                  | cap to the first N PRs; `none`/`0`/empty = full backlog        |
| `PRS`      | —                     | explicit comma-separated PR numbers (overrides `LIMIT`)        |
| `REPO`     | — (config's repo)     | `owner/name` override                                          |
| `CONFIG`   | `<project>/config.yaml` | config path                                                  |
| `ENV_FILE` | `<project>/.env`      | env file with `GH_TOKEN_READONLY` / `GH_TOKEN_WRITE`           |
| `DRY_RUN`  | `0`                   | `1` = add `--dry-run`, list candidates, skip classification    |

## Logs & the failure surface

A scheduled run can fail **silently**: `agy` exits 0 even when its quota is
exhausted or auth is broken, so a dead run can look like a quiet "reviewed 0".
The wrapper guards against that on three fronts:

1. **A dated log** — `pr-reviews-generated/logs/backlog-YYYY-MM-DD.log`, ending in a
   `STATUS:` line you can grep:
   - `STATUS: ok (...)` — reviews staged.
   - `STATUS: ok-empty (...)` — nothing to review (all already staged). Fine.
   - `STATUS: ok-partial (...)` — some PRs staged, some failed. Exit 0; soft notification.
   - `STATUS: FAILED (...)` — dead run (`0 reviewed, N failed`), config/preflight
     error, missing tokens, bad `HOME`, or a crash. **Exit 1 + notification.**
2. **A macOS notification** on `FAILED`/`ok-partial` (Notification Center; a 6am
   Do-Not-Disturb may mute the banner but it still lands in history).
3. **A non-zero exit** on `FAILED`, visible in `launchctl print ...`
   (`LastExitStatus`). launchd also captures raw output at
   `pr-reviews-generated/logs/launchd.{out,err}.log`.

## Testing (without waiting for 06:00)

```sh
cd /path/to/review_2

# 1) Plumbing only — proves cd/PATH/HOME/.env/lock/log with NO agy, NO staging:
DRY_RUN=1 LIMIT=2 bash scheduling/run-backlog.sh
#    Lock proof: inject a LIVE holder pid, re-run, see it skip + exit 0:
mkdir -p pr-reviews-generated/locks/backlog.lock && echo $$ > pr-reviews-generated/locks/backlog.lock/pid
DRY_RUN=1 bash scheduling/run-backlog.sh        # -> "another run (pid ...) holds the lock; exiting"
rm -rf pr-reviews-generated/locks/backlog.lock

# 2) Failure surface fires (real .env/config untouched):
printf '' > /tmp/empty.env
ENV_FILE=/tmp/empty.env bash scheduling/run-backlog.sh           # -> missing-tokens FAILED + notify + exit 1
sed 's/^agy_command:.*/agy_command: \/bin\/false/' config.yaml > /tmp/deadrun.yaml
CONFIG=/tmp/deadrun.yaml LIMIT=2 bash scheduling/run-backlog.sh  # -> "0 reviewed, 2 failed" dead-run FAILED + notify + exit 1

# 3) End-to-end through launchd (stages 2 PENDING reviews on flutter/flutter):
scheduling/install-launchd.sh --test         # generate + load the kickstart-only test agent (LIMIT=2)
launchctl kickstart -k gui/$(id -u)/local.pr-review-backlog.test
#    ...watch pr-reviews-generated/logs/backlog-*.log for STATUS: ok, then verify PENDING
#    with the READ-ONLY token (must show "state": "PENDING", never submitted):
GH_TOKEN="$GH_TOKEN_READONLY" gh api repos/flutter/flutter/pulls/<N>/reviews \
  --jq '.[] | select(.user.login=="<your-github-login>") | {state, submitted_at}'
#    teardown:
scheduling/install-launchd.sh --test --uninstall
```

## Linux (systemd) — future

The wrapper is already portable (it sets PATH, sources `.env`, uses a `mkdir`
lock, and `notify_failure()` falls back to `notify-send`). To extend to Linux,
fill in the paths in `systemd/pr-review-backlog.service` and:

```sh
cp systemd/pr-review-backlog.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pr-review-backlog.timer   # Persistent=true => fires on wake
loginctl enable-linger "$USER"                          # only if it must run without a login
```

## Safety (unchanged)

No `pr_review/` code is modified. No `event` is ever set; the write token is
scrubbed from the `agy` child env by the tool; `--dangerously-skip-permissions`
is never used; the sandbox stays on. launchd/systemd have no "auto-submit"
setting, and the tool has no submit path — a human submits each PENDING review
in the GitHub UI.
