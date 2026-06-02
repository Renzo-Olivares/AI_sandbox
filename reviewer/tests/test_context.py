"""Tests for context-assembly branching (fresh vs. re-review). No network."""

import pytest

from pr_review import context
from pr_review.errors import ConfigError
from pr_review.github import GithubError
from pr_review.models import DiffFile, PriorComment, PriorReview, PRMeta

GUIDE_PATH = "docs/contributing/Style-guide-for-Flutter-repo.md"

PR = PRMeta(
  number=1,
  title="t",
  url="u",
  head_sha="deadbeef",
  head_ref="feature",
  base_ref="master",
  author="someone",
  state="OPEN",
)


class FakeGitHub:
  def __init__(
    self,
    *,
    prior_reviews=(),
    prior_comments=(),
    force_pushed=False,
    pr=PR,
    diff_files=None,
    file_text=None,
    file_error=None,
  ):
    self._pr = pr
    self._prior_reviews = list(prior_reviews)
    self._prior_comments = list(prior_comments)
    self._force_pushed = force_pushed
    self._diff_files = diff_files
    self._file_text = file_text
    self._file_error = file_error
    self.calls = []
    self.diff_base = None
    self.file_args = None

  def get_pr_meta(self, repo, number):
    self.calls.append("get_pr_meta")
    return self._pr

  def get_diff_files(self, repo, base, head_sha):
    self.calls.append("get_diff_files")
    self.diff_base = base
    if self._diff_files is not None:
      return list(self._diff_files)
    return [
      DiffFile(
        filename="lib/a.dart",
        status="modified",
        patch="@@ -1,1 +1,2 @@\n a\n+b\n",
      )
    ]

  def get_file_text(self, repo, path, ref):
    self.calls.append("get_file_text")
    self.file_args = (repo, path, ref)
    if self._file_error is not None:
      raise self._file_error
    return self._file_text

  def get_prior_reviews(self, repo, number, username):
    self.calls.append("get_prior_reviews")
    return list(self._prior_reviews)

  def get_prior_comments(self, repo, number, username):
    self.calls.append("get_prior_comments")
    return list(self._prior_comments)

  def was_force_pushed(self, repo, number):
    self.calls.append("was_force_pushed")
    return self._force_pushed


def test_classify_rereview_when_prior_reviews_exist():
  gh = FakeGitHub(prior_reviews=[PriorReview(body="x", state="COMMENTED")])
  assert context.classify_pr(gh, "o/r", 1, "u") == context.REREVIEW


def test_classify_fresh_when_no_prior_reviews():
  gh = FakeGitHub(prior_reviews=[])
  assert context.classify_pr(gh, "o/r", 1, "u") == context.FRESH


def test_fresh_skips_prior_context():
  gh = FakeGitHub(
    prior_reviews=[PriorReview(body="x", state="COMMENTED")],
    prior_comments=[PriorComment(path="lib/a.dart", body="c", line=1)],
    force_pushed=True,
  )
  ctx = context.assemble_context(gh, "o/r", 1, context.FRESH, "u", "master")
  assert ctx.kind == "fresh"
  assert ctx.prior_reviews == ()
  assert ctx.prior_comments == ()
  assert ctx.force_pushed is False
  # fresh must NOT query prior comments / force-push
  assert "get_prior_comments" not in gh.calls
  assert "was_force_pushed" not in gh.calls
  assert len(ctx.anchor_map) >= 1  # anchor map still built from the diff


def test_rereview_populates_prior_context():
  gh = FakeGitHub(
    prior_reviews=[PriorReview(body="x", state="COMMENTED")],
    prior_comments=[PriorComment(path="lib/a.dart", body="c", line=1)],
    force_pushed=True,
  )
  ctx = context.assemble_context(gh, "o/r", 1, context.REREVIEW, "u", "master")
  assert ctx.kind == "rereview"
  assert len(ctx.prior_reviews) == 1
  assert len(ctx.prior_comments) == 1
  assert ctx.force_pushed is True


