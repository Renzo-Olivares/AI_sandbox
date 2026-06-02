"""Serialize the per-PR context bundle into the worktree (plan §4.5).

agy has no ``--context`` flag and stdin is consumed by ``/dev/null``, so the
orchestrator writes the bundle (diff, prior comments, force-push note) to a file
inside the worktree and the prompt tells the agent to read it. The agent writes
its review JSON to a sibling path. Both live under ``<worktree>/.pr-review/`` so
they are inside the workspace (agent writes there are auto-allowed) — the review
file is copied out before the worktree is torn down.
"""

from __future__ import annotations

import json
import pathlib
import shutil

from pr_review.diff_anchors import render_annotated_patch
from pr_review.models import ReviewContext

_OUTPUT_SUBDIR = ".pr-review"


def _prepare_output_dir(worktree: pathlib.Path) -> pathlib.Path:
  """Return a clean, freshly-created ``<worktree>/.pr-review`` (anti-injection).

  The worktree is the PR's *untrusted* checkout (plan §1). A PR could ship
  ``.pr-review`` as a symlink, a regular file, or a directory containing
  symlinks so that our ``mkdir``/``write_text`` follow links and the context /
  review / style-guide files land OUTSIDE the worktree (an arbitrary-file
  overwrite primitive — the sibling of the ``.gemini`` escape). So we remove any
  pre-existing ``.pr-review`` entirely — the link/file/dir itself, never
  following a link to its target — and create a fresh real dir, so the writes
  below land in a known-clean location strictly inside the worktree.
  """
  out_dir = worktree / _OUTPUT_SUBDIR
  if out_dir.is_symlink() or out_dir.is_file():
    out_dir.unlink()  # remove the link/file, never follow it to its target
  elif out_dir.is_dir():
    shutil.rmtree(out_dir)  # nuke a PR-shipped dir (may contain symlinks)
  if out_dir.exists() or out_dir.is_symlink():
    raise PermissionError(
      f"refusing to write the context bundle: a {_OUTPUT_SUBDIR} survived "
      f"cleaning at {out_dir} (PR-injected symlink or undeletable entry?). It "
      "could redirect our writes outside the worktree (plan §1)."
    )
  out_dir.mkdir(parents=True)
  if worktree.resolve() not in out_dir.resolve().parents:
    raise PermissionError(
      f"refusing to write the context bundle outside the worktree: "
      f"{out_dir.resolve()}"
    )
  return out_dir


def write_context_file(
  worktree, ctx: ReviewContext, repo: str
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path | None]:
  """Write the context bundle + style guide; return the three worktree paths.

  Args:
    worktree: the PR's worktree.
    ctx: the assembled review context.
    repo: ``owner/name`` (for the agent's reference).

  Returns:
    ``(context_path, output_path, style_guide_path)`` — where the bundle was
    written, where the agent should write its review JSON, and where the
    trusted style guide was written (``None`` when the lens is disabled / the
    guide was not fetched). The guide is written as its own ``.md`` file (not
    embedded in the JSON) so it stays readable; the prompt points the agent at
    it as the ONLY authoritative guide (the in-tree copy is untrusted, §1).
  """
  out_dir = _prepare_output_dir(pathlib.Path(worktree))

  bundle = {
    "repo": repo,
    "pr": {
      "number": ctx.pr.number,
      "title": ctx.pr.title,
      "url": ctx.pr.url,
      "head_sha": ctx.pr.head_sha,
      "base_ref": ctx.pr.base_ref,
      "author": ctx.pr.author,
    },
    "kind": ctx.kind,
    "force_pushed": ctx.force_pushed,
    "touches_style_guide": ctx.touches_style_guide,
    "diff": [
      {
        "filename": f.filename,
        "status": f.status,
        "annotated_diff": render_annotated_patch(f),
      }
      for f in ctx.diff_files
    ],
  }
  if ctx.kind == "rereview":
    bundle["my_prior_reviews"] = [
      {"body": r.body, "state": r.state, "submitted_at": r.submitted_at}
      for r in ctx.prior_reviews
    ]
    bundle["my_prior_inline_comments"] = [
      {"path": c.path, "line": c.line, "side": c.side, "body": c.body}
      for c in ctx.prior_comments
    ]

  context_path = out_dir / "context.json"
  context_path.write_text(json.dumps(bundle, indent=2))
  output_path = out_dir / "review.json"

  style_guide_path = None
  if ctx.style_guide_text is not None:
    style_guide_path = out_dir / "style-guide.md"
    style_guide_path.write_text(ctx.style_guide_text)

  return context_path, output_path, style_guide_path
