#!/usr/bin/env python3
"""Explicit scratch-to-commitment promotion and actionable Chief exceptions.

Drafts stay outside active work until an explicit promotion. This store supplements,
rather than rewrites, the ledger and source systems. Each change records actor and source.
"""

from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

from . import atomic as atomicio

ROOT = Path.cwd()
STATUSES = {"scratch", "open", "blocked", "done"}


def now_text():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def store_path(root):
    return atomicio.state_dir(root) / "tasks.json"


def load(root):
    path = store_path(root)
    if path.is_symlink():
        raise ValueError("Task store is a symlink")
    if not path.exists():
        raise ValueError("Task store is missing; initialise or restore the project")
    data = json.loads(atomicio.read_text(path))
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data.get("version") != 1
        or not isinstance(data.get("tasks"), list)
    ):
        raise ValueError("invalid Chief task store")
    ids = set()
    for t in data["tasks"]:
        if (
            not isinstance(t, dict)
            or not re.fullmatch(r"CT-[0-9a-f]{12}", str(t.get("id", "")))
            or t["id"] in ids
            or t.get("status") not in STATUSES
            or not isinstance(t.get("owners"), list)
            or not all(isinstance(x, str) and x.strip() for x in t["owners"])
            or not isinstance(t.get("decision_ids"), list)
            or not all(
                isinstance(x, str)
                and re.fullmatch(r"COS-[A-Za-z0-9][A-Za-z0-9_-]{0,63}", x)
                for x in t["decision_ids"]
            )
            or not isinstance(t.get("title"), str)
            or not t["title"].strip()
            or not isinstance(t.get("history"), list)
        ):
            raise ValueError("invalid Chief task record")
        if t["status"] in {"open", "blocked"} and not t["owners"]:
            raise ValueError("promoted task has no owner")
        if t["status"] == "blocked" and (
            not t.get("blocked_reason") or not t.get("next_decision")
        ):
            raise ValueError("blocked task lacks reason or next decision")
        ids.add(t["id"])
    return data


@contextlib.contextmanager
def locked(root):
    from .locking import file_lock

    path = store_path(root)
    with file_lock(path.with_suffix(".lock")):
        yield


