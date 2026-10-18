"""Show how a changed artifact becomes an attention item in a temporary project."""

import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chief_of_staff import runtime, tasks

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    runtime.init(root)
    auth = dict(actor="Example User", source="chat:example")
    task = tasks.change(
        root, "draft", title="Check the export", owners=["Example User"], **auth
    )
    tasks.change(root, "promote", task_id=task["id"], confirmed=True, **auth)
    (root / "result.txt").write_text("checked result")
    tasks.change(
        root,
        "complete",
        task_id=task["id"],
        confirmed=True,
        evidence=["result.txt"],
        **auth,
    )
    (root / "result.txt").write_text("changed after the check")
    print(json.dumps(runtime.report(root), indent=2))
