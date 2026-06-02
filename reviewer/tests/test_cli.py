"""Tests for CLI argument parsing helpers and clean error surfacing."""

import types

import typer
from typer.testing import CliRunner

from pr_review import cli
from pr_review.batch import BatchRun, PROutcome
from pr_review.cli import _explicit_backlog
from pr_review.errors import ConfigError, PreflightError
from pr_review.github import GithubError
from pr_review.models import PRRef
from pr_review.worktree import WorktreeError


def _raise(exc):
  raise exc


def _backlog_app():
  app = typer.Typer()
  app.command()(cli._review_backlog)
  return app


def _fake_cfg():
  return types.SimpleNamespace(
    repo="o/r",
    username="u",
    labels_to_skip=(),
    batch_size=5,
    mode="auto-stage-review",
    report_dir="/tmp",
    agent_github_token_env="A",
    orchestrator_github_token_env="B",
  )


def test_explicit_backlog_dedups_preserving_order():
  # --prs 5,5 must not create two refs for #5 (they collide on the pr-5
  # worktree dir and double-stage; review finding F06).
  assert [r.number for r in _explicit_backlog("5,5")] == [5]
  assert [r.number for r in _explicit_backlog("3,1,3,2,1")] == [3, 1, 2]


def test_explicit_backlog_dedups_equivalent_spellings():
  # "5", "05", " 5 ", "+5" all parse to the same int and the same pr-5 dir.
  assert [r.number for r in _explicit_backlog("5,05, 5 ,+5")] == [5]


def test_explicit_backlog_skips_empty_parts():
  assert [r.number for r in _explicit_backlog("1,,  ,2, ,1")] == [1, 2]
  refs = _explicit_backlog("1,2")
  assert [r.pipeline for r in refs] == ["explicit", "explicit"]


# --- Fatal setup/run errors surface cleanly, never as a traceback (F15, F16) -


def test_review_backlog_missing_token_is_clean_not_traceback(monkeypatch):
  monkeypatch.setattr(cli, "_load_cfg", lambda config: _fake_cfg())
  monkeypatch.setattr(
    cli, "resolve_token", lambda env: _raise(ConfigError("token X is not set"))
  )
  result = CliRunner().invoke(_backlog_app(), [])
  assert result.exit_code == 1
  assert "PRE-FLIGHT FAILED (ConfigError)" in result.output  # type tag (A)
  assert "token X is not set" in result.output


def test_review_backlog_query_github_error_is_clean(monkeypatch):
  monkeypatch.setattr(cli, "_load_cfg", lambda config: _fake_cfg())
  monkeypatch.setattr(cli, "resolve_token", lambda env: "tok")
  monkeypatch.setattr(cli, "GitHub", lambda *a, **k: object())
  monkeypatch.setattr(
    cli.queries,
    "assemble_backlog",
    lambda *a, **k: _raise(GithubError("gh search failed")),
  )
  result = CliRunner().invoke(_backlog_app(), [])
  assert result.exit_code == 1
  assert "PRE-FLIGHT FAILED (GithubError)" in result.output  # type tag (A)
  assert "gh search failed" in result.output


def test_review_backlog_worktree_error_is_clean(monkeypatch):
  monkeypatch.setattr(cli, "_load_cfg", lambda config: _fake_cfg())
  monkeypatch.setattr(cli, "resolve_token", lambda env: "tok")
  monkeypatch.setattr(cli, "GitHub", lambda *a, **k: object())
  monkeypatch.setattr(
    cli.queries,
    "filter_unstaged",
    lambda gh, repo, backlog, user: (backlog, []),
  )
  monkeypatch.setattr(cli.preflight, "preflight", lambda cfg, repo: None)
  monkeypatch.setattr(
    cli.batch,
    "run_backlog",
    lambda *a, **k: _raise(WorktreeError("base clone at X is not a clone")),
  )
  result = CliRunner().invoke(_backlog_app(), ["--prs", "5"])  # skip the query
  assert result.exit_code == 1
  assert "PRE-FLIGHT FAILED (WorktreeError)" in result.output  # type tag (A)
  assert "base clone" in result.output


