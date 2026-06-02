"""Pre-flight checks — fatal/abort-the-run validation (plan §6.2).

These abort the whole run with a loud, standalone, actionable error before any
per-PR work: missing tokens, missing tooling, unwritable dirs, a
precedence-broken Layer-1 config, or — the security gate — a read-only agent
token that can actually write.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
import tempfile

from pr_review import agy_settings, worktree
from pr_review.config import (
  check_global_agy_settings,
  resolve_token,
  validate_agy_settings,
)
from pr_review.errors import PreflightError

_LOG = logging.getLogger(__name__)


def preflight(cfg, repo: str) -> None:
  """Run the fatal pre-flight checks; raise on the first failure (plan §6.2)."""
  resolve_token(cfg.agent_github_token_env)
  resolve_token(cfg.orchestrator_github_token_env)
  _check_tooling(cfg)
  for label, directory in (
    ("review_file_dir", cfg.review_file_dir),
    ("report_dir", cfg.report_dir),
    ("worktree_dir", cfg.worktree_dir),
    ("base_clone_dir parent", pathlib.Path(cfg.base_clone_dir).parent),
  ):
    _check_writable(label, directory)
  _check_base_clone(cfg, repo)
  _check_layer1_renders_valid()
  # The agent inherits the union of project-local + global agy settings; a
  # broad global allow the project deny-list does not counter would broaden it
  # beyond its Layer-1 allowlist (plan §1 Layer 1). Advisory — warn, don't
  # abort: the dangerous outcomes are already bounded by Layer 2 + sandbox +
  # never-submit, and a hard fail would break a legitimately-broad global
  # config. (Finding F12.)
  broad = check_global_agy_settings(
    cfg.agy_settings_path, project_allow=agy_settings.DEFAULT_ALLOW
  )
  if broad:
    _LOG.warning(
      "global agy settings (%s) grant the review agent capabilities beyond "
      "its Layer-1 allowlist that the project deny-list does not counter: %s. "
      "These carry into the agent via the settings union (deny>ask>allow); "
      "scope them down if the agent should stay isolated (plan §1 Layer 1).",
      cfg.agy_settings_path,
      broad,
    )


def _check_base_clone(cfg, repo: str) -> None:
  base = pathlib.Path(cfg.base_clone_dir)
  if (base / ".git").is_dir() and not worktree.origin_matches(base, repo):
    raise PreflightError(
      f"base clone at {base} is a clone of a different repo, not {repo}. "
      f"Remove it (rm -rf {base}) or set base_clone_dir to a fresh path "
      "(plan §4.3)."
    )


def check_command_tooling(tools) -> None:
  """Verify each named tool is on PATH; raise PreflightError if any is missing.

  Exposed so narrower entry points (e.g. the manual stage step, which only
  shells out to ``gh``) can require their own subset without the full check.
  """
  for tool in tools:
    if shutil.which(tool) is None:
      raise PreflightError(
        f"required tool '{tool}' not found on PATH (plan §4.3)."
      )


def _check_tooling(cfg) -> None:
  check_command_tooling(("gh", "git", cfg.agy_command, "flutter"))


def _check_writable(label: str, directory) -> None:
  path = pathlib.Path(directory)
  try:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".pr-review-write-probe"
    probe.write_text("x")
    probe.unlink()
  except OSError as error:
    raise PreflightError(
      f"{label} ({directory}) is not writable: {error}."
    ) from error


def _check_layer1_renders_valid() -> None:
  settings = {"permissions": agy_settings.render_permissions()}
  fd, path = tempfile.mkstemp(suffix=".json")
  os.close(fd)
  try:
    pathlib.Path(path).write_text(json.dumps(settings))
    validate_agy_settings(path)  # precedence-correctness (plan §1, §6.2)
  finally:
    with contextlib.suppress(OSError):
      os.unlink(path)


def verify_read_only_token(
  repo: str, pr_number: int, readonly_token: str, gh_path: str = "gh"
) -> None:
  """Write-isolation probe (§6.2): the agent's token must NOT be able to write.

  Attempts an event-less review create with the read-only token and expects it
  to FAIL. If it succeeds, that is a fatal security misconfiguration (and the
  stray pending review is deleted).
  """
  env = dict(os.environ)
  env["GH_TOKEN"] = readonly_token
  env["GITHUB_TOKEN"] = readonly_token
  try:
    proc = subprocess.run(
      [
        gh_path,
        "api",
        "--method",
        "POST",
        f"repos/{repo}/pulls/{pr_number}/reviews",
        "--input",
        "-",
      ],
      input='{"body":"pr-review write-isolation probe (must be denied)"}',
      env=env,
      capture_output=True,
      text=True,
      timeout=60,
      check=False,
    )
  except (subprocess.SubprocessError, OSError) as error:
    # A hung gh (TimeoutExpired) or a transport/exec failure (OSError) leaves
    # write-capability UNPROVEN — fail loud as a PreflightError (the same
    # fail-closed stance as an inconclusive non-zero exit below), not a raw
    # traceback escaping the caller's `except PreflightError` (finding M1).
    raise PreflightError(
      f"SECURITY: the write-isolation probe on {repo}#{pr_number} could not "
      f"run ({type(error).__name__}: {error}); write-capability is unproven. "
      "Resolve the failure and re-run (plan §1 Layer 2)."
    ) from error
  if proc.returncode != 0:
    # Only an AUTHORIZATION denial proves the token cannot write. Any other
    # non-zero exit (network error, 404 on a bad/closed PR, 401 expired token,
    # 429 rate limit) leaves write-capability UNPROVEN and would otherwise let
    # a write-capable token slip through this gate — so fail loud, inconclusive.
    #
    # A bare "403" is not enough: GitHub returns HTTP 403 (not 429) for both
    # primary and SECONDARY rate limits, so a write-capable token that is
    # rate-limited at probe time must NOT be read as "denied". We require an
    # explicit authorization signal AND the absence of any rate-limit phrasing.
    # Matching "http 403" (not bare "403") also avoids spurious digit hits like
    # a PR number "#4031" echoed in an unrelated 404's stderr.
    stderr = (proc.stderr or "").lower()
    rate_limited = "rate limit" in stderr or "abuse" in stderr
    authz_denied = "resource not accessible" in stderr or "http 403" in stderr
    if authz_denied and not rate_limited:
      return  # write denied — the desired outcome
    raise PreflightError(
      f"SECURITY: could not prove the agent's read-only token cannot write to "
      f"{repo}#{pr_number} — the write-isolation probe failed without a clear "
      "denial (HTTP 403 / 'Resource not accessible'). It may be a transient "
      "network error, a bad/closed PR number, an expired token, or a rate "
      "limit — any of which would also mask a write-capable token. Resolve the "
      f"failure and re-run (plan §1 Layer 2). gh exit {proc.returncode}: "
      f"{(proc.stderr or '').strip()}"
    )

  # The read-only token WROTE. Delete the stray pending review, then fail loud.
  review_id = None
  try:
    parsed = json.loads(proc.stdout)
    review_id = parsed.get("id") if isinstance(parsed, dict) else None
    if review_id:
      subprocess.run(
        [
          gh_path,
          "api",
          "--method",
          "DELETE",
          f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,  # bound the cleanup; a hang here once froze the run (F18)
        check=False,
      )
  except Exception as cleanup:  # noqa: BLE001 - cleanup must never mask the
    # security failure: log the stray review so an operator can remove it, then
    # still raise the fatal error below (F18).
    _LOG.warning(
      "could not delete the stray write-isolation probe review (id %s) on "
      "%s#%s: %s — remove it manually.",
      review_id,
      repo,
      pr_number,
      cleanup,
    )
  raise PreflightError(
    f"SECURITY: the agent's read-only token CREATED a review on "
    f"{repo}#{pr_number} — it is NOT read-only. Fix its scopes before running "
    "(plan §1 Layer 2)."
  )
