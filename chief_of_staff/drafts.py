"""Draft-only email. A draft is saved and never sent.

This module has no send path. A backend saves the message where the person will find
it, and an audit entry records the action. Two backends ship with the package.

1. `AppleMailBackend` saves into the Drafts mailbox through Mail on macOS.
2. `EmlBackend` writes a portable `.eml` file with an `X-Unsent: 1` header. Most mail
   clients open such a file as a draft that is ready to send.

`save` tries the backends in order and falls back to the next one on any failure.
The `.eml` backend is the last resort and always works on a writable folder.
"""

import datetime as dt
import email.message
import email.utils
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from . import atomic, audit, clock
from .locking import file_lock

EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
EXPIRE_DAYS = 7


def store_path(root):
    return atomic.state_dir(root) / "drafts.json"


def draft_key(to, subject, body):
    text = "\n".join([",".join(sorted(to)), subject, body])
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "draft"


def _quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mail_script(subject, body, to, cc, sender=""):
    """The AppleScript that makes and saves one draft. It holds no delivery command."""
    lines = [
        'tell application "Mail"',
        "  set m to make new outgoing message with properties {subject:%s, content:%s, visible:false}"
        % (_quote(subject), _quote(body)),
        "  tell m",
    ]
    if sender:
        lines += ["    try", "      set sender to " + _quote(sender), "    end try"]
    for kind, addresses in (("to", to), ("cc", cc)):
        for address in addresses:
            lines.append(
                "    make new %s recipient at end of %s recipients with properties {address:%s}"
                % (kind, kind, _quote(address))
            )
    lines += ["  end tell", "  save m", '  return "drafted"', "end tell"]
    return "\n".join(lines)


class EmlBackend:
    """Portable fallback: one `.eml` file per draft in a folder."""

    name = "eml"

    def __init__(self, folder):
        self.folder = Path(folder).expanduser()

    def save(self, draft, when):
        msg = email.message.EmailMessage()
        if draft.get("sender"):
            msg["From"] = draft["sender"]
        msg["To"] = ", ".join(draft["to"])
        if draft["cc"]:
            msg["Cc"] = ", ".join(draft["cc"])
        msg["Subject"] = draft["subject"]
        msg["Date"] = email.utils.format_datetime(when)
        msg["X-Unsent"] = "1"
        msg.set_content(draft["body"])
        self.folder.mkdir(parents=True, exist_ok=True)
        stem = "%s-%s" % (when.strftime("%Y-%m-%d"), _slug(draft["subject"]))
        path, n = self.folder / (stem + ".eml"), 2
        while path.exists():
            path = self.folder / ("%s-%d.eml" % (stem, n))
            n += 1
        path.write_bytes(bytes(msg))
        return str(path)


class AppleMailBackend:
    """Saves into Mail's Drafts on macOS. `runner` is replaceable for tests."""

    name = "apple-mail"

    def __init__(self, runner=None, timeout=60):
        self.runner = runner or self._osascript
        self.timeout = timeout

    def _osascript(self, script):
        if sys.platform != "darwin":
            raise RuntimeError("Apple Mail needs macOS")
        run = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if run.returncode != 0:
            raise RuntimeError("Mail refused the draft: " + run.stderr.strip()[:200])
        return run.stdout.strip()

    def save(self, draft, when):
        self.runner(
            mail_script(
                draft["subject"], draft["body"], draft["to"], draft["cc"], draft.get("sender", "")
            )
        )
        return "Mail Drafts"


def default_backends(folder):
    chain = [EmlBackend(folder)]
    if sys.platform == "darwin":
        chain.insert(0, AppleMailBackend())
    return chain


def load(root):
    path = store_path(root)
    if not path.exists():
        return dict(version=1, drafts={})
    data = json.loads(atomic.read_text(path))
    if not isinstance(data, dict) or not isinstance(data.get("drafts"), dict):
        raise ValueError("invalid draft store")
    return data


