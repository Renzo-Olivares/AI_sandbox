"""Configuration loading and safety validation (plan §1, §6.2, §9).

The config file holds only the *names* of the token environment variables,
never token values; values are read from the environment at runtime. The
safety-critical fields are validated here, on load — not read ad hoc elsewhere
— so a misconfiguration fails fast and loudly rather than surfacing mid-run.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re

import dotenv
import yaml

from pr_review.errors import ConfigError

VALID_MODES = ("auto-stage-review", "manual-stage-review")

# Namespaces the agent must be denied entirely (plan §1 Layer 1): page loads
# (read_url), the browser "click submit" path (execute_url), MCP servers (mcp),
# and the sandbox-escape grant (unsandboxed).
REQUIRED_DENY_NAMESPACES = ("read_url", "execute_url", "mcp", "unsandboxed")

# Matches an Antigravity permission entry of the form "action(target)".
_PERMISSION_RE = re.compile(r"^\s*([a-z_]+)\s*\((.*)\)\s*$")


@dataclasses.dataclass(frozen=True)
class Config:
  """Validated tool configuration (frozen so it cannot drift after load)."""

  repo: str
  username: str
  mode: str
  default_branch: str
  labels_to_skip: tuple[str, ...]
  batch_size: int
  review_file_dir: pathlib.Path
  report_dir: pathlib.Path
  base_clone_dir: pathlib.Path
  worktree_dir: pathlib.Path
  agent_github_token_env: str
  orchestrator_github_token_env: str
  use_skip_permissions: bool
  agy_settings_path: pathlib.Path
  agy_command: str
  agy_model: str | None
  agy_timeout_seconds: int
  style_guide_enabled: bool
  style_guide_repo: str
  style_guide_ref: str
  style_guide_path: str


def _require(raw: dict, key: str) -> object:
  """Return ``raw[key]`` or raise ConfigError if missing/null."""
  if key not in raw or raw[key] is None:
    raise ConfigError(f"Config is missing required key: '{key}'.")
  return raw[key]


def _require_str(raw: dict, key: str) -> str:
  """Return a required non-empty string config value."""
  value = _require(raw, key)
  if not isinstance(value, str) or not value.strip():
    raise ConfigError(f"Config key '{key}' must be a non-empty string.")
  return value


def _path(value: object, base_dir: pathlib.Path | None = None) -> pathlib.Path:
  """Expand ``~`` and resolve a still-relative path against ``base_dir``.

  ``~`` is expanded as usual. A value that is STILL relative afterwards is
  resolved against ``base_dir`` (the config file's own directory) rather than
  the process's current working directory — so e.g. ``pr-reviews-generated/...``
  always lands next to ``config.yaml`` regardless of where the CLI is invoked
  from. Absolute paths (including ``~``-expanded ones) are returned unchanged,
  so configs using absolute/``~`` paths — and callers that pass no
  ``base_dir`` — are unaffected.
  """
  path = pathlib.Path(os.path.expanduser(str(value)))
  if base_dir is not None and not path.is_absolute():
    path = base_dir / path
  return path


def load_config(path) -> Config:
  """Load and validate the YAML config (plan §9).

  Args:
    path: path to the YAML config file.

  Returns:
    A validated, frozen :class:`Config`.

  Raises:
    ConfigError: if the file is missing/malformed or any safety-critical field
      is invalid. This is a fatal pre-flight error (plan §6.2).
  """
  cfg_path = _path(path)
  if not cfg_path.is_file():
    raise ConfigError(f"Config file not found: {cfg_path}.")
  # Relative directory paths resolve against the config file's location (not the
  # CLI's cwd), so generated data always lands next to config.yaml.
  config_dir = cfg_path.resolve().parent
  try:
    raw = yaml.safe_load(cfg_path.read_text())
  except yaml.YAMLError as e:
    raise ConfigError(f"Config file {cfg_path} is not valid YAML: {e}") from e
  if not isinstance(raw, dict):
    raise ConfigError(f"Config file {cfg_path} must be a YAML mapping.")

  # --- SAFETY (plan §1): the nuclear flag must never be enabled. ---
  skip = raw.get("use_skip_permissions", False)
  if skip is True:
    raise ConfigError(
      "use_skip_permissions is true. The --dangerously-skip-permissions flag "
      "voids the permission allowlist, the sandbox, and every other in-app "
      "safety control (plan §1); it is never required for headless operation. "
      "Set it to false or remove the key."
    )
  if skip is not False:
    # A non-bool (e.g. quoted "false", 0, "no") is a config mistake, not an
    # attempt to enable the flag — say so plainly rather than cry "nuclear".
    raise ConfigError(
      f"use_skip_permissions must be a YAML boolean (false), not {skip!r}. "
      "Use an unquoted false, or remove the key."
    )

  # --- SAFETY (plan §1 Layer 2): two distinct token env vars. ---
  agent_env = _require_str(raw, "agent_github_token_env")
  orchestrator_env = _require_str(raw, "orchestrator_github_token_env")
  if agent_env == orchestrator_env:
    raise ConfigError(
      "agent_github_token_env and orchestrator_github_token_env must name two "
      "DIFFERENT environment variables: the agent gets a read-only token and "
      f"the orchestrator a write-capable one (plan §1 Layer 2). Both are set "
      f"to '{agent_env}'."
    )

  mode = _require_str(raw, "mode")
  if mode not in VALID_MODES:
    raise ConfigError(f"mode must be one of {list(VALID_MODES)}, got '{mode}'.")

  batch_size = raw.get("batch_size", 5)
  if not isinstance(batch_size, int) or batch_size < 1:
    raise ConfigError(
      f"batch_size must be a positive integer, got {batch_size!r}."
    )

  labels = raw.get("labels_to_skip", []) or []
  if not isinstance(labels, list) or not all(
    isinstance(x, str) for x in labels
  ):
    raise ConfigError("labels_to_skip must be a list of strings.")

  agy_timeout = raw.get("agy_timeout_seconds", 1800)
  if not isinstance(agy_timeout, int) or agy_timeout < 1:
    raise ConfigError(
      f"agy_timeout_seconds must be a positive integer, got {agy_timeout!r}."
    )

  agy_model = raw.get("agy_model")
  if agy_model is not None and not isinstance(agy_model, str):
    raise ConfigError(
      'agy_model must be a string (e.g. "Claude Sonnet 4.6 (Thinking)") or '
      f"null, got {agy_model!r}."
    )

  return Config(
    repo=_require_str(raw, "repo"),
    username=_require_str(raw, "username"),
    mode=mode,
    default_branch=str(raw.get("default_branch", "master")),
    labels_to_skip=tuple(labels),
    batch_size=batch_size,
    review_file_dir=_path(_require(raw, "review_file_dir"), config_dir),
    report_dir=_path(_require(raw, "report_dir"), config_dir),
    base_clone_dir=_path(_require(raw, "base_clone_dir"), config_dir),
    worktree_dir=_path(
      raw.get("worktree_dir", "pr-reviews-generated/worktrees"), config_dir
    ),
    agent_github_token_env=agent_env,
    orchestrator_github_token_env=orchestrator_env,
    use_skip_permissions=False,
    agy_settings_path=_path(
      raw.get("agy_settings_path", "~/.gemini/antigravity-cli/settings.json")
    ),
    agy_command=str(raw.get("agy_command", "agy")),
    agy_model=(agy_model or None),
    agy_timeout_seconds=agy_timeout,
    style_guide_enabled=bool(raw.get("style_guide_enabled", True)),
    style_guide_repo=str(raw.get("style_guide_repo") or "flutter/flutter"),
    style_guide_ref=str(raw.get("style_guide_ref") or "master"),
    style_guide_path=str(
      raw.get("style_guide_path")
      or "docs/contributing/Style-guide-for-Flutter-repo.md"
    ),
  )


def load_dotenv_file(directory) -> None:
  """Load a git-ignored local ``.env`` for dev convenience (kickoff).

  No-op if absent (e.g. a scheduled run whose tokens come from the
  launchd/systemd environment or a secret store).

  Args:
    directory: directory to look for a ``.env`` file in.
  """
  env_path = pathlib.Path(os.path.expanduser(str(directory))) / ".env"
  if env_path.is_file():
    dotenv.load_dotenv(env_path)


def resolve_token(env_var_name: str) -> str:
  """Read a token value from the environment, or fail fast (plan §6.2).

  Args:
    env_var_name: the environment variable to read.

  Returns:
    The token value.

  Raises:
    ConfigError: if the variable is unset or empty. We never prompt — the
      production tool runs headless with no one to answer — so a missing token
      is a loud, fatal pre-flight error.
  """
  # Strip surrounding whitespace: a copy-pasted token with a trailing newline
  # otherwise causes confusing gh auth failures and weakens the secret-scrub /
  # write-isolation probe (finding F41).
  value = os.environ.get(env_var_name, "").strip()
  if not value:
    raise ConfigError(
      f"Required GitHub token env var '{env_var_name}' is not set or is empty "
      "(or whitespace-only). Provide it via the environment (a git-ignored "
      ".env in dev, or the scheduled job's environment in production)."
    )
  return value


def _parse_permission(entry: str) -> tuple[str, str] | None:
  """Parse an ``action(target)`` permission entry.

  Args:
    entry: a permission string such as ``"command(git)"``.

  Returns:
    ``(action, target)`` stripped, or ``None`` if it does not match the
    ``action(target)`` shape.
  """
  match = _PERMISSION_RE.match(entry or "")
  if not match:
    return None
  return match.group(1).strip(), match.group(2).strip()


def validate_agy_settings(path) -> None:
  """Validate the ``agy`` permission config is present + precedence-correct.

  Antigravity evaluates permissions with precedence ``deny > ask > allow``
  (plan §1). Because deny outranks allow, the allowlist must be built
  positively: a ``command(*)`` / ``write_file(*)`` deny catch-all would
  override the agent's own grants and break every run. This asserts the
  load-bearing invariants of the Layer-1 enforcement check (plan §1, §6.2):

    * the file exists and parses as JSON (a stray comma silently voids it);
    * no ``command(*)`` / ``write_file(*)`` catch-all in ``deny``;
    * no ``unsandboxed(...)`` and no broad ``command(gh)`` in ``allow``;
    * ``deny`` covers read_url, execute_url, mcp, unsandboxed.

  The exact settings.json schema is confirmed against the installed ``agy`` at
  M4 ("verify on first contact"); this implements the documented shape.

  Args:
    path: path to ``agy``'s ``settings.json``.

  Raises:
    ConfigError: on any violation (fatal pre-flight — plan §6.2).
  """
  settings_path = _path(path)
  if not settings_path.is_file():
    raise ConfigError(
      f"agy permission config not found: {settings_path}. The Layer-1 "
      "allowlist must exist before the agent runs (plan §1)."
    )
  try:
    data = json.loads(settings_path.read_text())
  except json.JSONDecodeError as e:
    raise ConfigError(
      f"agy settings {settings_path} is not valid JSON ({e}). A stray comma "
      "silently voids the whole permission file (plan §6.2)."
    ) from e

  permissions = (data or {}).get("permissions", {})
  if not isinstance(permissions, dict):
    raise ConfigError(
      f"agy settings {settings_path}: 'permissions' must be an object with "
      "allow/deny/ask lists (plan §1)."
    )
  allow = permissions.get("allow", []) or []
  deny = permissions.get("deny", []) or []

  for entry in deny:
    parsed = _parse_permission(entry)
    if parsed and parsed[1] == "*" and parsed[0] in ("command", "write_file"):
      raise ConfigError(
        f"agy settings: deny contains '{entry}'. A '{parsed[0]}(*)' deny "
        "outranks every allow (precedence deny > allow), so it would block the "
        "agent's own git/flutter/write_file grants and break the run (plan "
        "§1). Deny only read_url/execute_url/mcp/unsandboxed."
      )

  for entry in allow:
    parsed = _parse_permission(entry)
    if not parsed:
      continue
    action, target = parsed
    if action == "unsandboxed":
      raise ConfigError(
        f"agy settings: allow contains '{entry}'. 'unsandboxed(...)' runs a "
        "command outside the sandbox and must never be allowed (plan §1)."
      )
    if action == "command" and target in ("gh", "gh*", "gh *"):
      raise ConfigError(
        f"agy settings: allow contains a broad gh grant '{entry}'. "
        "'gh api --method POST' writes, so never allow gh broadly; allow only "
        "an exact read pattern if the agent needs one (plan §1)."
      )

  deny_norm = {
    f"{parsed[0]}({parsed[1]})"
    for parsed in (_parse_permission(entry) for entry in deny)
    if parsed
  }
  missing = [
    ns for ns in REQUIRED_DENY_NAMESPACES if f"{ns}(*)" not in deny_norm
  ]
  if missing:
    covered = ", ".join(f"{ns}(*)" for ns in REQUIRED_DENY_NAMESPACES)
    gaps = ", ".join(f"{ns}(*)" for ns in missing)
    raise ConfigError(
      f"agy settings: deny must cover {covered} (plan §1 Layer 1). "
      f"Missing: {gaps}."
    )


def check_global_agy_settings(path, *, project_allow=()) -> list[str]:
  """Return GLOBAL agy allow-grants not countered by Layer 1 (advisory).

  agy merges the project-local Layer-1 settings with the user's GLOBAL settings
  under ``deny > ask > allow`` ACROSS THE UNION (plan §1). The project-local
  deny-list only covers read_url/execute_url/mcp/unsandboxed, so a GLOBAL
  ``allow`` for any OTHER action (``command(bash)``, ``command(gh)``,
  ``write_file(...)``, ...) is NOT countered and carries into the agent's
  session, broadening it beyond its Layer-1 allowlist.

  This is ADVISORY defense-in-depth: it returns EVERY uncountered grant so the
  caller can warn loudly, and deliberately does NOT abort the run. A hard fail
  would (a) require an inevitably-incomplete denylist of "dangerous" command
  targets, and (b) break the common case of an operator whose global agy config
  is broad for their own interactive use — while the dangerous OUTCOMES are
  already bounded by the read-only token (Layer 2), the terminal sandbox, and
  the never-submit invariant. Grants the agent already holds (``project_allow``)
  and grants in the project-denied namespaces are excluded as non-broadening.

  Returns ``[]`` when the global file is absent, unreadable, or malformed (that
  is agy's own concern — never a reason to block or crash the run).

  Args:
    path: path to the user's GLOBAL ``agy`` settings.json (the
      ``agy_settings_path`` config field).
    project_allow: the agent's own Layer-1 allow entries, excluded from the
      result (a global grant the agent already has does not broaden it).

  Returns:
    The uncountered, broadening global allow entries (possibly empty).
  """
  settings_path = _path(path)
  if not settings_path.is_file():
    return []
  try:
    data = json.loads(settings_path.read_text())
  except (OSError, json.JSONDecodeError):
    return []
  permissions = data.get("permissions") if isinstance(data, dict) else None
  allow = permissions.get("allow") if isinstance(permissions, dict) else None
  if not isinstance(allow, list):
    return []
  countered = set(REQUIRED_DENY_NAMESPACES)
  already = {
    f"{p[0]}({p[1]})"
    for p in (_parse_permission(e) for e in project_allow if isinstance(e, str))
    if p
  }
  broad = []
  for entry in allow:
    if not isinstance(entry, str):
      continue
    parsed = _parse_permission(entry)
    if not parsed:
      continue
    action, target = parsed
    if action in countered:
      continue  # the project deny overrides this in the union → safe
    if f"{action}({target})" in already:
      continue  # the agent already holds this exact grant → not broadening
    broad.append(entry)
  return broad
