"""Tests for the single write path: anchor validation, degrade, no-event."""

import pytest

from pr_review.models import Anchor, AnchorMap, ProposedComment, ReviewFile
from pr_review.staging import (
  StagingError,
  assert_no_event,
  build_review_payload,
  validate_comments,
)

# A small map: a.dart:10 RIGHT, a.dart:20 LEFT, b.dart:3 RIGHT, b.dart:5 RIGHT.
AMAP = AnchorMap(
  [
    Anchor("a.dart", 10, "RIGHT", "x"),
    Anchor("a.dart", 20, "LEFT", "y"),
    Anchor("b.dart", 3, "RIGHT", "p"),
    Anchor("b.dart", 5, "RIGHT", "q"),
  ]
)


def _review(*comments):
  return ReviewFile(summary="s", comments=tuple(comments))


class _FakeGH:
  """Stand-in for github.GitHub used by stage_pending_review's pre-check."""

  def __init__(self, pending):
    self._pending = pending

  def has_pending_review_by(self, repo, number, username):
    return self._pending


@pytest.fixture(autouse=True)
def _no_pending_by_default(monkeypatch):
  # stage_pending_review now pre-checks for an existing pending review by
  # constructing GitHub(write_token). Default it to "none" so the POST-path
  # tests proceed; the pre-check test below overrides it to True.
  from pr_review import staging as staging_mod

  monkeypatch.setattr(staging_mod, "GitHub", lambda *a, **k: _FakeGH(False))


def test_stage_pre_check_skips_when_already_staged(monkeypatch):
  # Robust idempotency: a pre-existing pending review is detected up front and
  # raised as AlreadyStagedError WITHOUT attempting the POST (F24).
  from pr_review import staging as staging_mod

  posted = []
  monkeypatch.setattr(staging_mod, "GitHub", lambda *a, **k: _FakeGH(True))
  monkeypatch.setattr(
    staging_mod, "_gh_write", lambda *a, **k: posted.append(a) or ""
  )
  with pytest.raises(staging_mod.AlreadyStagedError, match="already has"):
    staging_mod.stage_pending_review(
      repo="o/r",
      number=1,
      head_sha="sha",
      review_file=_review(ProposedComment("a.dart", 10, "RIGHT", "c")),
      anchor_map=AMAP,
      write_token="t",
      username="me",
    )
  assert posted == []  # pre-check short-circuited; no POST attempted


def test_gh_write_surfaces_response_body_not_just_stderr(monkeypatch):
  # The duplicate-pending-review 422 detail lives in gh's STDOUT body, not its
  # generic stderr line; _gh_write must surface it so the 422 backstop matches.
  from pr_review import staging as staging_mod

  class _Proc:
    returncode = 1
    stderr = "gh: Unprocessable Entity (HTTP 422)"
    stdout = (
      '{"errors":[{"message":"User can only have one pending review per '
      'pull request."}]}'
    )

  monkeypatch.setattr(staging_mod.subprocess, "run", lambda *a, **k: _Proc())
  with pytest.raises(staging_mod.StagingError, match="one pending review"):
    staging_mod._gh_write(["api", "x"], "tok", "gh")


def test_single_line_keeps_valid_and_drops_out_of_diff():
  kept, dropped, degraded = validate_comments(
    _review(
      ProposedComment("a.dart", 10, "RIGHT", "valid"),
      ProposedComment("a.dart", 99, "RIGHT", "off-diff line"),
      ProposedComment("a.dart", 10, "LEFT", "wrong side"),
      ProposedComment("zzz.dart", 10, "RIGHT", "wrong file"),
    ),
    AMAP,
  )
  assert [c.body for c in kept] == ["valid"]
  assert len(dropped) == 3
  assert degraded == []


def test_left_side_anchor_kept():
  kept, dropped, degraded = validate_comments(
    _review(ProposedComment("a.dart", 20, "LEFT", "deleted line")), AMAP
  )
  assert len(kept) == 1 and not dropped and not degraded


