"""Per-run Markdown report — the morning triage dashboard (plan §6.2).

Produced on every run that gets past pre-flight, including partial failure
("reviewed 7, 2 failed" beats all-or-nothing). It's a jump-off index: open it,
see what was reviewed, click straight through to each staged inline comment.
"""

from __future__ import annotations

import datetime
import pathlib
import re

from pr_review.atomicio import write_text_atomic

_PIPELINE_LABEL = {"A": "requested review", "B": "silent update"}

_WS_RE = re.compile(r"\s+")
_MD_SPECIAL = str.maketrans({c: "\\" + c for c in "\\`*_[]<>|"})


def _md_inline(text: str) -> str:
  """Make untrusted text safe to drop into a Markdown line (finding F33).

  The PR title is author-controlled. Flatten newlines/control chars to single
  spaces (so a title can't inject a new heading/list line) and escape inline
  Markdown metacharacters (so it can't render as a link/emphasis/code/HTML) in
  the human triage report.
  """
  printable = "".join(ch if ch.isprintable() else " " for ch in text or "")
  flattened = _WS_RE.sub(" ", printable).strip()
  return flattened.translate(_MD_SPECIAL)


def _pr_url(repo: str, number: int) -> str:
  return f"https://github.com/{repo}/pull/{number}"


def _render_pr(outcome, repo: str) -> list:
  ref = outcome.ref
  result = outcome.result
  pipeline = _PIPELINE_LABEL.get(ref.pipeline, ref.pipeline or "explicit")
  title = _md_inline(result.pr.title if result and result.pr else ref.title)
  lines = [
    f"### [#{ref.number}]({_pr_url(repo, ref.number)}) — {title}  ·  "
    f"_{pipeline}_"
  ]
  if result and result.staging is not None:
    staging = result.staging
    note = f"posted {staging.posted_comments}"
    if staging.dropped:
      note += f", dropped {len(staging.dropped)}"
    if staging.degraded:
      note += f", degraded {len(staging.degraded)}"
    lines.append(f"- **Staged pending review:** {staging.review_url} ({note})")
    for url in staging.comment_urls:
      lines.append(f"  - {url}")
  elif result:
    lines.append(
      f"- Review written to `{result.review_file_path}` — run "
      f"`stage-review {ref.number}` to stage (manual mode)."
    )
  lines.append("")
  return lines


def render_report(run, *, timestamp: str) -> str:
  """Render the run report as Markdown (plan §6.2)."""
  succeeded = [o for o in run.outcomes if o.result is not None]
  failed = [o for o in run.outcomes if o.failure is not None]
  skipped = [o for o in run.outcomes if o.skipped is not None]
  count_a = sum(1 for r in run.backlog if r.pipeline == "A")
  count_b = sum(1 for r in run.backlog if r.pipeline == "B")
  count_x = len(run.backlog) - count_a - count_b
  backlog_desc = f"A={count_a} (requested) + B={count_b} (silent updates)"
  if count_x:
    backlog_desc += f" + {count_x} explicit"

  lines = [
    f"# PR Review Run — {timestamp}",
    "",
    f"- **Repo:** {run.repo}",
    f"- **Mode:** {run.mode}",
    f"- **Candidates:** {backlog_desc} = {run.total} total",
    f"- **Processed this run:** {len(run.outcomes)}"
    + (
      f" ({run.skipped_for_limit} skipped via --limit)"
      if run.skipped_for_limit
      else ""
    ),
    f"- **Succeeded:** {len(succeeded)} · **Failed:** {len(failed)}"
    + (f" · **Already staged:** {len(skipped)}" if skipped else ""),
    "",
  ]

  if succeeded:
    lines.append("## Reviewed")
    lines.append("")
    for outcome in succeeded:
      lines.extend(_render_pr(outcome, run.repo))

  if failed:
    lines.append("## Failures")
    lines.append("")
    for outcome in failed:
      # The failure string can embed attacker-controlled text (e.g. a diff
      # filename in a parse error), so sanitize it like the title (F33).
      lines.append(
        f"- [#{outcome.ref.number}]({_pr_url(run.repo, outcome.ref.number)}) "
        f"— {_md_inline(outcome.failure)}"
      )
    lines.append("")

  if skipped:
    lines.append("## Already staged (skipped)")
    lines.append("")
    for outcome in skipped:
      lines.append(
        f"- [#{outcome.ref.number}]({_pr_url(run.repo, outcome.ref.number)}) "
        f"— {_md_inline(outcome.skipped)}"  # uniform: all per-PR text sanitized
      )
    lines.append("")

  return "\n".join(lines) + "\n"


def write_report(report_dir, run, *, timestamp=None) -> pathlib.Path:
  """Render and write the report to ``report_dir/report-<timestamp>.md`` (§6.2).

  The filename includes the time, not just the date, so two runs on the same day
  (a re-run after a partial failure, or a manual run after the scheduled one)
  don't silently overwrite each other's report (finding F34).
  """
  if timestamp is None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  dest_dir = pathlib.Path(report_dir)
  dest_dir.mkdir(parents=True, exist_ok=True)
  slug = timestamp.replace(" ", "-").replace(":", "")  # 2026-05-31-090000
  dest = dest_dir / f"report-{slug}.md"
  write_text_atomic(dest, render_report(run, timestamp=timestamp))  # F35
  return dest
