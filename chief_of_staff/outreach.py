"""Scan configured folders for outreach that was drafted and never sent.

Read-only. Configure `.chief/outreach.json`:

    {"folders": ["~/notes/*prep"], "file_glob": "*outreach*.md",
     "max_age_days": 14, "limit": 5}

`folders` are glob patterns. A file counts as unsent when its text says nothing was
sent and has no line that says it was. Quoted lines (starting with `> `) count as drafts.
"""

import glob
import json
import re
import time
from pathlib import Path
from . import atomic

UNSENT_RE = re.compile(
    r"nothing (?:has been |was |is )?sent|none (?:of them )?(?:were |was |have been )?sent"
    r"|not (?:yet )?sent|unsent|nothing goes out until",
    re.I,
)
SENT_RE = re.compile(r"^\s*(?:status:\s*sent|all (?:notes |messages )?sent\b|sent on\b)", re.I | re.M)
DEFAULTS = dict(folders=[], file_glob="*outreach*.md", max_age_days=14, limit=5)


def load_config(root):
    path = atomic.state_dir(root) / "outreach.json"
    if not path.exists():
        return dict(DEFAULTS)
    data = json.loads(atomic.read_text(path))
    if not isinstance(data, dict) or not isinstance(data.get("folders", []), list):
        raise ValueError("invalid outreach config")
    return dict(DEFAULTS, **data)


def scan(config, now=None):
    now = now if now is not None else time.time()
    found = []
    for pattern in config["folders"]:
        for folder in sorted(glob.glob(str(Path(pattern).expanduser()))):
            folder = Path(folder)
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob(config["file_glob"])):
                try:
                    age = (now - f.stat().st_mtime) / 86400
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if age > config["max_age_days"] or SENT_RE.search(text) or not UNSENT_RE.search(text):
                    continue
                found.append(dict(path=str(f), folder=folder.name, age_days=round(age, 1),
                                  drafts=sum(1 for ln in text.splitlines() if ln.startswith("> "))))
    return found[: config["limit"]]


def exceptions(root, now=None):
    """Report exceptions for unsent outreach. An unconfigured project reports none."""
    config = load_config(root)
    if not config["folders"]:
        return []
    out = []
    for o in scan(config, now):
        out.append(dict(
            kind="unsent-outreach",
            title="Unsent outreach: " + Path(o["path"]).name,
            owner="unassigned",
            reason="%d draft note(s) in %s, none sent, file last changed %s days ago. File: %s"
            % (o["drafts"], o["folder"], o["age_days"], o["path"]),
            next_decision="Send the notes, drop them, or pick another route.",
        ))
    return out
