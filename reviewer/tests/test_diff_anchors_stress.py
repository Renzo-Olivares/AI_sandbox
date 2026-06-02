"""Stress tests for the diff -> anchor-map parser (plan §6.1; finding F59).

Consolidates the former root-level stress_*.py scripts into the pytest suite so
the highest-risk anchor edge cases run in CI with real (collected) assertions:
large multi-hunk diffs, LEFT-side (deleted) old/new line-number divergence, the
"\\ No newline at end of file" marker, separate hunks at different ranges, and
renames (anchors must land on the NEW filename, never previous_filename). A
regression in any of these would 422 every inline comment yet otherwise pass.
"""

import pytest

from pr_review.diff_anchors import (
  anchors_for_file,
  build_anchor_map,
  render_annotated_patch,
)
from pr_review.models import DiffFile


def _df(filename, patch, **kw):
  return DiffFile(filename=filename, status="modified", patch=patch, **kw)


def _amap(filename, patch, **kw):
  return build_anchor_map([_df(filename, patch, **kw)])


# --- Large multi-hunk patch (a rebased/force-pushed compare diff) -------------
# Hunks at shifting ranges; added=RIGHT@new, deleted=LEFT@old, ctx=RIGHT@new.
_LARGE_PATCH = "\n".join(
  [
    "@@ -1,10 +1,11 @@",
    " ctx_a1",
    " ctx_a2",
    "-del_a1",
    "-del_a2",
    "+add_a1",
    "+add_a2",
    "+add_a3",
    " ctx_a3",
    " ctx_a4",
    " ctx_a5",
    " ctx_a6",
    " ctx_a7",
    " ctx_a8",
    "@@ -50,9 +51,8 @@",
    " ctx_b1",
    " ctx_b2",
    "-del_b1",
    "-del_b2",
    "-del_b3",
    "+add_b1",
    "+add_b2",
    " ctx_b3",
    " ctx_b4",
    " ctx_b5",
    " ctx_b6",
    "@@ -100,7 +100,10 @@",
    " ctx_c1",
    " ctx_c2",
    "+add_c1",
    "+add_c2",
    "+add_c3",
    "+add_c4",
    " ctx_c3",
    "-del_c1",
    " ctx_c4",
    " ctx_c5",
    " ctx_c6",
    "@@ -200,6 +203,5 @@",
    " ctx_d1",
    "-del_d1",
    "-del_d2",
    "+add_d1",
    " ctx_d2",
    " ctx_d3",
    " ctx_d4",
  ]
)


@pytest.mark.parametrize(
  "line,side,content",
  [
    (1, "RIGHT", "ctx_a1"),  # first context of hunk 1
    (3, "LEFT", "del_a1"),  # deleted -> OLD line 3
    (3, "RIGHT", "add_a1"),  # added -> NEW line 3 (same num, other side)
    (11, "RIGHT", "ctx_a8"),
    (52, "LEFT", "del_b1"),
    (53, "RIGHT", "add_b1"),
    (58, "RIGHT", "ctx_b6"),
    (102, "RIGHT", "add_c1"),
    (103, "LEFT", "del_c1"),
    (106, "RIGHT", "ctx_c3"),
    (203, "RIGHT", "ctx_d1"),  # hunk 4 new-side start shifted to 203
    (201, "LEFT", "del_d1"),
    (204, "RIGHT", "add_d1"),
    (207, "RIGHT", "ctx_d4"),
  ],
)
def test_large_multihunk_anchor_present(line, side, content):
  amap = _amap("src/rebased.py", _LARGE_PATCH)
  assert amap.is_valid("src/rebased.py", line, side)
  by_index = {(a.line, a.side): a.content for a in amap.anchors}
  assert by_index[(line, side)] == content


def test_large_multihunk_absent_anchors():
  amap = _amap("src/rebased.py", _LARGE_PATCH)
  assert not amap.is_valid("src/rebased.py", 1, "LEFT")  # ctx: RIGHT-only
  assert not amap.is_valid("src/rebased.py", 201, "RIGHT")  # del: LEFT-only
  assert not amap.is_valid("src/rebased.py", 9999, "RIGHT")  # nonexistent


# --- "\ No newline at end of file" markers must not leak an extra anchor ------
def test_no_newline_marker_does_not_leak_anchor():
  patch = (
    "@@ -1,2 +1,2 @@\n"
    " keep line one\n"
    "-old last line\n"
    "\\ No newline at end of file\n"
    "+new last line\n"
    "\\ No newline at end of file\n"
  )
  amap = _amap("src/nonl.txt", patch)
  assert amap.is_valid("src/nonl.txt", 1, "RIGHT")
  assert amap.is_valid("src/nonl.txt", 2, "LEFT")
  assert amap.is_valid("src/nonl.txt", 2, "RIGHT")
  # the marker line must NOT produce a bogus line-3 anchor
  assert not amap.is_valid("src/nonl.txt", 3, "RIGHT")
  assert not amap.is_valid("src/nonl.txt", 3, "LEFT")


def test_no_newline_marker_midpatch_then_more_lines():
  patch = (
    "@@ -1,3 +1,4 @@\n"
    " alpha\n"
    " beta\n"
    "-gamma\n"
    "\\ No newline at end of file\n"
    "+gamma\n"
    "+delta\n"
  )
  amap = _amap("src/gain.txt", patch)
  for line, side in [
    (1, "RIGHT"),
    (2, "RIGHT"),
    (3, "LEFT"),
    (3, "RIGHT"),
    (4, "RIGHT"),
  ]:
    assert amap.is_valid("src/gain.txt", line, side)


