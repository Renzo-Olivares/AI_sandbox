"""Tests for worktree provisioning/teardown against a tiny local git repo.

No network: these exercise the add/remove/cleanup mechanics. The network paths
(ensure_base_clone, fetch pull/N/head) are exercised by the live end-to-end run.
"""

import subprocess

import pytest

from pr_review import worktree


def _init_repo(path):
  path.mkdir(parents=True, exist_ok=True)

  def run(*args):
    subprocess.run(
      ["git", "-C", str(path), *args],
      check=True,
      capture_output=True,
      text=True,
    )

  subprocess.run(
    ["git", "init", "-q", str(path)], check=True, capture_output=True, text=True
  )
  run("config", "user.email", "t@example.com")
  run("config", "user.name", "Tester")
  (path / "f.txt").write_text("hello\n")
  run("add", ".")
  run("commit", "-q", "-m", "init")
  out = subprocess.run(
    ["git", "-C", str(path), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
  )
  return out.stdout.strip()


def test_session_adds_then_removes(tmp_path):
  base = tmp_path / "base"
  sha = _init_repo(base)
  with worktree.worktree_session(base, tmp_path / "wts", 7, sha) as wt:
    assert wt.exists()
    assert (wt / "f.txt").read_text() == "hello\n"
    seen = wt
  assert not seen.exists()  # torn down on normal exit


def test_session_cleans_up_on_exception(tmp_path):
  base = tmp_path / "base"
  sha = _init_repo(base)
  seen = {}
  with pytest.raises(RuntimeError):
    with worktree.worktree_session(base, tmp_path / "wts", 7, sha) as wt:
      seen["wt"] = wt
      assert wt.exists()
      raise RuntimeError("boom")
  assert not seen["wt"].exists()  # torn down despite the exception


def test_provisioned_worktree_cleans_up_on_add_failure(tmp_path, monkeypatch):
  # F21: a partial add_worktree failure must still tear down the deterministic
  # worktree path, not leak a half-created worktree.
  cleaned = []
  monkeypatch.setattr(worktree, "ensure_base_clone", lambda *a, **k: None)
  monkeypatch.setattr(worktree, "prune_stale", lambda base: None)
  monkeypatch.setattr(worktree, "_fetch_pr_head", lambda base, number: "ref")

  def boom(*a, **k):
    raise worktree.WorktreeError("git worktree add failed partway")

  monkeypatch.setattr(worktree, "add_worktree", boom)
  monkeypatch.setattr(
    worktree, "remove_worktree", lambda base, wt: cleaned.append(wt)
  )

  with pytest.raises(worktree.WorktreeError):
    with worktree.provisioned_worktree(
      "o/r", 7, "sha", tmp_path / "base", tmp_path / "wts"
    ):
      raise AssertionError("body must not run when provisioning fails")

  assert cleaned and str(cleaned[0]).endswith("pr-7")  # teardown still ran


def test_session_cleans_up_on_add_failure(tmp_path, monkeypatch):
  # F21: same guard for the worktree_session helper.
  cleaned = []
  monkeypatch.setattr(worktree, "prune_stale", lambda base: None)

  def boom(*a, **k):
    raise worktree.WorktreeError("git worktree add failed partway")

  monkeypatch.setattr(worktree, "add_worktree", boom)
  monkeypatch.setattr(
    worktree, "remove_worktree", lambda base, wt: cleaned.append(wt)
  )
  with pytest.raises(worktree.WorktreeError):
    with worktree.worktree_session(tmp_path / "base", tmp_path / "wts", 7, "s"):
      raise AssertionError("body must not run when provisioning fails")
  assert cleaned and str(cleaned[0]).endswith("pr-7")


def test_add_worktree_replaces_stale(tmp_path):
  base = tmp_path / "base"
  sha = _init_repo(base)
  wtdir = tmp_path / "wts"
  wt1 = worktree.add_worktree(base, wtdir, 7, sha)
  assert wt1.exists()
  # Re-adding for the same PR number must clean and recreate without error.
  wt2 = worktree.add_worktree(base, wtdir, 7, sha)
  assert wt2.exists()
  assert wt1 == wt2
  worktree.remove_worktree(base, wt2)
  assert not wt2.exists()


def test_remove_is_safe_when_already_gone(tmp_path):
  base = tmp_path / "base"
  _init_repo(base)
  # Removing a non-existent worktree path must not raise.
  worktree.remove_worktree(base, tmp_path / "wts" / "pr-99")


def test_origin_matches_guards_wrong_repo(tmp_path):
  base = tmp_path / "clone"
  base.mkdir()
  subprocess.run(
    ["git", "init", "-q", str(base)], check=True, capture_output=True, text=True
  )
  subprocess.run(
    [
      "git",
      "-C",
      str(base),
      "remote",
      "add",
      "origin",
      "https://github.com/flutter/flutter.git",
    ],
    check=True,
    capture_output=True,
    text=True,
  )
  assert worktree.origin_matches(base, "flutter/flutter") is True
  assert worktree.origin_matches(base, "Renzo-Olivares/flutter") is False
