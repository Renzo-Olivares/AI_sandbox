"""GitHub read access via the ``gh`` CLI (plan §7).

Every call sets the token explicitly in the child environment — never ambient
``gh auth`` (plan §1 Layer 2). The orchestrator performs these reads with its
READ-ONLY token (least privilege); the tool's single GitHub *write* (event-less
pending-review staging) lives in :mod:`pr_review.staging` and uses the separate
write token. Keeping reads here and the lone write there makes the never-submit
invariant grep-auditable.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

from pr_review.models import DiffFile, PriorComment, PriorReview, PRMeta

_LOG = logging.getLogger(__name__)

# GitHub's compare endpoint returns at most 300 files in one response.
_COMPARE_FILE_CAP = 300


def _login_matches(obj: dict, username: str) -> bool:
  """Case-insensitively match ``obj``'s GitHub login against ``username``.

  GitHub logins are case-insensitive — the API may return ``"Hixie"`` while the
  config has ``"hixie"`` — so a case-sensitive compare would miss the user's own
  reviews/comments, breaking idempotency and prior-review filtering (the run
  would re-review and hit the one-pending-review 422). (Finding F22.)
  """
  login = (obj.get("user") or {}).get("login") or ""
  return login.lower() == (username or "").lower()


class GithubError(Exception):
  """A ``gh``/GitHub read failed (per-PR or fatal error upstream)."""


class GitHub:
  """Thin ``gh``-CLI wrapper for the orchestrator's GitHub reads.

  Holds one token (the read-only token in normal operation) and forces it into
  every ``gh`` invocation's environment so no ambient credential is used.
  """

  def __init__(
    self, token: str, gh_path: str = "gh", timeout: int = 60
  ) -> None:
    """Initialize.

    Args:
      token: the GitHub token to force into every ``gh`` call's environment.
      gh_path: the ``gh`` executable (overridable for tests).
      timeout: per-invocation timeout in seconds.
    """
    self._token = token
    self._gh = gh_path
    self._timeout = timeout

  def _child_env(self) -> dict:
    env = dict(os.environ)
    # Force this exact token; do not let ambient gh auth leak in (Layer 2).
    env["GH_TOKEN"] = self._token
    env["GITHUB_TOKEN"] = self._token
    return env

  def _run(self, args: list) -> str:
    try:
      proc = subprocess.run(
        [self._gh, *args],
        env=self._child_env(),
        capture_output=True,
        text=True,
        timeout=self._timeout,
        check=False,
      )
    except FileNotFoundError as e:
      raise GithubError(f"'{self._gh}' not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
      raise GithubError(
        f"gh timed out after {self._timeout}s: gh {' '.join(args)}"
      ) from e
    if proc.returncode != 0:
      detail = proc.stderr.strip() or proc.stdout.strip()
      raise GithubError(
        f"gh {' '.join(args)} failed (exit {proc.returncode}): {detail}"
      )
    return proc.stdout

  def _api_json(self, endpoint: str, paginate: bool = False, headers=()):
    args = ["api", endpoint]
    for header in headers:
      args += ["-H", header]
    if paginate:
      args.append("--paginate")
    out = self._run(args).strip()
    return json.loads(out) if out else None

  def get_pr_meta(self, repo: str, number: int) -> PRMeta:
    """Fetch PR metadata (number, title, head SHA, base/head refs, author)."""
    out = self._run(
      [
        "pr",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,title,url,headRefOid,headRefName,baseRefName,author,state",
      ]
    )
    data = json.loads(out)
    # headRefOid is required (we pin the review to it). A PR whose head ref was
    # deleted returns it null/absent — surface a clear GithubError rather than a
    # raw KeyError outside the per-PR error taxonomy (finding F19).
    if not isinstance(data, dict) or not data.get("headRefOid"):
      raise GithubError(
        f"PR {repo}#{number}: GitHub returned no head SHA (headRefOid) — the "
        "PR's head ref may have been deleted; cannot review it."
      )
    author = (data.get("author") or {}).get("login", "")
    return PRMeta(
      number=data.get("number", number),
      title=data.get("title", ""),
      url=data.get("url", ""),
      head_sha=data["headRefOid"],
      head_ref=data.get("headRefName", ""),
      base_ref=data.get("baseRefName", ""),
      author=author,
      state=data.get("state", ""),
    )

  def get_diff_files(
    self, repo: str, base: str, head_sha: str
  ) -> list[DiffFile]:
    """Fetch the THREE-DOT diff files (merge-base relative, plan §6).

    Uses ``compare/<base>...<head_sha>`` (three-dot) so the diff is immune to
    rebases/merge churn. Pins to ``head_sha`` for determinism.

    Note: returns at most ~300 files in one response; >300-file diffs need
    pagination, deferred to §10/Phase C. A warning is logged if the cap is hit
    so truncation is never silent.
    """
    data = self._api_json(f"repos/{repo}/compare/{base}...{head_sha}")
    files = (data or {}).get("files", []) or []
    if len(files) >= _COMPARE_FILE_CAP:
      _LOG.warning(
        "compare returned %d files (cap %d) for %s %s...%s; diff may be "
        "truncated (pagination is a §10/Phase-C follow-up).",
        len(files),
        _COMPARE_FILE_CAP,
        repo,
        base,
        head_sha,
      )
    out = []
    for f in files:
      filename = f.get("filename")
      if not filename:  # malformed compare entry → clear error, not KeyError
        raise GithubError(
          f"compare entry for {repo} {base}...{head_sha} is missing a "
          "'filename'; cannot map the diff."
        )
      out.append(
        DiffFile(
          filename=filename,
          status=f.get("status", ""),
          patch=f.get("patch"),  # None for binary/too-large/rename (plan §10)
          previous_filename=f.get("previous_filename"),
        )
      )
    return out

  def get_file_text(self, repo: str, path: str, ref: str) -> str:
    """Fetch a single file's raw text at a ref (read-only token).

    Used to pull the trusted style guide from upstream ``master``. Uses the
    Contents API with the ``raw`` media type, which returns the file body
    verbatim (fine for files under ~1 MB; the style guide is ~90 KB).

    Args:
      repo: ``owner/name`` to read from — the TRUSTED source (e.g.
        ``flutter/flutter``), never the PR's untrusted worktree or a fork's
        master (plan §1 anti-injection).
      path: repo-relative file path.
      ref: branch, tag, or SHA to read at.

    Returns:
      The file's raw text.

    Raises:
      GithubError: if the fetch fails (missing file, bad ref, auth, network).
    """
    return self._run(
      [
        "api",
        f"repos/{repo}/contents/{path}?ref={ref}",
        "-H",
        "Accept: application/vnd.github.raw",
      ]
    )

  def get_prior_reviews(
    self, repo: str, number: int, username: str
  ) -> list[PriorReview]:
    """Fetch the user's SUBMITTED prior reviews on the PR (plan §6).

    Excludes the user's own PENDING review (an unsent draft is not prior
    feedback).
    """
    data = (
      self._api_json(f"repos/{repo}/pulls/{number}/reviews", paginate=True)
      or []
    )
    out = []
    for review in data:
      if not _login_matches(review, username):
        continue
      if review.get("state") == "PENDING":
        continue
      out.append(
        PriorReview(
          body=review.get("body") or "",
          state=review.get("state", ""),
          submitted_at=review.get("submitted_at"),
        )
      )
    return out

  def get_prior_comments(
    self, repo: str, number: int, username: str
  ) -> list[PriorComment]:
    """Fetch the user's prior inline review comments on the PR (plan §6)."""
    data = (
      self._api_json(f"repos/{repo}/pulls/{number}/comments", paginate=True)
      or []
    )
    out = []
    for comment in data:
      if not _login_matches(comment, username):
        continue
      out.append(
        PriorComment(
          path=comment.get("path", ""),
          body=comment.get("body") or "",
          line=comment.get("line"),
          side=comment.get("side"),
        )
      )
    return out

  def was_force_pushed(self, repo: str, number: int) -> bool:
    """Return whether the PR branch was force-pushed (timeline, plan §6)."""
    data = (
      self._api_json(
        f"repos/{repo}/issues/{number}/timeline",
        paginate=True,
        headers=("Accept: application/vnd.github+json",),
      )
      or []
    )
    return any(e.get("event") == "head_ref_force_pushed" for e in data)

  def search_prs(
    self,
    *,
    repo: str,
    review_requested: str | None = None,
    reviewed_by: str | None = None,
    state: str = "open",
    limit: int = 200,
  ) -> list:
    """Search PRs via ``gh search`` FLAGS (plan §5).

    Raw ``repo:`` / ``review-requested:`` qualifiers in a positional query get
    mangled by gh (the whole string becomes one ``repo:"..."`` value → "invalid
    search query"), so we use the dedicated flags. ``labels`` is requested so
    the caller can filter skip-labels (gh has no exclude-specific-label flag).
    """
    args = [
      "search",
      "prs",
      "--repo",
      repo,
      "--state",
      state,
      "--json",
      "number,title,url,updatedAt,author,labels",
      "--limit",
      str(limit),
    ]
    if review_requested:
      args += ["--review-requested", review_requested]
    if reviewed_by:
      args += ["--reviewed-by", reviewed_by]
    out = self._run(args)
    return json.loads(out) if out.strip() else []

  def has_pending_review_by(
    self, repo: str, number: int, username: str
  ) -> bool:
    """Whether the user already has a PENDING review on the PR (plan §4).

    Pending reviews are visible to their author (the user), so re-runs can skip
    PRs already staged this/a prior run — keeping the morning run idempotent.
    """
    data = (
      self._api_json(f"repos/{repo}/pulls/{number}/reviews", paginate=True)
      or []
    )
    return any(
      _login_matches(r, username) and r.get("state") == "PENDING" for r in data
    )

  def get_pending_review_id_by(
    self, repo: str, number: int, username: str
  ) -> int | None:
    """Return the id of the user's PENDING review on the PR, or ``None``.

    The id-returning counterpart to :meth:`has_pending_review_by` (deleting a
    pending review needs its id). Pending reviews are visible to their author,
    so the read-only token suffices. GitHub allows at most one pending review
    per PR per user, so the first match is returned.
    """
    data = (
      self._api_json(f"repos/{repo}/pulls/{number}/reviews", paginate=True)
      or []
    )
    for review in data:
      if _login_matches(review, username) and review.get("state") == "PENDING":
        return review.get("id")
    return None
