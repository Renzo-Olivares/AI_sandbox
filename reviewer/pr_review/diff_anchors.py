"""Deterministic unified-diff -> inline-comment anchor map (plan §6.1).

No LLM is involved here: the unified-diff hunk headers (``@@ -old,+new @@``)
give exact line numbers, from which we compute the set of commentable
``(path, line, side)`` anchors. The agent is later constrained to attach
findings only to lines in this set, and staging validates against it before the
single create-review POST — eliminating the out-of-diff-anchor 422 (plan §6.1,
§10).

Side convention (plan §6.1): added and context lines are ``RIGHT`` (new-file
line number); deleted lines are ``LEFT`` (old-file line number).
"""

from __future__ import annotations

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from pr_review.models import Anchor, AnchorMap, DiffFile


class DiffParseError(Exception):
  """A file's patch could not be parsed.

  Treated as a per-PR failure upstream (plan §10): the PR is recorded and
  skipped, not crashed.
  """

  def __init__(self, filename: str, detail: str) -> None:
    """Initialize with the offending filename and parser detail."""
    super().__init__(f"could not parse diff for {filename}: {detail}")
    self.filename = filename


def _synthesize_patch(filename: str, patch: str) -> str:
  """Wrap a compare-API file ``patch`` in the headers unidiff requires.

  The ``compare`` endpoint returns only the hunks for a file (no ``diff --git``
  / ``---`` / ``+++`` header); unidiff needs the ``---``/``+++`` lines to
  construct a file object.
  """
  return f"--- a/{filename}\n+++ b/{filename}\n{patch}"


def anchors_for_file(diff_file: DiffFile) -> list[Anchor]:
  """Compute the commentable anchors for one file's patch.

  Args:
    diff_file: the file's diff.

  Returns:
    The anchors for this file, or an empty list when the file has no patch
    (binary, too large, or a pure rename — plan §10).

  Raises:
    DiffParseError: if the patch is present but malformed (plan §10).
  """
  if not diff_file.patch:
    return []
  text = _synthesize_patch(diff_file.filename, diff_file.patch)
  try:
    patch_set = PatchSet.from_string(text)
  except UnidiffParseError as e:
    raise DiffParseError(diff_file.filename, str(e)) from e

  anchors: list[Anchor] = []
  for patched_file in patch_set:
    for hunk in patched_file:
      for line in hunk:
        if line.is_added and line.target_line_no is not None:
          anchors.append(
            Anchor(
              path=diff_file.filename,
              line=line.target_line_no,
              side="RIGHT",
              content=line.value.rstrip("\n"),
            )
          )
        elif line.is_removed and line.source_line_no is not None:
          anchors.append(
            Anchor(
              path=diff_file.filename,
              line=line.source_line_no,
              side="LEFT",
              content=line.value.rstrip("\n"),
            )
          )
        elif line.is_context and line.target_line_no is not None:
          anchors.append(
            Anchor(
              path=diff_file.filename,
              line=line.target_line_no,
              side="RIGHT",
              content=line.value.rstrip("\n"),
            )
          )
  return anchors


def build_anchor_map(diff_files) -> AnchorMap:
  """Build the :class:`AnchorMap` for a PR's full diff (plan §6.1).

  Args:
    diff_files: the PR's :class:`~pr_review.models.DiffFile` list.

  Returns:
    The combined anchor map across all files.

  Raises:
    DiffParseError: if any file's patch is malformed (plan §10).
  """
  anchors: list[Anchor] = []
  for diff_file in diff_files:
    anchors.extend(anchors_for_file(diff_file))
  return AnchorMap(anchors)


def render_annotated_patch(diff_file: DiffFile) -> str:
  """Render a file's patch with each line labeled by its anchor (plan §6.1).

  Each line is prefixed with the exact file line number and side it can be
  anchored to, so the agent READS anchors instead of computing them from hunk
  headers (the main mis-anchor source). Added/context lines are RIGHT (new-file
  number); deleted lines are LEFT (old-file number).

  Raises:
    DiffParseError: if the patch is present but malformed (plan §10).
  """
  if not diff_file.patch:
    return (
      f"### {diff_file.filename}\n"
      "(no textual diff — binary, rename, or too large)\n"
    )
  text = _synthesize_patch(diff_file.filename, diff_file.patch)
  try:
    patch_set = PatchSet.from_string(text)
  except UnidiffParseError as e:
    raise DiffParseError(diff_file.filename, str(e)) from e

  out = [f"### {diff_file.filename}"]
  for patched_file in patch_set:
    for hunk in patched_file:
      if hunk.target_length:
        last = hunk.target_start + hunk.target_length - 1
        out.append(f"  @@ new lines {hunk.target_start}-{last} @@")
      else:
        # Pure-deletion hunk: no new-side lines, so a "new lines X-Y" range
        # would read backwards (e.g. 9-8). The deleted lines anchor LEFT at
        # their OLD numbers (shown per-line below) (finding F31).
        src_last = hunk.source_start + hunk.source_length - 1
        out.append(
          f"  @@ deleted old lines {hunk.source_start}-{src_last} (LEFT) @@"
        )
      for line in hunk:
        content = line.value.rstrip("\n")
        if line.is_added:
          number, side, marker = line.target_line_no, "RIGHT", "+"
        elif line.is_removed:
          number, side, marker = line.source_line_no, "LEFT", "-"
        else:
          number, side, marker = line.target_line_no, "RIGHT", " "
        if number is None:
          continue
        out.append(f"{number:>7} {side:<5} {marker}| {content}")
  return "\n".join(out) + "\n"