def test_uses_pr_base_ref_for_compare():
  gh = FakeGitHub()  # PR.base_ref == "master"
  context.assemble_context(gh, "o/r", 1, context.FRESH, "u", "fallback")
  assert gh.diff_base == "master"


def test_falls_back_to_default_branch_when_no_base_ref():
  pr = PRMeta(
    number=1,
    title="t",
    url="u",
    head_sha="deadbeef",
    head_ref="feature",
    base_ref="",
    author="someone",
    state="OPEN",
  )
  gh = FakeGitHub(pr=pr)
  context.assemble_context(gh, "o/r", 1, context.FRESH, "u", "fallback")
  assert gh.diff_base == "fallback"


# --- Style-guide conformance (plan §8 rubric) ---


class _StyleCfg:
  def __init__(
    self,
    *,
    enabled=True,
    repo="flutter/flutter",
    ref="master",
    path=GUIDE_PATH,
  ):
    self.style_guide_enabled = enabled
    self.style_guide_repo = repo
    self.style_guide_ref = ref
    self.style_guide_path = path


def test_fetch_style_guide_disabled_returns_none_without_fetching():
  gh = FakeGitHub(file_text="GUIDE")
  assert context.fetch_style_guide_text(gh, _StyleCfg(enabled=False)) is None
  assert "get_file_text" not in gh.calls  # disabled => no network at all


def test_fetch_style_guide_enabled_returns_text_from_trusted_source():
  gh = FakeGitHub(file_text="GUIDE BODY")
  out = context.fetch_style_guide_text(gh, _StyleCfg())
  assert out == "GUIDE BODY"
  assert gh.file_args == ("flutter/flutter", GUIDE_PATH, "master")


def test_fetch_style_guide_fatal_on_error_with_helpful_message():
  gh = FakeGitHub(file_error=GithubError("HTTP 404: Not Found"))
  with pytest.raises(ConfigError) as exc:
    context.fetch_style_guide_text(gh, _StyleCfg())
  msg = str(exc.value)
  assert "style_guide_enabled" in msg  # the disable hint is offered
  assert "404" in msg  # the exact underlying reason is surfaced


def test_touches_style_guide_true_when_diff_edits_guide():
  gh = FakeGitHub(
    diff_files=[DiffFile(filename=GUIDE_PATH, status="modified", patch="x")]
  )
  ctx = context.assemble_context(
    gh,
    "o/r",
    1,
    context.FRESH,
    "u",
    "master",
    style_guide_text="GUIDE",
    style_guide_path=GUIDE_PATH,
  )
  assert ctx.touches_style_guide is True
  assert ctx.style_guide_text == "GUIDE"


def test_touches_style_guide_detects_a_rename_of_the_guide():
  gh = FakeGitHub(
    diff_files=[
      DiffFile(
        filename="docs/new.md",
        status="renamed",
        previous_filename=GUIDE_PATH,
      )
    ]
  )
  ctx = context.assemble_context(
    gh,
    "o/r",
    1,
    context.FRESH,
    "u",
    "master",
    style_guide_text="GUIDE",
    style_guide_path=GUIDE_PATH,
  )
  assert ctx.touches_style_guide is True


def test_touches_style_guide_false_for_unrelated_diff():
  gh = FakeGitHub()  # default lib/a.dart diff
  ctx = context.assemble_context(
    gh,
    "o/r",
    1,
    context.FRESH,
    "u",
    "master",
    style_guide_text="GUIDE",
    style_guide_path=GUIDE_PATH,
  )
  assert ctx.touches_style_guide is False


def test_touches_style_guide_false_when_lens_disabled():
  # A PR that edits the guide must NOT flip the lens when the lens is off
  # (style_guide_path=None), and no guide text is carried.
  gh = FakeGitHub(
    diff_files=[DiffFile(filename=GUIDE_PATH, status="modified", patch="x")]
  )
  ctx = context.assemble_context(
    gh,
    "o/r",
    1,
    context.FRESH,
    "u",
    "master",
    style_guide_text=None,
    style_guide_path=None,
  )
  assert ctx.touches_style_guide is False
  assert ctx.style_guide_text is None
