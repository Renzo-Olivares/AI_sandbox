"""The single-PR review unit + the manual-mode stage step (plan §4.4, §2).

The morning run is "do this N times"; the Phase-C batch pipeline calls
:func:`review_single_pr` in a loop. The unit assembles context, provisions an
isolated worktree, installs Layer 1 project-locally (scrubbing PR-shipped
config), injects the context bundle, runs agy, reads + validates the review,
persists it as a hand-off *envelope*, and — in ``auto-stage-review`` mode —
stages an event-less pending review immediately. In ``manual-stage-review``
mode it stops after writing the envelope; the human later runs the stage step
(:func:`stage_review`), which re-derives the anchor map and posts.

Both modes share one review-file format and one staging path; they differ only
in *when* staging runs (plan §2).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib

from pr_review import agy_seam, agy_settings, context, worktree
from pr_review.atomicio import write_text_atomic
from pr_review.config import resolve_token
from pr_review.context_file import write_context_file
from pr_review.diff_anchors import build_anchor_map
from pr_review.github import GitHub
from pr_review.models import DiffFile, PRMeta, ReviewFile
from pr_review.prompt import build_prompt
from pr_review.review_file import (
  parse_review,
  read_review_file,
  review_from_dict,
  review_to_dict,
)
from pr_review.staging import StagingResult, stage_pending_review

_LOG = logging.getLogger(__name__)

AUTO_MODE = "auto-stage-review"

# Sentinel: the caller did not supply a style guide, so fetch it here. Distinct
# from an explicit ``None`` (a batch run that resolved "no style lens"), which
# must NOT trigger a per-PR fetch.
_UNSET = object()


@dataclasses.dataclass
class ReviewResult:
  """The outcome of reviewing one PR."""

  pr: PRMeta
  kind: str
  anchors: int
  review: ReviewFile
  review_file_path: str
  staging: StagingResult | None  # None when not staged (manual mode, pre-stage)


def review_single_pr(
  cfg,
  repo: str,
  number: int,
  *,
  kind: str | None = None,
  stage: bool | None = None,
  agy_timeout: int | None = None,
  git_lock=None,
  fetch_base: bool = True,
  style_guide_text=_UNSET,
) -> ReviewResult:
  """Review one PR end-to-end (plan §4.4).

  Args:
    cfg: the loaded Config.
    repo: ``owner/name`` (the fork override for this session's tests).
    number: the PR number.
    kind: ``"fresh"`` / ``"rereview"`` to force; ``None`` to auto-detect (§4.4).
    stage: stage the pending review now? ``None`` derives it from ``cfg.mode``
      (auto-stage stages; manual-stage does not).
    agy_timeout: override the per-invocation agy timeout (else cfg value).
    git_lock: optional shared lock serializing base-clone git ops during a
      concurrent batch run (plan §4.3).
    fetch_base: fetch the base clone during provisioning (the batch fetches it
      once upfront and passes False).
    style_guide_text: the trusted style guide, pre-fetched once per run by the
      batch (or an explicit ``None`` meaning "no style lens this run"). Left
      unset on the single-PR path, where this unit fetches it itself (§8).

  Returns:
    A :class:`ReviewResult`.
  """
  readonly_token = resolve_token(cfg.agent_github_token_env)
  write_token = resolve_token(cfg.orchestrator_github_token_env)
  gh = GitHub(readonly_token)

  if style_guide_text is _UNSET:
    style_guide_text = context.fetch_style_guide_text(gh, cfg)
  style_guide_path = cfg.style_guide_path if cfg.style_guide_enabled else None

  if kind is None:
    kind = context.classify_pr(gh, repo, number, cfg.username)
  ctx = context.assemble_context(
    gh,
    repo,
    number,
    kind,
    cfg.username,
    cfg.default_branch,
    style_guide_text=style_guide_text,
    style_guide_path=style_guide_path,
  )
  timeout = agy_timeout if agy_timeout is not None else cfg.agy_timeout_seconds
  base_ref = ctx.pr.base_ref or cfg.default_branch

  with worktree.provisioned_worktree(
    repo,
    number,
    ctx.pr.head_sha,
    cfg.base_clone_dir,
    cfg.worktree_dir,
    git_lock=git_lock,
    fetch_base=fetch_base,
  ) as wt:
    settings_path = agy_settings.install_project_settings(
      wt, model=cfg.agy_model
    )
    _LOG.info("installed project-local Layer-1 settings at %s", settings_path)
    context_path, output_path, guide_path = write_context_file(wt, ctx, repo)
    prompt = build_prompt(
      repo,
      ctx.pr,
      kind,
      context_path,
      output_path,
      style_guide_path=guide_path,
      touches_style_guide=ctx.touches_style_guide,
    )
    agy_stdout = agy_seam.run_agent(
      agy_command=cfg.agy_command,
      prompt=prompt,
      worktree=wt,
      readonly_token=readonly_token,
      write_token_env_name=cfg.orchestrator_github_token_env,
      timeout_seconds=timeout,
    )
    review = _load_review(output_path, agy_stdout)
    persisted = _write_envelope(
      cfg.review_file_dir,
      repo,
      number,
      ctx.pr.head_sha,
      base_ref,
      kind,
      review,
      ctx.diff_files,
    )

  # The worktree is now torn down; staging needs only the in-memory review and
  # anchor map (and the write token). The agent never reached this point.
  if stage is None:
    stage = cfg.mode == AUTO_MODE
  staging_result = None
  if stage:
    staging_result = stage_pending_review(
      repo=repo,
      number=number,
      head_sha=ctx.pr.head_sha,
      review_file=review,
      anchor_map=ctx.anchor_map,
      write_token=write_token,
      username=cfg.username,
    )

  return ReviewResult(
    pr=ctx.pr,
    kind=kind,
    anchors=len(ctx.anchor_map),
    review=review,
    review_file_path=str(persisted),
    staging=staging_result,
  )


def stage_review(cfg, number: int, *, repo: str | None = None) -> StagingResult:
  """Manual-mode phase 2: stage a previously-written review (plan §2).

  Rebuilds the anchor map from the diff PERSISTED in the envelope (the exact
  diff the review was generated against), so a base branch that advanced since
  review cannot shift the anchors and silently drop/relocate comments (F10). No
  agy involved. Legacy envelopes without a persisted diff fall back to
  re-deriving against the stored base ref.
  """
  envelope = read_envelope(cfg.review_file_dir, number)
  repo = repo or envelope["repo"]
  write_token = resolve_token(cfg.orchestrator_github_token_env)
  diff_files_data = envelope.get("diff_files")
  if diff_files_data is not None:
    diff_files = [DiffFile(**d) for d in diff_files_data]
  else:
    # Pre-F10 envelope: re-derive from the base ref (can drift if the base
    # advanced — exactly what persisting the diff now avoids).
    readonly_token = resolve_token(cfg.agent_github_token_env)
    diff_files = GitHub(readonly_token).get_diff_files(
      repo, envelope["base_ref"], envelope["head_sha"]
    )
  anchor_map = build_anchor_map(diff_files)
  review = review_from_dict(envelope["review"])
  return stage_pending_review(
    repo=repo,
    number=number,
    head_sha=envelope["head_sha"],
    review_file=review,
    anchor_map=anchor_map,
    write_token=write_token,
    username=cfg.username,
  )


def _load_review(output_path, agy_stdout):
  """Load the agent's review from its file, or recover it from stdout (F28).

  The agent occasionally prints a valid review to stdout without writing the
  file; rather than fail outright, fall back to parsing stdout. When neither is
  available, the file read raises the clear "no review file" error.
  """
  if pathlib.Path(output_path).is_file():
    return read_review_file(output_path)
  if agy_stdout and agy_stdout.strip():
    _LOG.warning(
      "agent wrote no review file at %s; parsing the review from agy stdout "
      "instead.",
      output_path,
    )
    return parse_review(agy_stdout)
  return read_review_file(output_path)  # raises the clear "no file" error


def _write_envelope(
  review_file_dir, repo, number, head_sha, base_ref, kind, review, diff_files
) -> pathlib.Path:
  """Persist the review hand-off envelope (plan §2): metadata + the review.

  Persists ``diff_files`` (the exact diff the review was generated against) so
  manual staging rebuilds the SAME anchor map rather than re-deriving from a
  moving base branch ref (finding F10).
  """
  dest_dir = pathlib.Path(review_file_dir)
  dest_dir.mkdir(parents=True, exist_ok=True)
  dest = dest_dir / f"{number}.json"
  envelope = {
    "repo": repo,
    "number": number,
    "head_sha": head_sha,
    "base_ref": base_ref,
    "kind": kind,
    "review": review_to_dict(review),
    "diff_files": [dataclasses.asdict(f) for f in diff_files],
  }
  write_text_atomic(dest, json.dumps(envelope, indent=2) + "\n")  # F35
  return dest


def read_envelope(review_file_dir, number: int) -> dict:
  """Read a persisted review envelope, or raise if absent (plan §2)."""
  path = pathlib.Path(review_file_dir) / f"{number}.json"
  if not path.is_file():
    raise FileNotFoundError(
      f"no staged review at {path} — run the review first (manual mode writes "
      "it before you stage)."
    )
  return json.loads(path.read_text())
