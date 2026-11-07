"""An append-only audit trail in `.chief/audit.jsonl`."""

import json
from . import atomic, clock
from .locking import file_lock


def path(root):
    return atomic.state_dir(root) / "audit.jsonl"


def record(root, action, **fields):
    """Append one entry. Bodies and secrets stay out of the entry."""
    base = atomic.state_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    entry = dict(at=clock.now(), action=action, **fields)
    with file_lock(base / "audit.lock"):
        with path(root).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def read(root):
    p = path(root)
    if not p.exists():
        return []
    return [json.loads(x) for x in atomic.read_text(p).splitlines() if x.strip()]
