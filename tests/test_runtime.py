import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from chief_of_staff import cli, decisions as D, runtime as R, tasks as T, watch as W
from chief_of_staff.locking import Busy, file_lock


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        R.init(self.root)
        self.auth = dict(actor="Example User", source="chat:instruction")

    def draft(self, **extra):
        return T.change(
            self.root,
            "draft",
            title="Check the result",
            owners=["Example User"],
            **self.auth,
            **extra,
        )

    def promoted(self, **extra):
        row = self.draft(**extra)
        return T.change(
            self.root, "promote", task_id=row["id"], confirmed=True, **self.auth
        )

    def done(self):
        row = self.promoted()
        p = self.root / "result.txt"
        p.write_text("accepted result")
        return (
            T.change(
                self.root,
                "complete",
                task_id=row["id"],
                confirmed=True,
                evidence=["result.txt"],
                **self.auth,
            ),
            p,
        )

    def arm(self):
        for name in ("writes-enabled", "watch-enabled"):
            (R.folder(self.root) / name).touch()

    def test_scratch_never_promotes_itself(self):
        row = self.draft()
        self.assertEqual(R.report(self.root)["tasks"], [])
        with self.assertRaises(ValueError):
            T.change(self.root, "promote", task_id=row["id"], **self.auth)

    def test_promotion_needs_named_responsibility(self):
        row = T.change(self.root, "draft", title="Later", **self.auth)
        with self.assertRaises(ValueError):
            T.change(
                self.root, "promote", task_id=row["id"], confirmed=True, **self.auth
            )

    def test_blocked_work_has_reason_and_next_decision(self):
        row = self.promoted()
        with self.assertRaises(ValueError):
            T.change(
                self.root, "block", task_id=row["id"], reason="Waiting", **self.auth
            )
        T.change(
            self.root,
            "block",
            task_id=row["id"],
            reason="Missing input",
            next_decision="Choose the input",
            **self.auth,
        )
        item = R.report(self.root)["exceptions"][0]
        self.assertTrue(item["owner"])
        self.assertEqual(item["next_decision"], "Choose the input")

    def test_conflicting_responsibility_is_visible(self):
        row = self.promoted()
        T.change(self.root, "assign", task_id=row["id"], owners=["A", "B"], **self.auth)
        self.assertTrue(
            any(e["kind"] == "ownership" for e in R.report(self.root)["exceptions"])
        )

    def test_decision_protection_survives_done_evidence(self):
        row = self.promoted()
        (self.root / "result.txt").write_text("result")
        path = R.folder(self.root) / "decisions.json"

        def record(reg):
            D.record(
                reg,
                "COS-ACCEPT",
                "Keep this choice",
                "Agreed reason",
                "Example User",
                "chat:instruction",
                True,
            )
            D.protect(
                reg,
                "task:" + row["id"],
                "COS-ACCEPT",
                "Example User",
                "chat:instruction",
                True,
            )

        D.mutate(path, record)
        with self.assertRaises(ValueError):
            T.change(
                self.root,
                "complete",
                task_id=row["id"],
                confirmed=True,
                evidence=["result.txt"],
                **self.auth,
            )
        self.assertTrue(
            any(
                e["kind"] == "protected-decision"
                for e in R.report(self.root)["exceptions"]
            )
        )
        D.mutate(
            path,
            lambda reg: D.review(
                reg,
                "COS-ACCEPT",
                "allow-close",
                "Checked result",
                "Example User",
                "chat:instruction",
                True,
            ),
        )
        self.assertEqual(
            T.change(
                self.root,
                "complete",
                task_id=row["id"],
                confirmed=True,
                evidence=["result.txt"],
                **self.auth,
            )["status"],
            "done",
        )

    def test_unknown_or_malformed_decisions_hold(self):
        for ident in ("COS-missing", "", "COS-bad space"):
            self.assertFalse(
                D.guard_task("example", [ident], D.blank_registry(), self.root)[
                    "allowed"
                ]
            )
        for text in (
            "[decision:]",
            "[decision:COS-bad space]",
            "[decision:COS-missing",
        ):
            self.assertFalse(
                D.closure_guard(text, "example", [], D.blank_registry(), self.root)[
                    "allowed"
                ]
            )

    def test_changed_working_files_invalidate_completion(self):
        row, p = self.done()
        p.write_text("changed")
        self.assertFalse(
            D.guard_task(row["id"].lower(), [], D.blank_registry(), self.root)[
                "allowed"
            ]
        )
        self.assertTrue(
            any(
                e["kind"] == "stale-evidence" for e in R.report(self.root)["exceptions"]
            )
        )

    def test_missing_or_symlink_evidence_holds(self):
        row, p = self.done()
        p.unlink()
        self.assertFalse(
            D.guard_task(row["id"], [], D.blank_registry(), self.root)["allowed"]
        )
        other = self.root / "other.txt"
        other.write_text("accepted result")
        p.symlink_to(other)
        self.assertFalse(
            D.guard_task(row["id"], [], D.blank_registry(), self.root)["allowed"]
        )

    def test_reaffirmation_keeps_old_receipt(self):
        row, p = self.done()
        old = row["evidence"]
        p.write_text("checked new result")
        with self.assertRaises(ValueError):
            T.change(
                self.root,
                "complete",
                task_id=row["id"],
                evidence=["result.txt"],
                **self.auth,
            )
        updated = T.change(
            self.root,
            "complete",
            task_id=row["id"],
            confirmed=True,
            evidence=["result.txt"],
            **self.auth,
        )
        self.assertEqual(updated["completion_history"][0]["evidence"], old)
        self.assertTrue(
            D.guard_task(row["id"], [], D.blank_registry(), self.root)["allowed"]
        )

    def test_corrupt_state_preserved(self):
        path = R.folder(self.root) / "decisions.json"
        path.write_text("{broken")
        with self.assertRaises(ValueError):
            D.mutate(path, lambda reg: None)
        self.assertEqual(path.read_text(), "{broken")
        self.assertEqual(R.report(self.root)["status"], "degraded")

    def test_invalid_task_store_is_visible(self):
        T.store_path(self.root).write_text("{broken")
        self.assertEqual(R.report(self.root)["status"], "degraded")

    def test_sensitive_and_escaping_paths_refused(self):
        (self.root / "passwords.txt").write_text("example")
        for path in ("../outside", "/etc/passwd", "passwords.txt", ".git/config"):
            with self.assertRaises(ValueError):
                T.fingerprint(self.root, path)

    def test_reports_disabled_by_default(self):
        self.assertFalse(R.reconcile(self.root)["persisted"])
        self.assertFalse((R.folder(self.root) / "report.json").exists())
        self.arm()
        self.assertTrue(R.reconcile(self.root)["persisted"])

    def test_reconcile_lock_blocks_other_invocation(self):
        with file_lock(R.folder(self.root) / "reconcile.lock"):
            with self.assertRaises(Busy):
                R.reconcile(self.root)

    def test_watch_requires_both_flags(self):
        self.assertEqual(W.tick(self.root, now=1000)["status"], "disabled")
        (R.folder(self.root) / "watch-enabled").touch()
        self.assertEqual(W.tick(self.root, now=1000)["status"], "disabled")

    def test_watch_coalesces_and_deduplicates(self):
        self.arm()
        self.assertEqual(W.tick(self.root, now=1000)["status"], "baselined")
        self.draft()
        self.assertEqual(W.tick(self.root, now=1001)["status"], "coalescing")
        self.assertEqual(W.tick(self.root, now=1032)["status"], "reconciled")
        self.assertEqual(W.tick(self.root, now=1033)["status"], "idle")

    def test_watch_tracks_artifact_changes_without_git_commit(self):
        row, p = self.done()
        self.arm()
        W.tick(self.root, now=1000)
        p.write_text("changed")
        self.assertEqual(W.tick(self.root, now=1001)["status"], "coalescing")
        self.assertEqual(W.tick(self.root, now=1032)["status"], "reconciled")
        self.assertTrue(
            json.loads((R.folder(self.root) / "report.json").read_text())["exceptions"]
        )

    def test_changes_during_report_stay_pending(self):
        self.arm()
        W.tick(self.root, now=1000)
        self.draft()
        W.tick(self.root, now=1001)

        def runner(root):
            self.draft()
            return R.reconcile(root)

        self.assertEqual(
            W.tick(self.root, now=1032, runner=runner)["status"], "reconciled"
        )
        self.assertEqual(W.tick(self.root, now=1033)["status"], "coalescing")

    def test_failed_report_retries_without_acknowledging(self):
        self.arm()
        W.tick(self.root, now=1000)
        self.draft()
        W.tick(self.root, now=1001)

        def failure(root):
            raise OSError("Temporary fixture failure")

        self.assertEqual(
            W.tick(self.root, now=1032, runner=failure)["status"], "failed"
        )
        self.assertEqual(W.tick(self.root, now=1033)["status"], "backoff")
        self.assertEqual(W.tick(self.root, now=1092)["status"], "reconciled")

    def test_interrupted_watch_is_not_success(self):
        self.arm()
        W.tick(self.root, now=1000)
        path = R.folder(self.root) / "watch-cursor.json"
        cursor = json.loads(path.read_text())
        cursor["inflight"] = True
        R.save(path, cursor)
        self.assertEqual(W.tick(self.root, now=1001)["status"], "interrupted")
        self.assertNotIn("success", json.loads(path.read_text()))

    def test_preview_does_not_change_cursor(self):
        self.arm()
        W.tick(self.root, now=1000)
        path = R.folder(self.root) / "watch-cursor.json"
        before = path.read_bytes()
        self.draft()
        self.assertEqual(W.tick(self.root, now=1001, dry_run=True)["status"], "preview")
        self.assertEqual(path.read_bytes(), before)

    def test_state_directory_symlink_refused(self):
        with tempfile.TemporaryDirectory() as second:
            root = Path(second)
            (root / ".chief").symlink_to(R.folder(self.root), target_is_directory=True)
            with self.assertRaises(ValueError):
                R.init(root)

    def test_same_cli_for_either_assistant(self):
        for actor in ("Codex", "Claude"):
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(
                    [
                        "--project",
                        str(self.root),
                        "task",
                        "draft",
                        "--title",
                        actor + " example",
                        "--actor",
                        actor,
                        "--source",
                        "chat:instruction",
                    ]
                )
            self.assertEqual(code, 0)
        self.assertEqual(
            T.load(self.root)["tasks"][0]["status"],
            T.load(self.root)["tasks"][1]["status"],
        )

    def test_lost_state_fails_closed_and_init_cannot_erase_history(self):
        self.done()
        path = R.folder(self.root) / "decisions.json"
        path.unlink()
        self.assertEqual(R.report(self.root)["status"], "degraded")
        with self.assertRaises(ValueError):
            R.init(self.root)
        self.assertFalse(path.exists())

    def test_symlinked_task_state_is_not_read(self):
        path = T.store_path(self.root)
        path.unlink()
        target = self.root / "external.json"
        target.write_text(json.dumps(dict(version=1, tasks=[])))
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            T.load(self.root)
        self.assertEqual(R.report(self.root)["status"], "degraded")

    def test_concurrent_process_gets_busy_exit(self):
        with file_lock(R.folder(self.root) / "reconcile.lock"):
            out = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "chief_of_staff",
                    "--project",
                    str(self.root),
                    "reconcile",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(out.returncode, 75, out.stderr)

    def test_malformed_watch_cursor_is_preserved(self):
        self.arm()
        path = R.folder(self.root) / "watch-cursor.json"
        path.write_text("{broken")
        self.assertEqual(W.tick(self.root, now=1000)["status"], "failed")
        self.assertEqual(path.read_text(), "{broken")

    def test_hourly_check_runs_without_source_edits(self):
        self.arm()
        W.tick(self.root, now=1000)
        self.assertEqual(W.tick(self.root, now=4600)["status"], "coalescing")
        self.assertEqual(W.tick(self.root, now=4631)["status"], "reconciled")

    def test_watch_limits_do_not_advance_cursor(self):
        self.done()
        self.arm()
        W.tick(self.root, now=1000)
        path = R.folder(self.root) / "watch-cursor.json"
        before = path.read_bytes()
        with patch.object(W, "MAX_BYTES", 1):
            self.assertEqual(W.tick(self.root, now=1001)["status"], "failed")
        self.assertEqual(path.read_bytes(), before)

    def test_symlinked_lock_preserves_external_file(self):
        target = self.root / "outside.bin"
        target.write_bytes(b"unchanged external content")
        path = R.folder(self.root) / "reconcile.lock"
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            R.reconcile(self.root)
        self.assertEqual(target.read_bytes(), b"unchanged external content")

    def test_symlinked_cursor_is_rejected_without_reading_or_editing_target(self):
        self.arm()
        target = self.root / "outside-cursor.json"
        content = json.dumps(dict(version=1, baseline="a" * 64))
        target.write_text(content)
        (R.folder(self.root) / "watch-cursor.json").symlink_to(target)
        self.assertEqual(W.tick(self.root, now=1000)["status"], "failed")
        self.assertEqual(target.read_text(), content)

    def test_boolean_task_version_is_invalid(self):
        T.store_path(self.root).write_text(json.dumps(dict(version=True, tasks=[])))
        with self.assertRaises(ValueError):
            T.load(self.root)

    def test_symlinked_enable_flag_cannot_enable_reports(self):
        target = self.root / "flag.txt"
        target.write_text("external flag")
        (R.folder(self.root) / "writes-enabled").symlink_to(target)
        self.assertFalse(R.reconcile(self.root)["persisted"])
        self.assertEqual(target.read_text(), "external flag")

    def test_oversized_saved_state_fails_before_json_parsing(self):
        path = T.store_path(self.root)
        path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            T.load(self.root)
        self.assertEqual(R.report(self.root)["status"], "degraded")

    def test_cli_failure_exits_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                cli.main(
                    [
                        "--project",
                        str(self.root),
                        "task",
                        "complete",
                        "CT-missing",
                        "--actor",
                        "Example User",
                        "--source",
                        "chat:instruction",
                        "--confirmed",
                        "--evidence",
                        "missing",
                    ]
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
