"""Single-PR command-line interface (plan §4.4, §2).

Two commands, installed as separate console scripts:

  review-pr <N> [--as fresh|rereview] [--repo OWNER/REPO] [--mode ...]
      Review one PR. In auto-stage mode it stages an event-less pending review;
      in manual-stage mode it writes the review file and stops.

  stage-review <N> [--repo OWNER/REPO]
      Manual-mode phase 2: stage a previously-written review as a pending
      review (no agy involved).

Note: this module intentionally does NOT use ``from __future__ import
annotations`` — typer resolves real type objects from the signatures at runtime.
"""

import logging
import os
from typing import Optional

import typer

from pr_review import batch, preflight, queries, report, review_unit, staging
from pr_review.config import load_config, load_dotenv_file, resolve_token
from pr_review.context import FRESH, REREVIEW
from pr_review.errors import PreflightError
from pr_review.github import GitHub, GithubError
from pr_review.models import PRRef
from pr_review.worktree import WorktreeError

_LOG = logging.getLogger(__name__)

_AS_TO_KIND = {"fresh": FRESH, "rereview": REREVIEW}
_VALID_MODES = ("auto-stage-review", "manual-stage-review")


def _setup_logging() -> None:
  """Configure logging from ``$PR_REVIEW_LOG_LEVEL`` (default WARNING).

  Set ``PR_REVIEW_LOG_LEVEL=DEBUG`` to surface the full traceback the broad
  per-PR / per-command handlers log on failure (finding F46) — so a genuine bug
  hiding behind a friendly one-line failure message is diagnosable, while normal
  runs stay quiet. WARNING (the default) preserves prior behavior: warnings
  show, info/debug do not.
  """
  level = os.environ.get("PR_REVIEW_LOG_LEVEL", "WARNING").upper()
  logging.basicConfig(
    level=getattr(logging, level, logging.WARNING),
    format="%(levelname)s %(name)s: %(message)s",
  )