def fingerprint(root, relative):
    path = Path(relative)
    resolved = (Path(root) / path).resolve()
    if (
        path.is_absolute()
        or ".." in path.parts
        or not resolved.is_relative_to(Path(root).resolve())
    ):
        raise ValueError("evidence must be a file inside the project")
    if (
        any(p.startswith("secrets") or p.startswith(".") for p in path.parts)
        or ".git" in path.parts
    ):
        raise ValueError("evidence must be a regular project artifact")
    from . import decisions as chief_decisions

    resolved = chief_decisions._file(Path(root), path.as_posix())
    return {
        "path": path.as_posix(),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def change(
    root,
    action,
    *,
    task_id="",
    title="",
    owners=None,
    decision_ids=None,
    actor="",
    source="",
    confirmed=False,
    reason="",
    next_decision="",
    evidence=None,
):
    if any(
        not isinstance(x, str)
        or not re.fullmatch(r"COS-[A-Za-z0-9][A-Za-z0-9_-]{0,63}", x)
        for x in (decision_ids or [])
    ):
        raise ValueError("invalid linked decision ID")
    if any(not str(x).strip() for x in (owners or [])):
        raise ValueError("owners must be nonempty names")
    if not actor.strip() or not source.strip():
        raise ValueError("actor and source are required")
    if action in {"promote", "complete"} and not confirmed:
        raise ValueError(
            "explicit confirmation is required for promotion or completion"
        )
    with contextlib.ExitStack() as stack:
        if action == "complete":
            from . import decisions as chief_decisions

            stack.enter_context(
                chief_decisions.registry_lock(
                    atomicio.state_dir(root) / "decisions.json"
                )
            )
        stack.enter_context(locked(root))
        data = load(root)
        if action == "draft":
            if not title.strip():
                raise ValueError("title is required")
            t = dict(
                id="CT-" + uuid.uuid4().hex[:12],
                title=title.strip(),
                status="scratch",
                owners=list(dict.fromkeys(owners or [])),
                decision_ids=list(dict.fromkeys(decision_ids or [])),
                created_at=now_text(),
                history=[],
            )
            data["tasks"].append(t)
        else:
            t = next((x for x in data["tasks"] if x["id"] == task_id), None)
            if t is None:
                raise ValueError("unknown task")
            if t["status"] == "done" and action != "complete":
                raise ValueError("completed task is immutable")
            if action == "promote":
                if t["status"] != "scratch":
                    raise ValueError("only scratch work can be promoted")
                chosen = list(dict.fromkeys(owners or t["owners"]))
                if not chosen:
                    raise ValueError("promotion needs an owner")
                t.update(status="open", owners=chosen, promoted_at=now_text())
            elif action == "assign":
                if not owners:
                    raise ValueError("assignment needs an owner")
                t["owners"] = list(dict.fromkeys(owners))
            elif action == "block":
                if (
                    t["status"] == "scratch"
                    or not reason.strip()
                    or not next_decision.strip()
                ):
                    raise ValueError(
                        "block a promoted task with reason and next decision"
                    )
                t.update(
                    status="blocked",
                    blocked_reason=reason.strip(),
                    next_decision=next_decision.strip(),
                )
            elif action == "resume":
                if t["status"] != "blocked":
                    raise ValueError("only blocked tasks can resume")
                t.update(status="open", blocked_reason="", next_decision="")
            elif action == "complete":
                if t["status"] == "scratch" or not evidence:
                    raise ValueError(
                        "completion needs a promoted task and current artifact evidence"
                    )
                from . import decisions as chief_decisions

                registry = chief_decisions.load_registry(
                    atomicio.state_dir(root) / "decisions.json"
                )
                guard = chief_decisions.guard_task(
                    t["id"],
                    t["decision_ids"],
                    registry,
                    Path(root),
                    check_completion=False,
                )
                if not guard["allowed"]:
                    raise ValueError("completion blocked: " + guard["reason"])
                snapshots = [fingerprint(root, path) for path in evidence]
                # Compare again immediately before the local transition. This is file evidence,
                # not a claim that the artifact semantically proves the task is complete.
                if any(fingerprint(root, item["path"]) != item for item in snapshots):
                    raise ValueError("completion evidence changed")
                if t["status"] == "done":
                    t.setdefault("completion_history", []).append(
                        {
                            "completed_at": t.get("completed_at"),
                            "evidence": t.get("evidence"),
                        }
                    )
                t.update(status="done", completed_at=now_text(), evidence=snapshots)
            else:
                raise ValueError("unknown task action")
        t["history"].append(
            dict(
                at=now_text(),
                action=action,
                actor=actor.strip(),
                source=source.strip(),
                confirmed=bool(confirmed),
            )
        )
        atomicio.write_text(
            store_path(root), json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        )
        return t


def focus(root, now, jobs=(), threads=()):
    """Read-only exceptions. Scratch drafts never become a day's commitments implicitly."""
    out = dict(tasks=[], exceptions=[], counts={}, error="")
    try:
        tasks = load(root)["tasks"]
    except (ValueError, OSError) as exc:
        out["error"] = str(exc)
        out["exceptions"].append(
            dict(
                kind="state-error",
                title="Chief task store needs repair",
                owner="maintainer",
                reason=str(exc),
                next_decision="Repair the task store before changing commitments.",
            )
        )
        return out
    out["counts"] = {s: sum(t["status"] == s for t in tasks) for s in sorted(STATUSES)}
    for task in tasks:
        if task["status"] == "done":
            try:
                saved = task.get("evidence", [])
                if not saved or any(
                    fingerprint(root, item["path"]) != item for item in saved
                ):
                    raise ValueError(
                        "Completed artifact changed or has no current evidence."
                    )
            except (ValueError, OSError, TypeError, KeyError) as exc:
                out["exceptions"].append(
                    dict(
                        kind="stale-evidence",
                        title=task["title"],
                        task_id=task["id"],
                        owner=", ".join(task["owners"]) or "unassigned",
                        reason=str(exc),
                        next_decision="Recheck the result and explicitly reaffirm completion with current evidence.",
                    )
                )
            continue
        if task["status"] == "scratch":
            continue
        item = {
            k: task.get(k)
            for k in (
                "id",
                "title",
                "status",
                "owners",
                "decision_ids",
                "blocked_reason",
                "next_decision",
            )
        }
        out["tasks"].append(item)
        if len(task["owners"]) != 1:
            out["exceptions"].append(
                dict(
                    kind="ownership",
                    title=task["title"],
                    task_id=task["id"],
                    owner=", ".join(task["owners"]),
                    reason="Multiple owners are recorded.",
                    next_decision="Choose one accountable owner and keep collaborators separate.",
                )
            )
        if task["status"] == "blocked":
            out["exceptions"].append(
                dict(
                    kind="blocked",
                    title=task["title"],
                    task_id=task["id"],
                    owner=", ".join(task["owners"]),
                    reason=task["blocked_reason"],
                    next_decision=task["next_decision"],
                )
            )
    latest_ok = {}
    for job in jobs:
        if job.get("status") == "ok" and job.get("slot"):
            latest_ok[job["job"]] = max(
                job["slot"], latest_ok.get(job["job"], job["slot"])
            )
    seen = set()
    for job in sorted(jobs, key=lambda r: r.get("slot") or now, reverse=True):
        status, slot, name = (
            job.get("status"),
            job.get("slot"),
            job.get("job", "unknown"),
        )
        if status in {"ok", "running"} or not slot:
            continue
        if (
            (now - slot).total_seconds() > 86400
            or latest_ok.get(name, slot) > slot
            or name in seen
        ):
            continue
        seen.add(name)
        reason = "; ".join(
            job.get("problems")
            or job.get("notes")
            or [job.get("note") or status or "unknown status"]
        )
        status = (
            status if status in {"failed", "skipped", "missing", "died"} else "unknown"
        )
        out["exceptions"].append(
            dict(
                kind="job-" + status,
                title=name,
                owner="Chief / job maintainer",
                reason=reason,
                next_decision="Check the blocking cause, then retry the permitted job or leave it stopped.",
            )
        )
    for thread in threads:
        text = thread.get("thread", "")
        if re.search(r"\b(blocked|paused|waiting on|uncertain)\b", text, re.I):
            out["exceptions"].append(
                dict(
                    kind="open-thread",
                    title=text[:240],
                    owner="unassigned",
                    reason="The ledger records unfinished or uncertain work.",
                    next_decision="Confirm one owner and the dependency or approval needed to continue.",
                )
            )
    # Stable ids allow clients to compare exceptions without creating another task per poll.
    for e in out["exceptions"]:
        e["id"] = hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest()[
            :16
        ]
    return out
