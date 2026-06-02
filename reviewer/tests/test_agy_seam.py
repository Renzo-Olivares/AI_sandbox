"""Tests for the agy invocation seam — the safety-critical argv and env."""

import pathlib
import subprocess

import pytest

from pr_review import agy_seam
from pr_review.agy_seam import AgentError, AgentTimeout


def test_build_argv_is_sandboxed_one_shot_without_skip_or_cwd():
  argv = agy_seam.build_argv("agy", "do review", print_timeout_seconds=120)
  assert argv[0] == "agy"
  assert "--sandbox" in argv  # unconditional; not configurable off
  assert "-p" in argv
  assert "do review" in argv
  i = argv.index("--print-timeout")
  assert argv[i + 1] == "120s"
  # The nuclear flag must never be present (plan §1).
  assert agy_seam.FORBIDDEN_SKIP_PERMISSIONS_FLAG not in argv
  # --cwd is not a flag in agy 1.0.3; cwd is set on the subprocess instead.
  assert "--cwd" not in argv


def test_assert_safe_argv_rejects_skip_flag():
  with pytest.raises(AgentError):
    agy_seam.assert_safe_argv(["agy", agy_seam.FORBIDDEN_SKIP_PERMISSIONS_FLAG])


def test_assert_safe_argv_rejects_skip_flag_equals_form():
  with pytest.raises(AgentError):
    agy_seam.assert_safe_argv(
      ["agy", f"{agy_seam.FORBIDDEN_SKIP_PERMISSIONS_FLAG}=true", "-p", "x"]
    )


def test_build_child_env_scrubs_write_and_forces_readonly(monkeypatch):
  monkeypatch.setenv("GH_TOKEN_WRITE", "WRITE-SECRET")
  monkeypatch.setenv("GH_TOKEN_READONLY", "RO-SECRET")
  monkeypatch.setenv(
    "GH_TOKEN", "WRITE-SECRET"
  )  # ambient could be the write tok
  env = agy_seam.build_child_env("RO-SECRET", "GH_TOKEN_WRITE")
  assert "GH_TOKEN_WRITE" not in env  # write token var removed
  assert "GH_TOKEN_READONLY" not in env  # read token only under canonical names
  assert env["GH_TOKEN"] == "RO-SECRET"  # read-only exposed under canonical var
  assert env["GITHUB_TOKEN"] == "RO-SECRET"
  assert env["TERM"] == "dumb"  # TTY-hang guard
  # The write secret must not survive anywhere in the child env.
  assert "WRITE-SECRET" not in set(env.values())


def test_build_child_env_value_scrubs_secret_under_other_name(monkeypatch):
  monkeypatch.setenv("GH_TOKEN_WRITE", "WRITE-SECRET")
  # the same secret value exported under an UNRELATED var name
  monkeypatch.setenv("SOME_OTHER_VAR", "WRITE-SECRET")
  monkeypatch.setenv("UNRELATED", "keep-me")
  env = agy_seam.build_child_env("RO-SECRET", "GH_TOKEN_WRITE")
  assert "GH_TOKEN_WRITE" not in env
  assert "SOME_OTHER_VAR" not in env  # value-scrubbed (same secret, other name)
  assert "WRITE-SECRET" not in set(env.values())
  assert env.get("UNRELATED") == "keep-me"  # unrelated vars preserved


def test_build_argv_includes_log_file_when_given():
  argv = agy_seam.build_argv("agy", "p", log_file="/tmp/x.log")
  assert "--log-file" in argv
  assert argv[argv.index("--log-file") + 1] == "/tmp/x.log"


def test_build_argv_omits_log_file_by_default():
  assert "--log-file" not in agy_seam.build_argv("agy", "p")


def test_scan_agy_log_detects_quota(tmp_path):
  log = tmp_path / "agy.log"
  log.write_text(
    "I0531 12:00:00 x.go:1] starting\n"
    "E0531 12:00:01 log.go:398] RESOURCE_EXHAUSTED (code 429): "
    "Individual quota reached. Resets in 147h4m42s.\n"
  )
  message = agy_seam.scan_agy_log(log)
  assert message is not None
  assert "quota" in message.lower() and "429" in message


def test_scan_agy_log_clean_returns_none(tmp_path):
  log = tmp_path / "agy.log"
  log.write_text("I0531 12:00:00 x.go:1] all good\n")
  assert agy_seam.scan_agy_log(log) is None


def test_scan_agy_log_ignores_benign_quota_word(tmp_path):
  log = tmp_path / "agy.log"
  log.write_text("x] applyAuthResult: email=a@b.com, quotaProject=\n")
  assert agy_seam.scan_agy_log(log) is None


def test_scan_agy_log_detects_http_429_without_resource_exhausted(tmp_path):
  # Broadened detection (F29): an (HTTP 429) line with different wording.
  log = tmp_path / "agy.log"
  log.write_text("E0531 x] model call failed: Too Many Requests (HTTP 429)\n")
  assert agy_seam.scan_agy_log(log) is not None


def test_scan_agy_log_detects_rate_limit_case_insensitive(tmp_path):
  log = tmp_path / "agy.log"
  log.write_text("E0531 x] Rate Limit exceeded for the selected model\n")
  assert agy_seam.scan_agy_log(log) is not None


def test_scan_agy_log_ignores_benign_rate_limit_mention(tmp_path):  # L3
  # A successful run that merely logs rate-limit HEADERS must NOT be failed.
  log = tmp_path / "agy.log"
  log.write_text("x] X-RateLimit-Remaining=4999, rate limit OK\n")
  assert agy_seam.scan_agy_log(log) is None


def test_run_agent_timeout_surfaces_quota_from_log(tmp_path, monkeypatch):
  # F29: a quota error that ALSO hangs must surface in the timeout message, not
  # be hidden behind a generic "exceeded Ns" timeout.
  def fake_run(*args, **kwargs):
    argv = args[0]
    log_path = argv[argv.index("--log-file") + 1]
    pathlib.Path(log_path).write_text(
      "E0531 x] RESOURCE_EXHAUSTED (code 429): quota reached\n"
    )
    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

  monkeypatch.setattr(agy_seam.subprocess, "run", fake_run)
  with pytest.raises(AgentTimeout) as exc:
    agy_seam.run_agent(
      agy_command="agy",
      prompt="p",
      worktree=tmp_path,
      readonly_token="ro",
      write_token_env_name="W",
    )
  assert "quota" in str(exc.value).lower()
