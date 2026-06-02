"""The single seam to the Antigravity (``agy``) review agent (plan §4.5).

This is the ONLY place the orchestrator launches ``agy``. Isolating the
invocation here keeps the swappable "invoke the review agent" dependency in one
auditable spot and enforces the safety invariants on *every* launch:

  * NEVER ``--dangerously-skip-permissions`` (plan §1) — that flag voids the
    allowlist + sandbox. It is asserted-absent here and never emitted.
  * Filtered child env (plan §1 Layer 2): EVERY var carrying either token's
    value is removed, then ONLY the read-only token is re-added under the
    canonical ``GH_TOKEN`` / ``GITHUB_TOKEN`` that gh/agy read for auth — so the
    write token appears nowhere and the read token only under the standard vars.
  * ``TERM=dumb`` + stdin from ``/dev/null`` (plan §4.5): without these a
    non-interactive launch is suspended by the OS (SIGTTIN/SIGTTOU) and hangs
    forever.
  * ``--sandbox`` is UNCONDITIONAL — the terminal sandbox is not configurable
    off; the working directory is the PR's worktree.
  * A per-invocation subprocess timeout AND ``agy``'s own ``--print-timeout``
    bound an unanswerable-``ask`` hang (plan §1, §10).

Diagnostics: ``agy`` exits 0 even when its model call fails (e.g. a 429 quota
error), printing nothing — which looks like "the agent wrote no review file".
So every run captures ``agy``'s ``--log-file`` and scans it for such errors,
raising a clear, actionable :class:`AgentError` instead of failing silently.

Verified against **agy 1.0.3**: there is NO ``--cwd`` flag (working directory
is set via the child process ``cwd``); ``-p`` / ``--print`` / ``--prompt`` are
aliases for one-shot mode; ``--print-timeout`` bounds print-mode waits;
``--log-file`` overrides the log path; and ``agy`` auth is HOME-relative (so the
env keeps the real HOME and Layer 1 lives in a project-local settings.json).
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import subprocess
import tempfile

# The nuclear flag, named once so it can be asserted-absent and never emitted.
FORBIDDEN_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"


class AgentError(Exception):
  """The ``agy`` review agent failed (per-PR failure upstream, plan §6.2)."""


class AgentTimeout(AgentError):
  """The ``agy`` invocation exceeded its timeout (a hang, plan §4.5/§10)."""


def build_child_env(readonly_token: str, write_token_env_name: str) -> dict:
  """Build the agent's environment with ONLY read access (plan §1 Layer 2).

  Copies the current environment (``agy`` needs HOME for auth, PATH, etc.),
  then removes EVERY var carrying either token's value — the tool's own
  ``GH_TOKEN_WRITE`` / ``GH_TOKEN_READONLY``, any ambient ``GH_TOKEN`` /
  ``GITHUB_TOKEN``, and the same secret exported under any other name — and
  re-adds ONLY the read-only token under the canonical ``GH_TOKEN`` /
  ``GITHUB_TOKEN`` that gh/agy read for auth (the agent needs read, never
  write). Sets ``TERM=dumb`` (the TTY-hang guard, plan §4.5).

  Args:
    readonly_token: the read-only GitHub token to expose to the agent.
    write_token_env_name: the env var holding the write token (used to learn its
      value so every copy can be scrubbed).

  Returns:
    The child environment mapping.
  """
  env = dict(os.environ)
  write_value = env.get(write_token_env_name)
  # Drop every var holding EITHER secret, so neither token survives under any
  # name; then expose only the read-only token under the canonical auth vars.
  secrets = {value for value in (write_value, readonly_token) if value}
  for name in [n for n, value in env.items() if value in secrets]:
    env.pop(name, None)
  env["GH_TOKEN"] = readonly_token
  env["GITHUB_TOKEN"] = readonly_token
  env["TERM"] = "dumb"
  return env


def build_argv(
  agy_command: str,
  prompt: str,
  *,
  print_timeout_seconds: int = 1800,
  log_file=None,
) -> list:
  """Build the ``agy`` argv for a headless one-shot review (plan §4.5).

  ``--sandbox`` is always present (the terminal sandbox is not configurable
  off) and ``--dangerously-skip-permissions`` is never present. The working
  directory is set via the subprocess ``cwd`` (agy 1.0.3 has no ``--cwd``
  flag), not a flag.

  Raises:
    AgentError: if the forbidden skip-permissions flag is ever present
      (defense-in-depth; this branch must be unreachable).
  """
  argv = [agy_command, "--sandbox"]
  if log_file is not None:
    argv += ["--log-file", str(log_file)]
  argv += ["--print-timeout", f"{print_timeout_seconds}s"]
  argv += ["-p", prompt]
  assert_safe_argv(argv)
  return argv


def assert_safe_argv(argv) -> None:
  """Assert the argv never carries the skip-permissions flag (plan §1, §6.2).

  Used both when building the argv and as a pre-flight check, so the
  never-skip-permissions invariant is enforced structurally and is auditable.

  Raises:
    AgentError: if the forbidden flag is present (in any ``=value`` form).
  """
  for arg in argv:
    if arg == FORBIDDEN_SKIP_PERMISSIONS_FLAG or arg.startswith(
      FORBIDDEN_SKIP_PERMISSIONS_FLAG + "="
    ):
      raise AgentError(
        "refusing to launch agy with --dangerously-skip-permissions: it voids "
        "the permission allowlist and the sandbox (plan §1)."
      )


# Quota / rate-limit markers (matched case-insensitively). Deliberately NOT a
# bare "quota", bare "429", or bare "rate limit": agy prints benign
# "quotaProject=" auth lines and rate-limit HEADERS (e.g. X-RateLimit-Remaining)
# on SUCCESSFUL runs too, and a lone "429" can appear in unrelated log noise —
# matching those would turn a good review into a failure. So we require explicit
# exhaustion/denial phrasing (findings F29, L3).
_QUOTA_MARKERS = (
  "resource_exhausted",
  "code 429",
  "http 429",
  "rate limit exceeded",
  "rate limited",
  "secondary rate limit",
  "quota exceeded",
  "quota exhausted",
)


def scan_agy_log(log_path) -> str | None:
  """Scan an ``agy`` log for a fatal error that exits 0 (e.g. a 429 quota).

  Returns a clear, actionable message if one is found, else ``None``. Matching
  is case-insensitive and covers several quota/rate-limit phrasings, since the
  exact wording is only verified against one agy version (finding F29).
  """
  try:
    text = pathlib.Path(log_path).read_text()
  except OSError:
    return None
  lines = text.splitlines()
  for line in lines:
    if any(marker in line.lower() for marker in _QUOTA_MARKERS):
      message = line.split("] ", 1)[-1].strip()
      return (
        f"agy model quota exhausted / rate limited: {message[:300]} — enable "
        "overages or wait for the quota to reset."
      )
  for line in lines:
    if "agent executor error" in line.lower():
      message = line.split("] ", 1)[-1].strip()
      return f"agy agent error: {message[:300]}"
  return None


def run_agent(
  *,
  agy_command: str,
  prompt: str,
  worktree,
  readonly_token: str,
  write_token_env_name: str,
  timeout_seconds: int = 1800,
) -> str:
  """Launch ``agy`` headlessly in ``worktree`` and return its stdout.

  The agent is prompted to WRITE its review as a JSON file into the worktree
  (route (a), plan §4.5); the returned stdout is incidental — the orchestrator
  reads and validates the file. The agent has no GitHub write access.

  No retry/backoff on a transient quota (429): this is a deliberate choice (F29)
  — a quota error fails just this PR (recorded, the batch continues), and the
  next scheduled run picks it up again via the idempotency backlog, so a blip
  costs at most one cycle's delay rather than burning more quota mid-run.

  Raises:
    AgentTimeout: if the invocation exceeds the hard timeout (likely a hang).
    AgentError: on launch failure, non-zero exit, or a detected agy-internal
      error (quota/agent error) that exits 0 — with an actionable message.
  """
  log_fd, log_path = tempfile.mkstemp(prefix="agy-review-", suffix=".log")
  os.close(log_fd)
  argv = build_argv(
    agy_command,
    prompt,
    print_timeout_seconds=timeout_seconds,
    log_file=log_path,
  )
  env = build_child_env(readonly_token, write_token_env_name)
  # Slightly higher hard timeout than agy's own --print-timeout, so agy exits
  # itself first and we only hard-kill if it ignores its own bound.
  hard_timeout = timeout_seconds + 60
  try:
    with open(os.devnull, "rb") as devnull:
      proc = subprocess.run(
        argv,
        cwd=str(worktree),
        env=env,
        stdin=devnull,
        capture_output=True,
        text=True,
        timeout=hard_timeout,
        check=False,
      )
  except FileNotFoundError as e:
    _unlink(log_path)
    raise AgentError(f"agy command not found: {argv[0]}") from e
  except subprocess.TimeoutExpired as e:
    # Scan the log BEFORE unlinking: a quota/agent error that ALSO hung would
    # otherwise be misreported as a generic timeout (finding F29).
    agy_error = scan_agy_log(log_path)
    _unlink(log_path)
    detail = f" (log shows: {agy_error})" if agy_error else ""
    raise AgentTimeout(
      f"agy exceeded {hard_timeout}s in {worktree} — possible hang or an "
      f"unanswerable permission prompt (plan §4.5).{detail}"
    ) from e

  agy_error = scan_agy_log(log_path)
  _unlink(log_path)
  if proc.returncode != 0:
    detail = agy_error or (proc.stderr or "").strip()[:500]
    raise AgentError(f"agy exited {proc.returncode}: {detail}")
  if agy_error:
    raise AgentError(agy_error)
  return proc.stdout


def _unlink(path) -> None:
  with contextlib.suppress(OSError):
    os.unlink(path)
