"""Per-PR git worktree provisioning off a shared base clone (plan §4.3).

Each PR is reviewed in its own isolated worktree branched off ONE base clone,
so the (large) repo is cloned once — not once per PR — and concurrent checkouts
never stomp on each other. Worktrees are torn down after each review, including
on failure, so they never accumulate or leave a locked tree behind.

The target repos (``flutter/flutter`` and its forks) are public, so clone/fetch
need no credentials; git runs anonymously here. (A private repo would need
explicit token auth — out of scope, plan §1 keeps tokens on the orchestrator's
GitHub API calls, not git.)
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import shutil
import subprocess

_LOG = logging.getLogger(__name__)

# Namespace for fetched PR-head refs in the base clone — avoids clobbering real
# branches and is unique per PR (forward-compatible with concurrent batches).
_PR_REF = "refs/pr-review/pr-{number}"


class WorktreeError(Exception):
  """A git/worktree operation failed (per-PR failure upstream, plan §6.2)."""


def _git(args: list, *, timeout: int = 600) -> str:
  """Run ``git`` with the given args; raise WorktreeError on failure."""
  try:
    proc = subprocess.run(
      ["git", *args],
      capture_output=True,
      text=True,
      timeout=timeout,
      check=False,
    )
  except FileNotFoundError as e:
    raise WorktreeError("'git' not found on PATH.") from e
  except subprocess.TimeoutExpired as e:
    raise WorktreeError(f"git timed out after {timeout}s: git {args[0]}") from e
  if proc.returncode != 0:
    raise WorktreeError(
      f"git {' '.join(args)} failed (exit {proc.returncode}): "
      f"{proc.stderr.strip()}"
    )
  return proc.stdout


def ensure_base_clone(
  repo: str, base_clone_dir, *, blobless: bool = True, fetch: bool = True
) -> pathlib.Path:
  """Ensure a base clone exists and is reasonably fresh (plan §4.3).

  Clones the repo if absent (a blobless partial clone by default — fast for a
  large repo; blobs are fetched on demand at checkout), otherwise fetches.

  Args:
    repo: ``owner/name``.
    base_clone_dir: where the single shared clone lives.
    blobless: use ``--filter=blob:none`` on first clone.
    fetch: fetch origin when the clone already exists.

  Returns:
    The base clone path.
  """
  base = pathlib.Path(base_clone_dir)
  if (base / ".git").is_dir():
    if not origin_matches(base, repo):
      raise WorktreeError(
        f"base clone at {base} is not a clone of {repo} (its origin points "
        f"elsewhere). Remove it (rm -rf {base}) or set base_clone_dir to a "
        f"fresh path — the tool needs a dedicated {repo} clone (plan §4.3)."
      )
    if fetch:
      _git(["-C", str(base), "fetch", "--prune", "origin"], timeout=1800)
    return base
  base.parent.mkdir(parents=True, exist_ok=True)
  url = f"https://github.com/{repo}.git"
  args = ["clone"]
  if blobless:
    args.append("--filter=blob:none")
  args += [url, str(base)]
  _git(args, timeout=1800)
  return base


def origin_matches(base, repo: str) -> bool:
  """Return whether the clone's ``origin`` remote is the given ``owner/name``.

  Guards against pointing ``base_clone_dir`` at a clone of a *different* repo
  (e.g. a fork), whose PR-head namespace would not contain the target's PRs.
  """
  try:
    url = _git(["-C", str(base), "remote", "get-url", "origin"]).strip()
  except WorktreeError:
    return False
  normalized = url.removesuffix(".git").replace(":", "/")
  return normalized.endswith("/" + repo)


def prune_stale(base) -> None:
  """Defensively prune dangling worktree administrative entries (plan §4.3)."""
  with contextlib.suppress(WorktreeError):
    _git(["-C", str(base), "worktree", "prune"])


def _fetch_pr_head(base, number: int) -> str:
  """Fetch ``pull/<N>/head`` into a private ref (works for fork PRs)."""
  ref = _PR_REF.format(number=number)
  _git(
    [
      "-C",
      str(base),
      "fetch",
      "--force",
      "origin",
      f"pull/{number}/head:{ref}",
    ],
    timeout=1800,
  )
  return ref


def add_worktree(base, worktree_dir, number: int, commit_ish: str):
  """Add a detached worktree for a PR, replacing any stale one (plan §4.3)."""
  wt = pathlib.Path(worktree_dir) / f"pr-{number}"
  if wt.exists():
    remove_worktree(base, wt)
  wt.parent.mkdir(parents=True, exist_ok=True)
  _git(["-C", str(base), "worktree", "add", "--detach", str(wt), commit_ish])
  return wt


def remove_worktree(base, wt) -> None:
  """Remove a worktree and prune; force-clean the dir if git can't (plan §4.3).

  Best-effort and exception-safe so it can run in a ``finally`` even after a
  failed review — a crashed review must not leave a dangling/locked worktree.
  """
  wt = pathlib.Path(wt)
  with contextlib.suppress(WorktreeError):
    _git(["-C", str(base), "worktree", "remove", "--force", str(wt)])
  with contextlib.suppress(WorktreeError):
    _git(["-C", str(base), "worktree", "prune"])
  if wt.exists():
    shutil.rmtree(wt, ignore_errors=True)


@contextlib.contextmanager
def worktree_session(base, worktree_dir, number: int, commit_ish: str):
  """Yield a provisioned worktree, guaranteeing teardown (plan §4.3).

  The teardown runs even if the body raises, so per-PR failures still clean up.
  """
  base = pathlib.Path(base)
  # Deterministic teardown target so a partial add_worktree failure still cleans
  # up rather than leaking a half-created worktree (finding F21).
  wt = pathlib.Path(worktree_dir) / f"pr-{number}"
  try:
    prune_stale(base)
    add_worktree(base, worktree_dir, number, commit_ish)
    yield wt
  finally:
    remove_worktree(base, wt)


@contextlib.contextmanager
def provisioned_worktree(
  repo: str,
  number: int,
  head_sha: str,
  base_clone_dir,
  worktree_dir,
  *,
  blobless: bool = True,
  fetch_base: bool = True,
  git_lock=None,
):
  """Full provisioning: ensure base clone, fetch PR head, yield a worktree.

  Checks out exactly ``head_sha`` (determinism: the agent reviews the same
  commit the diff was computed against, §6). Tears the worktree down on exit,
  including on failure.

  For concurrent fan-out (the batch run), pass a shared ``git_lock`` so the
  base-clone git operations (fetch / worktree add+remove) are serialized — git
  takes repo-level locks, so concurrent worktree adds would collide — while the
  long agy reviews run concurrently. Pass ``fetch_base=False`` once the batch
  has fetched the base once upfront, to avoid redundant per-PR fetches.
  """
  lock = git_lock if git_lock is not None else contextlib.nullcontext()
  base = pathlib.Path(base_clone_dir)
  # Compute the teardown target up front (it is deterministic): if add_worktree
  # fails partway — git can create the dir/admin entry, then fail — the old code
  # bound `wt` only on success and leaked the half-created worktree (finding
  # F21). remove_worktree is best-effort, so cleaning up when nothing was
  # created is a harmless no-op.
  wt = pathlib.Path(worktree_dir) / f"pr-{number}"
  try:
    with lock:
      ensure_base_clone(
        repo, base_clone_dir, blobless=blobless, fetch=fetch_base
      )
      prune_stale(base)
      _fetch_pr_head(base, number)
      add_worktree(base, worktree_dir, number, head_sha)
    yield wt
  finally:
    with lock:
      remove_worktree(base, wt)


def resolve_dependencies(
  worktree, flutter: str = "flutter", timeout: int = 900
) -> bool:
  """Best-effort ``flutter pub get`` so analyze/tests work (plan §4.3).

  Logged-but-not-fatal: a pub-get failure must not abort provisioning. The
  agent can still read code, and (network permitting) resolve deps itself.

  Returns:
    True if pub get succeeded, else False.
  """
  try:
    proc = subprocess.run(
      [flutter, "pub", "get"],
      cwd=str(worktree),
      capture_output=True,
      text=True,
      timeout=timeout,
      check=False,
    )
  except (FileNotFoundError, subprocess.TimeoutExpired) as e:
    _LOG.warning("flutter pub get not run in %s: %s", worktree, e)
    return False
  if proc.returncode != 0:
    _LOG.warning(
      "flutter pub get failed in %s: %s",
      worktree,
      proc.stderr.strip()[:200],
    )
    return False
  return True
