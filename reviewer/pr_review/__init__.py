"""Automated PR-review backlog tool (the builder's orchestrator).

This package assembles per-PR review context, provisions isolated git
worktrees, invokes the Antigravity ``agy`` review agent behind a single
swappable seam, and stages the agent's review as an event-less *pending*
GitHub review for human approval.

It never submits a review, and the agent never writes to GitHub at all — the
safety model lives in three independent layers (see the module docstrings and
plan §1).
"""

__version__ = "0.1.0"
