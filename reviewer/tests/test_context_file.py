"""Tests for serializing the context bundle + style guide into the worktree."""

import json

import pytest

from pr_review.context_file import write_context_file
from pr_review.models import AnchorMap, PRMeta, ReviewContext

PR = PRMeta(
  number=7,
  title="t",
  url="u",
  head_sha="sha",
  head_ref="f",
  base_ref="master",
  author="a",
  state="OPEN",
)


def _ctx(**kw):
  return ReviewContext(
    pr=PR,
    kind="fresh",
    diff_files=(),
    anchor_map=AnchorMap(()),
    **kw,
  )


def test_writes_three_paths_and_guide_file(tmp_path):
  ctx = _ctx(style_guide_text="GUIDE BODY", touches_style_guide=False)
  context_path, output_path, guide_path = write_context_file(
    tmp_path, ctx, "o/r"
  )
  assert context_path.is_file()
  assert output_path.name == "review.json"
  assert guide_path is not None
  assert guide_path.name == "style-guide.md"
  assert guide_path.read_text() == "GUIDE BODY"
  bundle = json.loads(context_path.read_text())
  assert bundle["touches_style_guide"] is False


def test_no_guide_file_when_text_absent(tmp_path):
  ctx = _ctx(style_guide_text=None, touches_style_guide=False)
  _context, _output, guide_path = write_context_file(tmp_path, ctx, "o/r")
  assert guide_path is None
  assert not (tmp_path / ".pr-review" / "style-guide.md").exists()


def test_touches_flag_true_is_serialized(tmp_path):
  ctx = _ctx(style_guide_text="G", touches_style_guide=True)
  context_path, _o, _g = write_context_file(tmp_path, ctx, "o/r")
  bundle = json.loads(context_path.read_text())
  assert bundle["touches_style_guide"] is True


# --- Anti-injection: untrusted .pr-review must not redirect writes (F13) -----


def test_symlinked_pr_review_does_not_redirect_writes(tmp_path):
  # A PR ships .pr-review as a symlink to a dir outside the worktree. The write
  # must NOT follow it: the link is removed and our files land inside the wt.
  outside = tmp_path / "outside"
  outside.mkdir()
  worktree = tmp_path / "wt"
  worktree.mkdir()
  (worktree / ".pr-review").symlink_to(outside, target_is_directory=True)

  ctx = _ctx(style_guide_text="G", touches_style_guide=False)
  context_path, _o, guide_path = write_context_file(worktree, ctx, "o/r")

  assert not (outside / "context.json").exists()  # never written through
  assert not (worktree / ".pr-review").is_symlink()  # link removed
  assert context_path.is_file()
  assert worktree.resolve() in context_path.resolve().parents
  assert guide_path.read_text() == "G"


def test_file_pr_review_is_replaced_with_real_dir(tmp_path):
  # .pr-review shipped as a regular file would make mkdir/write fail or follow
  # it; it must be removed and replaced with our real dir.
  worktree = tmp_path / "wt"
  worktree.mkdir()
  (worktree / ".pr-review").write_text("not a dir")

  ctx = _ctx(style_guide_text=None, touches_style_guide=False)
  context_path, _o, _g = write_context_file(worktree, ctx, "o/r")

  assert (worktree / ".pr-review").is_dir()
  assert context_path.is_file()


def test_nested_symlink_in_shipped_pr_review_does_not_escape(tmp_path):
  # A PR ships a real .pr-review dir containing a context.json symlink pointing
  # outside. Nuking the dir first means our write cannot follow that symlink.
  outside = tmp_path / "outside"
  outside.mkdir()
  victim = outside / "victim.json"
  victim.write_text("ORIGINAL")
  worktree = tmp_path / "wt"
  worktree.mkdir()
  shipped = worktree / ".pr-review"
  shipped.mkdir()
  (shipped / "context.json").symlink_to(victim)

  ctx = _ctx(style_guide_text=None, touches_style_guide=False)
  context_path, _o, _g = write_context_file(worktree, ctx, "o/r")

  assert victim.read_text() == "ORIGINAL"  # the outside file is untouched
  assert not context_path.is_symlink()
  assert worktree.resolve() in context_path.resolve().parents


def test_refuses_when_pr_review_symlink_survives(tmp_path, monkeypatch):
  # Defense in depth: if cleaning fails to remove an injected symlink, the
  # write must REFUSE rather than follow it outside the worktree.
  import pr_review.context_file as cf

  outside = tmp_path / "outside"
  outside.mkdir()
  worktree = tmp_path / "wt"
  worktree.mkdir()
  (worktree / ".pr-review").symlink_to(outside, target_is_directory=True)

  # Simulate a unlink/rmtree that does not actually remove the link.
  monkeypatch.setattr(cf.pathlib.Path, "unlink", lambda self, **kw: None)
  with pytest.raises(PermissionError):
    write_context_file(worktree, _ctx(touches_style_guide=False), "o/r")

  assert not (outside / "context.json").exists()  # never written through
