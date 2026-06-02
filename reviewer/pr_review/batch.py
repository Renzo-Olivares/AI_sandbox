"""Batched fan-out — the morning run (plan §4).

The caller (CLI) does the query-first snapshot and dedupe, filters out PRs that
already have a pending review (idempotency), then hands the ordered backlog
here. This module fans the single-PR unit out per PR with concurrency bounded to
``batch_size`` (never more than that many isolated reviews/worktrees at once),
and one PR failing is recorded and skipped, never killing the run (plan §6.2).
Taking the backlog as a parameter lets the same fan-out serve the query backlog
and an explicit ``--prs`` list.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import logging
import threading

from pr_review import context, preflight, review_unit, staging, worktree
from pr_review.config import resolve_token
from pr_review.errors import PreflightError
from pr_review.github import GitHub
from pr_review.models import PRRef
from pr_review.review_unit import ReviewResult

_LOG = logging.getLogger(__name__)

# Pipeline A PRs get a fresh review; Pipeline B (silent updates) get a
# re-review; an explicit list (no pipeline) auto-detects (plan §4.4, §6).
_KIND_BY_PIPELINE = {"A": "fresh", "B": "rereview"}


@dataclasses.dataclass
class PROutcome:
  """The result (or failure) of one PR in the batch."""

  ref: PRRef
  result: ReviewResult | None
  failure: str | None
  skipped: str | None = None  # set when already staged (benign skip, F24)


@dataclasses.dataclass
class BatchRun:
  """The outcome of a whole backlog run (input to the report, §6.2)."""

  repo: str
  mode: str
  backlog: list  # the ordered PRRef candidates handed in (post-filter)
  total: int
  skipped_for_limit: int
  outcomes: list  # PROutcome, in backlog order


def run_backlog(
  cfg, repo: str, backlog, *, limit=None, agy_timeout=None
) -> BatchRun:
  """Fan the single-PR unit out over ``backlog`` in bounded batches (plan §4).

  Args:
    cfg: the loaded Config.
    repo: ``owner/name``.
    backlog: the ordered list of :class:`~pr_review.models.PRRef` to review.
    limit: cap to the first ``limit`` of ``backlog`` (the first-batch test);
      ``None`` reviews them all.
    agy_timeout: override the per-invocation agy timeout.

  Returns:
    A :class:`BatchRun`.
  """
  if limit is not None and limit < 1:
    raise PreflightError(
      f"--limit must be a positive integer (got {limit}); use a value >= 1, "
      "or omit it to review the whole backlog (finding F25)."
    )
  total = len(backlog)
  processed = backlog if limit is None else backlog[:limit]
  skipped = total - len(processed)
  _LOG.info(
    "fan-out: %d candidates, processing %d (limit=%s)",
    total,
    len(processed),
    limit,
  )

  style_guide_text = None
  if processed:
    # Layer-2 write-isolation probe before any agent runs (plan §6.2), then
    # fetch the style guide + base clone once; per-PR provisioning skips the
    # fetch and serializes git ops via this lock while reviews run concurrently.
    readonly_token = resolve_token(cfg.agent_github_token_env)
    preflight.verify_read_only_token(repo, processed[0].number, readonly_token)
    # Fetch the trusted style guide ONCE per run, not per PR; fatal if enabled
    # and unreachable (plan §8). Before fan-out, so it aborts cleanly.
    style_guide_text = context.fetch_style_guide_text(
      GitHub(readonly_token), cfg
    )
    worktree.ensure_base_clone(repo, cfg.base_clone_dir, fetch=True)
  git_lock = threading.Lock()

  outcomes = []
  with concurrent.futures.ThreadPoolExecutor(
    max_workers=cfg.batch_size
  ) as executor:
    futures = {
      executor.submit(
        _review_one, cfg, repo, ref, agy_timeout, git_lock, style_guide_text
      ): ref
      for ref in processed
    }
    for future in concurrent.futures.as_completed(futures):
      ref = futures[future]
      try:
        outcomes.append(
          PROutcome(ref=ref, result=future.result(), failure=None)
        )
      except staging.AlreadyStagedError as skip:
        # The PR was staged by a prior/concurrent run (the window after the
        # idempotency filter) — a benign SKIP, not a failure (finding F24).
        _LOG.info("PR #%s already staged; skipping: %s", ref.number, skip)
        outcomes.append(
          PROutcome(ref=ref, result=None, failure=None, skipped=str(skip))
        )
      except Exception as error:  # per-PR isolation (plan §6.2)
        _LOG.warning("PR #%s failed: %s", ref.number, error)
        # Full traceback at DEBUG so a real bug behind the friendly per-PR
        # failure string is diagnosable (finding F46).
        _LOG.debug("PR #%s failure traceback", ref.number, exc_info=True)
        outcomes.append(PROutcome(ref=ref, result=None, failure=str(error)))

  order = {ref.number: i for i, ref in enumerate(processed)}
  outcomes.sort(key=lambda o: order.get(o.ref.number, 1_000_000))
  return BatchRun(
    repo=repo,
    mode=cfg.mode,
    backlog=backlog,
    total=total,
    skipped_for_limit=skipped,
    outcomes=outcomes,
  )


def _review_one(
  cfg, repo, ref: PRRef, agy_timeout, git_lock, style_guide_text
) -> ReviewResult:
  kind = _KIND_BY_PIPELINE.get(ref.pipeline)  # None => auto-detect
  return review_unit.review_single_pr(
    cfg,
    repo,
    ref.number,
    kind=kind,
    agy_timeout=agy_timeout,
    git_lock=git_lock,
    fetch_base=False,
    style_guide_text=style_guide_text,
  )
