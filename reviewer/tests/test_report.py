"""Tests for the Markdown run report (no network)."""

from pr_review import report
from pr_review.batch import BatchRun, PROutcome
from pr_review.models import PRMeta, PRRef, ReviewFile
from pr_review.review_unit import ReviewResult
from pr_review.staging import StagingResult


def _success(num, pipeline="A"):
  ref = PRRef(
    number=num,
    title=f"Title {num}",
    url="u",
    author="a",
    updated_at="d",
    pipeline=pipeline,
  )
  pr = PRMeta(
    number=num,
    title=f"Title {num}",
    url="u",
    head_sha="sha",
    head_ref="h",
    base_ref="master",
    author="a",
    state="OPEN",
  )
  staging = StagingResult(
    review_id=1,
    review_url=f"https://x/{num}#rev",
    posted_comments=2,
    comment_urls=(f"https://x/{num}#c1",),
    dropped=(),
    degraded=(),
  )
  result = ReviewResult(
    pr=pr,
    kind="fresh",
    anchors=10,
    review=ReviewFile(summary="s"),
    review_file_path="/p.json",
    staging=staging,
  )
  return PROutcome(ref=ref, result=result, failure=None)


def _failure(num):
  ref = PRRef(
    number=num,
    title=f"Title {num}",
    url="u",
    author="a",
    updated_at="d",
    pipeline="A",
  )
  return PROutcome(ref=ref, result=None, failure="agy quota exhausted")


def _skipped(num):
  ref = PRRef(
    number=num,
    title=f"Title {num}",
    url="u",
    author="a",
    updated_at="d",
    pipeline="A",
  )
  return PROutcome(
    ref=ref, result=None, failure=None, skipped="already staged (skip)"
  )


def test_report_renders_counts_links_and_failures():
  o1 = _success(1)  # pipeline A
  o2 = _failure(2)  # pipeline A
  b_ref = PRRef(
    number=3, title="t3", url="u", author="a", updated_at="d", pipeline="B"
  )
  run = BatchRun(
    repo="flutter/flutter",
    mode="auto-stage-review",
    backlog=[o1.ref, o2.ref, b_ref],  # 2 A + 1 B; the B was --limit-skipped
    total=3,
    skipped_for_limit=1,
    outcomes=[o1, o2],
  )
  md = report.render_report(run, timestamp="2026-05-31 09:00:00")
  assert "flutter/flutter" in md
  assert "A=2 (requested) + B=1" in md  # counts derived from backlog pipelines
  assert "Succeeded:** 1" in md and "Failed:** 1" in md
  assert "https://github.com/flutter/flutter/pull/1" in md
  assert "https://x/1#rev" in md  # staged review url
  assert "https://x/1#c1" in md  # inline comment deep link
  assert "agy quota exhausted" in md  # failure reason
  assert "skipped via --limit" in md


def test_report_renders_already_staged_skips(tmp_path):
  # F24: an already-staged PR shows in its own section, not as a failure.
  o1 = _success(1)
  o2 = _skipped(2)
  run = BatchRun(
    repo="o/r",
    mode="auto-stage-review",
    backlog=[o1.ref, o2.ref],
    total=2,
    skipped_for_limit=0,
    outcomes=[o1, o2],
  )
  md = report.render_report(run, timestamp="2026-05-31 09:00:00")
  assert "Already staged" in md
  assert "already staged (skip)" in md
  assert "Failed:** 0" in md  # the skip is NOT counted as a failure


def test_md_inline_flattens_and_escapes():  # F33
  from pr_review.report import _md_inline

  assert _md_inline("plain title") == "plain title"
  assert "\n" not in _md_inline("evil\n## Failures")  # no injected line
  out = _md_inline("a [x](u) *b* `c`")
  assert "\\[x\\]" in out and "\\*b\\*" in out and "\\`c\\`" in out


def test_report_sanitizes_malicious_pr_title():  # F33
  pr = PRMeta(
    number=9,
    title="pwn\n## Injected [x](http://evil)",
    url="u",
    head_sha="s",
    head_ref="h",
    base_ref="master",
    author="a",
    state="OPEN",
  )
  result = ReviewResult(
    pr=pr,
    kind="fresh",
    anchors=1,
    review=ReviewFile(summary="s"),
    review_file_path="/p.json",
    staging=None,
  )
  ref = PRRef(
    number=9, title="t", url="u", author="a", updated_at="d", pipeline="A"
  )
  run = BatchRun(
    repo="o/r",
    mode="auto-stage-review",
    backlog=[ref],
    total=1,
    skipped_for_limit=0,
    outcomes=[PROutcome(ref=ref, result=result, failure=None)],
  )
  md = report.render_report(run, timestamp="2026-05-31 09:00:00")
  assert "\n## Injected" not in md  # newline-injected heading neutralized
  assert "\\[x\\]" in md  # markdown link escaped
  assert "pwn" in md  # title text still present (flattened)


def test_report_sanitizes_failure_message():  # F33 (failure path)
  # A diff parse error embeds the attacker-chosen filename; it must be escaped
  # in the Failures section just like the title.
  ref = PRRef(
    number=7, title="t", url="u", author="a", updated_at="d", pipeline="A"
  )
  msg = "could not parse diff for weird](http://evil) <script>*x*: bad hunk"
  run = BatchRun(
    repo="o/r",
    mode="auto-stage-review",
    backlog=[ref],
    total=1,
    skipped_for_limit=0,
    outcomes=[PROutcome(ref=ref, result=None, failure=msg)],
  )
  md = report.render_report(run, timestamp="2026-05-31 09:00:00")
  assert "\\]" in md and "\\<script\\>" in md and "\\*x\\*" in md  # escaped
  assert "<script>" not in md  # raw HTML neutralized


def test_write_report_creates_dated_file(tmp_path):
  run = BatchRun(
    repo="o/r",
    mode="auto-stage-review",
    backlog=[],
    total=0,
    skipped_for_limit=0,
    outcomes=[],
  )
  path = report.write_report(tmp_path, run, timestamp="2026-05-31 09:00:00")
  # F34: filename includes the time so same-day runs don't overwrite.
  assert path.exists() and path.name == "report-2026-05-31-090000.md"
