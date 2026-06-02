"""Tests for the backlog queries + dedupe + label filter (no network)."""

from pr_review import queries
from pr_review.models import PRRef


def _ref(n, pipeline="A"):
  return PRRef(
    number=n,
    title=f"t{n}",
    url="u",
    author="a",
    updated_at="d",
    pipeline=pipeline,
  )


def _raw(n, labels=()):
  return {
    "number": n,
    "title": f"t{n}",
    "url": f"u{n}",
    "author": {"login": "someone"},
    "updatedAt": "2026-01-01T00:00:00Z",
    "labels": [{"name": label} for label in labels],
  }


class FakeGH:
  def __init__(self, a_raw=(), b_raw=(), pending=()):
    self._a = list(a_raw)
    self._b = list(b_raw)
    self._pending = set(pending)
    self.calls = []

  def has_pending_review_by(self, repo, number, username):
    return number in self._pending

  def search_prs(
    self,
    *,
    repo,
    review_requested=None,
    reviewed_by=None,
    state="open",
    limit=200,
  ):
    self.calls.append(
      {"review_requested": review_requested, "reviewed_by": reviewed_by}
    )
    if review_requested:
      return self._a
    if reviewed_by:
      return self._b
    return []


def test_dedupe_b_minus_a_with_a_precedence():
  gh = FakeGH(a_raw=[_raw(1), _raw(2)], b_raw=[_raw(2), _raw(3)])  # 2 in both
  list_a, b_minus_a = queries.assemble_backlog(
    gh, "o/r", "u", ["waiting for response"]
  )
  assert [r.number for r in list_a] == [1, 2]
  assert [r.number for r in b_minus_a] == [3]  # 2 removed (already in A)
  assert all(r.pipeline == "A" for r in list_a)
  assert all(r.pipeline == "B" for r in b_minus_a)


def test_skip_label_filters_out_prs():
  gh = FakeGH(
    a_raw=[_raw(1), _raw(2, labels=["waiting for response"])], b_raw=[]
  )
  list_a, _ = queries.assemble_backlog(gh, "o/r", "u", ["waiting for response"])
  assert [r.number for r in list_a] == [1]  # #2 filtered out (skip label)


def test_pipelines_use_correct_signals():
  gh = FakeGH()
  queries.pipeline_a(gh, "flutter/flutter", "Renzo-Olivares", [])
  queries.pipeline_b(gh, "flutter/flutter", "Renzo-Olivares", [])
  assert gh.calls[0]["review_requested"] == "Renzo-Olivares"
  assert gh.calls[1]["reviewed_by"] == "Renzo-Olivares"


def test_filter_unstaged_drops_already_pending():
  gh = FakeGH(pending={2})  # #2 already has the user's pending review
  to_review, already = queries.filter_unstaged(
    gh, "o/r", [_ref(1), _ref(2), _ref(3)], "u"
  )
  assert [r.number for r in to_review] == [1, 3]  # order preserved
  assert [r.number for r in already] == [2]


def test_filter_unstaged_keeps_all_when_none_pending():
  gh = FakeGH()
  to_review, already = queries.filter_unstaged(
    gh, "o/r", [_ref(1), _ref(2)], "u"
  )
  assert [r.number for r in to_review] == [1, 2]
  assert already == []