def test_batch_summary_emits_machine_status_line(capsys):
  # F47: the scheduler classifies off this stable token, not the human wording.
  def _ref(n):
    return PRRef(
      number=n, title="t", url="u", author="a", updated_at="d", pipeline="A"
    )

  run = BatchRun(
    repo="o/r",
    mode="auto-stage-review",
    backlog=[],
    total=2,
    skipped_for_limit=0,
    outcomes=[
      PROutcome(ref=_ref(1), result=None, failure="boom"),
      PROutcome(ref=_ref(2), result=None, failure=None, skipped="already"),
    ],
  )
  cli._print_batch_summary(run, "/tmp/report.md")
  out = capsys.readouterr().out
  assert "PR-REVIEW-STATUS reviewed=0 failed=1 already-staged=1" in out


def test_review_backlog_nothing_to_review_emits_token(monkeypatch):
  # F47: the empty-backlog path must emit the machine token too.
  monkeypatch.setattr(cli, "_load_cfg", lambda config: _fake_cfg())
  monkeypatch.setattr(cli, "resolve_token", lambda env: "tok")
  monkeypatch.setattr(cli, "GitHub", lambda *a, **k: object())
  monkeypatch.setattr(cli.queries, "assemble_backlog", lambda *a, **k: ([], []))
  monkeypatch.setattr(
    cli.queries, "filter_unstaged", lambda gh, repo, backlog, user: ([], [])
  )
  result = CliRunner().invoke(_backlog_app(), [])
  assert result.exit_code == 0
  assert "PR-REVIEW-STATUS nothing-to-review" in result.output


def test_review_pr_preflight_failure_is_tagged(monkeypatch):
  # Covers the single-PR PRE-FLIGHT path (F03 wiring) + the type tag (A) at a
  # second site distinct from _review_backlog.
  monkeypatch.setattr(cli, "_load_cfg", lambda config: _fake_cfg())
  monkeypatch.setattr(
    cli.preflight,
    "preflight",
    lambda cfg, repo: _raise(PreflightError("required tool 'gh' not found")),
  )
  app = typer.Typer()
  app.command()(cli._review_pr)
  result = CliRunner().invoke(app, ["5"])
  assert result.exit_code == 1
  assert "PRE-FLIGHT FAILED (PreflightError)" in result.output
  assert "required tool 'gh' not found" in result.output


def test_review_pr_already_staged_is_skip(monkeypatch):  # L2 (guards the order)
  monkeypatch.setattr(cli, "_load_cfg", lambda config: _fake_cfg())
  monkeypatch.setattr(cli, "resolve_token", lambda env: "tok")
  monkeypatch.setattr(cli.preflight, "preflight", lambda cfg, repo: None)
  monkeypatch.setattr(
    cli.preflight, "verify_read_only_token", lambda *a, **k: None
  )
  monkeypatch.setattr(
    cli.review_unit,
    "review_single_pr",
    lambda *a, **k: _raise(cli.staging.AlreadyStagedError("already pending")),
  )
  app = typer.Typer()
  app.command()(cli._review_pr)
  result = CliRunner().invoke(app, ["5"])
  assert result.exit_code == 0  # benign skip, not a failure
  assert "already staged" in result.output.lower()


def test_stage_review_already_staged_is_skip(monkeypatch):  # L1
  monkeypatch.setattr(cli, "_load_cfg", lambda config: _fake_cfg())
  monkeypatch.setattr(
    cli.preflight, "check_command_tooling", lambda tools: None
  )
  monkeypatch.setattr(
    cli.review_unit,
    "stage_review",
    lambda *a, **k: _raise(cli.staging.AlreadyStagedError("already pending")),
  )
  app = typer.Typer()
  app.command()(cli._stage_review)
  result = CliRunner().invoke(app, ["5"])
  assert result.exit_code == 0  # mirrors review-pr, not exit 1
  assert "already staged" in result.output.lower()


def test_broad_except_logs_debug_traceback(monkeypatch, caplog):
  # F46: a genuine bug behind the friendly one-line failure is logged at DEBUG
  # (with the traceback), so it is diagnosable without being shown by default.
  monkeypatch.setattr(cli, "_load_cfg", lambda config: _fake_cfg())
  monkeypatch.setattr(
    cli.preflight, "check_command_tooling", lambda tools: None
  )
  monkeypatch.setattr(
    cli.review_unit,
    "stage_review",
    lambda *a, **k: _raise(RuntimeError("boom in staging internals")),
  )
  app = typer.Typer()
  app.command()(cli._stage_review)
  with caplog.at_level("DEBUG", logger="pr_review.cli"):
    result = CliRunner().invoke(app, ["5"])
  assert result.exit_code == 1
  assert "STAGE FAILED" in result.output  # friendly message still shown
  assert any(
    r.levelname == "DEBUG" and r.exc_info for r in caplog.records
  )  # full traceback captured at DEBUG
