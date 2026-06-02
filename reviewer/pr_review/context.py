"""Per-PR context assembly: fresh vs. re-review (plan §6, §4.4).

Single-PR mode skips the Pipeline A/B queries, so it must still decide whether
to treat a PR as a fresh review (review the PR as-is) or a re-review (assemble
prior review comments + three-dot diff + force-push flag, with the "does the
current state address my prior feedback?" framing). The kind is either
auto-detected (:func:`classify_pr`) or forced by the CLI ``--as`` flag (§4.4).
"""

from __future__ import annotations

from pr_review.diff_anchors import build_anchor_map
from pr_review.errors import ConfigError
from pr_review.github import GitHub, GithubError
from pr_review.models import ReviewContext

FRESH = "fresh"
REREVIEW = "rereview"


def fetch_style_guide_text(gh: GitHub, cfg) -> str | None:
  """Fetch the trusted Flutter style guide ONCE per run (plan §8 rubric).

  Sourced from ``cfg.style_guide_repo@cfg.style_guide_ref`` — the TRUSTED
  upstream guide, NEVER the PR's untrusted worktree or a fork's master (a
  doctored guide would be prompt injection, plan §1). Returns ``None`` when the
  style-guide lens is disabled.

  Raises:
    ConfigError: if the lens is enabled but the guide cannot be fetched. Fatal
      (plan §6.2) so a misconfigured/unreachable source fails fast and loud,
      rather than every review silently dropping the style lens. The message
      names the exact failure reason and the disable knob.
  """
  if not cfg.style_guide_enabled:
    return None
  try:
    return gh.get_file_text(
      cfg.style_guide_repo, cfg.style_guide_path, cfg.style_guide_ref
    )
  except GithubError as error:
    raise ConfigError(
      "Failed to fetch the Flutter style guide from "
      f"{cfg.style_guide_repo}@{cfg.style_guide_ref}:{cfg.style_guide_path} "
      f"(reason: {error}). Style-guide conformance is enabled but the guide "
      "could not be retrieved — fix the source (style_guide_repo / "
      "style_guide_ref / style_guide_path) or set 'style_guide_enabled: false' "
      "in the config to run without the style lens, then re-run."
    ) from error


def classify_pr(gh: GitHub, repo: str, number: int, username: str) -> str:
  """Auto-detect fresh vs. re-review via the reviewed-by signal (plan §4.4).

  If the user has any submitted review on the PR, treat it as a re-review;
  otherwise fresh.
  """
  return REREVIEW if gh.get_prior_reviews(repo, number, username) else FRESH


def assemble_context(
  gh: GitHub,
  repo: str,
  number: int,
  kind: str,
  username: str,
  default_branch: str,
  *,
  style_guide_text: str | None = None,
  style_guide_path: str | None = None,
) -> ReviewContext:
  """Assemble the per-PR review bundle (plan §6).

  Args:
    gh: the GitHub read client (read-only token).
    repo: ``owner/name``.
    number: the PR number.
    kind: ``"fresh"`` or ``"rereview"`` (already resolved).
    username: the reviewing user's login (for prior-review filtering).
    default_branch: fallback compare base if the PR has no base ref.
    style_guide_text: the trusted style guide (from a run-level fetch), or
      ``None`` when the lens is disabled/not fetched (plan §8 rubric).
    style_guide_path: the repo-relative guide path used to flag a PR whose own
      diff edits the guide; ``None`` when the lens is disabled. (The flag is
      computed from the diff, independent of whether the fetch succeeded.)

  Returns:
    The assembled :class:`~pr_review.models.ReviewContext`.
  """
  pr = gh.get_pr_meta(repo, number)
  base = pr.base_ref or default_branch
  diff_files = gh.get_diff_files(repo, base, pr.head_sha)
  anchor_map = build_anchor_map(diff_files)
  touches_style_guide = bool(style_guide_path) and any(
    f.filename == style_guide_path or f.previous_filename == style_guide_path
    for f in diff_files
  )

  prior_reviews = ()
  prior_comments = ()
  force_pushed = False
  if kind == REREVIEW:
    prior_reviews = tuple(gh.get_prior_reviews(repo, number, username))
    prior_comments = tuple(gh.get_prior_comments(repo, number, username))
    force_pushed = gh.was_force_pushed(repo, number)

  return ReviewContext(
    pr=pr,
    kind=kind,
    diff_files=tuple(diff_files),
    anchor_map=anchor_map,
    prior_reviews=prior_reviews,
    prior_comments=prior_comments,
    force_pushed=force_pushed,
    style_guide_text=style_guide_text,
    touches_style_guide=touches_style_guide,
  )
