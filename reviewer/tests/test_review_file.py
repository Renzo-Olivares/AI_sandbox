"""Tests for tolerant parsing + strict validation of the agent's review JSON."""

import pytest

from pr_review.review_file import ReviewParseError, parse_review

PLAIN = """
{"summary": "Looks good overall.",
 "comments": [
   {"path": "lib/a.dart", "line": 12, "side": "RIGHT", "body": "nit"}
 ]}
"""


def test_parses_plain_json():
  rf = parse_review(PLAIN)
  assert rf.summary == "Looks good overall."
  assert len(rf.comments) == 1
  c = rf.comments[0]
  assert (c.path, c.line, c.side, c.body) == ("lib/a.dart", 12, "RIGHT", "nit")


def test_parses_json_wrapped_in_markdown_fence():
  text = "Here is my review:\n```json\n" + PLAIN.strip() + "\n```\n"
  rf = parse_review(text)
  assert rf.summary == "Looks good overall."
  assert len(rf.comments) == 1


def test_parses_json_with_prose_preamble():
  text = "Sure! My review is below.\n\n" + PLAIN.strip()
  rf = parse_review(text)
  assert len(rf.comments) == 1


def test_parses_multiline_comment():
  text = """
  {"summary": "s", "comments": [
    {"path": "lib/b.dart", "start_line": 10, "start_side": "RIGHT",
     "line": 14, "side": "RIGHT", "body": "range"}]}
  """
  rf = parse_review(text)
  c = rf.comments[0]
  assert c.start_line == 10 and c.start_side == "RIGHT"
  assert c.line == 14 and c.side == "RIGHT"


def test_tolerates_stringified_int_line():
  text = (
    '{"summary": "s", "comments": '
    '[{"path": "a", "line": "7", "side": "RIGHT", "body": "b"}]}'
  )
  rf = parse_review(text)
  assert rf.comments[0].line == 7


def test_summary_only_review_is_valid():
  rf = parse_review('{"summary": "no inline findings"}')
  assert rf.summary == "no inline findings"
  assert rf.comments == ()


def test_rejects_non_json():
  with pytest.raises(ReviewParseError):
    parse_review("I could not complete the review.")


def test_rejects_bad_side():
  text = (
    '{"summary": "s", "comments": '
    '[{"path": "a", "line": 1, "side": "MIDDLE", "body": "b"}]}'
  )
  with pytest.raises(ReviewParseError, match="side"):
    parse_review(text)


def test_rejects_comment_missing_path():
  text = (
    '{"summary": "s", "comments": [{"line": 1, "side": "RIGHT", "body": "b"}]}'
  )
  with pytest.raises(ReviewParseError, match="path"):
    parse_review(text)


def test_tolerates_whole_number_float_line():  # F26
  text = (
    '{"summary": "s", "comments": '
    '[{"path": "a", "line": 42.0, "side": "RIGHT", "body": "b"}]}'
  )
  assert parse_review(text).comments[0].line == 42


def test_rejects_fractional_float_line():  # F26: a real fraction is invalid
  text = (
    '{"summary": "s", "comments": '
    '[{"path": "a", "line": 42.5, "side": "RIGHT", "body": "b"}]}'
  )
  with pytest.raises(ReviewParseError, match="integer"):
    parse_review(text)


def test_extract_skips_unrelated_brace_object():  # F27
  # An earlier non-review object must not be latched onto; the review wins.
  text = 'prelude {"foo": 1} then: {"summary": "ok", "comments": []}'
  assert parse_review(text).summary == "ok"


def test_extract_skips_invalid_brace_then_finds_review():  # F27
  text = (
    'a Dart map {bad: } and ```json\n{"summary": "good", "comments": []}\n```'
  )
  assert parse_review(text).summary == "good"
