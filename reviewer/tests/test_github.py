"""Tests for the GitHub read client's pure parsing logic (no network)."""

import pytest

from pr_review.github import GitHub, GithubError


def _gh_with_reviews(reviews):
  gh = GitHub("tok")
  gh._api_json = lambda *a, **k: reviews  # stub the network call
  return gh


def test_has_pending_review_by_matches_user_and_state():
  gh = _gh_with_reviews(
    [
      {"user": {"login": "other"}, "state": "PENDING"},
      {"user": {"login": "me"}, "state": "COMMENTED"},
      {"user": {"login": "me"}, "state": "PENDING"},  # the one that matches
    ]
  )
  assert gh.has_pending_review_by("o/r", 1, "me") is True


def test_has_pending_review_by_false_when_only_submitted():
  gh = _gh_with_reviews(
    [
      {"user": {"login": "me"}, "state": "COMMENTED"},
      {"user": {"login": "me"}, "state": "APPROVED"},
    ]
  )
  assert gh.has_pending_review_by("o/r", 1, "me") is False


def test_has_pending_review_by_ignores_other_users_pending():
  gh = _gh_with_reviews([{"user": {"login": "other"}, "state": "PENDING"}])
  assert gh.has_pending_review_by("o/r", 1, "me") is False


def test_has_pending_review_by_handles_null_user_and_empty():
  null_user = _gh_with_reviews([{"user": None, "state": "PENDING"}])
  assert null_user.has_pending_review_by("o/r", 1, "me") is False
  assert _gh_with_reviews([]).has_pending_review_by("o/r", 1, "me") is False


def test_get_pending_review_id_returns_matching_id():
  gh = _gh_with_reviews(
    [
      {"id": 1, "user": {"login": "other"}, "state": "PENDING"},
      {"id": 2, "user": {"login": "me"}, "state": "COMMENTED"},
      {"id": 3, "user": {"login": "me"}, "state": "PENDING"},  # the match
    ]
  )
  assert gh.get_pending_review_id_by("o/r", 1, "me") == 3


def test_get_pending_review_id_none_when_only_submitted():
  gh = _gh_with_reviews(
    [{"id": 2, "user": {"login": "me"}, "state": "APPROVED"}]
  )
  assert gh.get_pending_review_id_by("o/r", 1, "me") is None


def test_get_pending_review_id_ignores_other_users_and_empty():
  other = _gh_with_reviews(
    [{"id": 9, "user": {"login": "other"}, "state": "PENDING"}]
  )
  assert other.get_pending_review_id_by("o/r", 1, "me") is None
  assert _gh_with_reviews([]).get_pending_review_id_by("o/r", 1, "me") is None


# --- Case-insensitive login matching (GitHub logins are case-insensitive; F22)


def test_has_pending_review_by_is_case_insensitive():
  gh = _gh_with_reviews([{"user": {"login": "Hixie"}, "state": "PENDING"}])
  assert gh.has_pending_review_by("o/r", 1, "hixie") is True


def test_get_pending_review_id_is_case_insensitive():
  gh = _gh_with_reviews(
    [{"id": 5, "user": {"login": "HIXIE"}, "state": "PENDING"}]
  )
  assert gh.get_pending_review_id_by("o/r", 1, "hixie") == 5


def test_get_prior_reviews_is_case_insensitive():
  gh = _gh_with_reviews(
    [{"user": {"login": "Renzo-Olivares"}, "state": "APPROVED", "body": "ok"}]
  )
  out = gh.get_prior_reviews("o/r", 1, "renzo-olivares")
  assert len(out) == 1 and out[0].state == "APPROVED"


def test_get_prior_comments_is_case_insensitive():
  gh = GitHub("tok")
  gh._api_json = lambda *a, **k: [
    {"user": {"login": "Me"}, "path": "x.dart", "body": "c", "line": 3}
  ]
  out = gh.get_prior_comments("o/r", 1, "me")
  assert len(out) == 1 and out[0].path == "x.dart"


def test_get_file_text_returns_raw_body_with_raw_accept_header():
  gh = GitHub("tok")
  captured = {}

  def fake_run(args):
    captured["args"] = args
    return "# Style guide\n\nbody"

  gh._run = fake_run  # stub the network call
  out = gh.get_file_text("flutter/flutter", "docs/x.md", "master")
  assert out == "# Style guide\n\nbody"
  args = captured["args"]
  assert args[0] == "api"
  assert args[1] == "repos/flutter/flutter/contents/docs/x.md?ref=master"
  assert "Accept: application/vnd.github.raw" in args


def test_get_pr_meta_raises_on_missing_head_sha():  # F19
  gh = GitHub("tok")
  gh._run = lambda args: '{"number": 5, "title": "t"}'  # no headRefOid
  with pytest.raises(GithubError, match="head SHA"):
    gh.get_pr_meta("o/r", 5)


def test_get_pr_meta_parses_valid_metadata():  # F63 (partial)
  gh = GitHub("tok")
  gh._run = lambda args: (
    '{"number":5,"title":"t","url":"u","headRefOid":"abc",'
    '"headRefName":"f","baseRefName":"master",'
    '"author":{"login":"me"},"state":"OPEN"}'
  )
  pr = gh.get_pr_meta("o/r", 5)
  assert pr.head_sha == "abc" and pr.author == "me" and pr.number == 5


def test_get_diff_files_raises_on_missing_filename():  # F19
  gh = GitHub("tok")
  gh._api_json = lambda *a, **k: {"files": [{"status": "modified"}]}
  with pytest.raises(GithubError, match="filename"):
    gh.get_diff_files("o/r", "base", "sha")


def test_get_file_text_propagates_github_error():
  gh = GitHub("tok")

  def boom(args):
    raise GithubError("HTTP 404: Not Found")

  gh._run = boom
  with pytest.raises(GithubError):
    gh.get_file_text("flutter/flutter", "missing.md", "master")
