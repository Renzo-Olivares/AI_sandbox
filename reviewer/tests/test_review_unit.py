"""Tests for the review hand-off envelope + review<->dict round-trip (§2)."""

import types

import pytest

from pr_review import review_unit
from pr_review.models import DiffFile, ProposedComment, ReviewFile
from pr_review.review_file import review_from_dict, review_to_dict


def test_review_dict_roundtrip_single_and_multiline():
  review = ReviewFile(
    summary="s",
    comments=(
      ProposedComment("a.dart", 10, "RIGHT", "single"),
      ProposedComment(
        "b.dart", 14, "RIGHT", "range", start_line=10, start_side="RIGHT"
      ),
    ),
  )
  back = review_from_dict(review_to_dict(review))
  assert back.summary == "s"
  assert back.comments[0].start_line is None  # single-line stays single
  assert back.comments[1].start_line == 10 and back.comments[1].line == 14


def test_envelope_roundtrip(tmp_path):
  review = ReviewFile(
    summary="hi", comments=(ProposedComment("a", 1, "RIGHT", "b"),)
  )
  diff_files = (
    DiffFile("a.dart", "modified", "@@ -1,1 +1,2 @@\n existing\n+x"),
  )
  path = review_unit._write_envelope(
    tmp_path, "o/r", 7, "sha123", "master", "fresh", review, diff_files
  )
  assert path.name == "7.json"
  envelope = review_unit.read_envelope(tmp_path, 7)
  assert envelope["repo"] == "o/r" and envelope["head_sha"] == "sha123"
  assert envelope["base_ref"] == "master" and envelope["kind"] == "fresh"
  assert envelope["diff_files"][0]["filename"] == "a.dart"  # diff pinned (F10)
  back = review_from_dict(envelope["review"])
  assert back.summary == "hi" and back.comments[0].path == "a"


def test_stage_review_anchors_from_persisted_diff_not_github(
  tmp_path, monkeypatch
):
  # F10: manual staging rebuilds anchors from the diff PERSISTED at review time,
  # never a fresh GitHub fetch that could have drifted as the base advanced.
  review = ReviewFile(summary="s", comments=())
  diff_files = (
    DiffFile("a.dart", "modified", "@@ -1,1 +1,2 @@\n existing\n+y"),
  )
  review_unit._write_envelope(
    tmp_path, "o/r", 7, "sha123", "master", "fresh", review, diff_files
  )

  monkeypatch.setattr(review_unit, "resolve_token", lambda *a, **k: "tok")

  def boom(*a, **k):
    raise AssertionError("must not fetch the diff from GitHub when persisted")

  monkeypatch.setattr(review_unit.GitHub, "get_diff_files", boom)

  captured = {}

  def fake_stage(**kwargs):
    captured["anchor_map"] = kwargs["anchor_map"]
    return "staged"

  monkeypatch.setattr(review_unit, "stage_pending_review", fake_stage)

  cfg = types.SimpleNamespace(
    review_file_dir=tmp_path,
    agent_github_token_env="A",
    orchestrator_github_token_env="B",
    username="me",
  )
  assert review_unit.stage_review(cfg, 7) == "staged"
  assert captured["anchor_map"].is_valid("a.dart", 2, "RIGHT")  # added line


def test_read_envelope_missing_raises(tmp_path):
  with pytest.raises(FileNotFoundError):
    review_unit.read_envelope(tmp_path, 99)


def test_load_review_recovers_from_stdout_when_no_file(tmp_path):
  # F28: a valid review on stdout is recovered when the file wasn't written.
  review = review_unit._load_review(
    tmp_path / "review.json", '{"summary": "from stdout", "comments": []}'
  )
  assert review.summary == "from stdout"


def test_load_review_prefers_file_over_stdout(tmp_path):
  f = tmp_path / "review.json"
  f.write_text('{"summary": "from file", "comments": []}')
  review = review_unit._load_review(
    f, '{"summary": "from stdout", "comments": []}'
  )
  assert review.summary == "from file"


def test_load_review_raises_when_no_file_and_no_stdout(tmp_path):
  from pr_review.review_file import ReviewParseError

  with pytest.raises(ReviewParseError):
    review_unit._load_review(tmp_path / "nope.json", "")
