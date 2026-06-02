"""Tests for the batch fan-out: limit, isolation, ordering (no network)."""

import pytest

from pr_review import batch
from pr_review.errors import PreflightError
from pr_review.models import PRRef


def _refs(*specs):
  return [
    PRRef(
      number=n,
      title=f"t{n}",
      url="u",
      author="a",
      updated_at="d",
      pipeline=p,
    )
    for n, p in specs
  ]


class FakeCfg:
  agent_github_token_env = "GH_TOKEN_READONLY"
  username = "u"
  labels_to_skip = ()
  batch_size = 5
  mode = "auto-stage-review"
  base_clone_dir = "/tmp/base"
  worktree_dir = "/tmp/wts"


def _patch_common(monkeypatch):
  # run_backlog no longer queries — the caller hands in the backlog. It still
  # runs the Layer-2 probe + style-guide fetch + base-clone fetch, stubbed here.
  monkeypatch.setattr(batch, "resolve_token", lambda *a, **k: "tok")
  monkeypatch.setattr(batch.worktree, "ensure_base_clone", lambda *a, **k: None)
  monkeypatch.setattr(
    batch.preflight, "verify_read_only_token", lambda *a, **k: None
  )
  monkeypatch.setattr(
    batch.context, "fetch_style_guide_text", lambda *a, **k: None
  )


def test_limit_caps_processed_and_records_skip(monkeypatch):
  _patch_common(monkeypatch)
  monkeypatch.setattr(
    batch.review_unit, "review_single_pr", lambda *a, **k: f"r{a[2]}"
  )
  backlog = _refs((1, "A"), (2, "A"), (3, "B"))
  run = batch.run_backlog(FakeCfg(), "o/r", backlog, limit=2)
  assert run.total == 3 and run.skipped_for_limit == 1
  assert run.backlog is backlog  # the full candidate list is preserved
  assert [o.ref.number for o in run.outcomes] == [1, 2]  # first 2, in order


def test_per_pr_failure_is_isolated(monkeypatch):
  _patch_common(monkeypatch)

  def fake_review(cfg, repo, number, **k):
    if number == 2:
      raise RuntimeError("boom on 2")
    return f"ok-{number}"

  monkeypatch.setattr(batch.review_unit, "review_single_pr", fake_review)
  run = batch.run_backlog(FakeCfg(), "o/r", _refs((1, "A"), (2, "A"), (3, "B")))
  by_num = {o.ref.number: o for o in run.outcomes}
  assert by_num[1].result == "ok-1" and by_num[1].failure is None
  assert by_num[3].result == "ok-3"  # run continued past the failure
  assert by_num[2].result is None and "boom on 2" in by_num[2].failure


def test_per_pr_failure_logs_debug_traceback(monkeypatch, caplog):
  # F46: the friendly per-PR failure string is kept, plus the full traceback at
  # DEBUG for diagnosing a real bug behind it.
  _patch_common(monkeypatch)

  def fake_review(cfg, repo, number, **k):
    raise RuntimeError("bug in review internals")

  monkeypatch.setattr(batch.review_unit, "review_single_pr", fake_review)
  with caplog.at_level("DEBUG", logger="pr_review.batch"):
    batch.run_backlog(FakeCfg(), "o/r", _refs((1, "A")))
  assert any(r.levelname == "DEBUG" and r.exc_info for r in caplog.records)


def test_pipeline_maps_to_review_kind(monkeypatch):
  _patch_common(monkeypatch)
  seen = {}

  def fake_review(cfg, repo, number, *, kind=None, **k):
    seen[number] = kind
    return number

  monkeypatch.setattr(batch.review_unit, "review_single_pr", fake_review)
  backlog = _refs((1, "A"), (2, "B"), (9, "explicit"))
  batch.run_backlog(FakeCfg(), "o/r", backlog)
  assert seen[1] == "fresh"  # Pipeline A
  assert seen[2] == "rereview"  # Pipeline B
  assert seen[9] is None  # explicit --prs list => auto-detect fresh/rereview


def test_already_staged_recorded_as_skip_not_failure(monkeypatch):
  _patch_common(monkeypatch)

  def fake_review(cfg, repo, number, **k):
    if number == 2:
      raise batch.staging.AlreadyStagedError("already staged")
    return f"ok-{number}"

  monkeypatch.setattr(batch.review_unit, "review_single_pr", fake_review)
  run = batch.run_backlog(FakeCfg(), "o/r", _refs((1, "A"), (2, "A"), (3, "B")))
  by_num = {o.ref.number: o for o in run.outcomes}
  assert by_num[2].result is None and by_num[2].failure is None
  assert by_num[2].skipped == "already staged"  # benign skip, not a failure
  assert by_num[1].result == "ok-1" and by_num[3].result == "ok-3"


def test_rejects_nonpositive_limit():
  for bad in (0, -1):
    with pytest.raises(PreflightError, match="limit"):
      batch.run_backlog(FakeCfg(), "o/r", _refs((1, "A")), limit=bad)


def test_empty_backlog_runs_no_probe(monkeypatch):
  # With nothing to process, the probe/fetch must not run (no first PR).
  monkeypatch.setattr(batch, "resolve_token", lambda *a, **k: "tok")

  def boom(*a, **k):
    raise AssertionError("must not probe on an empty backlog")

  monkeypatch.setattr(batch.preflight, "verify_read_only_token", boom)
  monkeypatch.setattr(batch.worktree, "ensure_base_clone", boom)
  run = batch.run_backlog(FakeCfg(), "o/r", [])
  assert run.total == 0 and run.outcomes == []
