# Automated PR-Review Backlog Tool

An orchestrator that drives the **Antigravity (`agy`)** AI agent to review a
backlog of GitHub pull requests and **stage them as PENDING reviews** for you to
approve and submit by hand. It is built for one repository (`flutter/flutter`)
and for unattended, scheduled operation.

> **The core safety property:** the tool **never submits a review to the author,
> and the agent never writes to GitHub at all.** It only ever *stages* a pending
> review (no `event`); a human submits it in the GitHub UI. This is enforced by
> three independent layers (see [Safety model](#safety-model)). If you remember
> one thing about this tool, remember that.

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Safety model](#safety-model)
- [Directory structure](#directory-structure)
- [Requirements](#requirements)
- [Installation & setup](#installation--setup)
- [Configuration reference](#configuration-reference)
- [GitHub tokens](#github-tokens)
- [CLI usage](#cli-usage)
- [Operating model & scheduling](#operating-model--scheduling)
- [Output: where things land](#output-where-things-land)
- [Troubleshooting & gotchas](#troubleshooting--gotchas)
- [Development](#development)
- [Out of scope / deferred](#out-of-scope--deferred)

## What it does

A single "morning run" turns your review backlog into staged, ready-to-submit
reviews. The sequence:

1. **Query** the backlog as two pipelines (snapshot both before doing any work):
   - **Pipeline A** — PRs *awaiting your review* (`review-requested:<you>`).
   - **Pipeline B** — PRs you *already reviewed* that the author has since acted
     on (`reviewed-by:<you>` **and** missing the `waiting for response` label).
   - **Dedupe** B-minus-A (A takes precedence).
2. **Filter** out PRs that already carry your pending review (idempotency — a
   re-run can never double-stage).
3. **Assemble context** per PR (a *fresh* bundle for A, a *re-review* bundle for
   B that includes your prior review and what changed).
4. **Fan out** in bounded batches: each PR is reviewed in its **own isolated
   `agy` context** inside its **own git worktree**, concurrently up to
   `batch_size`. The agent writes a structured JSON review (summary + inline
   findings, optionally including **Flutter style-guide conformance** — see
   [Configuration reference](#configuration-reference)); it has **no GitHub
   write access**.
5. **Stage** the review as an **event-less PENDING review** (in
   `auto-stage-review` mode), anchoring inline comments to the diff.
6. **Report** — a per-run Markdown dashboard linking to everything staged.

One PR failing is recorded and skipped, never killing the run.

## Quick start

From a clean checkout to a scheduled run. Each step links to its detail section.

```sh
cd /path/to/review_2

# 0. Prerequisites on PATH: agy (logged in under your $HOME), gh, git, flutter.
#    (See "Requirements".)

# 1. Create the local virtualenv and install (deps resolve from pyproject.toml).
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# 2. Provide the two GitHub tokens. (See "GitHub tokens".)
cp .env.example .env
"$EDITOR" .env                       # set GH_TOKEN_READONLY and GH_TOKEN_WRITE

# 3. Confirm config.yaml: repo, username, mode. (See "Configuration reference".)

# 4. Dry-run — list today's backlog; no agent runs, nothing is staged.
review-backlog --dry-run

# 5. First reviews (attended catch-up) — stage a hand-picked few...
review-backlog --prs 12345,67890     # replace with the PR numbers you want
#    ...or the first N of the live backlog:  review-backlog --limit 5
#    Then open each PR on GitHub and SUBMIT it yourself — the tool never submits.
```

**6. Enable scheduling (optional, later).** Once your backlog is a daily trickle,
turn on the unattended weekday-06:00 run (full detail + pause/status in
[`scheduling/README.md`](scheduling/README.md)):

```sh
scheduling/install-launchd.sh        # generates the plist for THIS machine, then loads it
```

Until you run step 6, nothing runs automatically. To stop it later:
`scheduling/install-launchd.sh --uninstall`.

## Safety model

The agent is the **untrusted process** — its input is AI-generated PR content,
so prompt injection is a real risk. It therefore gets no write capability on any
front. Three independent layers enforce this:

1. **Layer 1 — strict permission allowlist.** `agy` runs `--sandbox` with a
   `deny > ask > allow` allowlist. The tool **scrubs any `.gemini` config shipped
   in the PR checkout** (anti-injection) and installs its own strict settings
   **project-locally in each worktree**. `deny` covers `read_url`, `execute_url`
   (the browser "click submit" path), `mcp`, and `unsandboxed`; `allow` is built
   positively (`git` reads, `flutter analyze`/`test`, `dart`) — never a
   `command(*)`/`write_file(*)` deny catch-all (it would outrank the agent's own
   grants), never broad `command(gh)`. `--dangerously-skip-permissions` is
   **never** used and is asserted-absent on every launch.
2. **Layer 2 — credential split.** The agent gets a **read-only** token; the
   orchestrator holds a **separate write-capable** token. Neither uses ambient
   `gh auth`. The agent's child environment is **value-scrubbed**: every variable
   carrying either token's value is removed, then only the read-only token is
   re-exposed — so the write token appears nowhere, under any name.
3. **Layer 3 — the orchestrator never sets `event`.** Its only GitHub writes
   create or delete a PENDING review (no `event` field) — it never submits.
   Submitting is something *you* do in the GitHub UI; there is no submit path
   anywhere in the tool.

**Style-guide conformance** (when enabled) follows the same principle: the guide
is fetched from **trusted upstream** (`style_guide_repo@style_guide_ref`, default
`flutter/flutter@master`) using the read-only token — **never** from the PR's
worktree, so a PR cannot smuggle in a doctored rubric. A PR that *edits* the
guide is reviewed as a **proposed diff**, never adopted as the rubric.

`config.py` validates load-bearing invariants at startup (fails fast):
`use_skip_permissions` must be `false`, and the two token env-var names must
differ. Preflight additionally renders the tool's **built-in** `agy` allowlist
and verifies it is precedence-correct (`deny > ask > allow`) before any agent
runs — so the enforcement does not depend on a user-maintained settings file.

## Directory structure

```
review_2/
├── pr_review/                       # the tool (Python package)
│   ├── cli.py                       # entry points: review-pr, stage-review, review-backlog, unstage-review
│   ├── config.py                    # config load + safety validation
│   ├── queries.py                   # backlog Pipelines A & B + dedupe
│   ├── context.py                   # per-PR context assembly (fresh vs re-review)
│   ├── context_file.py              # serialize the context bundle into the worktree
│   ├── github.py                    # GitHub reads via the gh CLI
│   ├── worktree.py                  # per-PR git worktrees off a shared base clone
│   ├── agy_settings.py              # Layer-1 permission settings for agy
│   ├── agy_seam.py                  # the SINGLE seam that launches agy (safety enforced here)
│   ├── prompt.py                    # the agy review prompt
│   ├── review_file.py               # parse/validate the agent's JSON review file
│   ├── diff_anchors.py              # unified-diff -> inline-comment anchor map
│   ├── staging.py                   # the tool's ONE GitHub-write: event-less pending review
│   ├── review_unit.py               # single-PR review unit + manual-mode stage step
│   ├── batch.py                     # batched concurrent fan-out (the morning run)
│   ├── report.py                    # per-run Markdown report
│   ├── preflight.py                 # fatal, abort-the-run pre-flight checks
│   ├── models.py                    # shared data models
│   └── errors.py                    # error taxonomy (config/preflight vs per-PR)
├── scheduling/                      # unattended scheduler — see scheduling/README.md
│   ├── run-backlog.sh               # portable wrapper (macOS launchd + Linux systemd)
│   ├── install-launchd.sh           # generates + (un)installs the macOS LaunchAgent per machine
│   ├── launchd/*.template           # plist template the installer fills (per machine)
│   └── systemd/*                    # Linux unit templates (future)
├── tests/                           # pytest suite (one test module per source module)
├── pr-reviews-generated/            # ALL generated runtime data (git-ignored) — see "Output: where things land"
├── config.yaml                      # tool configuration (see below)
├── .env / .env.example              # GitHub token VALUES (git-ignored) / no-secret template
├── pyproject.toml                   # package, deps, console scripts, ruff/pytest config
└── *.md (plan, decisions, kickoffs) # historical design/build docs — for future implementers, not needed to use the tool
```

## Requirements

- **Python ≥ 3.9.**
- **`agy`** (Antigravity CLI; verified against 1.0.3), **installed and
  authenticated**. Auth is **`HOME`-relative**, so the tool must run under the
  same `$HOME` the `agy` account is logged into.
- **`gh`** (GitHub CLI), **`git`**, and **`flutter`/`dart`** on `PATH` (the agent
  runs `flutter analyze`/`test` inside each worktree).
- **Two GitHub tokens** (see [GitHub tokens](#github-tokens)).

## Installation & setup

The [Quick start](#quick-start) has the full command sequence (venv → install →
tokens → dry-run); this section covers the details behind those steps.

You do **not** create an `agy` permissions file by hand — Layer 1 is installed
automatically into each worktree at review time (see [Safety model](#safety-model)).

### The virtual environment

This project **always uses a local virtual environment at `.venv/`** — never a
global or system-wide install; `.venv/` is git-ignored. The four console scripts
install into `.venv/bin/` (`review-pr`, `stage-review`, `review-backlog`,
`unstage-review`), so you can invoke them either by activating the venv
(`source .venv/bin/activate`, then `review-backlog …`) or by absolute path
(`.venv/bin/review-backlog …`). The scheduler uses the absolute-path form, so it
needs no activation.

### Python dependencies

Dependencies are declared in `pyproject.toml` and installed for you by the
`pip install -e` step in the [Quick start](#quick-start) — you do not install
them by hand. There are two groups:

- **Runtime** (`[project.dependencies]` — needed to run the tool):
  `unidiff>=0.7.5`, `PyYAML>=6.0`, `python-dotenv>=1.0.0`, `typer>=0.9.0`.
  (`typer` transitively pulls in `click`, `rich`, `shellingham`, etc.
  automatically — no need to list them.)
- **Dev** (`[project.optional-dependencies].dev` — only for tests + lint,
  installed when you use the `[dev]` extra): `pytest>=7.0`, `ruff>=0.4.0`.

`pip install -e .` gives you the runtime set only; `pip install -e '.[dev]'`
adds the dev tools. Either way the tool runs identically — the dev packages are
not imported at runtime.

External CLIs are **not** Python packages and must be installed separately (see
[Requirements](#requirements)): `agy`, `gh`, `git`, and `flutter`/`dart`.

## Configuration reference

All knobs live in `config.yaml`. The config holds only the **names** of the token
environment variables — never token values. Safety-critical fields are
**validated on load** (a bad value is a fatal, loud pre-flight error).

| Key | Required | Default | Meaning |
|-----|----------|---------|---------|
| `repo` | ✅ | — | Target repository, `owner/name`. |
| `username` | ✅ | — | Your GitHub login (drives the A/B queries). **Must be the account behind the read-only token**, or the queries return nothing. |
| `mode` | ✅ | — | `auto-stage-review` or `manual-stage-review`. Neither submits. |
| `default_branch` | | `master` | Repo default branch. |
| `labels_to_skip` | | `[]` | PRs carrying any of these labels are skipped (exact, case-sensitive). |
| `batch_size` | | `5` | Max reviews run concurrently per batch (positive int). |
| `review_file_dir` | ✅ | — | Where the agent writes each review JSON. |
| `report_dir` | ✅ | — | Per-run Markdown report directory. |
| `base_clone_dir` | ✅ | — | Single shared clone; worktrees branch off it. |
| `worktree_dir` | | `pr-reviews-generated/worktrees` | One worktree per PR. |
| `agent_github_token_env` | ✅ 🔒 | — | Env-var **name** of the **read-only** token the agent sees. |
| `orchestrator_github_token_env` | ✅ 🔒 | — | Env-var **name** of the **write** token (must differ from the above). |
| `use_skip_permissions` | 🔒 | `false` | **Must be `false`** — validated; `--dangerously-skip-permissions` voids every safety layer. |
| `agy_settings_path` | | `~/.gemini/antigravity-cli/settings.json` | Documented location of the (untouched) global `agy` config — a fallback path. **Not read by the default project-local flow**: Layer 1 is installed per-worktree automatically. |
| `agy_command` | | `agy` | The review-agent command (the swappable seam). |
| `agy_model` | | `null` (inherit global) | Pin the review model for reproducible quality, e.g. `"Claude Sonnet 4.6 (Thinking)"` (thorough) or `"Gemini 3.5 Flash"` (fast). `null` = whatever `agy` is globally set to. |
| `agy_timeout_seconds` | | `1800` | Per-invocation agy timeout (backstops a hang). |
| `style_guide_enabled` | | `true` | Add a Flutter style-guide conformance lens to each review. Adds ~22K tokens per PR — set `false` to save `agy` quota (e.g. while burning down a large backlog). |
| `style_guide_repo` | | `flutter/flutter` | **Trusted** source repo for the guide — fetched fresh each run. Never a fork: a doctored guide would be a prompt-injection vector (see [Safety model](#safety-model)). |
| `style_guide_ref` | | `master` | Ref the guide is fetched from, so the rubric tracks upstream as it evolves. |
| `style_guide_path` | | `docs/contributing/Style-guide-for-Flutter-repo.md` | Repo-relative guide path; also used to detect (and flip the lens for) a PR that edits the guide. |

🔒 = safety-critical. In path fields, `~` is expanded and a **relative path
resolves next to `config.yaml`** (not the shell's cwd), so generated data lands
in the project regardless of where you invoke the CLI from.

## GitHub tokens

Two tokens, supplied as **values** via the environment (a git-ignored `.env` in
dev; the scheduler's environment in production). `config.yaml` names them via
`agent_github_token_env` / `orchestrator_github_token_env`; the defaults are:

- **`GH_TOKEN_READONLY`** — the **read-only** token. Used by the agent *and* by
  all orchestrator reads. Must not be able to write (verified empirically by a
  pre-flight write-isolation probe that requires a write attempt to fail).
- **`GH_TOKEN_WRITE`** — the **write** token. Used **only** by the orchestrator's
  single event-less staging write.

Token shapes:

- **Fine-grained PATs** (preferred): both scoped to the repo, repository
  permissions only — read-only = Contents/PR/Issues *Read*; write = same but
  *Pull requests: Read **and** write*. No account permissions.
- **Interim classic PATs**: read-only = a classic PAT with **no scopes**; write =
  a classic PAT with **`public_repo`** only. Swapping to fine-grained later is a
  values-only edit in `.env` — no code change.

`.env` keeps **both** variable names; the write-token scrub from the agent
happens in-memory only (do not rename/remove them in `.env`).

## CLI usage

Four console scripts are installed into the venv. All accept `--config`
(default `config.yaml`) and `--repo OWNER/REPO` to override the configured repo.

### `review-backlog` — the batch run

```
review-backlog [--limit N] [--prs 1,2,3] [--dry-run] [--repo OWNER/REPO] [--config PATH]
```

- `--limit N` — cap to the first N of the backlog (test/throttle runs).
- `--prs 1,2,3` — review an **explicit** list, **bypassing the query + label
  filter** (but still flowing through skip → preflight → fan-out → report). Ideal
  for hand-picked catch-up.
- `--dry-run` — list what *would* be reviewed and stop (no agent, no staging).

```sh
review-backlog --dry-run                 # see today's backlog
review-backlog --prs 186618,186617       # review two specific PRs
review-backlog --limit 5                 # review the first 5 of the live backlog
review-backlog                           # the full morning run
```

It is **idempotent**: PRs that already carry your pending review are skipped, so
re-runs (or an overlapping scheduled run) never double-stage. Exits **0** on
success *including partial* ("7 reviewed, 2 failed"); non-zero only on a fatal
pre-flight failure. The terminal line `Done: N reviewed, M failed, K skipped.` is
the machine-readable outcome the scheduler classifies.

### `review-pr` — review one PR (the core unit / debug entry point)

```
review-pr NUMBER [--as fresh|rereview] [--repo OWNER/REPO] [--mode MODE] [--config PATH]
```

- `--as fresh|rereview` — force the context kind; auto-detected if omitted (a PR
  you've `reviewed-by` auto-classifies as a re-review).
- `--mode` — override `auto-stage-review` / `manual-stage-review` for this run.

```sh
review-pr 186618                         # review + stage pending (auto mode)
review-pr 186618 --as fresh              # force a fresh-review bundle
```

### `stage-review` — manual-mode phase 2

```
stage-review NUMBER [--repo OWNER/REPO] [--config PATH]
```

In `manual-stage-review` mode, `review-pr`/`review-backlog` write the review file
and **stop**; you read it, then run `stage-review N` to stage the pending review
(no agent involved). In `auto-stage-review` mode you don't need this.

### `unstage-review` — clear a pending review

```
unstage-review [NUMBER] [--prs 1,2,3] [--dry-run] [--repo OWNER/REPO] [--config PATH]
```

Deletes **your own PENDING** review on one or more PRs — the inverse of
`stage-review`. It can only remove an unsubmitted pending review by you; it
**cannot** delete or un-submit a review you've already submitted, and it never
touches anyone else's. A PR with no pending review by you is skipped, so it's
safe to re-run; `--dry-run` previews without deleting.

```sh
unstage-review 186618                          # clear one PR's pending review
unstage-review --prs 186618,186617             # clear several
unstage-review --prs 186618,186617 --dry-run   # preview only; delete nothing
```

### Modes

- **`auto-stage-review`** — review and stage the pending review in one shot.
- **`manual-stage-review`** — review, write the file, stop; you run `stage-review`
  for phase 2.
- **Neither submits.** Submitting is always a manual GitHub-UI action.

## Operating model & scheduling

Because **you** are the rate-limiter — every staged review still needs you to
read and submit it — the intended workflow is two-phase:

1. **Catch-up (manual, attended).** While the backlog is large, review
   hand-picked chunks with `review-backlog --prs …` (or `--limit N`) so you only
   stage what you can process in a day.
2. **Maintenance (scheduled).** Once the backlog is a daily trickle, enable the
   unattended scheduler (weekday 06:00 on macOS via `launchd`; Linux `systemd`
   templates provided). It stages PENDING reviews each morning with a failure
   surface so a dead run (e.g. exhausted `agy` quota) is **visible, not silent**.

The scheduler ships **dormant** (not loaded). See **[`scheduling/README.md`](scheduling/README.md)**
for the install/test/uninstall one-liners and the failure-surface details.

## Output: where things land

Everything lives under `pr-reviews-generated/` in the project dir — git-ignored
and fully regenerable (configurable; relative paths resolve next to
`config.yaml`):

| Path | What |
|------|------|
| `pr-reviews-generated/pending/` | the agent's review JSON per PR (`review_file_dir`) |
| `pr-reviews-generated/reports/report-YYYY-MM-DD.md` | per-run triage dashboard (`report_dir`) |
| `pr-reviews-generated/flutter-base/` | the shared blobless base clone (`base_clone_dir`) |
| `pr-reviews-generated/worktrees/` | per-PR worktrees, provisioned then torn down (`worktree_dir`) |
| `pr-reviews-generated/logs/` | scheduler logs (`backlog-*.log`, `launchd.*.log`) |
| `pr-reviews-generated/locks/` | the scheduler's single-run lock |

## Troubleshooting & gotchas

These mostly fail **silently** — the worst kind for an unattended run:

- **`agy` exits 0 on a quota 429** (`RESOURCE_EXHAUSTED`), printing nothing — a
  dead run looks like "reviewed 0". The tool greps each `agy` log and marks that
  PR failed; the scheduler additionally flags a wholesale `0 reviewed, N failed`
  run as FAILED. **If reviews come back empty, suspect quota/auth first.**
- **Wrong/blank `HOME`** → `agy` auth fails on every PR (auth is HOME-relative).
  Scheduled jobs must set `HOME` explicitly.
- **Tiny `PATH`** in a scheduled job → `agy`/`gh`/`git`/`flutter` not found. Set a
  full `PATH` (the scheduler wrapper does this).
- **Review quality dropped?** Suspect the **model**, not the prompt — pin
  `agy_model` for reproducibility.
- **Empty backlog unexpectedly?** `gh search prs` must use flags, not raw query
  qualifiers, or it silently returns 0 (already handled).
- **>300-file PRs**: GitHub's compare returns ≤300 files; the diff is truncated
  with a logged warning (pagination deferred).
- **422 on staging**: only one pending review per PR per user — the idempotency
  filter prevents this; an out-of-diff inline anchor also 422s the whole review
  atomically, so anchors are validated against the diff before the POST.
- **Style-guide fetch failed** (loud and fatal — *not* silent): the run aborts
  before any agent work with the exact reason and a fix hint. Check
  `style_guide_repo`/`_ref`/`_path`, or set `style_guide_enabled: false` to run
  without the lens.

## Development

```sh
pip install -e '.[dev]'
pytest                      # the suite under tests/ (one module per source file)
ruff check pr_review tests  # lint  (Google style, 2-space indent — see pyproject.toml)
ruff format pr_review tests # format (2-space; black/ruff-format default to 4)
```

See [Python dependencies](#python-dependencies) for the package set; `gh`,
`git`, `agy`, and `flutter` are external CLIs invoked via `subprocess`. The
diff-anchor parser's hardest cases (renames, multi-hunk and post-force-push
diffs, LEFT-side anchors, no-newline markers) are covered by
`tests/test_diff_anchors_stress.py`.

## Out of scope / deferred

Deliberately not built (none block daily use):

- **Network-layer egress isolation** — running the agent behind a firewall that
  physically blocks GitHub, as a 4th belt-and-suspenders layer for the unattended
  deployment. Optional; Layers 1–3 already prevent writes.
- **>300-file PR pagination** (truncates with a warning today).
- **Multi-repo support** (single repo by design).
- **Auto-submit** — explicitly prohibited, by design.
