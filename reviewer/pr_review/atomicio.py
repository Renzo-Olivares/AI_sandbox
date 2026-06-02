"""Atomic text-file writes (plan §6.2 durability; finding F35).

A plain ``path.write_text(...)`` is not atomic: a crash, kill, or full disk
mid-write leaves a TRUNCATED file. Later readers — manual ``stage-review``
parsing the review envelope, or someone opening the run report — then silently
mis-parse or fail. Writing to a temp file in the SAME directory and
``os.replace`` onto the final path is atomic within a filesystem, so a reader
always sees either the previous complete file or the new complete one, never a
partial.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import tempfile


def write_text_atomic(
  path, text: str, *, encoding: str = "utf-8"
) -> pathlib.Path:
  """Write ``text`` to ``path`` atomically (temp file + fsync + ``os.replace``).

  Creates parent directories as needed. The temp file is made in the target's
  own directory (so ``os.replace`` stays within one filesystem) and is removed
  if the write fails, leaving any existing file at ``path`` untouched.

  Returns:
    The final path written.
  """
  path = pathlib.Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(
    dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
  )
  try:
    with os.fdopen(fd, "w", encoding=encoding) as handle:
      handle.write(text)
      handle.flush()
      os.fsync(handle.fileno())  # durable before the rename (full-disk/crash)
    os.replace(tmp, path)  # atomic within the filesystem
  except BaseException:
    with contextlib.suppress(OSError):
      os.unlink(tmp)
    raise
  return path
