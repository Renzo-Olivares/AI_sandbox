"""The tool's GitHub-write path: create or delete an event-less PENDING review.

This is the only module that writes to GitHub, and it uses the WRITE token
(never the agent's read-only token). The two writes — staging a pending review
(:func:`stage_pending_review`) and clearing one (:func:`unstage_pending_review`)
— are both PENDING-only and never submit. Two invariants are enforced
structurally and are grep-auditable here:

  * **No ``event`` — ever** (plan §1 Layer 3): the payload builder has no event
    parameter, and :func:`assert_no_event` re-checks before the POST. A review
    with no ``event`` stays *pending* (invisible to the author) until the human
    submits it in the GitHub UI.
  * **Every inline anchor is validated against the parsed diff map before the
    single POST** (plan §6.1): GitHub rejects an out-of-diff anchor with HTTP
    422 and fails the *entire* create-review call atomically, so invalid anchors
    are handled up front. A multi-line comment with one valid endpoint is
    *degraded* to a single-line comment on that endpoint (rather than dropped),
    so a finding is not lost just because one end of its range fell outside the
    diff; only genuinely unanchorable comments are dropped.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import subprocess

from pr_review.github import GitHub
from pr_review.models import AnchorMap, ProposedComment, ReviewFile

_LOG = logging.getLogger(__name__)


class StagingError(Exception):
  """A staging write failed (per-PR failure upstream, plan §6.2)."""


class AlreadyStagedError(StagingError):
  """The PR already has a pending review by this account (benign skip, F24).

  Distinct from a real :class:`StagingError` so callers can record it as an
  already-staged SKIP rather than a per-PR FAILURE: it means a prior run (or a
  concurrent one, in the window after the idempotency filter) already staged
  this PR, so there is nothing to do — not that staging went wrong.
  """


@dataclasses.dataclass(frozen=True)
class DroppedComment:
  """An inline comment dropped because its anchor was not in the diff."""

  comment: ProposedComment
  reason: str


@dataclasses.dataclass(frozen=True)
class DegradedComment:
  """A multi-line comment narrowed to single-line (one endpoint off-diff)."""

  comment: ProposedComment
  reason: str


@dataclasses.dataclass(frozen=True)
class StagingResult:
  """The outcome of staging one pending review."""

  review_id: int
  review_url: str
  posted_comments: int
  comment_urls: tuple
  dropped: tuple
  degraded: tuple = ()


def validate_comments(
  review_file: ReviewFile, anchor_map: AnchorMap
) -> tuple[list, list, list]:
  """Resolve each proposed comment against the anchor map (plan §6.1).

  Returns ``(kept, dropped, degraded)``:
    * ``kept`` — comments to post. A multi-line comment with exactly one valid
      endpoint is degraded here to a single-line comment on that endpoint.
    * ``dropped`` — unsalvageable: a single-line comment off the diff, or a
      multi-line comment with BOTH ends off the diff.
    * ``degraded`` — record of comments narrowed from multi-line to single-line.

  Degrade table:
    start ✓ / end ✓  -> keep multi-line
    start ✗ / end ✓  -> single-line on the end
    start ✓ / end ✗  -> single-line on the start
    start ✗ / end ✗  -> drop
  """
  kept = []
  dropped = []
  degraded = []
  for comment in review_file.comments:
    end_valid = anchor_map.is_valid(comment.path, comment.line, comment.side)

    if comment.start_line is None:  # single-line comment
      if end_valid:
        kept.append(comment)
      else:
        dropped.append(
          DroppedComment(
            comment,
            f"anchor not in diff: {comment.path}:{comment.line} {comment.side}",
          )
        )
      continue

    # Multi-line comment: check both endpoints, degrade if exactly one is valid.
    start_side = comment.start_side or comment.side
    start_valid = anchor_map.is_valid(
      comment.path, comment.start_line, start_side
    )
    if end_valid and start_valid:
      # Both endpoints anchor, but GitHub also requires start_line < line on the
      # SAME side for a multi-line comment; an equal/reversed or cross-side
      # range (which an LLM can emit) would 422 the whole atomic create-review
      # POST. Keep multi-line only when the range is well-formed; otherwise
      # degrade to a single-line comment on the end. (Finding F09.)
      if start_side == comment.side and comment.start_line < comment.line:
        kept.append(comment)
      else:
        kept.append(
          dataclasses.replace(comment, start_line=None, start_side=None)
        )
        degraded.append(
          DegradedComment(
            comment,
            f"invalid multi-line range {comment.path} "
            f"{comment.start_line} {start_side} -> {comment.line} "
            f"{comment.side} (GitHub needs start_line<line, same side); "
            f"anchored single-line at end {comment.line} {comment.side}",
          )
        )
    elif end_valid:
      kept.append(
        dataclasses.replace(comment, start_line=None, start_side=None)
      )
      degraded.append(
        DegradedComment(
          comment,
          f"start {comment.path}:{comment.start_line} {start_side} not in "
          f"diff; anchored single-line at end {comment.line} {comment.side}",
        )
      )
    elif start_valid:
      kept.append(
        dataclasses.replace(
          comment,
          line=comment.start_line,
          side=start_side,
          start_line=None,
          start_side=None,
        )
      )
      degraded.append(
        DegradedComment(
          comment,
          f"end {comment.path}:{comment.line} {comment.side} not in diff; "
          f"anchored single-line at start {comment.start_line} {start_side}",
        )
      )
    else:
      dropped.append(
        DroppedComment(
          comment,
          f"neither end of multi-line anchor in diff: {comment.path} "
          f"{comment.start_line}/{comment.line}",
        )
      )
  return kept, dropped, degraded


# GitHub rejects review/comment bodies over 65536 chars with a 422 that fails
# the WHOLE atomic create-review POST. Cap (with a marker) so one verbose LLM
# comment can't sink the entire review (finding F37). Leave headroom for the
# truncation marker.
_MAX_BODY = 65000
_TRUNCATED = "\n\n…[truncated]"


def _cap_body(text: str) -> str:
  if text is None or len(text) <= _MAX_BODY:
    return text or ""
  return text[: _MAX_BODY - len(_TRUNCATED)] + _TRUNCATED


def _comment_payload(comment: ProposedComment) -> dict:
  out = {
    "path": comment.path,
    "line": comment.line,
    "side": comment.side,
    "body": _cap_body(comment.body),
  }
  if comment.start_line is not None:
    out["start_line"] = comment.start_line
    out["start_side"] = comment.start_side or comment.side
  return out


def build_review_payload(summary: str, comments, commit_id: str) -> dict:
  """Build the create-review payload — with NO ``event`` (plan §1 Layer 3).

  Raises:
    StagingError: if the assembled payload would carry an ``event`` key.
  """
  payload = {
    "commit_id": commit_id,
    "body": _cap_body(summary),
    "comments": [_comment_payload(c) for c in comments],
  }
  assert_no_event(payload)
  return payload


def assert_no_event(payload: dict) -> None:
  """Assert the payload never carries an ``event`` (plan §1 Layer 3).

  Submitting (any ``event``) notifies the author; the tool must never do it.

  Raises:
    StagingError: if an ``event`` key is present.
  """
  if "event" in payload:
    raise StagingError(
      "create-review payload must NEVER contain 'event' — that would submit "
      "the review to the author (plan §1 Layer 3)."
    )


def _gh_write(args: list, write_token: str, gh_path: str, *, input_text=None):
  full_env = dict(os.environ)
  full_env["GH_TOKEN"] = write_token
  full_env["GITHUB_TOKEN"] = write_token
  try:
    proc = subprocess.run(
      [gh_path, *args],
      env=full_env,
      input=input_text,
      capture_output=True,
      text=True,
      timeout=60,
      check=False,
    )
  except FileNotFoundError as e:
    raise StagingError(f"'{gh_path}' not found on PATH.") from e
  except subprocess.TimeoutExpired as e:  # F17
    raise StagingError(f"gh {' '.join(args)} timed out after 60s.") from e
  if proc.returncode != 0:
    # Surface BOTH stderr and stdout: on an HTTP error `gh` writes a generic
    # line to stderr ("Unprocessable Entity (HTTP 422)") but puts the actionable
    # API body — e.g. "...only have one pending review per pull request" — on
    # stdout. Relying on stderr alone discarded the detail the 422 backstop in
    # stage_pending_review needs to recognize a duplicate-pending-review.
    detail = " ".join(
      part.strip()
      for part in (proc.stderr, proc.stdout)
      if part and part.strip()
    )
    raise StagingError(
      f"gh {' '.join(args)} failed (exit {proc.returncode}): {detail[:500]}"
    )
  return proc.stdout


def stage_pending_review(
  *,
  repo: str,
  number: int,
  head_sha: str,
  review_file: ReviewFile,
  anchor_map: AnchorMap,
  write_token: str,
  username: str,
  gh_path: str = "gh",
) -> StagingResult:
  """Stage an event-less PENDING review on GitHub (the tool's only write).

  Validates anchors (degrading or dropping as needed), builds the no-``event``
  payload, and POSTs it with the write token. Captures created review/comment
  URLs (plan §6.2).

  Raises:
    AlreadyStagedError: if a pending review by ``username`` already exists — a
      benign skip. Detected by a pre-check, with the duplicate-POST 422 as a
      race backstop (F24).
    StagingError: if the POST fails (e.g. an HTTP 422 we did not anticipate).
  """
  # Robust idempotency: GitHub allows only ONE pending review per PR per user.
  # Pre-checking turns a re-stage into a clean AlreadyStagedError (benign skip)
  # rather than parsing the duplicate POST's 422 (whose detail GitHub buries in
  # a nested body). ``username`` is the user's own login: the account both
  # tokens authenticate as, and the author of the staged pending review. So
  # listing with the write token (an author sees its own pending review) finds
  # it; the 422 backstop below covers a write token from a different account.
  if GitHub(write_token, gh_path=gh_path).has_pending_review_by(
    repo, number, username
  ):
    raise AlreadyStagedError(
      f"PR #{number} already has an unsubmitted pending review by this account "
      "— submit or discard it on GitHub, then re-run (GitHub allows only one "
      "pending review per PR)."
    )
  kept, dropped, degraded = validate_comments(review_file, anchor_map)
  for drop in dropped:
    _LOG.warning("dropped inline comment (%s)", drop.reason)
  for deg in degraded:
    _LOG.info("degraded multi-line comment to single-line (%s)", deg.reason)
  payload = build_review_payload(review_file.summary, kept, head_sha)

  try:
    out = _gh_write(
      [
        "api",
        "--method",
        "POST",
        f"repos/{repo}/pulls/{number}/reviews",
        "--input",
        "-",
      ],
      write_token,
      gh_path,
      input_text=json.dumps(payload),
    )
  except StagingError as error:
    # GitHub allows only one pending review per PR per user; translate the
    # cryptic 422 into an actionable message rather than auto-deleting a
    # review the human may have started editing.
    if "one pending review" in str(error).lower():
      raise AlreadyStagedError(
        f"PR #{number} already has an unsubmitted pending review by this "
        "account — submit or discard it on GitHub, then re-run (GitHub allows "
        "only one pending review per PR)."
      ) from error
    raise
  # The POST already created the pending review as a side effect. If parsing or
  # validating the response now fails, that review is stranded — a re-run would
  # then hit "one pending review". Best-effort delete it, then surface a clear
  # error rather than a confusing failure that leaves a dangling review (F23).
  review = None
  try:
    review = json.loads(out)
    if not isinstance(review, dict):
      raise StagingError(
        f"expected a JSON object from create-review on {repo}#{number}, got "
        f"{type(review).__name__}."
      )
    state = review.get("state")
    if state and state != "PENDING":
      # Defense-in-depth: we never set event, so this must be PENDING.
      raise StagingError(
        f"expected a PENDING review but GitHub returned state={state!r} "
        "(plan §1)."
      )
    review_id = review["id"]
  except (json.JSONDecodeError, KeyError, StagingError) as error:
    recovered_id = review.get("id") if isinstance(review, dict) else None
    if recovered_id:
      with contextlib.suppress(StagingError):
        unstage_pending_review(
          repo=repo,
          number=number,
          review_id=recovered_id,
          write_token=write_token,
          gh_path=gh_path,
        )
    raise StagingError(
      f"staged a pending review on {repo}#{number} but could not process the "
      f"response ({error}); cleaned it up best-effort — if a stray pending "
      "review remains, clear it on GitHub before re-running."
    ) from error
  review_url = review.get("html_url", "")

  comment_urls = _fetch_comment_urls(
    repo, number, review_id, write_token, gh_path
  )
  return StagingResult(
    review_id=review_id,
    review_url=review_url,
    posted_comments=len(kept),
    comment_urls=tuple(comment_urls),
    dropped=tuple(dropped),
    degraded=tuple(degraded),
  )


def _fetch_comment_urls(repo, number, review_id, write_token, gh_path):
  """Best-effort capture of per-comment URLs for the run report (plan §6.2)."""
  try:
    out = _gh_write(
      [
        "api",
        # Capture ALL comment links, not just the first 30 (F36). Do NOT add
        # --slurp: gh applies --jq per page and concatenates, which is what the
        # .[].html_url filter + splitlines parsing below expect; --slurp would
        # wrap pages in an outer array and silently break the filter.
        "--paginate",
        f"repos/{repo}/pulls/{number}/reviews/{review_id}/comments",
        "--jq",
        ".[].html_url",
      ],
      write_token,
      gh_path,
    )
  except StagingError:
    return []
  return [line for line in out.splitlines() if line.strip()]


def unstage_pending_review(
  *,
  repo: str,
  number: int,
  review_id: int,
  write_token: str,
  gh_path: str = "gh",
) -> None:
  """Delete a PENDING review — inverse of staging, using the WRITE token.

  GitHub's delete-review endpoint applies ONLY to pending reviews: it cannot
  delete or un-submit a SUBMITTED review, so this never touches submitted
  feedback. No ``event`` is involved (a delete is not a submit), so the
  never-submit invariant (plan §1 Layer 3) is unaffected.

  Raises:
    StagingError: if the DELETE fails — e.g. the review was submitted in the
      meantime, which GitHub then refuses to delete.
  """
  _gh_write(
    [
      "api",
      "--method",
      "DELETE",
      f"repos/{repo}/pulls/{number}/reviews/{review_id}",
    ],
    write_token,
    gh_path,
  )
