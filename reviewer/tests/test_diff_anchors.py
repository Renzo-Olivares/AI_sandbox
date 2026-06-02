"""Tests for the deterministic diff->anchor parser (plan §6.1, §10).

All synthetic patches — no network. This is the highest-leverage component to
test, since a wrong anchor would be rejected by GitHub with an atomic 422.
"""

import pytest

from pr_review.diff_anchors import (
  DiffParseError,
  anchors_for_file,
  build_anchor_map,
  render_annotated_patch,
)
from pr_review.models import DiffFile

# A hunk mirroring PR #1's text.dart change. Three leading context lines, then
# three added lines, then two deleted, then one trailing context line, so the
# M1-proven anchor (the `isOriginSelectable` line) lands at new line 1293.
# Header counts: old 1392..1397 (6), new 1290..1296 (7).
TEXT_DART = DiffFile(
  filename="packages/flutter/lib/src/widgets/text.dart",
  status="modified",
  patch=(
    "@@ -1392,6 +1290,7 @@ class _SelectableTextContainerDelegate\n"
    "       if (index >= skipStart && index <= skipEnd) {\n"
    "         continue;\n"
    "       }\n"
    "+      if (isOriginSelectable(selectables[index])) {\n"
    "+        continue;\n"
    "+      }\n"
    "-      oldLineToRemoveOne();\n"
    "-      oldLineToRemoveTwo();\n"
    "       dispatchSelectionEventToChild();\n"
  ),
)


def _by_side(anchors, side):
  return [a for a in anchors if a.side == side]


def test_added_and_context_lines_are_right_side():
  anchors = anchors_for_file(TEXT_DART)
  right = {a.line for a in _by_side(anchors, "RIGHT")}
  # new-side: 1290/1291/1292 (ctx), 1293/1294/1295 (+), 1296 (ctx)
  assert right == {1290, 1291, 1292, 1293, 1294, 1295, 1296}


def test_removed_lines_are_left_side_with_old_numbers():
  anchors = anchors_for_file(TEXT_DART)
  left = {a.line for a in _by_side(anchors, "LEFT")}
  # old-side removed lines: 1395, 1396
  assert left == {1395, 1396}


def test_m1_proven_anchor_is_valid():
  amap = build_anchor_map([TEXT_DART])
  path = TEXT_DART.filename
  assert amap.is_valid(path, 1293, "RIGHT") is True  # the M1 added line
  assert amap.is_valid(path, 1290, "RIGHT") is True  # a context line
  assert amap.is_valid(path, 1395, "LEFT") is True  # a deleted line
  # wrong side / out-of-diff line / wrong path must all be rejected
  assert amap.is_valid(path, 1293, "LEFT") is False
  assert amap.is_valid(path, 9999, "RIGHT") is False
  assert amap.is_valid("other/file.dart", 1293, "RIGHT") is False


def test_anchor_content_is_captured():
  anchors = anchors_for_file(TEXT_DART)
  added = [a for a in anchors if a.line == 1293 and a.side == "RIGHT"]
  assert len(added) == 1
  assert "isOriginSelectable" in added[0].content
  assert not added[0].content.endswith("\n")


def test_multi_hunk_file():
  diff = DiffFile(
    filename="lib/foo.dart",
    status="modified",
    patch="@@ -1,3 +1,4 @@\n a\n+b\n c\n d\n@@ -10,2 +11,3 @@\n e\n+f\n g\n",
  )
  amap = build_anchor_map([diff])
  # hunk 1 new: a=1, b=2(+), c=3, d=4 ; hunk 2 new: e=11, f=12(+), g=13
  for line in (1, 2, 3, 4, 11, 12, 13):
    assert amap.is_valid("lib/foo.dart", line, "RIGHT") is True
  assert len(amap) == 7  # all RIGHT, no removed lines
  assert amap.is_valid("lib/foo.dart", 5, "RIGHT") is False


def test_no_patch_yields_no_anchors():
  binary = DiffFile(filename="assets/logo.png", status="modified", patch=None)
  assert anchors_for_file(binary) == []


def test_pure_rename_yields_no_anchors():
  renamed = DiffFile(
    filename="lib/new_name.dart",
    status="renamed",
    patch=None,
    previous_filename="lib/old_name.dart",
  )
  assert anchors_for_file(renamed) == []


def test_truncated_hunk_raises_diff_parse_error():
  bad = DiffFile(
    filename="lib/bad.dart",
    status="modified",
    # Header declares 5 lines but only 1 is present (truncated, plan §10).
    patch="@@ -1,5 +1,5 @@\n only one line\n",
  )
  with pytest.raises(DiffParseError) as exc:
    anchors_for_file(bad)
  assert exc.value.filename == "lib/bad.dart"


def test_empty_diff_file_list_gives_empty_map():
  amap = build_anchor_map([])
  assert len(amap) == 0
  assert amap.paths() == set()


def test_render_annotated_patch_labels_each_line():
  out = render_annotated_patch(TEXT_DART)
  lines = out.splitlines()
  added = next(ln for ln in lines if "isOriginSelectable" in ln)
  assert "1293" in added and "RIGHT" in added and "+" in added
  removed = next(ln for ln in lines if "oldLineToRemoveOne" in ln)
  assert "1395" in removed and "LEFT" in removed
  context = next(ln for ln in lines if "skipStart" in ln)
  assert "1290" in context and "RIGHT" in context
  # Every annotated number must be a real anchor (parser-consistent).
  amap = build_anchor_map([TEXT_DART])
  assert amap.is_valid(TEXT_DART.filename, 1293, "RIGHT")


def test_render_annotated_patch_no_patch():
  out = render_annotated_patch(
    DiffFile(filename="x.png", status="modified", patch=None)
  )
  assert "no textual diff" in out
