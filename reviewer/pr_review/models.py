"""Shared data models for the review pipeline (plan §6, §6.1).

These are plain data holders. The deterministic logic that builds an
:class:`AnchorMap` lives in :mod:`pr_review.diff_anchors`; context assembly that
produces a :class:`ReviewContext` lives in :mod:`pr_review.context`.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class PRMeta:
  """Metadata for a single pull request."""

  number: int
  title: str
  url: str
  head_sha: str
  head_ref: str
  base_ref: str
  author: str
  state: str


@dataclasses.dataclass(frozen=True)
class DiffFile:
  """One file in a PR's three-dot diff (plan §6).

  ``patch`` is ``None`` for files the compare API returns without one — binary,
  too-large, or pure-rename files (plan §10) — which yield zero anchors.
  """

  filename: str
  status: str
  patch: str | None = None
  previous_filename: str | None = None


@dataclasses.dataclass(frozen=True)
class PriorReview:
  """A prior review body the user left on the PR (re-review context)."""

  body: str
  state: str
  submitted_at: str | None = None


@dataclasses.dataclass(frozen=True)
class PriorComment:
  """A prior inline review comment by the user (re-review context, plan §6)."""

  path: str
  body: str
  line: int | None = None
  side: str | None = None


@dataclasses.dataclass(frozen=True)
class Anchor:
  """A commentable diff line (plan §6.1).

  ``line`` is the file line number: the new-file number for ``RIGHT`` (added or
  context lines) and the old-file number for ``LEFT`` (deleted lines).
  """

  path: str
  line: int
  side: str  # "RIGHT" or "LEFT"
  content: str


class AnchorMap:
  """The set of valid inline-comment anchors for a PR diff (plan §6.1).

  Built deterministically from the unified diff (no LLM). Used to (a) tell the
  agent which lines it may comment on and (b) validate the agent's proposed
  anchors before the single create-review POST, so an out-of-diff anchor never
  reaches GitHub and triggers the atomic HTTP 422 (plan §6.1, §10).
  """

  def __init__(self, anchors):
    """Initialize from an iterable of :class:`Anchor`."""
    self._anchors = tuple(anchors)
    self._index = frozenset((a.path, a.line, a.side) for a in self._anchors)

  @property
  def anchors(self) -> tuple[Anchor, ...]:
    """All anchors, in diff order."""
    return self._anchors

  def is_valid(self, path: str, line: int, side: str) -> bool:
    """Return whether ``(path, line, side)`` is a commentable diff line."""
    return (path, line, side) in self._index

  def paths(self) -> set[str]:
    """Return the set of file paths that have at least one anchor."""
    return {a.path for a in self._anchors}

  def __len__(self) -> int:
    """Return the number of anchors."""
    return len(self._anchors)


@dataclasses.dataclass(frozen=True)
class ReviewContext:
  """The assembled per-PR bundle handed to the agent (plan §6, §4.5).

  ``kind`` is ``"fresh"`` (Pipeline A style) or ``"rereview"`` (Pipeline B
  style). Prior reviews/comments and the force-push flag are populated only for
  re-reviews.
  """

  pr: PRMeta
  kind: str
  diff_files: tuple[DiffFile, ...]
  anchor_map: AnchorMap
  prior_reviews: tuple[PriorReview, ...] = ()
  prior_comments: tuple[PriorComment, ...] = ()
  force_pushed: bool = False
  # Style-guide conformance (plan §8 rubric). ``style_guide_text`` is the FULL
  # guide from trusted master (None when disabled/not fetched);
  # ``touches_style_guide`` flags a PR whose own diff edits the guide file.
  style_guide_text: str | None = None
  touches_style_guide: bool = False


@dataclasses.dataclass(frozen=True)
class ProposedComment:
  """An inline comment the agent proposes (review-file format, plan §6.1).

  ``line``/``side`` anchor it to a diff line; ``start_line``/``start_side`` are
  populated only for multi-line comments.
  """

  path: str
  line: int
  side: str
  body: str
  start_line: int | None = None
  start_side: str | None = None


@dataclasses.dataclass(frozen=True)
class ReviewFile:
  """The agent's review output: a summary body plus inline comments (plan §2).

  This is the shared hand-off format both modes write and staging reads; the
  agent writes it to a JSON file in its worktree (route (a), plan §4.5).
  """

  summary: str
  comments: tuple[ProposedComment, ...] = ()


@dataclasses.dataclass(frozen=True)
class PRRef:
  """A PR in the review backlog, from a pipeline query (plan §5).

  ``pipeline`` is ``"A"`` (the user is an explicitly-requested reviewer) or
  ``"B"`` (a PR the user reviewed that has since silently updated).
  """

  number: int
  title: str
  url: str
  author: str
  updated_at: str
  pipeline: str