def test_multiline_both_valid_kept_as_multiline():
  kept, dropped, degraded = validate_comments(
    _review(
      ProposedComment(
        "b.dart", 5, "RIGHT", "range", start_line=3, start_side="RIGHT"
      )
    ),
    AMAP,
  )
  assert len(kept) == 1 and not dropped and not degraded
  assert kept[0].start_line == 3  # preserved as multi-line


def test_multiline_invalid_start_degrades_to_end():
  kept, dropped, degraded = validate_comments(
    _review(
      ProposedComment(
        "b.dart", 5, "RIGHT", "range", start_line=99, start_side="RIGHT"
      )
    ),
    AMAP,
  )
  assert not dropped and len(degraded) == 1
  assert len(kept) == 1
  assert kept[0].line == 5 and kept[0].start_line is None  # single-line on end


def test_multiline_invalid_end_degrades_to_start():
  kept, dropped, degraded = validate_comments(
    _review(
      ProposedComment(
        "b.dart", 99, "RIGHT", "range", start_line=3, start_side="RIGHT"
      )
    ),
    AMAP,
  )
  assert not dropped and len(degraded) == 1
  assert len(kept) == 1
  assert (
    kept[0].line == 3 and kept[0].start_line is None
  )  # single-line on start


def test_multiline_both_invalid_dropped():
  kept, dropped, degraded = validate_comments(
    _review(
      ProposedComment(
        "b.dart", 99, "RIGHT", "range", start_line=98, start_side="RIGHT"
      )
    ),
    AMAP,
  )
  assert not kept and not degraded and len(dropped) == 1
  assert "neither end" in dropped[0].reason


def test_multiline_equal_endpoints_degrades_to_single_line():
  # start_line == line is invalid for a GitHub multi-line comment (would 422 the
  # whole POST); both ends anchor, so degrade to single-line on the end (F09).
  kept, dropped, degraded = validate_comments(
    _review(
      ProposedComment(
        "b.dart", 5, "RIGHT", "range", start_line=5, start_side="RIGHT"
      )
    ),
    AMAP,
  )
  assert not dropped and len(degraded) == 1
  assert len(kept) == 1 and kept[0].start_line is None and kept[0].line == 5


def test_multiline_reversed_range_degrades_to_end():
  # start_line > line (both valid) is invalid; degrade to single-line on end.
  kept, dropped, degraded = validate_comments(
    _review(
      ProposedComment(
        "b.dart", 3, "RIGHT", "range", start_line=5, start_side="RIGHT"
      )
    ),
    AMAP,
  )
  assert not dropped and len(degraded) == 1
  assert len(kept) == 1 and kept[0].start_line is None and kept[0].line == 3


def test_multiline_cross_side_degrades_to_end():
  # Both endpoints valid but on different sides → invalid multi-line; degrade.
  kept, dropped, degraded = validate_comments(
    _review(
      ProposedComment(
        "a.dart", 10, "RIGHT", "range", start_line=20, start_side="LEFT"
      )
    ),
    AMAP,
  )
  assert not dropped and len(degraded) == 1
  assert len(kept) == 1 and kept[0].start_line is None
  assert kept[0].line == 10 and kept[0].side == "RIGHT"


def test_build_payload_has_no_event_and_maps_comments():
  payload = build_review_payload(
    "summary text",
    [ProposedComment("a.dart", 10, "RIGHT", "c")],
    "deadbeefsha",
  )
  assert "event" not in payload  # never submit (plan §1 Layer 3)
  assert payload["commit_id"] == "deadbeefsha"
  assert payload["body"] == "summary text"
  assert payload["comments"][0] == {
    "path": "a.dart",
    "line": 10,
    "side": "RIGHT",
    "body": "c",
  }


