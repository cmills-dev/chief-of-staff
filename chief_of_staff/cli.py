"""The same local command interface for a person, Claude or Codex."""

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time
from . import (
    atomic,
    booking,
    decisions,
    drafts,
    notes,
    outreach,
    policy,
    runtime,
    tasks,
    watch,
)
from .locking import Busy


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", type=Path, default=Path.cwd())
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    for name in ("enable", "disable"):
        q = sub.add_parser(name)
        q.add_argument(
            "--watch",
            action="store_true",
            help="Change the watch flag instead of report writes",
        )
    q = sub.add_parser("reconcile")
    q.add_argument("--dry-run", action="store_true")
    q = sub.add_parser("watch")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--ticks", type=int, default=1)
    q.add_argument("--interval", type=float, default=60)
    q = sub.add_parser("task")
    ts = q.add_subparsers(dest="action", required=True)
    ts.add_parser("list")
    for action in ("draft", "promote", "assign", "block", "resume", "complete"):
        a = ts.add_parser(action)
        if action == "draft":
            a.add_argument("--title", required=True)
            a.add_argument("--decision", action="append", default=[])
        else:
            a.add_argument("id")
        a.add_argument("--owner", action="append", default=[])
        a.add_argument("--actor", required=True)
        a.add_argument("--source", required=True)
        a.add_argument("--confirmed", action="store_true")
        a.add_argument("--reason", default="")
        a.add_argument("--next", dest="next_decision", default="")
        a.add_argument("--evidence", action="append", default=[])
    q = sub.add_parser("decision")
    ds = q.add_subparsers(dest="action", required=True)
    ds.add_parser("list")
    for action in ("record", "protect", "review", "snapshot"):
        a = ds.add_parser(action)
        a.add_argument("--actor", required=True)
        a.add_argument("--source", required=True)
        a.add_argument("--confirmed", action="store_true", required=True)
        if action in ("record", "protect", "review"):
            a.add_argument("--id", required=True)
        if action in ("protect", "snapshot"):
            a.add_argument("--task", required=True)
        if action == "record":
            a.add_argument("--decision", required=True)
            a.add_argument("--reason", required=True)
        if action == "review":
            a.add_argument(
                "--outcome", choices=("allow-close", "keep-open"), required=True
            )
            a.add_argument("--reason", required=True)
        if action == "snapshot":
            a.add_argument("--path", required=True)
            a.add_argument("--assumption", required=True)
    q = sub.add_parser("policy", help="Check a request against the permission policy")
    q.add_argument("--role", default="assistant")
    q.add_argument("--branch", default="")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="A command or request to check against the hard denies")
    g.add_argument("--git", help="A git command line to check for the role")
    g.add_argument("--outbound", help="An outbound verb such as draft, send or reply")
    q = sub.add_parser("draft", help="Draft-only email")
    dsub = q.add_subparsers(dest="action", required=True)
    a = dsub.add_parser("save")
    a.add_argument("--to", action="append", required=True)
    a.add_argument("--cc", action="append", default=[])
    a.add_argument("--subject", required=True)
    a.add_argument("--body-file", type=Path, required=True)
    a.add_argument("--sender", default="")
    a.add_argument("--note", default="")
    a.add_argument("--backend", choices=("auto", "eml", "apple-mail"), default="auto")
    a.add_argument("--eml-folder", type=Path)
    dsub.add_parser("list")
    a = dsub.add_parser("done")
    a.add_argument("key")
    q = sub.add_parser("note", help="PDF-only research notes")
    nsub = q.add_subparsers(dest="action", required=True)
    a = nsub.add_parser("render")
    a.add_argument("--src", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--title", default="")
    q = sub.add_parser("book", help="Book a calendar slot and verify it by read-back")
    q.add_argument("--calendar-folder", type=Path, required=True)
    q.add_argument("--key", required=True)
    q.add_argument("--title", required=True)
    q.add_argument("--at", required=True, help="ISO time with a UTC offset")
    q.add_argument("--minutes", type=int, default=45)
    q.add_argument("--note", default="")
    q.add_argument("--actor", default="assistant")
    q = sub.add_parser("outreach", help="Scan for outreach drafted and never sent")
    q.add_argument("action", choices=("scan",))
    return p


def _backends(a):
    if a.backend == "auto":
        return None
    folder = a.eml_folder or atomic.state_dir(a.project.resolve()) / "drafts"
    if a.backend == "eml":
        return [drafts.EmlBackend(folder)]
    return [drafts.AppleMailBackend(), drafts.EmlBackend(folder)]


def main(argv=None):
    a = parser().parse_args(argv)
    root = a.project.resolve()
    base = runtime.folder(root)
    try:
        if a.command == "init":
            out = runtime.init(root)
        elif a.command == "status":
            out = runtime.report(root)
        elif a.command in ("enable", "disable"):
            if not base.is_dir():
                raise ValueError("Run init for this project first")
            path = base / ("watch-enabled" if a.watch else "writes-enabled")
            if a.command == "enable":
                atomic.write_text(path, "enabled\n")
            elif path.exists():
                path.unlink()
            out = dict(flag=path.name, enabled=path.exists())
        elif a.command == "reconcile":
            out = runtime.reconcile(root, dry_run=a.dry_run)
        elif a.command == "watch":
            if not 1 <= a.ticks <= 1440 or not 1 <= a.interval <= 3600:
                raise ValueError(
                    "Use 1 to 1440 ticks and an interval from 1 to 3600 seconds"
                )
            for i in range(a.ticks):
                if i:
                    time.sleep(a.interval)
                out = watch.tick(root, dry_run=a.dry_run)
                if a.ticks > 1:
                    print(json.dumps(out), flush=True)
        elif a.command == "task":
            if a.action == "list":
                out = tasks.load(root)
            else:
                out = tasks.change(
                    root,
                    a.action,
                    task_id=getattr(a, "id", ""),
                    title=getattr(a, "title", ""),
                    owners=a.owner,
                    decision_ids=getattr(a, "decision", []),
                    actor=a.actor,
                    source=a.source,
                    confirmed=a.confirmed,
                    reason=a.reason,
                    next_decision=a.next_decision,
                    evidence=a.evidence,
                )
        elif a.command == "decision":
            path = base / "decisions.json"
            if a.action == "list":
                out = decisions.load_registry(path)
            else:
                common = (a.actor, a.source, a.confirmed)

                def operation(reg):
                    if a.action == "record":
                        return decisions.record(
                            reg, a.id, a.decision, a.reason, *common
                        )
                    if a.action == "protect":
                        return decisions.protect(reg, a.task, a.id, *common)
                    if a.action == "review":
                        return decisions.review(reg, a.id, a.outcome, a.reason, *common)
                    return decisions.snapshot(
                        reg, a.task, a.path, a.assumption, *common, root=root
                    )

                out = dict(result=decisions.mutate(path, operation))
        elif a.command == "policy":
            pol = policy.Policy.load(root)
            if a.text is not None:
                verdict = pol.check_text(a.text)
            elif a.git is not None:
                verdict = pol.check_git(a.role, a.git, a.branch)
            else:
                verdict = pol.check_outbound(a.outbound)
            out = verdict.as_dict()
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0 if verdict.allowed else 2
        elif a.command == "draft":
            if not base.is_dir():
                raise ValueError("Run init for this project first")
            if a.action == "save":
                out = drafts.save(
                    root, a.to, a.subject, a.body_file.read_text(encoding="utf-8"),
                    cc=a.cc, sender=a.sender, backends=_backends(a),
                    eml_folder=a.eml_folder, note=a.note,
                )
            elif a.action == "list":
                out = dict(waiting=drafts.waiting(root))
            else:
                out = dict(done=drafts.mark_done(root, a.key))
        elif a.command == "note":
            out = notes.render_pdf(
                a.src.read_text(encoding="utf-8"), a.out, title=a.title, src=a.src
            )
        elif a.command == "book":
            if not base.is_dir():
                raise ValueError("Run init for this project first")
            out = booking.book(
                root, booking.IcsFolderBackend(a.calendar_folder), key=a.key,
                title=a.title, start=dt.datetime.fromisoformat(a.at),
                minutes=a.minutes, note=a.note, actor=a.actor,
            )
        elif a.command == "outreach":
            out = dict(unsent=outreach.scan(outreach.load_config(root)))
        print(json.dumps(out, indent=2, sort_keys=True))
        return 1 if out.get("status") in ("failed", "degraded", "interrupted", "uncertain") else 0
    except Busy as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (OSError, ValueError, TypeError, KeyError, notes.RenderError) as exc:
        print("chief: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
