"""Tests for pre-flight checks (no network where possible)."""

import json
import subprocess
import types

import pytest

from pr_review import preflight
from pr_review.errors import PreflightError


def test_layer1_rendered_config_is_precedence_valid():
  # The settings we WRITE per-worktree must pass the precedence validator.
  preflight._check_layer1_renders_valid()  # must not raise


def test_check_writable_ok(tmp_path):
  preflight._check_writable("x", tmp_path / "subdir")  # creates + probes


def _stub_gh(monkeypatch, *, returncode, stderr):
  def fake_run(*args, **kwargs):
    return subprocess.CompletedProcess(
      args, returncode=returncode, stdout="", stderr=stderr
    )

  monkeypatch.setattr(preflight.subprocess, "run", fake_run)


def test_verify_read_only_passes_when_write_denied(monkeypatch):
  # A plain authorization 403 is the desired "denied" outcome.
  _stub_gh(monkeypatch, returncode=1, stderr="gh: Forbidden (HTTP 403)")
  preflight.verify_read_only_token("o/r", 1, "ro-tok")  # must not raise


def test_verify_read_only_passes_on_resource_not_accessible(monkeypatch):
  # gh's real 403 phrasing for a read-only PAT must count as "denied".
  _stub_gh(
    monkeypatch,
    returncode=1,
    stderr="gh: Resource not accessible by personal access token (HTTP 403)",
  )
  preflight.verify_read_only_token("o/r", 1, "ro-tok")  # must not raise


def test_verify_read_only_raises_on_network_error(monkeypatch):
  # A transient failure with NO 403 must NOT be mistaken for "write denied" —
  # it leaves write-capability unproven (review finding F02/F60).
  _stub_gh(monkeypatch, returncode=1, stderr="dial tcp: connection refused")
  with pytest.raises(PreflightError, match="could not prove"):
    preflight.verify_read_only_token("o/r", 1, "ro-tok")


def test_verify_read_only_raises_on_404_and_ignores_spurious_403(monkeypatch):
  # A bad/closed PR is inconclusive, not a write denial — and a PR number whose
  # digits happen to contain "403" must not be mistaken for an HTTP 403.
  _stub_gh(
    monkeypatch, returncode=1, stderr="gh: Not Found (HTTP 404) for pull/4031"
  )
  with pytest.raises(PreflightError, match="could not prove"):
    preflight.verify_read_only_token("o/r", 4031, "ro-tok")


def test_verify_read_only_raises_on_secondary_rate_limit_403(monkeypatch):
  # GitHub's secondary rate limit is HTTP 403 (not 429); a write-capable token
  # rate-limited at probe time must NOT pass the gate (review finding F02).
  _stub_gh(
    monkeypatch,
    returncode=1,
    stderr="You have exceeded a secondary rate limit (HTTP 403)",
  )
  with pytest.raises(PreflightError, match="could not prove"):
    preflight.verify_read_only_token("o/r", 1, "ro-tok")


def test_verify_read_only_raises_on_429(monkeypatch):
  _stub_gh(
    monkeypatch, returncode=1, stderr="API rate limit exceeded (HTTP 429)"
  )
  with pytest.raises(PreflightError, match="could not prove"):
    preflight.verify_read_only_token("o/r", 1, "ro-tok")


def test_verify_read_only_raises_on_401(monkeypatch):
  # An expired/invalid token (401) is inconclusive, not a write denial.
  _stub_gh(monkeypatch, returncode=1, stderr="gh: Bad credentials (HTTP 401)")
  with pytest.raises(PreflightError, match="could not prove"):
    preflight.verify_read_only_token("o/r", 1, "ro-tok")


def test_verify_read_only_probe_timeout_is_preflight_error(monkeypatch):  # M1
  # A hung probe must surface as PreflightError, not a raw TimeoutExpired.
  def boom(*a, **k):
    raise subprocess.TimeoutExpired("gh", 60)

  monkeypatch.setattr(preflight.subprocess, "run", boom)
  with pytest.raises(PreflightError, match="could not run"):
    preflight.verify_read_only_token("o/r", 1, "ro-tok")


def test_verify_read_only_probe_oserror_is_preflight_error(monkeypatch):  # M1
  def boom(*a, **k):
    raise OSError("connection reset")

  monkeypatch.setattr(preflight.subprocess, "run", boom)
  with pytest.raises(PreflightError, match="could not run"):
    preflight.verify_read_only_token("o/r", 1, "ro-tok")


def test_preflight_warns_but_does_not_fail_on_broad_global_settings(
  tmp_path, monkeypatch, caplog
):
  # F12: a broad GLOBAL agy grant must WARN, never abort preflight (the prior
  # design hard-failed it). Stub the orthogonal checks to isolate the branch.
  for name in (
    "resolve_token",
    "_check_tooling",
    "_check_writable",
    "_check_base_clone",
    "_check_layer1_renders_valid",
  ):
    monkeypatch.setattr(preflight, name, lambda *a, **k: None)
  global_settings = tmp_path / "settings.json"
  global_settings.write_text(
    json.dumps({"permissions": {"allow": ["command(gh)"]}})
  )
  cfg = types.SimpleNamespace(
    agent_github_token_env="A",
    orchestrator_github_token_env="B",
    review_file_dir=tmp_path,
    report_dir=tmp_path,
    worktree_dir=tmp_path,
    base_clone_dir=tmp_path / "base",
    agy_settings_path=global_settings,
  )
  with caplog.at_level("WARNING"):
    preflight.preflight(cfg, "o/r")  # must NOT raise
  assert any("command(gh)" in r.message for r in caplog.records)


def test_verify_read_only_delete_has_timeout_and_logs_cleanup_failure(
  monkeypatch, caplog
):
  # F18: the cleanup DELETE must carry a timeout (a hang here once froze the
  # run), and a cleanup failure must be logged (naming the stray review), not
  # mask the security failure.
  calls = []

  def fake_run(args, **kwargs):
    calls.append((args, kwargs))
    if "POST" in args:
      return subprocess.CompletedProcess(
        args, returncode=0, stdout='{"id": 777}', stderr=""
      )
    raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))  # DELETE hangs

  monkeypatch.setattr(preflight.subprocess, "run", fake_run)
  with caplog.at_level("WARNING"):
    with pytest.raises(PreflightError, match="NOT read-only"):
      preflight.verify_read_only_token("o/r", 1, "ro-tok")
  delete = [(a, k) for a, k in calls if "DELETE" in a]
  assert delete and delete[0][1].get("timeout") == 60  # DELETE is bounded
  assert any("777" in r.message for r in caplog.records)  # stray review named


def test_verify_read_only_fatal_when_write_succeeds(monkeypatch):
  calls = []

  def fake_run(args, **kwargs):
    calls.append(args)
    if "POST" in args:
      return subprocess.CompletedProcess(
        args, returncode=0, stdout='{"id": 999}', stderr=""
      )
    return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

  monkeypatch.setattr(preflight.subprocess, "run", fake_run)
  with pytest.raises(PreflightError, match="NOT read-only"):
    preflight.verify_read_only_token("o/r", 1, "ro-tok")
  # the stray review (id 999) must have been deleted
  assert any("DELETE" in c and "999" in " ".join(c) for c in calls)