def test_long_body_and_summary_are_capped():  # F37
  long = "x" * 70000
  payload = build_review_payload(
    long, [ProposedComment("a.dart", 1, "RIGHT", long)], "sha"
  )
  assert len(payload["body"]) <= 65000
  assert payload["body"].endswith("…[truncated]")
  assert len(payload["comments"][0]["body"]) <= 65000
  assert payload["comments"][0]["body"].endswith("…[truncated]")


def test_short_body_and_summary_untouched():  # F37
  payload = build_review_payload(
    "ok", [ProposedComment("a.dart", 1, "RIGHT", "fine")], "sha"
  )
  assert payload["body"] == "ok"
  assert payload["comments"][0]["body"] == "fine"


def test_fetch_comment_urls_paginates(monkeypatch):  # F36
  from pr_review import staging as staging_mod

  captured = {}

  def fake_gh_write(args, write_token, gh_path, input_text=None):
    captured["args"] = args
    return "https://x/1\nhttps://x/2\n"

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  urls = staging_mod._fetch_comment_urls("o/r", 1, 5, "tok", "gh")
  assert "--paginate" in captured["args"]
  assert urls == ["https://x/1", "https://x/2"]


def test_build_payload_includes_multiline_fields():
  payload = build_review_payload(
    "s",
    [
      ProposedComment(
        "b.dart", 5, "RIGHT", "range", start_line=3, start_side="RIGHT"
      )
    ],
    "sha",
  )
  c = payload["comments"][0]
  assert c["start_line"] == 3 and c["start_side"] == "RIGHT"


def test_assert_no_event_raises_when_event_present():
  with pytest.raises(StagingError, match="event"):
    assert_no_event({"body": "x", "event": "COMMENT"})


def test_existing_pending_review_gives_clear_error(monkeypatch):
  from pr_review import staging as staging_mod

  def fake_gh_write(*args, **kwargs):
    raise staging_mod.StagingError(
      "gh ... failed: User can only have one pending review per pull request"
    )

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  review = ReviewFile(
    summary="s", comments=(ProposedComment("a.dart", 10, "RIGHT", "c"),)
  )
  # Raised as AlreadyStagedError (a StagingError subclass) so callers can treat
  # it as a benign already-staged SKIP rather than a failure (F24).
  with pytest.raises(staging_mod.AlreadyStagedError, match="already has"):
    staging_mod.stage_pending_review(
      repo="o/r",
      number=1,
      head_sha="sha",
      review_file=review,
      anchor_map=AMAP,
      write_token="t",
      username="me",
    )


def test_unstage_pending_review_issues_delete(monkeypatch):
  from pr_review import staging as staging_mod

  calls = {}

  def fake_gh_write(args, write_token, gh_path, input_text=None):
    calls["args"] = args
    calls["token"] = write_token
    return ""

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  staging_mod.unstage_pending_review(
    repo="o/r", number=7, review_id=123, write_token="wtok"
  )
  assert calls["token"] == "wtok"
  assert calls["args"] == [
    "api",
    "--method",
    "DELETE",
    "repos/o/r/pulls/7/reviews/123",
  ]


def test_gh_write_timeout_raises_staging_error(monkeypatch):  # F17
  import subprocess

  from pr_review import staging as staging_mod

  def fake_run(*a, **k):
    raise subprocess.TimeoutExpired(a[0], 60)

  monkeypatch.setattr(staging_mod.subprocess, "run", fake_run)
  with pytest.raises(staging_mod.StagingError, match="timed out"):
    staging_mod._gh_write(["api", "x"], "tok", "gh")


