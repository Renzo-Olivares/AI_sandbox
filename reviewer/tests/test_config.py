"""Tests for config loading and the safety-critical validation gates."""

import json
import textwrap

import pytest

from pr_review.config import (
  check_global_agy_settings,
  load_config,
  resolve_token,
)
from pr_review.errors import ConfigError

VALID = """
  repo: flutter/flutter
  username: Renzo-Olivares
  mode: auto-stage-review
  default_branch: master
  labels_to_skip: ["waiting for response"]
  batch_size: 5
  review_file_dir: ~/pr-reviews/pending
  report_dir: ~/pr-reviews/reports
  base_clone_dir: ~/pr-reviews/flutter-base
  agent_github_token_env: GH_TOKEN_READONLY
  orchestrator_github_token_env: GH_TOKEN_WRITE
  use_skip_permissions: false
  sandbox: true
"""


def _write(tmp_path, text):
  path = tmp_path / "config.yaml"
  path.write_text(textwrap.dedent(text))
  return path


def test_loads_valid_config(tmp_path):
  cfg = load_config(_write(tmp_path, VALID))
  assert cfg.repo == "flutter/flutter"
  assert cfg.mode == "auto-stage-review"
  assert cfg.use_skip_permissions is False
  assert cfg.batch_size == 5
  assert cfg.labels_to_skip == ("waiting for response",)
  assert str(cfg.review_file_dir).startswith("/")  # ~ expanded


def test_rejects_skip_permissions_true(tmp_path):
  text = VALID.replace(
    "use_skip_permissions: false", "use_skip_permissions: true"
  )
  with pytest.raises(ConfigError, match="use_skip_permissions is true"):
    load_config(_write(tmp_path, text))


def test_rejects_non_bool_skip_permissions(tmp_path):  # F44
  # A quoted "false" (a string, not a bool) gets the clear "must be a boolean"
  # message, not the alarming nuclear-flag error.
  text = VALID.replace(
    "use_skip_permissions: false", 'use_skip_permissions: "false"'
  )
  with pytest.raises(ConfigError, match="must be a YAML boolean"):
    load_config(_write(tmp_path, text))


def test_rejects_identical_token_envs(tmp_path):
  text = VALID.replace(
    "orchestrator_github_token_env: GH_TOKEN_WRITE",
    "orchestrator_github_token_env: GH_TOKEN_READONLY",
  )
  with pytest.raises(ConfigError, match="DIFFERENT"):
    load_config(_write(tmp_path, text))


def test_rejects_unknown_mode(tmp_path):
  text = VALID.replace("mode: auto-stage-review", "mode: auto-submit")
  with pytest.raises(ConfigError, match="mode"):
    load_config(_write(tmp_path, text))


def test_rejects_bad_batch_size(tmp_path):
  text = VALID.replace("batch_size: 5", "batch_size: 0")
  with pytest.raises(ConfigError, match="batch_size"):
    load_config(_write(tmp_path, text))


def test_rejects_missing_required_key(tmp_path):
  text = VALID.replace("  repo: flutter/flutter\n", "")
  with pytest.raises(ConfigError, match="repo"):
    load_config(_write(tmp_path, text))


def test_agy_model_defaults_to_none(tmp_path):
  assert load_config(_write(tmp_path, VALID)).agy_model is None


def test_loads_agy_model_when_set(tmp_path):
  text = VALID + '  agy_model: "Gemini 3.5 Flash (Medium)"\n'
  assert (
    load_config(_write(tmp_path, text)).agy_model == "Gemini 3.5 Flash (Medium)"
  )


def test_rejects_non_string_agy_model(tmp_path):  # F43
  text = VALID + "  agy_model: 123\n"
  with pytest.raises(ConfigError, match="agy_model"):
    load_config(_write(tmp_path, text))


def test_resolve_token_strips_whitespace(monkeypatch):  # F41
  monkeypatch.setenv("TOK", "  ghp_abc\n")
  assert resolve_token("TOK") == "ghp_abc"


def test_resolve_token_rejects_whitespace_only(monkeypatch):  # F41
  monkeypatch.setenv("TOK", "   \n")
  with pytest.raises(ConfigError):
    resolve_token("TOK")


