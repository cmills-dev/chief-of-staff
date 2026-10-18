#!/usr/bin/env python3
"""Explicit decision protection and file evidence for Chief's deterministic closure rules.

The CLI records a human instruction, not a model's inference. Each binding change needs
--human-confirmed, an actor and a source pointing to that instruction. Existing project
notes is never promoted into a binding decision automatically.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from . import atomic

ROOT = Path.cwd()
REGISTRY = ROOT / ".chief/decisions.json"
ID = re.compile(r"COS-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DECISION_REF = re.compile(r"\[decision:([^\]\s]+)\]", re.I)
SHA = re.compile(r"[0-9a-f]{64}$")


class RegistryError(ValueError):
    pass


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def blank_registry():
    return {"version": 1, "decisions": {}, "tasks": {}}


def thread_key(sid, text):
    norm = " ".join(text.lower().split()).rstrip(".")
    return "thread:" + hashlib.sha256((sid + "\n" + norm).encode()).hexdigest()


def task_key(value):
    """Stable exact identifiers, not substring matches on task names."""
    if value.startswith("task:"):
        value = "task:" + value[5:].lower()
    if not re.fullmatch(
        r"(?:obligation|card|branch|task):[A-Za-z0-9_.\-/]+|thread:[0-9a-f]{64}", value
    ):
        raise RegistryError("Invalid task key: " + value)
    if any(part in (".", "..", "") for part in value.split(":", 1)[1].split("/")):
        raise RegistryError("Unsafe task key: " + value)
    return value


def _required_text(row, fields):
    if not isinstance(row, dict) or any(
        not isinstance(row.get(k), str) or not row[k].strip() for k in fields
    ):
        raise RegistryError("Missing or invalid fields: " + ", ".join(fields))


def _provenance(row):
    _required_text(row, ("actor", "source", "at"))
    if not re.match(r"(?:chat|user|mail|ledger|file|https?):\S+", row["source"]):
        raise RegistryError(
            "Source must identify a chat, user instruction, mail, ledger entry, file or URL"
        )
    if row.get("human_confirmed") is not True:
        raise RegistryError("Binding change lacks explicit human confirmation")


def validate(reg):
    if (
        not isinstance(reg, dict)
        or type(reg.get("version")) is not int
        or reg.get("version") != 1
    ):
        raise RegistryError("Invalid decision registry version")
    if not isinstance(reg.get("decisions"), dict) or not isinstance(
        reg.get("tasks"), dict
    ):
        raise RegistryError("Invalid decision registry stores")
    for ident, row in reg["decisions"].items():
        if not ID.fullmatch(ident):
            raise RegistryError("Invalid decision ID")
        _required_text(row, ("reason", "decision"))
        _provenance(row)
        if type(row.get("requires_human_review")) is not bool or not isinstance(
            row.get("reviews"), list
        ):
            raise RegistryError("Invalid decision review state")
        for review in row["reviews"]:
            _required_text(review, ("reason", "outcome"))
            _provenance(review)
            if review["outcome"] not in ("allow-close", "keep-open"):
                raise RegistryError("Invalid decision review outcome")
        # An unreviewed or held decision cannot be silently made unprotected by editing a flag.
        allowed = bool(
            row["reviews"] and row["reviews"][-1]["outcome"] == "allow-close"
        )
        if row["requires_human_review"] == allowed:
            raise RegistryError(
                "Decision review flag disagrees with its recorded review"
            )
    for key, row in reg["tasks"].items():
        if task_key(key) != key:
            raise RegistryError("Non-canonical task key")
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("decisions"), list)
            or not isinstance(row.get("evidence"), list)
        ):
            raise RegistryError("Invalid task record")
        for link in row["decisions"]:
            _required_text(link, ("decision_id",))
            _provenance(link)
            if link["decision_id"] not in reg["decisions"]:
                raise RegistryError("Task links an unknown decision")
        for evidence in row["evidence"]:
            _required_text(evidence, ("path", "sha256", "assumption"))
            _provenance(evidence)
            p = Path(evidence["path"])
            if (
                p.is_absolute()
                or ".." in p.parts
                or not p.parts
                or not SHA.fullmatch(evidence["sha256"])
            ):
                raise RegistryError("Invalid evidence snapshot")
    return reg


def load_registry(path=None):
    path = Path(path or REGISTRY)
    if path.is_symlink():
        raise RegistryError("Decision registry is a symlink")
    if not path.exists():
        raise RegistryError(
            "Decision registry is missing; initialise or restore the project"
        )
    try:
        return validate(json.loads(atomic.read_text(path)))
    except (OSError, ValueError, TypeError) as exc:
        raise RegistryError("Decision registry cannot be trusted: " + str(exc)) from exc


@contextmanager
def registry_lock(path):
    from .locking import file_lock

    path = Path(path)
    with file_lock(path.with_suffix(path.suffix + ".lock")):
        yield


def mutate(path, operation):
    path = Path(path)
    with registry_lock(path):
        reg = load_registry(path)
        result = operation(reg)
        validate(reg)
        fd, name = tempfile.mkstemp(prefix=".decisions-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(reg, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return result


def provenance(actor, source, human_confirmed):
    row = {
        "actor": actor,
        "source": source,
        "human_confirmed": human_confirmed,
        "at": timestamp(),
    }
    _provenance(row)
    return row


def record(reg, ident, decision, reason, actor, source, human_confirmed=False):
    if not ID.fullmatch(ident):
        raise RegistryError("Use a stable COS- decision ID")
    if ident in reg["decisions"]:
        raise RegistryError(
            "Decision already exists; retain its original reason and record a review"
        )
    reg["decisions"][ident] = {
        "decision": decision,
        "reason": reason,
        "requires_human_review": True,
        "reviews": [],
        **provenance(actor, source, human_confirmed),
    }
    validate(reg)
    return ident


def protect(reg, task, ident, actor, source, human_confirmed=False):
    task = task_key(task)
    if ident not in reg["decisions"]:
        raise RegistryError("Unknown decision ID: " + ident)
    row = reg["tasks"].setdefault(task, {"decisions": [], "evidence": []})
    if any(link["decision_id"] == ident for link in row["decisions"]):
        return task
    row["decisions"].append(
        {"decision_id": ident, **provenance(actor, source, human_confirmed)}
    )
    return task


def review(reg, ident, outcome, reason, actor, source, human_confirmed=False):
    if ident not in reg["decisions"] or outcome not in ("allow-close", "keep-open"):
        raise RegistryError("Unknown decision or review outcome")
    reg["decisions"][ident]["reviews"].append(
        {
            "outcome": outcome,
            "reason": reason,
            **provenance(actor, source, human_confirmed),
        }
    )
    reg["decisions"][ident]["requires_human_review"] = outcome != "allow-close"
    return ident


def _file(root, rel):
    root = Path(root).resolve()
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise RegistryError("Evidence must be an exact relative repository path")
    if any(
        re.search(r"(?:secret|credential|password|token|\.env)", part, re.I)
        for part in p.parts
    ):
        raise RegistryError("Sensitive files cannot be evidence snapshots")
    target = root / p
    # Reject symlinks even when they currently point inside the repository.
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise RegistryError("Evidence path contains a symlink: " + rel)
    if not target.is_file() or not target.resolve().is_relative_to(root):
        raise RegistryError("Evidence file is missing or outside repository: " + rel)
    return target


def snapshot(
    reg, task, path, assumption, actor, source, human_confirmed=False, root=None
):
    task = task_key(task)
    path = Path(path).as_posix()
    target = _file(root or ROOT, path)
    evidence = {
        "path": path,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "assumption": assumption,
        **provenance(actor, source, human_confirmed),
    }
    _required_text(evidence, ("assumption",))
    row = reg["tasks"].setdefault(task, {"decisions": [], "evidence": []})
    # A new snapshot replaces the exact path's old expectation and keeps an audit history.
    old = [ev for ev in row["evidence"] if ev["path"] == path]
    if old:
        row.setdefault("evidence_history", []).extend(old)
    row["evidence"] = [ev for ev in row["evidence"] if ev["path"] != path] + [evidence]
    return task


def closure_guard(thread, sid, refs, registry, root=None):
    """Check explicitly linked decisions and declared file assumptions.

    No snapshot means the existing completion rules still apply. Declared snapshots and
    protected decisions add requirements. They never count as proof of completion alone.
    """
    try:
        validate(registry)
        keys = [thread_key(sid, thread)]
        kinds = {
            "obligations": "obligation",
            "cards": "card",
            "branches": "branch",
            "tasks": "task",
        }
        for ref in refs:
            if isinstance(ref, dict):
                keys.append(task_key(ref["kind"] + ":" + ref["id"]))
            else:
                keys.append(task_key(kinds.get(ref[0], ref[0]) + ":" + ref[1]))
        starts = list(re.finditer(r"\[decision:", thread, re.I))
        for start in starts:
            marker = re.match(r"\[decision:([^\]\s]+)\]", thread[start.start() :], re.I)
            if not marker or not ID.fullmatch(marker.group(1)):
                return {
                    "allowed": False,
                    "reason": "unknown-decision",
                    "detail": "Malformed decision reference needs review",
                }
        ids = set(DECISION_REF.findall(thread))
        for key in set(keys):
            task = registry["tasks"].get(key, {})
            ids.update(link["decision_id"] for link in task.get("decisions", []))
            for ev in task.get("evidence", []):
                try:
                    target = _file(root or ROOT, ev["path"])
                    current = hashlib.sha256(target.read_bytes()).hexdigest()
                except (OSError, RegistryError) as exc:
                    return {
                        "allowed": False,
                        "reason": "stale-evidence",
                        "detail": str(exc),
                    }
                if current != ev["sha256"]:
                    return {
                        "allowed": False,
                        "reason": "stale-evidence",
                        "detail": "%s changed since snapshot; assumption: %s"
                        % (ev["path"], ev["assumption"]),
                    }
        for ident in sorted(ids):
            if ident not in registry["decisions"]:
                return {"allowed": False, "reason": "unknown-decision", "detail": ident}
            if registry["decisions"][ident]["requires_human_review"]:
                return {
                    "allowed": False,
                    "reason": "protected-decision",
                    "detail": ident,
                }
        return {"allowed": True, "reason": "", "detail": ""}
    except (ValueError, TypeError, KeyError) as exc:
        return {
            "allowed": False,
            "reason": "decision-registry-error",
            "detail": str(exc),
        }


def guard_task(task_id, decision_ids, registry, root=None, *, check_completion=True):
    """Task completion guard for chief_tasks, with explicitly linked decision IDs."""
    if not isinstance(decision_ids, (list, tuple, set)) or any(
        not isinstance(ident, str) or not ID.fullmatch(ident) for ident in decision_ids
    ):
        return {
            "allowed": False,
            "reason": "decision-registry-error",
            "detail": "Invalid linked decision ID",
        }
    text = " ".join("[decision:%s]" % ident for ident in decision_ids)
    result = closure_guard(text, "", [{"kind": "task", "id": task_id}], registry, root)
    if not result["allowed"] or not check_completion:
        return result
    try:
        from . import tasks as chief_tasks

        actual_root = Path(root or ROOT)
        task = next(
            (
                t
                for t in chief_tasks.load(actual_root)["tasks"]
                if t["id"].lower() == task_id.lower()
            ),
            None,
        )
        if task and task["status"] == "done":
            evidence = task.get("evidence")
            if not evidence or not isinstance(evidence, list):
                raise RegistryError("Completed task lacks artifact evidence")
            for saved in evidence:
                current = chief_tasks.fingerprint(actual_root, saved["path"])
                if current != saved:
                    raise RegistryError(
                        "Completed task artifact changed: " + saved["path"]
                    )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"allowed": False, "reason": "stale-evidence", "detail": str(exc)}
    return result
