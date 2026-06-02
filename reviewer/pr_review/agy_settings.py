"""Layer-1 permission settings for the ``agy`` review agent (plan §1).

Verified against agy 1.0.3: the headless CLI reads a **project-local**
``<cwd>/.gemini/antigravity-cli/settings.json`` (merged with the user's global
settings, ``deny > ask > allow`` across the union). So Layer 1 is installed
**per worktree** — the orchestrator writes our strict allowlist into the PR's
worktree and runs ``agy`` there — and the user's GLOBAL config is never touched.

Security note: the worktree is the PR's *untrusted* checkout, and agy honors
whatever ``.gemini`` it finds there — so a malicious PR could ship its own
settings to escalate permissions. The orchestrator therefore SCRUBS any
PR-shipped ``.gemini`` from the worktree and writes our strict settings last,
so ours always governs.

Precedence is ``deny > ask > allow`` (plan §1), so the allowlist is built
positively: allow exactly the tooling the agent needs and blanket-deny only the
namespaces it needs nothing from. We never put a ``command(*)`` or
``write_file(*)`` catch-all in ``deny`` (it would outrank the agent's own
allows), and never allow ``unsandboxed`` or a broad ``command(gh)``.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil

from pr_review.atomicio import write_text_atomic
from pr_review.config import validate_agy_settings

_LOG = logging.getLogger(__name__)

# Allow exactly what the review agent needs (plan §1 Layer 1). Worktree file
# reads/writes are auto-allowed by agy, so they need no entry.
DEFAULT_ALLOW = (
  "command(git)",
  "command(flutter analyze)",
  "command(flutter test)",
  "command(dart)",
)

# Blanket-deny only the namespaces the agent needs nothing from (plan §1): page
# loads, the browser "click submit" path, MCP, and the sandbox-escape grant.
DEFAULT_DENY = (
  "read_url(*)",
  "execute_url(*)",
  "mcp(*)",
  "unsandboxed(*)",
)


def render_permissions(
  allow=DEFAULT_ALLOW, deny=DEFAULT_DENY
) -> dict[str, list]:
  """Render the ``permissions`` block (allow/deny, empty ask)."""
  return {"allow": list(allow), "deny": list(deny), "ask": []}


def scrub_project_agy_config(worktree) -> list[str]:
  """Remove any ``.gemini`` config dirs from the (untrusted) PR checkout.

  Anti-injection (plan §1): agy reads project-local settings from the worktree,
  which is the PR's untrusted code — so a PR could ship a ``.gemini`` that
  loosens permissions. We delete all of them before writing our own.

  A PR can try to escape this scrub by shipping ``.gemini`` as a **symlink** or
  a regular **file** rather than a directory: ``is_dir()`` and ``rmtree`` follow
  symlinks, so a symlinked ``.gemini`` survives deletion (on the pinned 3.9
  runtime ``rmtree`` refuses to recurse a symlink) and would then redirect our
  settings write outside the worktree. So we handle the link/file itself (never
  its target), remove real dirs without ``ignore_errors`` (a survivor must be
  loud, not silently swallowed), record only paths verified gone, and WARN on
  anything that survives. We walk with ``followlinks=False`` so traversal never
  descends THROUGH a symlink (which could otherwise delete files outside the
  worktree).

  Returns:
    The removed paths (a PR shipping agy config is suspicious — worth logging).
  """
  worktree = pathlib.Path(worktree)
  candidates = []
  for dirpath, dirnames, filenames in os.walk(worktree, followlinks=False):
    for name in list(dirnames) + filenames:
      if name == ".gemini":
        candidates.append(pathlib.Path(dirpath) / name)

  removed = []
  for gemini in candidates:
    if not (gemini.exists() or gemini.is_symlink()):
      continue  # already gone (e.g. nested under a parent .gemini we removed)
    if gemini.is_symlink():
      gemini.unlink()  # remove the link, never follow it to its target
    elif gemini.is_dir():
      shutil.rmtree(gemini)  # no ignore_errors: a survivor must surface
    else:
      gemini.unlink()  # a regular file named .gemini
    if gemini.exists() or gemini.is_symlink():
      _LOG.warning(
        "failed to remove PR-shipped agy config at %s — it may still govern "
        "the review agent; the install step will refuse to proceed.",
        gemini,
      )
    else:
      removed.append(str(gemini))
  return removed


def install_project_settings(
  worktree, *, allow=DEFAULT_ALLOW, deny=DEFAULT_DENY, model=None
) -> pathlib.Path:
  """Install Layer 1 as a project-local settings.json in the worktree (plan §1).

  Scrubs any PR-shipped ``.gemini`` first (anti-injection), then writes our
  strict settings and trusts the worktree (so agy does not prompt to trust it
  headlessly — another silent-hang source). The user's global config is left
  untouched.

  Args:
    worktree: the PR's worktree (becomes agy's working dir).
    allow: allowlist entries.
    deny: denylist entries.
    model: pin this review model (``None`` = inherit agy's global selection).
      Pinning decouples review quality from whatever model the user happens to
      have selected interactively.

  Returns:
    The path of the written project-local settings.json.
  """
  worktree = pathlib.Path(worktree)
  removed = scrub_project_agy_config(worktree)
  if removed:
    _LOG.info(
      "removed %d .gemini config dir(s) from the checkout so our Layer-1 "
      "settings govern (repo-native or PR-injected): %s",
      len(removed),
      removed,
    )
  # Defense in depth (anti-injection): the scrub above should have cleared any
  # PR-shipped .gemini. If one survived in any form (an undeletable dir, or a
  # symlink/file we could not remove), refuse — writing through it could let a
  # PR redirect our settings outside the worktree or keep its config in play.
  gemini_root = worktree / ".gemini"
  if gemini_root.exists() or gemini_root.is_symlink():
    raise PermissionError(
      f"refusing to install Layer-1 settings: a .gemini survived scrubbing at "
      f"{gemini_root} (PR-injected symlink or undeletable dir?). It could "
      "redirect our write outside the worktree or govern the agent (plan §1)."
    )
  settings_dir = gemini_root / "antigravity-cli"
  settings_dir.mkdir(parents=True, exist_ok=True)
  settings = {
    "enableTerminalSandbox": True,
    "trustedWorkspaces": [str(worktree)],
    "permissions": render_permissions(allow, deny),
  }
  if model:
    settings["model"] = model
  path = settings_dir / "settings.json"
  # Final guard: the resolved write target must be strictly inside the worktree.
  resolved = path.resolve()
  if worktree.resolve() not in resolved.parents:
    raise PermissionError(
      f"refusing to write Layer-1 settings outside the worktree: {resolved}"
    )
  write_text_atomic(path, json.dumps(settings, indent=2) + "\n")  # F35
  # Validate the file we ACTUALLY installed — not just a separate render — so a
  # custom or drifted allow/deny cannot ship to the agent unchecked (F11).
  validate_agy_settings(path)
  return path


# --- Global-settings fallback (not used in the project-local design, kept for
# --- environments where a per-worktree settings file is not viable). ----------


def merge_settings(existing: dict, *, trusted_dirs=()) -> dict:
  """Merge the Layer-1 block into existing global settings, preserving keys."""
  merged = dict(existing or {})
  merged["permissions"] = render_permissions()
  merged["enableTerminalSandbox"] = True
  trusted = list(merged.get("trustedWorkspaces", []))
  for directory in trusted_dirs:
    if str(directory) not in trusted:
      trusted.append(str(directory))
  if trusted:
    merged["trustedWorkspaces"] = trusted
  return merged


def read_settings(settings_path) -> dict:
  """Read and parse a settings.json, returning ``{}`` if absent."""
  path = pathlib.Path(settings_path)
  if not path.is_file():
    return {}
  return json.loads(path.read_text()) or {}


def apply_settings(
  settings_path, *, trusted_dirs=(), backup_suffix=".pr-review-bak"
):
  """Install Layer 1 into a global settings.json, backing up first (fallback).

  Returns:
    ``(merged_dict, backup_path_or_None)``.
  """
  path = pathlib.Path(settings_path)
  existing = read_settings(path)
  backup_path = None
  if backup_suffix is not None and path.is_file():
    backup_path = path.with_name(path.name + backup_suffix)
    backup_path.write_text(path.read_text())
  merged = merge_settings(existing, trusted_dirs=trusted_dirs)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(merged, indent=2) + "\n")
  return merged, backup_path


def restore_settings(settings_path, backup_path) -> None:
  """Restore a settings.json from its backup (used after a temporary apply)."""
  path = pathlib.Path(settings_path)
  backup = pathlib.Path(backup_path)
  if backup.is_file():
    path.write_text(backup.read_text())