# --- LEFT uses OLD numbers, RIGHT uses NEW numbers (they diverge) -------------
def test_left_uses_old_numbers_right_uses_new():
  patch = (
    "@@ -5,7 +5,6 @@\n alpha\n-beta\n-gamma\n+GAMMA\n delta\n"
    "-epsilon\n+EPSILON\n zeta\n eta\n"
  )
  anchors = anchors_for_file(_df("e.py", patch))
  got = [(a.line, a.side, a.content) for a in anchors]
  assert got == [
    (5, "RIGHT", "alpha"),
    (6, "LEFT", "beta"),
    (7, "LEFT", "gamma"),
    (6, "RIGHT", "GAMMA"),
    (7, "RIGHT", "delta"),  # context: NEW 7 (its OLD number was 8)
    (9, "LEFT", "epsilon"),  # deleted: OLD 9 (NOT the NEW-side 8)
    (8, "RIGHT", "EPSILON"),
    (9, "RIGHT", "zeta"),
    (10, "RIGHT", "eta"),
  ]
  amap = _amap("e.py", patch)
  assert amap.is_valid("e.py", 9, "LEFT")  # epsilon at OLD 9
  assert not amap.is_valid("e.py", 8, "LEFT")  # 8 is a NEW-only number
  assert amap.is_valid("e.py", 8, "RIGHT")  # EPSILON added at NEW 8


def test_pure_deletion_render_header_not_backwards():  # F31
  patch = "@@ -20,3 +19,0 @@\n-d1\n-d2\n-d3\n"  # new side has 0 lines
  rendered = render_annotated_patch(_df("src/del.py", patch))
  assert "  @@ deleted old lines 20-22 (LEFT) @@" in rendered
  assert "20-19" not in rendered  # the old backwards "new lines X-(X-1)" bug
  assert f"{20:>7} {'LEFT':<5} -| d1" in rendered  # deleted lines still LEFT


def test_pure_deletion_is_left_only():
  amap = _amap("src/del.py", "@@ -20,4 +20,1 @@\n keep\n-d1\n-d2\n-d3\n")
  for ln in (21, 22, 23):
    assert amap.is_valid("src/del.py", ln, "LEFT")
    assert not amap.is_valid("src/del.py", ln, "RIGHT")
  assert amap.is_valid("src/del.py", 20, "RIGHT")
  assert not amap.is_valid("src/del.py", 20, "LEFT")


# --- Two hunks at different ranges --------------------------------------------
_TWO_HUNKS = (
  "@@ -1,4 +1,5 @@\n line A\n-line B\n+line B2\n+line B3\n line C\n line D\n"
  "@@ -20,3 +21,4 @@\n line X\n-line Y\n+line Y2\n+line Y3\n line Z\n"
)


def test_two_hunks_anchor_set_and_gaps():
  amap = _amap("src/example.py", _TWO_HUNKS)
  pairs = {(a.line, a.side) for a in amap.anchors}
  assert pairs == {
    (1, "RIGHT"),
    (2, "LEFT"),
    (2, "RIGHT"),
    (3, "RIGHT"),
    (4, "RIGHT"),
    (5, "RIGHT"),
    (21, "RIGHT"),
    (21, "LEFT"),
    (22, "RIGHT"),
    (23, "RIGHT"),
    (24, "RIGHT"),
  }
  assert not amap.is_valid("src/example.py", 7, "RIGHT")  # between hunks
  assert not amap.is_valid("src/example.py", 20, "RIGHT")  # h2 starts at 21


def test_two_hunks_render_labels_and_headers():
  rendered = render_annotated_patch(_df("src/example.py", _TWO_HUNKS))
  lines = rendered.splitlines()
  assert "  @@ new lines 1-5 @@" in lines
  assert "  @@ new lines 21-24 @@" in lines
  # exact label format: f"{number:>7} {side:<5} {marker}| {content}"
  assert f"{2:>7} {'LEFT':<5} -| line B" in lines
  assert f"{3:>7} {'RIGHT':<5} +| line B3" in lines


# --- Renames: anchors land on the NEW filename, never previous_filename -------
def test_pure_rename_yields_no_anchors():
  df = DiffFile(
    "lib/new.dart", "renamed", patch=None, previous_filename="lib/old.dart"
  )
  assert anchors_for_file(df) == []
  assert len(build_anchor_map([df])) == 0
  rendered = render_annotated_patch(df)
  assert "no textual diff" in rendered and "lib/new.dart" in rendered


def test_rename_with_modification_anchors_on_new_filename():
  patch = (
    "@@ -1,3 +1,5 @@\n line one\n line two\n"
    "+added line A\n+added line B\n line three\n"
  )
  df = DiffFile(
    "lib/new.dart", "renamed", patch=patch, previous_filename="lib/old.dart"
  )
  amap = build_anchor_map([df])
  assert amap.paths() == {"lib/new.dart"}  # NEVER the old name
  assert "lib/old.dart" not in amap.paths()
  right_lines = sorted(a.line for a in amap.anchors if a.side == "RIGHT")
  assert right_lines == [1, 2, 3, 4, 5]
  assert not any(a.side == "LEFT" for a in amap.anchors)  # no deletions
  assert amap.is_valid("lib/new.dart", 3, "RIGHT")
  assert not amap.is_valid("lib/old.dart", 3, "RIGHT")