def _write(root, data):
    atomic.write_text(store_path(root), json.dumps(data, indent=2, sort_keys=True) + "\n")


def save(root, to, subject, body, *, cc=(), sender="", backends=None, eml_folder=None,
         note="", actor="assistant", now=None):
    """Save one draft. Returns a dict with ok, where and key. Nothing is sent."""
    to, cc = list(to), list(cc)
    bad = [a for a in to + cc if not EMAIL_RE.match(a)]
    if not to or bad:
        raise ValueError("bad or missing address: " + str(bad or "no recipient"))
    if not subject.strip() or not body.strip():
        raise ValueError("a draft needs a subject and a body")
    root = Path(root).resolve()
    now = now or dt.datetime.now(dt.timezone.utc)
    key = draft_key(to, subject, body)
    folder = eml_folder or atomic.state_dir(root) / "drafts"
    chain = backends if backends is not None else default_backends(folder)
    if not chain:
        raise ValueError("no draft backend configured")
    with file_lock(atomic.state_dir(root) / "drafts.lock"):
        data = load(root)
        old = data["drafts"].get(key)
        if old and old["status"] == "waiting":
            return dict(ok=True, skipped="the same draft is already waiting", key=key,
                        backend=old["backend"], where=old["where"])
        draft = dict(to=to, cc=cc, subject=subject, body=body, sender=sender)
        errors = []
        for backend in chain:
            try:
                where = backend.save(draft, now)
                break
            except Exception as exc:  # any backend trouble falls through to the next
                errors.append("%s: %s" % (backend.name, str(exc)[:160]))
        else:
            raise OSError("no backend could save the draft: " + " | ".join(errors))
        row = dict(key=key, to=to, cc=cc, subject=subject, backend=backend.name,
                   where=where, created=now.isoformat(), status="waiting", note=note,
                   fallback_errors=errors)
        data["drafts"][key] = row
        _write(root, data)
    audit.record(root, "draft-email", key=key, to=to, subject=subject,
                 backend=backend.name, where=where, actor=actor, sent=False)
    return dict(ok=True, key=key, backend=backend.name, where=where, fallback_errors=errors)


def _scan(data, now):
    """(still waiting, changed). Marks a draft expired after a week or done when its file is gone."""
    out, changed = [], False
    for row in data["drafts"].values():
        if row["status"] != "waiting":
            continue
        age = now - dt.datetime.fromisoformat(row["created"])
        if age > dt.timedelta(days=EXPIRE_DAYS):
            row["status"], changed = "expired", True
        elif row["backend"] == "eml" and not Path(row["where"]).exists():
            row["status"], changed = "done", True
        else:
            out.append(row)
    return sorted(out, key=lambda r: r["created"]), changed


def waiting(root, now=None, persist=True):
    """Drafts still waiting. `persist=False` leaves the store untouched."""
    root = Path(root).resolve()
    now = now or dt.datetime.now(dt.timezone.utc)
    if not persist:
        return _scan(load(root), now)[0]
    with file_lock(atomic.state_dir(root) / "drafts.lock"):
        data = load(root)
        out, changed = _scan(data, now)
        if changed:
            _write(root, data)
    return out


def mark_done(root, key):
    root = Path(root).resolve()
    with file_lock(atomic.state_dir(root) / "drafts.lock"):
        data = load(root)
        row = data["drafts"].get(key)
        if not row:
            return False
        row["status"] = "done"
        _write(root, data)
    audit.record(root, "draft-done", key=key)
    return True


def summary_lines(root, now=None, persist=True):
    """Lines for the daily summary: every draft that waits for a person."""
    rows = waiting(root, now, persist)
    if not rows:
        return ["No drafts waiting."]
    out = []
    for r in rows:
        place = "in " + r["where"] if r["backend"] != "eml" else "as the file " + r["where"]
        out.append("To %s: %s (%s; id %s)" % (", ".join(r["to"]), r["subject"][:80], place, r["key"]))
    return out