def test_stage_happy_path_returns_result(monkeypatch):  # F62 (partial)
  from pr_review import staging as staging_mod

  def fake_gh_write(args, write_token, gh_path, input_text=None):
    if "POST" in args:
      return '{"id": 99, "state": "PENDING", "html_url": "https://x/99"}'
    return "https://x/99#c1\n"  # _fetch_comment_urls

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  review = ReviewFile(
    summary="s", comments=(ProposedComment("a.dart", 10, "RIGHT", "c"),)
  )
  result = staging_mod.stage_pending_review(
    repo="o/r",
    number=1,
    head_sha="sha",
    review_file=review,
    anchor_map=AMAP,
    write_token="t",
    username="me",
  )
  assert result.review_id == 99 and result.review_url == "https://x/99"
  assert result.posted_comments == 1
  assert result.comment_urls == ("https://x/99#c1",)


def test_stage_cleans_up_stray_review_on_post_post_error(monkeypatch):  # F23
  from pr_review import staging as staging_mod

  calls = []

  def fake_gh_write(args, write_token, gh_path, input_text=None):
    calls.append(args)
    if "POST" in args:
      return '{"id": 555, "state": "APPROVED"}'  # not PENDING → triggers F23
    return ""  # the cleanup DELETE

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  review = ReviewFile(summary="s", comments=())
  with pytest.raises(staging_mod.StagingError, match="clear it on GitHub"):
    staging_mod.stage_pending_review(
      repo="o/r",
      number=1,
      head_sha="sha",
      review_file=review,
      anchor_map=AMAP,
      write_token="t",
      username="me",
    )
  # the stray review (555) was deleted best-effort before re-raising
  assert any("DELETE" in c and "555" in " ".join(c) for c in calls)


def test_stage_non_dict_response_raises_clear_error(monkeypatch):  # F23
  from pr_review import staging as staging_mod

  def fake_gh_write(args, write_token, gh_path, input_text=None):
    return "[]" if "POST" in args else ""  # valid JSON, but not an object

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  with pytest.raises(staging_mod.StagingError, match="clear it on GitHub"):
    staging_mod.stage_pending_review(
      repo="o/r",
      number=1,
      head_sha="sha",
      review_file=ReviewFile(summary="s", comments=()),
      anchor_map=AMAP,
      write_token="t",
      username="me",
    )


def test_stage_dict_without_id_raises_clear_error(monkeypatch):  # F23
  from pr_review import staging as staging_mod

  def fake_gh_write(args, write_token, gh_path, input_text=None):
    return '{"state": "PENDING"}' if "POST" in args else ""  # PENDING but no id

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  with pytest.raises(staging_mod.StagingError, match="clear it on GitHub"):
    staging_mod.stage_pending_review(
      repo="o/r",
      number=1,
      head_sha="sha",
      review_file=ReviewFile(summary="s", comments=()),
      anchor_map=AMAP,
      write_token="t",
      username="me",
    )


def test_stage_cleanup_delete_failure_is_suppressed(monkeypatch):  # F23
  from pr_review import staging as staging_mod

  def fake_gh_write(args, write_token, gh_path, input_text=None):
    if "POST" in args:
      return '{"id": 7, "state": "APPROVED"}'  # non-PENDING with id → cleanup
    raise staging_mod.StagingError("delete failed")  # the DELETE itself fails

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  # the suppressed delete failure must NOT replace the actionable error
  with pytest.raises(staging_mod.StagingError, match="clear it on GitHub"):
    staging_mod.stage_pending_review(
      repo="o/r",
      number=1,
      head_sha="sha",
      review_file=ReviewFile(summary="s", comments=()),
      anchor_map=AMAP,
      write_token="t",
      username="me",
    )


def test_unstage_pending_review_propagates_error(monkeypatch):
  from pr_review import staging as staging_mod

  def fake_gh_write(*args, **kwargs):
    raise staging_mod.StagingError("Can not delete a submitted review")

  monkeypatch.setattr(staging_mod, "_gh_write", fake_gh_write)
  with pytest.raises(staging_mod.StagingError, match="submitted"):
    staging_mod.unstage_pending_review(
      repo="o/r", number=7, review_id=123, write_token="wtok"
    )