def test_relative_dirs_resolve_next_to_config_not_cwd(tmp_path, monkeypatch):
  # A relative dir path must resolve against the config file's directory, even
  # when the CLI is invoked from an unrelated cwd (the hardening).
  text = (
    VALID.replace(
      "review_file_dir: ~/pr-reviews/pending", "review_file_dir: gen/pending"
    )
    .replace("report_dir: ~/pr-reviews/reports", "report_dir: gen/reports")
    .replace(
      "base_clone_dir: ~/pr-reviews/flutter-base", "base_clone_dir: gen/base"
    )
  )
  cfg_path = _write(tmp_path, text)
  config_dir = cfg_path.resolve().parent
  other_cwd = tmp_path / "elsewhere"
  other_cwd.mkdir()
  monkeypatch.chdir(other_cwd)

  cfg = load_config(cfg_path)

  assert cfg.review_file_dir == config_dir / "gen" / "pending"
  assert cfg.base_clone_dir == config_dir / "gen" / "base"
  # the relative DEFAULT for worktree_dir also anchors to the config dir
  assert cfg.worktree_dir == config_dir / "pr-reviews-generated" / "worktrees"
  # crucially NOT resolved relative to the (foreign) cwd
  assert other_cwd.resolve() not in cfg.review_file_dir.parents


def test_tilde_paths_are_not_rewritten_under_config_dir(tmp_path):
  # Backward-compat: ~-expanded (absolute) paths stay under HOME, never get
  # re-anchored to the config dir.
  cfg = load_config(_write(tmp_path, VALID))
  assert str(cfg.review_file_dir).startswith("/")  # ~ expanded -> absolute
  assert tmp_path not in cfg.review_file_dir.parents


# --- Global agy settings union check (plan §1 Layer 1; finding F12) ----------
#
# Advisory: returns the broadening (uncountered) global allow grants; the caller
# warns. Detection must be COMPLETE (every uncountered grant) and must never
# crash or hard-fail a legitimate run.


def _write_global(tmp_path, allow):
  path = tmp_path / "global-settings.json"
  path.write_text(json.dumps({"permissions": {"allow": allow}}))
  return path


def test_global_agy_settings_empty_when_absent(tmp_path):
  assert check_global_agy_settings(tmp_path / "nope.json") == []


def test_global_agy_settings_excludes_countered_and_owned(tmp_path):
  # read_url/unsandboxed are countered by the project deny; command(git) is one
  # the agent already holds — none broadens the agent.
  path = _write_global(
    tmp_path, ["read_url(*)", "unsandboxed(x)", "command(git)"]
  )
  assert check_global_agy_settings(path, project_allow=("command(git)",)) == []


def test_global_agy_settings_flags_arbitrary_exec_commands(tmp_path):
  # The threat class, not just gh: arbitrary-exec command targets are caught.
  path = _write_global(
    tmp_path, ["command(bash)", "command(curl)", "command(python)"]
  )
  assert set(check_global_agy_settings(path)) == {
    "command(bash)",
    "command(curl)",
    "command(python)",
  }


def test_global_agy_settings_flags_gh_and_wildcards_and_writes(tmp_path):
  path = _write_global(
    tmp_path,
    ["command(gh)", "command(*)", "write_file(*)", "write_file(/etc/x)"],
  )
  assert set(check_global_agy_settings(path)) == {
    "command(gh)",
    "command(*)",
    "write_file(*)",
    "write_file(/etc/x)",
  }


def test_global_agy_settings_no_crash_on_malformed(tmp_path):
  # A top-level array and non-string/garbled allow entries must no-op, not crash
  # (otherwise an uncaught error would abort the run — the regression we avoid).
  arr = tmp_path / "arr.json"
  arr.write_text("[1, 2, 3]")
  assert check_global_agy_settings(arr) == []

  mixed = tmp_path / "mixed.json"
  mixed.write_text(
    json.dumps({"permissions": {"allow": [123, None, "x", "command(bash)"]}})
  )
  assert check_global_agy_settings(mixed) == ["command(bash)"]
