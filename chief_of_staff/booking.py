"""Calendar booking with read-back verification.

A booking writes an event through a calendar backend and then reads it back by its
stable key. A receipt is recorded only when the event read back matches the request.
An item that is uncertain or failed is never written a second time, because a second
write could duplicate the event.

A backend has three methods: `write(event)`, `read_back(key)` and, optionally,
`conflicts(start, end)`. `IcsFolderBackend` is a portable backend. It keeps one
`.ics` file per event in a folder that any calendar app can import or subscribe to.
"""

import datetime as dt
import json
import re
from pathlib import Path
from . import atomic, audit, clock
from .locking import file_lock

VERIFIED, UNCERTAIN, FAILED = "verified", "uncertain", "failed"


def store_path(root):
    return atomic.state_dir(root) / "bookings.json"


def _stamp(value):
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_stamp(text):
    return dt.datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)


def _escape(text):
    return text.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,").replace("\n", "\\n")


def _unescape(text):
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), text)


class IcsFolderBackend:
    """One `.ics` file per event in a folder. Read-back parses the file again."""

    def __init__(self, folder, domain="example.com"):
        self.folder = Path(folder).expanduser()
        self.domain = domain

    def _path(self, key):
        return self.folder / (re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-") + ".ics")

    def write(self, event):
        self.folder.mkdir(parents=True, exist_ok=True)
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//chief-of-staff//EN", "BEGIN:VEVENT",
                 "UID:%s@%s" % (event["key"], self.domain),
                 "DTSTAMP:" + _stamp(dt.datetime.now(dt.timezone.utc)),
                 "DTSTART:" + _stamp(event["start"]), "DTEND:" + _stamp(event["end"]),
                 "SUMMARY:" + _escape(event["title"]),
                 "DESCRIPTION:" + _escape(event.get("note", "")), "END:VEVENT", "END:VCALENDAR"]
        atomic.write_text(self._path(event["key"]), "\r\n".join(lines) + "\r\n")

    @staticmethod
    def _fields(text):
        return {k: v for k, _, v in (ln.partition(":") for ln in text.splitlines()) if k}

    def read_back(self, key):
        path = self._path(key)
        if not path.is_file():
            return None
        f = self._fields(atomic.read_text(path))
        try:
            return dict(uid=f["UID"], title=_unescape(f["SUMMARY"]),
                        start=_parse_stamp(f["DTSTART"]), end=_parse_stamp(f["DTEND"]))
        except (KeyError, ValueError):
            return None

    def conflicts(self, start, end):
        out = []
        for path in sorted(self.folder.glob("*.ics")) if self.folder.is_dir() else []:
            f = self._fields(atomic.read_text(path))
            try:
                s, e = _parse_stamp(f["DTSTART"]), _parse_stamp(f["DTEND"])
            except (KeyError, ValueError):
                continue
            if s < end and start < e:
                out.append(_unescape(f.get("SUMMARY", "")))
        return out


def load(root):
    path = store_path(root)
    if not path.exists():
        return dict(version=1, bookings={})
    data = json.loads(atomic.read_text(path))
    if not isinstance(data, dict) or not isinstance(data.get("bookings"), dict):
        raise ValueError("invalid booking store")
    return data


def _write(root, data):
    atomic.write_text(store_path(root), json.dumps(data, indent=2, sort_keys=True) + "\n")


def receipt(root, key):
    return load(root)["bookings"].get(key, {})


def book(root, backend, *, key, title, start, minutes=45, note="", actor="assistant"):
    """Write the event, read it back, then record the receipt.

    `start` is a timezone-aware datetime. Returns the stored record, which has
    status verified, uncertain or failed. A verified record is returned unchanged
    on a repeat call. An uncertain or failed record is never written again.
    """
    if start.tzinfo is None:
        raise ValueError("start must carry a timezone")
    if not 5 <= minutes <= 24 * 60:
        raise ValueError("minutes must be between 5 and 1440")
    root = Path(root).resolve()
    end = start + dt.timedelta(minutes=minutes)
    with file_lock(atomic.state_dir(root) / "bookings.lock"):
        data = load(root)
        old = data["bookings"].get(key)
        if old:
            return old
        record = dict(key=key, title=title, start=start.isoformat(), end=end.isoformat(),
                      status=UNCERTAIN, intent_at=clock.now(), last="intent recorded")
        data["bookings"][key] = record
        _write(root, data)
        check = getattr(backend, "conflicts", None)
        clash = check(start, end) if check else []
        if clash:
            record.update(status=FAILED, last="slot clashes with: " + ", ".join(clash))
        else:
            seen, problem = None, "event not found on read-back"
            try:
                backend.write(dict(key=key, title=title, start=start, end=end, note=note))
                seen = backend.read_back(key)
            except Exception as exc:  # the write may or may not have landed
                problem = "write or read-back error: " + str(exc)[:160]
            if seen is not None and (seen["title"], seen["start"], seen["end"]) == (title, start, end):
                record.update(status=VERIFIED, uid=seen["uid"], verified_at=clock.now(),
                              last="verified by read-back")
            else:
                record["last"] = problem if seen is None else "event on read-back differs from the request"
        _write(root, data)
    audit.record(root, "calendar-book", key=key, status=record["status"], title=title,
                 start=record["start"], actor=actor)
    return record