def _load_cfg(config_path: str):
  """Load config + a local .env, exiting cleanly on a pre-flight error."""
  load_dotenv_file(".")
  try:
    return load_config(config_path)
  except PreflightError as error:
    typer.secho(f"CONFIG ERROR: {error}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1) from error


def _review_pr(
  number: int = typer.Argument(..., help="PR number to review."),
  as_: Optional[str] = typer.Option(
    None, "--as", help="Force context: fresh|rereview (auto-detect if omitted)."
  ),
  repo: Optional[str] = typer.Option(
    None, "--repo", help="Override repo (owner/name); else config's repo."
  ),
  mode: Optional[str] = typer.Option(
    None, "--mode", help="Override mode: auto-stage-review|manual-stage-review."
  ),
  config: str = typer.Option("config.yaml", "--config", help="Config path."),
) -> None:
  """Review a single PR; auto mode stages an event-less pending review."""
  cfg = _load_cfg(config)
  target_repo = repo or cfg.repo

  kind = None
  if as_ is not None:
    if as_ not in _AS_TO_KIND:
      typer.secho(
        f"--as must be fresh|rereview, got {as_!r}", fg="red", err=True
      )
      raise typer.Exit(1)
    kind = _AS_TO_KIND[as_]

  effective_mode = mode or cfg.mode
  if effective_mode not in _VALID_MODES:
    typer.secho(f"--mode must be one of {_VALID_MODES}", fg="red", err=True)
    raise typer.Exit(1)
  stage = effective_mode == "auto-stage-review"

  # Same fatal gates as the batch path (plan §6.2): never hand the read-only
  # token to the untrusted agent without first proving it cannot write, and
  # verify tooling/dirs/Layer-1 up front. (Review finding F03.)
  try:
    preflight.preflight(cfg, target_repo)
    preflight.verify_read_only_token(
      target_repo, number, resolve_token(cfg.agent_github_token_env)
    )
  except PreflightError as error:
    typer.secho(
      f"PRE-FLIGHT FAILED ({type(error).__name__}): {error}",
      fg=typer.colors.RED,
      err=True,
    )
    raise typer.Exit(1) from error

  typer.echo(
    f"Reviewing PR #{number} on {target_repo} (mode={effective_mode})…"
  )
  try:
    result = review_unit.review_single_pr(
      cfg, target_repo, number, kind=kind, stage=stage
    )
  except staging.AlreadyStagedError as error:
    # Benign: a pending review already exists, so we don't re-stage (F24).
    typer.secho(f"PR #{number} already staged — skipping: {error}", fg="yellow")
    return
  except Exception as error:  # noqa: BLE001 - surface any per-PR failure cleanly
    _LOG.debug("review-pr #%s failed", number, exc_info=True)  # F46
    typer.secho(
      f"REVIEW FAILED for PR #{number}: {error}", fg=typer.colors.RED, err=True
    )
    raise typer.Exit(1) from error
  _print_review_result(result, effective_mode)


def _print_review_result(result, effective_mode: str) -> None:
  typer.echo(f"\nPR #{result.pr.number} [{result.kind}] — {result.pr.title}")
  typer.echo(
    f"  {len(result.review.comments)} inline finding(s); "
    f"{result.anchors} commentable diff lines"
  )
  typer.echo(f"  review file: {result.review_file_path}")
  if result.staging is not None:
    staging = result.staging
    typer.echo(f"  STAGED (pending): {staging.review_url}")
    typer.echo(
      f"    posted={staging.posted_comments} dropped={len(staging.dropped)} "
      f"degraded={len(staging.degraded)}"
    )
  else:
    typer.echo(
      f"  mode={effective_mode}: NOT staged. Review it, then run:\n"
      f"      stage-review {result.pr.number}"
    )


def _stage_review(
  number: int = typer.Argument(..., help="PR whose written review to stage."),
  repo: Optional[str] = typer.Option(None, "--repo", help="Override repo."),
  config: str = typer.Option("config.yaml", "--config", help="Config path."),
) -> None:
  """Stage a previously-written review as a pending review (manual phase 2)."""
  cfg = _load_cfg(config)
  # Staging runs no agent (it posts with the write token by design, so the
  # write-isolation probe does not apply) but it does shell out to gh — verify
  # that subset up front rather than failing mid-post (review finding F03).
  try:
    preflight.check_command_tooling(("gh",))
  except PreflightError as error:
    typer.secho(
      f"PRE-FLIGHT FAILED ({type(error).__name__}): {error}",
      fg=typer.colors.RED,
      err=True,
    )
    raise typer.Exit(1) from error
  try:
    result = review_unit.stage_review(cfg, number, repo=repo)
  except staging.AlreadyStagedError as error:
    # Benign skip, mirroring _review_pr (L1): an existing pending review is not
    # a failure, so don't trip the scheduler's exit-1 alerting.
    typer.secho(f"PR #{number} already staged — skipping: {error}", fg="yellow")
    return
  except Exception as error:  # noqa: BLE001 - surface failure cleanly
    _LOG.debug("stage-review #%s failed", number, exc_info=True)  # F46
    typer.secho(
      f"STAGE FAILED for PR #{number}: {error}", fg=typer.colors.RED, err=True
    )
    raise typer.Exit(1) from error
  typer.echo(f"STAGED (pending): {result.review_url}")
  typer.echo(
    f"  posted={result.posted_comments} dropped={len(result.dropped)} "
    f"degraded={len(result.degraded)}"
  )


def review_pr_entry() -> None:
  """Console-script entry for ``review-pr``."""
  _setup_logging()
  typer.run(_review_pr)


def stage_review_entry() -> None:
  """Console-script entry for ``stage-review``."""
  _setup_logging()
  typer.run(_stage_review)


def _review_backlog(
  limit: Optional[int] = typer.Option(
    None, "--limit", help="Cap to the first N PRs to review (test runs)."
  ),
  prs: Optional[str] = typer.Option(
    None,
    "--prs",
    help="Explicit comma-separated PR numbers (bypasses the query + label "
    "filter; still goes through the rest of the pipeline).",
  ),
  dry_run: bool = typer.Option(
    False, "--dry-run", help="List what would be reviewed and stop."
  ),
  repo: Optional[str] = typer.Option(None, "--repo", help="Override repo."),
  config: str = typer.Option("config.yaml", "--config", help="Config path."),
) -> None:
  """Review the backlog (or explicit --prs list) in batches; stage pending."""
  cfg = _load_cfg(config)
  target_repo = repo or cfg.repo

  # Every fatal setup/run failure surfaces through ONE clean, actionable message
  # + exit 1 — never a raw traceback: a missing token (ConfigError), a failed
  # gh backlog query (GithubError), a bad base clone (WorktreeError), or a
  # pre-flight gate (PreflightError). (Findings F15, F16.)
  try:
    gh = GitHub(resolve_token(cfg.agent_github_token_env))
    if prs:
      backlog = _explicit_backlog(prs)
      listed = ", ".join(f"#{ref.number}" for ref in backlog)
      typer.echo(f"Explicit PRs on {target_repo}: {listed}")
    else:
      list_a, b_minus_a = queries.assemble_backlog(
        gh, target_repo, cfg.username, list(cfg.labels_to_skip)
      )
      backlog = list_a + b_minus_a
      typer.echo(
        f"Backlog on {target_repo}: A={len(list_a)} (requested) + "
        f"B={len(b_minus_a)} (silent) = {len(backlog)} total"
      )

    # Skip PRs already staged this/a prior run (idempotency, plan §4).
    backlog, already = queries.filter_unstaged(
      gh, target_repo, backlog, cfg.username
    )
    if already:
      nums = ", ".join(f"#{ref.number}" for ref in already)
      typer.echo(f"  skipping {len(already)} already-staged: {nums}")

    if dry_run:
      for ref in backlog:
        typer.echo(f"  [{ref.pipeline}] #{ref.number}  {ref.title[:64]}")
      return
    if not backlog:
      typer.echo("Nothing to review — all candidates already staged.")
      typer.echo("PR-REVIEW-STATUS nothing-to-review")  # F47: scheduler token
      return

    preflight.preflight(cfg, target_repo)

    cap = "all" if limit is None else f"the first {limit}"
    typer.echo(
      f"Reviewing {cap} of {len(backlog)} in batches of {cfg.batch_size} "
      f"(mode={cfg.mode})…"
    )
    run = batch.run_backlog(cfg, target_repo, backlog, limit=limit)
  except (PreflightError, GithubError, WorktreeError) as error:
    typer.secho(
      f"PRE-FLIGHT FAILED ({type(error).__name__}): {error}",
      fg=typer.colors.RED,
      err=True,
    )
    raise typer.Exit(1) from error
  report_path = report.write_report(cfg.report_dir, run)
  _print_batch_summary(run, report_path)


def _explicit_backlog(prs: str):
  """Parse a comma-separated PR-number list into explicit-pipeline refs.

  De-duplicates while preserving first-seen order: a repeated number (e.g.
  ``--prs 5,5``) would otherwise produce two refs for the same PR that collide
  on the same ``pr-<n>`` worktree dir — corrupting one review's in-flight
  checkout and double-staging the other (review finding F06).
  """
  refs = []
  seen = set()
  for part in prs.split(","):
    part = part.strip()
    if not part:
      continue
    try:
      number = int(part)
    except ValueError as error:
      typer.secho(
        f"--prs must be comma-separated integers, got {part!r}",
        fg=typer.colors.RED,
        err=True,
      )
      raise typer.Exit(1) from error
    if number in seen:
      continue
    seen.add(number)
    refs.append(
      PRRef(
        number=number,
        title="",
        url="",
        author="",
        updated_at="",
        pipeline="explicit",
      )
    )
  return refs


def _print_batch_summary(run, report_path) -> None:
  ok = sum(1 for o in run.outcomes if o.result is not None)
  failed = sum(1 for o in run.outcomes if o.failure is not None)
  already = sum(1 for o in run.outcomes if o.skipped is not None)
  typer.echo(
    f"\nDone: {ok} reviewed, {failed} failed, {already} already-staged, "
    f"{run.skipped_for_limit} skipped."
  )
  # Stable machine-readable line for the scheduler wrapper to classify the run
  # off, so rewording the human summary above can't break dead-run detection
  # (finding F47). The token never changes; only the counts do.
  typer.echo(
    f"PR-REVIEW-STATUS reviewed={ok} failed={failed} "
    f"already-staged={already} limit-skipped={run.skipped_for_limit}"
  )
  for outcome in run.outcomes:
    if outcome.result is not None and outcome.result.staging is not None:
      typer.echo(
        f"  #{outcome.ref.number} [{outcome.ref.pipeline}] STAGED: "
        f"{outcome.result.staging.review_url}"
      )
    elif outcome.skipped is not None:
      typer.echo(f"  #{outcome.ref.number} ALREADY STAGED (skipped)")
    elif outcome.failure is not None:
      typer.echo(f"  #{outcome.ref.number} FAILED: {outcome.failure[:80]}")
  typer.echo(f"\nReport: {report_path}")


def review_backlog_entry() -> None:
  """Console-script entry for ``review-backlog``."""
  _setup_logging()
  typer.run(_review_backlog)


def _parse_pr_numbers(number, prs: Optional[str]) -> list:
  """Collect PR numbers from an optional positional arg and an optional list."""
  numbers = []
  if number is not None:
    numbers.append(number)
  if prs:
    for part in prs.split(","):
      part = part.strip()
      if not part:
        continue
      try:
        numbers.append(int(part))
      except ValueError as error:
        typer.secho(
          f"--prs must be comma-separated integers, got {part!r}",
          fg=typer.colors.RED,
          err=True,
        )
        raise typer.Exit(1) from error
  seen = set()
  ordered = []
  for num in numbers:
    if num not in seen:
      seen.add(num)
      ordered.append(num)
  return ordered


def _unstage_review(
  number: Optional[int] = typer.Argument(
    None, help="PR number to unstage (or use --prs)."
  ),
  prs: Optional[str] = typer.Option(
    None, "--prs", help="Comma-separated PR numbers to unstage."
  ),
  dry_run: bool = typer.Option(
    False, "--dry-run", help="List what would be unstaged and stop."
  ),
  repo: Optional[str] = typer.Option(None, "--repo", help="Override repo."),
  config: str = typer.Option("config.yaml", "--config", help="Config path."),
) -> None:
  """Delete your own PENDING review on one or more PRs (inverse of staging).

  Only ever removes an unsubmitted PENDING review by you; it cannot delete or
  un-submit a review you already submitted. Use ``--dry-run`` to preview.
  """
  cfg = _load_cfg(config)
  target_repo = repo or cfg.repo
  numbers = _parse_pr_numbers(number, prs)
  if not numbers:
    typer.secho(
      "Give a PR number or --prs 1,2,3 to unstage.", fg="red", err=True
    )
    raise typer.Exit(1)

  gh = GitHub(resolve_token(cfg.agent_github_token_env))
  write_token = (
    None if dry_run else resolve_token(cfg.orchestrator_github_token_env)
  )
  if dry_run:
    typer.echo("DRY RUN — nothing will be deleted.")

  unstaged = skipped = failed = 0
  for num in numbers:
    try:
      review_id = gh.get_pending_review_id_by(target_repo, num, cfg.username)
    except Exception as error:  # noqa: BLE001 - surface per-PR failure cleanly
      _LOG.debug("unstage #%s lookup failed", num, exc_info=True)  # F46
      typer.secho(f"  #{num}: lookup failed: {error}", fg="red", err=True)
      failed += 1
      continue
    if review_id is None:
      typer.echo(f"  #{num}: no pending review by you — skip")
      skipped += 1
      continue
    if dry_run:
      typer.echo(
        f"  #{num}: would unstage your pending review (id {review_id})"
      )
      continue
    try:
      staging.unstage_pending_review(
        repo=target_repo,
        number=num,
        review_id=review_id,
        write_token=write_token,
      )
    except Exception as error:  # noqa: BLE001 - surface per-PR failure cleanly
      _LOG.debug("unstage #%s failed", num, exc_info=True)  # F46
      typer.secho(f"  #{num}: unstage failed: {error}", fg="red", err=True)
      failed += 1
      continue
    typer.echo(f"  #{num}: unstaged pending review {review_id}")
    unstaged += 1

  if not dry_run:
    typer.echo(
      f"\nDone: {unstaged} unstaged, {skipped} skipped, {failed} failed."
    )
  if failed:
    raise typer.Exit(1)


def unstage_review_entry() -> None:
  """Console-script entry for ``unstage-review``."""
  _setup_logging()
  typer.run(_unstage_review)
