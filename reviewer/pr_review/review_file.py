"""Parse and validate the agent's JSON review file (plan §4.5, §6.1).

The agent writes its review as JSON (route (a)). A frontier model emitting JSON
is not in doubt, but the output may be freeform-adjacent — wrapped in markdown
fences or preceded by a prose preamble — so we parse tolerantly and then
validate strictly. Any malformed/non-conforming output is a per-PR failure
(plan §6.2), never a crash.

Expected schema::

    {
      "summary": "<review summary text>",
      "comments": [
        {"path": "lib/foo.dart", "line": 42, "side": "RIGHT", "body": "..."},
        {"path": "lib/bar.dart", "start_line": 10, "start_side": "RIGHT",
         "line": 14, "side": "RIGHT", "body": "<multi-line comment>"}
      ]
    }
"""

from __future__ import annotations

import json
import pathlib

from pr_review.models import ProposedComment, ReviewFile

_VALID_SIDES = ("LEFT", "RIGHT")


class ReviewParseError(Exception):
  """The agent's review file is missing or malformed (per-PR failure)."""


def _extract_json(text: str) -> str:
  """Best-effort extraction of the review JSON object from noisy agent output.

  Tolerates a prose preamble and/or a ```json fence: first try the whole output,
  then scan for the first brace-balanced object that LOOKS like a review (has a
  ``summary`` or ``comments`` key). Requiring those keys avoids latching onto an
  unrelated ``{...}`` — a Dart map literal, braces in prose, or a second JSON
  blob — which the old outermost-``{``…``}`` span would over-capture (F27).
  """
  stripped = text.strip()
  try:
    if isinstance(json.loads(stripped), dict):
      return stripped  # the whole output is a JSON object — unambiguous
  except json.JSONDecodeError:
    pass
  decoder = json.JSONDecoder()
  for i, ch in enumerate(stripped):
    if ch != "{":
      continue
    try:
      obj, _end = decoder.raw_decode(stripped, i)
    except json.JSONDecodeError:
      continue
    if isinstance(obj, dict) and ("summary" in obj or "comments" in obj):
      return json.dumps(obj)
  raise ReviewParseError("no JSON review object found in the agent's output.")


def parse_review(text: str) -> ReviewFile:
  """Parse review JSON text into a validated :class:`ReviewFile`.

  Raises:
    ReviewParseError: if the text is not valid JSON or violates the schema.
  """
  try:
    data = json.loads(_extract_json(text))
  except json.JSONDecodeError as e:
    raise ReviewParseError(f"review output is not valid JSON: {e}") from e
  if not isinstance(data, dict):
    raise ReviewParseError("review JSON must be an object.")

  summary = data.get("summary", "")
  if not isinstance(summary, str):
    raise ReviewParseError("review 'summary' must be a string.")

  raw_comments = data.get("comments", []) or []
  if not isinstance(raw_comments, list):
    raise ReviewParseError("review 'comments' must be a list.")

  comments = []
  for i, raw in enumerate(raw_comments):
    comments.append(_parse_comment(raw, i))
  return ReviewFile(summary=summary, comments=tuple(comments))


def _parse_comment(raw, index: int) -> ProposedComment:
  if not isinstance(raw, dict):
    raise ReviewParseError(f"comment #{index} must be an object.")
  path = raw.get("path")
  body = raw.get("body")
  if not isinstance(path, str) or not path:
    raise ReviewParseError(f"comment #{index} is missing a string 'path'.")
  if not isinstance(body, str) or not body:
    raise ReviewParseError(f"comment #{index} is missing a string 'body'.")
  line = _require_int(raw, "line", index)
  side = _require_side(raw.get("side"), index)
  start_line = None
  start_side = None
  if raw.get("start_line") is not None:
    start_line = _require_int(raw, "start_line", index)
    start_side = _require_side(raw.get("start_side", side), index)
  return ProposedComment(
    path=path,
    line=line,
    side=side,
    body=body,
    start_line=start_line,
    start_side=start_side,
  )


def _require_int(raw: dict, key: str, index: int) -> int:
  value = raw.get(key)
  # Whole-number floats (a common model slip — JSON `42.0`) are valid line
  # numbers; accept them instead of dropping the entire review (F26).
  if isinstance(value, float) and value.is_integer():
    return int(value)
  if isinstance(value, bool) or not isinstance(value, int):
    # Tolerate stringified ints (a common model slip).
    try:
      return int(str(value))
    except (TypeError, ValueError) as e:
      raise ReviewParseError(
        f"comment #{index} '{key}' must be an integer, got {value!r}."
      ) from e
  return value


def _require_side(value, index: int) -> str:
  if value not in _VALID_SIDES:
    raise ReviewParseError(
      f"comment #{index} 'side' must be one of {_VALID_SIDES}, got {value!r}."
    )
  return value


def read_review_file(path) -> ReviewFile:
  """Read and parse the agent's review file from disk (plan §4.5).

  Raises:
    ReviewParseError: if the file is missing or malformed.
  """
  file_path = pathlib.Path(path)
  if not file_path.is_file():
    raise ReviewParseError(f"agent did not write a review file at {file_path}.")
  return parse_review(file_path.read_text())


def review_to_dict(review: ReviewFile) -> dict:
  """Serialize a ReviewFile to a plain dict (for the persisted envelope, §2)."""
  comments = []
  for comment in review.comments:
    item = {
      "path": comment.path,
      "line": comment.line,
      "side": comment.side,
      "body": comment.body,
    }
    if comment.start_line is not None:
      item["start_line"] = comment.start_line
      item["start_side"] = comment.start_side or comment.side
    comments.append(item)
  return {"summary": review.summary, "comments": comments}


def review_from_dict(data: dict) -> ReviewFile:
  """Parse a ReviewFile from a plain dict (re-validates via parse_review)."""
  return parse_review(json.dumps(data))
