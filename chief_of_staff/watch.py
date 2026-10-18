"""Coalesce local changes and retry report reconciliation with a durable cursor."""

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from . import atomic, decisions, runtime, tasks
from .locking import Busy, file_lock

MAX_FILES = 2000
MAX_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024


def snapshot(root, now):
    root = Path(root).resolve()
    base = runtime.folder(root)
    paths = {".chief/tasks.json", ".chief/decisions.json"}
    reg = decisions.load_registry(base / "decisions.json")
    rows = tasks.load(root)["tasks"]
    for task in rows:
        for item in task.get("evidence", []):
            paths.add(item["path"])
    for row in reg["tasks"].values():
        for item in row["evidence"]:
            paths.add(item["path"])
    if len(paths) > MAX_FILES:
        raise ValueError("Watch file limit exceeded")
    values = []
    total = 0
    for relative in sorted(paths):
        if relative.startswith(".chief/"):
            path = root / relative
            if path.is_symlink():
                raise ValueError("State path is a symlink")
        else:
            try:
                path = decisions._file(root, relative)
            except (OSError, ValueError):
                values.append([relative, "missing-or-invalid"])
                continue
        if not path.exists():
            values.append([relative, "missing"])
            continue
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_BYTES:
            raise ValueError("Watch byte limit exceeded")
        values.append([relative, hashlib.sha256(data).hexdigest()])
    return hashlib.sha256(
        json.dumps(dict(files=values, hour=int(now // 3600)), sort_keys=True).encode()
    ).hexdigest()


def load_cursor(path):
    if path.is_symlink():
        raise ValueError("Watch cursor is a symlink")
    if not path.exists():
        return {}
    value = json.loads(atomic.read_text(path))
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value.get("version") != 1
    ):
        raise ValueError("Invalid watch cursor")
    for key in ("baseline", "observed", "success"):
        if key in value and (
            not isinstance(value[key], str)
            or len(value[key]) != 64
            or any(c not in "0123456789abcdef" for c in value[key])
        ):
            raise ValueError("Invalid watch digest")
    for key in ("observed_at", "pending_at", "retry_at", "failures"):
        if key in value and (
            type(value[key]) not in (int, float)
            or not math.isfinite(value[key])
            or value[key] < 0
        ):
            raise ValueError("Invalid watch timing")
    if "inflight" in value and type(value["inflight"]) is not bool:
        raise ValueError("Invalid watch intent")
    return value


def tick(root, *, dry_run=False, now=None, runner=runtime.reconcile):
    root = Path(root).resolve()
    base = runtime.folder(root)
    now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
    result = dict(at=dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat())
    try:
        with file_lock(base / "watch.lock", blocking=False):
            if not dry_run and not all(
                runtime.enabled(root, name)
                for name in ("writes-enabled", "watch-enabled")
            ):
                return dict(result, status="disabled")
            digest = snapshot(root, now)
            path = base / "watch-cursor.json"
            cursor = load_cursor(path)
            if dry_run:
                return dict(
                    result,
                    status="preview",
                    changed=digest != cursor.get("success", cursor.get("baseline")),
                )
            if not cursor:
                cursor = dict(
                    version=1,
                    baseline=digest,
                    observed=digest,
                    observed_at=now,
                    failures=0,
                )
                status = "baselined"
            elif cursor.pop("inflight", False):
                cursor["failures"] = min(cursor.get("failures", 0) + 1, 30)
                cursor["retry_at"] = now + 60
                status = "interrupted"
            elif digest == cursor.get("success", cursor.get("baseline")):
                cursor.pop("pending_at", None)
                status = "idle"
            else:
                cursor.setdefault("pending_at", now)
                if digest != cursor.get("observed"):
                    cursor.update(observed=digest, observed_at=now)
                if now < cursor.get("retry_at", 0):
                    status = "backoff"
                elif (
                    now - cursor["observed_at"] < 30
                    and now - cursor["pending_at"] < 300
                ):
                    status = "coalescing"
                else:
                    cursor["inflight"] = True
                    runtime.save(path, cursor)
                    try:
                        if not all(
                            runtime.enabled(root, name)
                            for name in ("writes-enabled", "watch-enabled")
                        ):
                            raise ValueError("Disabled before reconcile")
                        outcome = runner(root)
                        if outcome.get("status") == "degraded" or not outcome.get(
                            "persisted"
                        ):
                            raise ValueError("Report reconciliation did not complete")
                        cursor.update(
                            success=digest, failures=0, last_success_at=result["at"]
                        )
                        cursor.pop("pending_at", None)
                        cursor.pop("retry_at", None)
                        status = "reconciled"
                    except Exception as exc:
                        cursor["failures"] = min(cursor.get("failures", 0) + 1, 30)
                        cursor["retry_at"] = now + min(
                            1800, 60 * 2 ** (cursor["failures"] - 1)
                        )
                        status = "failed"
                        result["reason"] = str(exc)
                    cursor.pop("inflight", None)
            runtime.save(path, cursor)
            result.update(
                status=status,
                failures=cursor.get("failures", 0),
                retry_at=cursor.get("retry_at"),
            )
            runtime.save(base / "watch-status.json", result)
            return result
    except Busy:
        return dict(result, status="busy")
    except (ValueError, OSError, TypeError, KeyError) as exc:
        result.update(status="failed", reason=str(exc))
        runtime.save(base / "watch-status.json", result)
        return result
