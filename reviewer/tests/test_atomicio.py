"""Tests for atomic text writes (finding F35)."""

import os

import pytest

from pr_review.atomicio import write_text_atomic


def test_writes_content_and_creates_parent_dirs(tmp_path):
  dest = tmp_path / "sub" / "deep" / "f.txt"
  write_text_atomic(dest, "hello")
  assert dest.read_text() == "hello"


def test_overwrites_and_leaves_no_temp_files(tmp_path):
  dest = tmp_path / "f.txt"
  write_text_atomic(dest, "v1")
  write_text_atomic(dest, "v2")
  assert dest.read_text() == "v2"
  assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]  # no .tmp leftovers


def test_failed_write_leaves_existing_file_intact(tmp_path, monkeypatch):
  # A failure mid-replace must leave the prior file untouched and clean up the
  # temp — never a truncated/partial file (the whole point of F35).
  dest = tmp_path / "f.txt"
  write_text_atomic(dest, "original")

  def boom(src, dst):
    raise OSError("disk full")

  monkeypatch.setattr(os, "replace", boom)
  with pytest.raises(OSError):
    write_text_atomic(dest, "new-but-doomed")

  assert dest.read_text() == "original"  # untouched
  assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]  # temp cleaned up
