"""Tests for Layer-1 settings rendering/merge and its precedence validation."""

import json

import pytest

from pr_review import agy_settings
from pr_review.config import validate_agy_settings
from pr_review.errors import ConfigError


def _write(tmp_path, settings):
  path = tmp_path / "settings.json"
  path.write_text(json.dumps(settings))
  return path


def test_merge_preserves_existing_and_adds_layer1():
  existing = {
    "colorScheme": "tokyo night",
    "model": "X",
    "trustedWorkspaces": ["/a"],
  }
  merged = agy_settings.merge_settings(existing, trusted_dirs=["/wt/pr-1"])
  assert merged["colorScheme"] == "tokyo night"  # preserved
  assert merged["model"] == "X"
  assert merged["enableTerminalSandbox"] is True
  assert "/a" in merged["trustedWorkspaces"]  # existing trust kept
  assert "/wt/pr-1" in merged["trustedWorkspaces"]  # worktree trusted
  perms = merged["permissions"]
  assert "command(git)" in perms["allow"]
  assert "mcp(*)" in perms["deny"]
  assert perms["ask"] == []


def test_rendered_config_passes_validation(tmp_path):
  merged = agy_settings.merge_settings({}, trusted_dirs=[str(tmp_path)])
  validate_agy_settings(_write(tmp_path, merged))  # must not raise


def test_validation_rejects_command_star_deny(tmp_path):
  bad = agy_settings.merge_settings({})
  bad["permissions"]["deny"].append("command(*)")
  with pytest.raises(ConfigError, match="command"):
    validate_agy_settings(_write(tmp_path, bad))


def test_validation_rejects_write_file_star_deny(tmp_path):
  bad = agy_settings.merge_settings({})
  bad["permissions"]["deny"].append("write_file(*)")
  with pytest.raises(ConfigError, match="write_file"):
    validate_agy_settings(_write(tmp_path, bad))


def test_validation_rejects_broad_gh_allow(tmp_path):
  bad = agy_settings.merge_settings({})
  bad["permissions"]["allow"].append("command(gh)")
  with pytest.raises(ConfigError, match="gh"):
    validate_agy_settings(_write(tmp_path, bad))


def test_validation_rejects_unsandboxed_allow(tmp_path):
  bad = agy_settings.merge_settings({})
  bad["permissions"]["allow"].append("unsandboxed(git push)")
  with pytest.raises(ConfigError, match="unsandboxed"):
    validate_agy_settings(_write(tmp_path, bad))


def test_validation_rejects_missing_deny_namespace(tmp_path):
  bad = agy_settings.merge_settings({})
  bad["permissions"]["deny"] = ["read_url(*)", "execute_url(*)", "mcp(*)"]
  with pytest.raises(ConfigError, match="unsandboxed"):
    validate_agy_settings(_write(tmp_path, bad))


def test_validation_rejects_malformed_json(tmp_path):
  path = tmp_path / "settings.json"
  path.write_text('{"permissions": {"allow": [],}}')  # trailing comma
  with pytest.raises(ConfigError, match="JSON"):
    validate_agy_settings(path)


def test_apply_and_restore_roundtrip(tmp_path):
  path = tmp_path / "settings.json"
  path.write_text(json.dumps({"colorScheme": "x"}))
  merged, backup = agy_settings.apply_settings(
    path, trusted_dirs=[str(tmp_path)]
  )
  assert backup is not None and backup.is_file()
  data = json.loads(path.read_text())
  assert "permissions" in data and data["colorScheme"] == "x"
  agy_settings.restore_settings(path, backup)
  assert json.loads(path.read_text()) == {
    "colorScheme": "x"
  }  # restored exactly


def test_install_project_settings_pins_model(tmp_path):
  agy_settings.install_project_settings(
    tmp_path, model="Claude Sonnet 4.6 (Thinking)"
  )
  settings = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
  data = json.loads(settings.read_text())
  assert data["model"] == "Claude Sonnet 4.6 (Thinking)"
  assert "mcp(*)" in data["permissions"]["deny"]  # Layer 1 still applied


