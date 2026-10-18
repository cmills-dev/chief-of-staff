"""Atomic replacement for local state files."""

import os
import tempfile
from pathlib import Path


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".chief-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def state_dir(root):
    root = Path(root).resolve()
    path = root / ".chief"
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("Project state must be an ordinary local directory")
    return path


def read_text(path, limit=4 * 1024 * 1024):
    with Path(path).open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Saved state exceeds the size limit")
    return data.decode("utf-8")
