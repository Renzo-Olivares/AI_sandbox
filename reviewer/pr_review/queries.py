"""Backlog queries — Pipelines A and B with dedupe (plan §5, §4).

Pipeline A: PRs where the user is an explicitly-requested reviewer.
Pipeline B: PRs the user has reviewed that are unlabeled — the
``waiting for response`` label is removed by a repo workflow whenever the
author pushes or comments, so label-absence means the author has acted since
the user's review (a re-review candidate).

Both queries use ``gh search`` flags (raw query qualifiers get mangled by gh)
and filter out the configured skip labels in Python (gh has no
exclude-a-specific-label flag). Dedupe is B-minus-A, A precedence (§4).
"""

from __future__ import annotations

from pr_review.github import GitHub
from pr_review.models import PRRef


def _has_skip_label(raw: dict, skip_labels) -> bool:
  names = {label.get("name", "") for label in (raw.get("labels") or [])}
  return bool(names & set(skip_labels))


def _to_ref(raw: dict, pipeline: str) -> PRRef:
  return PRRef(
    number=raw["number"],
    title=raw.get("title", ""),
    url=raw.get("url", ""),
    author=(raw.get("author") or {}).get("login", ""),
    updated_at=raw.get("updatedAt", ""),
    pipeline=pipeline,
  )


def pipeline_a(gh: GitHub, repo: str, username: str, labels) -> list:
  """PRs where the user is an explicitly-requested reviewer (plan §5)."""
  raw = gh.search_prs(repo=repo, review_requested=username)
  return [_to_ref(r, "A") for r in raw if not _has_skip_label(r, labels)]


def pipeline_b(gh: GitHub, repo: str, username: str, labels) -> list:
  """PRs the user reviewed that are unlabeled — silent updates (plan §5)."""
  raw = gh.search_prs(repo=repo, reviewed_by=username)
  return [_to_ref(r, "B") for r in raw if not _has_skip_label(r, labels)]


def assemble_backlog(gh: GitHub, repo: str, username: str, labels):
  """Snapshot both pipelines and dedupe B-minus-A, A precedence (plan §4).

  Returns ``(list_a, b_minus_a)``; the ordered backlog is ``list_a +
  b_minus_a``. Snapshotting both before any staging is what makes the run
  immune to the double-handling race (§4).
  """
  list_a = pipeline_a(gh, repo, username, labels)
  list_b = pipeline_b(gh, repo, username, labels)
  a_numbers = {ref.number for ref in list_a}
  b_minus_a = [ref for ref in list_b if ref.number not in a_numbers]
  return list_a, b_minus_a


def filter_unstaged(gh: GitHub, repo: str, backlog, username: str):
  """Drop PRs the user already has a PENDING review on (idempotent re-runs).

  Returns ``(to_review, already_staged)`` — so a re-run picks up where the last
  one left off and never re-reviews (or fails to re-stage) a PR that already has
  a pending review awaiting the human's submit (plan §4).
  """
  to_review = []
  already_staged = []
  for ref in backlog:
    if gh.has_pending_review_by(repo, ref.number, username):
      already_staged.append(ref)
    else:
      to_review.append(ref)
  return to_review, already_staged
