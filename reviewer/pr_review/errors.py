"""Error taxonomy for the two-layer error model (plan §6.2).

The run distinguishes two failure classes:

  * :class:`PreflightError` — fatal; aborts the whole run before any per-PR
    work. Raised for missing tokens, invalid config, unsafe ``agy`` settings,
    missing tooling, or unwritable directories. Surfaced as a loud, standalone,
    plainly-worded message; no run report is produced.
  * :class:`PerPRError` — non-fatal; the failing PR is recorded with a clear
    reason and skipped, while the rest of the backlog continues. These are
    summarized in the run report, never raised as a crash.
"""

from __future__ import annotations


class PreflightError(Exception):
  """Fatal error that aborts the run before any PR is processed (plan §6.2).

  The message must be plain-English and actionable: say what failed and how to
  fix it. No run report is possible for a pre-flight failure, so the error
  message is the output.
  """


class ConfigError(PreflightError):
  """A pre-flight error specifically about configuration (plan §9)."""


class PerPRError(Exception):
  """Non-fatal failure scoped to a single PR (plan §6.2).

  Recorded with a clear reason and skipped so the run continues. Carries the
  PR number and a human-readable reason for the run report.
  """

  def __init__(
    self, pr_number: int, reason: str, *, cause: BaseException | None = None
  ) -> None:
    """Initialize.

    Args:
      pr_number: the PR this failure is scoped to.
      reason: plain-English, actionable explanation of what failed.
      cause: optional underlying exception, kept for debug logging (not shown
        as the primary message).
    """
    super().__init__(f"PR #{pr_number}: {reason}")
    self.pr_number = pr_number
    self.reason = reason
    self.cause = cause
