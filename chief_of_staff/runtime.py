"""Reconcile local records into an attention report. No external command execution."""

import datetime as dt
import hashlib
import json
from pathlib import Path
from . import atomic, decisions, tasks
from .locking import file_lock


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def folder(root):
    return atomic.state_dir(root)


def enabled(root, name):
    path = folder(root) / name
    return path.is_file() and not path.is_symlink()


def save(path, value):
    atomic.write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def init(root):
    root = Path(root).resolve()
    base = folder(root)
    base.mkdir(parents=True, exist_ok=True)
    with file_lock(base / "init.lock"):
        present = [(base / name).exists() for name in ("decisions.json", "tasks.json")]
        if ((base / "project.json").exists() and not all(present)) or (
            any(present) and not all(present)
        ):
            raise ValueError(
                "Project state is incomplete; restore the missing record before initialising"
            )
        if not any(present):
            save(base / "decisions.json", decisions.blank_registry())
            save(base / "tasks.json", dict(version=1, tasks=[]))
        decisions.load_registry(base / "decisions.json")
        tasks.load(root)
        if not (base / "project.json").exists():
            save(base / "project.json", dict(version=1))
    return dict(
        project=str(root),
        state=str(base),
        writes_enabled=enabled(root, "writes-enabled"),
        watch_enabled=enabled(root, "watch-enabled"),
    )


def report(root):
    root = Path(root).resolve()
    out = tasks.focus(root, dt.datetime.now(dt.timezone.utc))
    try:
        reg = decisions.load_registry(folder(root) / "decisions.json")
        for task in tasks.load(root)["tasks"]:
            if task["status"] == "scratch":
                continue
            guard = decisions.guard_task(task["id"], task["decision_ids"], reg, root)
            if not guard["allowed"] and not any(
                e.get("task_id") == task["id"] and e["kind"] == guard["reason"]
                for e in out["exceptions"]
            ):
                out["exceptions"].append(
                    dict(
                        kind=guard["reason"],
                        task_id=task["id"],
                        title=task["title"],
                        owner=", ".join(task["owners"]) or "unassigned",
                        reason=guard["detail"],
                        next_decision="Review the decision or refresh its evidence before using completion.",
                    )
                )
    except (OSError, ValueError) as exc:
        out["error"] = str(exc)
        out["exceptions"].append(
            dict(
                kind="state-error",
                title="Decision records need repair",
                owner="maintainer",
                reason=str(exc),
                next_decision="Repair saved state before changing commitments.",
            )
        )
    out["at"] = now()
    out["status"] = (
        "degraded" if out["error"] else "attention" if out["exceptions"] else "ok"
    )
    return out


def render(value):
    lines = ["Chief of Staff", value["at"], "", "What needs attention"]
    if not value["exceptions"]:
        lines.append("No current exceptions are recorded.")
    for item in value["exceptions"]:
        lines += [
            "",
            item["title"],
            "Reason: " + item["reason"],
            "Responsible: " + item["owner"],
            "Next: " + item["next_decision"],
        ]
    lines += ["", "Confirmed commitments"]
    for item in value["tasks"]:
        lines.append(item["id"] + ": " + item["title"] + " (" + item["status"] + ")")
    lines.append("Scratch drafts: " + str(value["counts"].get("scratch", 0)))
    return "\n".join(lines) + "\n"


def reconcile(root, *, dry_run=False):
    root = Path(root).resolve()
    base = folder(root)
    with file_lock(base / "reconcile.lock", blocking=False):
        value = report(root)
        if dry_run or not enabled(root, "writes-enabled"):
            return dict(
                value,
                persisted=False,
                reason="Preview only; enable report writes explicitly.",
            )
        save(base / "report.json", value)
        atomic.write_text(base / "note.txt", render(value))
        return dict(value, persisted=True)