def test_install_project_settings_omits_model_when_none(tmp_path):
  agy_settings.install_project_settings(tmp_path)
  settings = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
  assert "model" not in json.loads(settings.read_text())


def test_install_validates_the_installed_settings(tmp_path):
  # F11: validation runs on the file we ACTUALLY install, so a precedence-broken
  # deny is caught here (not only on a separate tempfile render).
  with pytest.raises(ConfigError):
    agy_settings.install_project_settings(
      tmp_path, deny=("command(*)", *agy_settings.DEFAULT_DENY)
    )


# --- Anti-injection scrub (plan §1 Layer 1; review findings F01/F61) ---------


def _our_settings(worktree):
  """Read the installed Layer-1 settings.json under a worktree."""
  return json.loads(
    (worktree / ".gemini" / "antigravity-cli" / "settings.json").read_text()
  )


def test_scrub_removes_injected_dir_and_reports_it(tmp_path):
  # A PR ships its own permissive .gemini at the repo root.
  injected = tmp_path / ".gemini" / "antigravity-cli"
  injected.mkdir(parents=True)
  (injected / "settings.json").write_text('{"permissions": {"allow": ["*"]}}')

  removed = agy_settings.scrub_project_agy_config(tmp_path)

  assert any(".gemini" in r for r in removed)  # flagged for the operator log
  assert not (tmp_path / ".gemini").exists()  # injected config is gone


def test_install_replaces_injected_config_with_ours(tmp_path):
  injected = tmp_path / ".gemini" / "antigravity-cli"
  injected.mkdir(parents=True)
  (injected / "settings.json").write_text('{"permissions": {"allow": ["*"]}}')

  agy_settings.install_project_settings(tmp_path)

  data = _our_settings(tmp_path)  # OUR strict settings now govern
  assert data["permissions"]["allow"] != ["*"]
  assert "mcp(*)" in data["permissions"]["deny"]


def test_scrub_removes_nested_gemini(tmp_path):
  nested = tmp_path / "packages" / "app" / ".gemini"
  nested.mkdir(parents=True)
  (nested / "settings.json").write_text("{}")

  agy_settings.scrub_project_agy_config(tmp_path)

  assert not nested.exists()


def test_scrub_removes_file_gemini(tmp_path):
  # .gemini shipped as a regular FILE (is_dir() is False, so the old scrub
  # skipped it entirely).
  (tmp_path / ".gemini").write_text("not a dir")

  agy_settings.install_project_settings(tmp_path)

  assert (tmp_path / ".gemini").is_dir()  # replaced by our real settings dir
  assert "mcp(*)" in _our_settings(tmp_path)["permissions"]["deny"]


def test_scrub_neutralizes_symlinked_gemini_without_following_it(tmp_path):
  # The core F01 escape: .gemini is a symlink to a dir outside the worktree.
  # The scrub must remove the LINK (not its target), and our write must land
  # inside the worktree, never through the link.
  outside = tmp_path / "outside"
  outside.mkdir()
  worktree = tmp_path / "wt"
  worktree.mkdir()
  (worktree / ".gemini").symlink_to(outside, target_is_directory=True)

  agy_settings.install_project_settings(worktree)

  assert not (outside / "antigravity-cli").exists()  # never written through
  assert not (worktree / ".gemini").is_symlink()  # link removed
  settings = worktree / ".gemini" / "antigravity-cli" / "settings.json"
  assert settings.is_file()
  assert "mcp(*)" in _our_settings(worktree)["permissions"]["deny"]


def test_install_refuses_to_write_through_a_surviving_symlink(
  tmp_path, monkeypatch
):
  # If the scrub somehow fails to remove an injected .gemini symlink, the
  # install step must REFUSE rather than write our settings outside the
  # worktree (defense in depth, review finding F01).
  outside = tmp_path / "outside"
  outside.mkdir()
  worktree = tmp_path / "wt"
  worktree.mkdir()
  (worktree / ".gemini").symlink_to(outside, target_is_directory=True)

  monkeypatch.setattr(agy_settings, "scrub_project_agy_config", lambda wt: [])
  with pytest.raises(PermissionError):
    agy_settings.install_project_settings(worktree)

  assert not (outside / "antigravity-cli").exists()  # never written through
