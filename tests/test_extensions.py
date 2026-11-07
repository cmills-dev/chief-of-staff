import contextlib
import datetime as dt
import io
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from chief_of_staff import (
    audit, booking, cli, drafts, notes, outreach, policy, runtime as R,
)

UTC = dt.timezone.utc


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        R.init(self.root)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--project", str(self.root), *argv])
        return code, json.loads(out.getvalue()) if out.getvalue().strip() else {}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = policy.Policy()

    def test_payment_and_deletion_are_the_hard_denies(self):
        self.assertEqual(self.p.check_text("please pay the invoice").category, "payment")
        self.assertEqual(self.p.check_text("rm -rf build").category, "file-deletion")
        self.assertEqual(self.p.check_text("find . -name x -delete").category, "file-deletion")
        self.assertTrue(self.p.check_text("read the report and write a summary").allowed)
        self.assertTrue(self.p.check_text("run the formatter").allowed)

    def test_other_actions_are_not_denied(self):
        for text in ("apply for the role", "create an account", "print the token name"):
            self.assertTrue(self.p.check_text(text).allowed, text)

    def test_outbound_is_draft_only(self):
        self.assertTrue(self.p.check_outbound("draft").allowed)
        for verb in ("send", "reply", "forward", "post"):
            v = self.p.check_outbound(verb)
            self.assertFalse(v.allowed)
            self.assertEqual(v.category, "draft-only")
        self.assertTrue(policy.Policy(outbound="allow").check_outbound("send").allowed)
        with self.assertRaises(ValueError):
            policy.Policy(outbound="maybe")

    def test_push_is_denied_for_every_role(self):
        for role in ("chief", "build", "reviewer"):
            for cmd in ("git push", "git push origin main", "git -C /x push --force"):
                self.assertFalse(self.p.check_git(role, cmd, "main").allowed, (role, cmd))

    def test_chief_merges_no_ff_into_main(self):
        self.assertTrue(self.p.check_git("chief", "git merge --no-ff feature/x", "main").allowed)
        self.assertTrue(self.p.check_git("chief", "git commit -m x", "main").allowed)
        self.assertFalse(self.p.check_git("chief", "git merge feature/x", "main").allowed)
        self.assertFalse(self.p.check_git("chief", "git merge --squash x", "main").allowed)
        self.assertFalse(self.p.check_git("chief", "git commit --amend", "main").allowed)
        self.assertFalse(self.p.check_git("chief", "git rebase main", "main").allowed)
        self.assertFalse(self.p.check_git("chief", "git reset --hard", "main").allowed)

    def test_other_roles_keep_the_review_first_path(self):
        v = self.p.check_git("build", "git merge --no-ff feature/x", "main")
        self.assertEqual((v.allowed, v.category), (False, "review-first"))
        self.assertFalse(self.p.check_git("build", "git commit -m x", "main").allowed)
        self.assertTrue(self.p.check_git("build", "git commit -m x", "feature/x").allowed)
        self.assertTrue(self.p.check_git("build", "git diff main", "main").allowed)

    def test_git_deletion_is_denied_for_the_chief_too(self):
        for cmd in ("git rm file", "git branch -D old", "git worktree remove ../w", "git clean -fd"):
            self.assertFalse(self.p.check_git("chief", cmd, "main").allowed, cmd)
        self.assertTrue(self.p.check_git("chief", "git worktree add ../w", "main").allowed)

    def test_policy_file_changes_roles_and_branch(self):
        p = policy.Policy.from_dict(
            dict(main_branch="trunk", merge_roles=["maintainer"],
                 extra_patterns={"payment": [r"\bwire\b"]})
        )
        self.assertTrue(p.check_git("maintainer", "git merge --no-ff x", "trunk").allowed)
        self.assertFalse(p.check_git("chief", "git merge --no-ff x", "trunk").allowed)
        self.assertFalse(p.check_text("wire the funds").allowed)
        with self.assertRaises(ValueError):
            policy.Policy.from_dict([])


class PolicyCliTests(Base):
    def test_cli_exit_codes(self):
        code, out = self.run_cli("policy", "--text", "buy a licence")
        self.assertEqual((code, out["allowed"]), (2, False))
        code, out = self.run_cli("policy", "--role", "chief", "--branch", "main",
                                 "--git", "git merge --no-ff x")
        self.assertEqual((code, out["allowed"]), (0, True))
        code, _ = self.run_cli("policy", "--role", "chief", "--git", "git push")
        self.assertEqual(code, 2)
        code, _ = self.run_cli("policy", "--outbound", "send")
        self.assertEqual(code, 2)

    def test_policy_file_is_loaded(self):
        (R.folder(self.root) / "policy.json").write_text(json.dumps(dict(outbound="allow")))
        code, out = self.run_cli("policy", "--outbound", "send")
        self.assertEqual((code, out["allowed"]), (0, True))


class FakeBackend:
    def __init__(self, name="apple-mail", fail=False):
        self.name, self.fail, self.saved = name, fail, []

    def save(self, draft, when):
        if self.fail:
            raise RuntimeError("Mail refused")
        self.saved.append(draft)
        return "Mail Drafts"


class DraftTests(Base):
    NOW = dt.datetime(2026, 11, 8, 6, 0, tzinfo=UTC)

    def save(self, backends, subject="Report", **kw):
        return drafts.save(self.root, ["a@example.com"], subject, "Hello.", backends=backends,
                           now=self.NOW, **kw)

    def test_saved_and_audited_never_sent(self):
        b = FakeBackend()
        out = self.save([b])
        self.assertEqual((out["ok"], out["backend"]), (True, "apple-mail"))
        self.assertEqual(len(b.saved), 1)
        row = audit.read(self.root)[-1]
        self.assertEqual((row["action"], row["sent"]), ("draft-email", False))
        self.assertNotIn("Hello.", json.dumps(row))

    def test_same_draft_is_not_saved_twice(self):
        b = FakeBackend()
        self.save([b])
        self.assertIn("skipped", self.save([b]))
        self.assertEqual(len(b.saved), 1)

    def test_failing_backend_falls_back_to_eml(self):
        folder = self.root / "out"
        out = self.save([FakeBackend(fail=True), drafts.EmlBackend(folder)])
        self.assertEqual(out["backend"], "eml")
        text = Path(out["where"]).read_text()
        self.assertIn("X-Unsent: 1", text)
        self.assertIn("To: a@example.com", text)
        self.assertEqual(len(out["fallback_errors"]), 1)

    def test_all_backends_failing_raises_and_records_nothing(self):
        with self.assertRaises(OSError):
            self.save([FakeBackend(fail=True)])
        self.assertEqual(drafts.load(self.root)["drafts"], {})

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError):
            drafts.save(self.root, ["nope"], "s", "b", backends=[FakeBackend()])
        with self.assertRaises(ValueError):
            drafts.save(self.root, ["a@example.com"], " ", "b", backends=[FakeBackend()])

    def test_waiting_list_expiry_and_done(self):
        folder = self.root / "out"
        eml = self.save([drafts.EmlBackend(folder)], subject="One")
        mail = self.save([FakeBackend()], subject="Two")
        self.assertEqual(len(drafts.waiting(self.root, self.NOW)), 2)
        self.assertTrue(drafts.mark_done(self.root, mail["key"]))
        self.assertEqual(len(drafts.waiting(self.root, self.NOW)), 1)
        Path(eml["where"]).rename(Path(eml["where"]).with_suffix(".moved"))
        self.assertEqual(drafts.waiting(self.root, self.NOW), [])
        again = self.save([FakeBackend()], subject="Three")
        self.assertEqual(drafts.waiting(self.root, self.NOW + dt.timedelta(days=8)), [])
        self.assertTrue(again["ok"])

    def test_summary_lines_and_report(self):
        self.assertEqual(drafts.summary_lines(self.root, self.NOW), ["No drafts waiting."])
        self.save([FakeBackend()], subject="Quarterly note")
        report = R.report(self.root)
        self.assertEqual(len(report["drafts_waiting"]), 1)
        self.assertIn("Quarterly note", R.render(report))
        self.assertIn("Drafts waiting for you", R.render(report))

    def test_report_leaves_the_draft_store_unchanged(self):
        self.save([FakeBackend()])
        before = drafts.store_path(self.root).read_bytes()
        R.report(self.root)
        self.assertEqual(drafts.store_path(self.root).read_bytes(), before)

    def test_apple_script_has_no_delivery_command(self):
        script = drafts.mail_script('Say "hi"', "Body\\n", ["a@example.com"], ["b@example.com"], "Me <me@example.com>")
        self.assertIn("visible:false", script)
        self.assertIn("save m", script)
        self.assertIn('\\"hi\\"', script)
        self.assertIsNone(re.search(r"\b(send|delete|reply|forward)\b", script, re.I))

    def test_apple_backend_uses_runner(self):
        seen = []
        b = drafts.AppleMailBackend(runner=seen.append)
        self.assertEqual(b.save(dict(subject="s", body="b", to=["a@example.com"], cc=[]), self.NOW), "Mail Drafts")
        self.assertEqual(len(seen), 1)

    def test_module_has_no_send_path(self):
        src = Path(drafts.__file__).read_text()
        self.assertIsNone(re.search(r"smtplib|sendmail|send_message|\.send\(|\bsend m\b", src))

    def test_cli_saves_an_eml_draft(self):
        body = self.root / "body.txt"
        body.write_text("Hello there.")
        folder = self.root / "eml"
        code, out = self.run_cli("draft", "save", "--to", "a@example.com", "--subject", "Hi",
                                 "--body-file", str(body), "--backend", "eml",
                                 "--eml-folder", str(folder))
        self.assertEqual((code, out["backend"]), (0, "eml"))
        self.assertTrue(Path(out["where"]).is_file())
        code, out = self.run_cli("draft", "list")
        self.assertEqual(len(out["waiting"]), 1)


def fake_engine(body, title, pdf, work):
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 600)


def broken_engine(body, title, pdf, work):
    raise notes.RenderError("no engine here")


class NoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        (self.dir / "dest").mkdir()
        (self.dir / "drafts").mkdir()

    def test_md_output_is_refused(self):
        with self.assertRaises(notes.RenderError):
            notes.render_pdf("# T\nbody", self.dir / "dest" / "n.md", engines=[("f", fake_engine)])
        self.assertEqual(list((self.dir / "dest").iterdir()), [])

    def test_pdf_written_and_no_markdown_beside_it(self):
        out = notes.render_pdf("# Title\nbody", self.dir / "dest" / "n.pdf", engines=[("f", fake_engine)])
        self.assertEqual((out["engine"], out["title"]), ("f", "Title"))
        self.assertTrue(notes.is_pdf(out["path"]))
        self.assertEqual([p.suffix for p in (self.dir / "dest").iterdir()], [".pdf"])

    def test_draft_in_destination_folder_is_refused(self):
        src = self.dir / "dest" / "n.md"
        src.write_text("# T\nbody")
        with self.assertRaises(notes.RenderError):
            notes.render_pdf(src.read_text(), self.dir / "dest" / "n.pdf", src=src, engines=[("f", fake_engine)])

    def test_existing_markdown_beside_the_target_is_refused(self):
        (self.dir / "dest" / "n.md").write_text("old")
        with self.assertRaises(notes.RenderError):
            notes.render_pdf("# T\nbody", self.dir / "dest" / "n.pdf", engines=[("f", fake_engine)])

    def test_empty_note_is_refused(self):
        with self.assertRaises(notes.RenderError):
            notes.render_pdf("  \n", self.dir / "dest" / "n.pdf", engines=[("f", fake_engine)])

    def test_fallback_engine_is_used(self):
        out = notes.render_pdf("# T\nbody", self.dir / "dest" / "n.pdf",
                               engines=[("a", broken_engine), ("b", fake_engine)])
        self.assertEqual(out["engine"], "b")

    def test_all_engines_failing_writes_nothing(self):
        with self.assertRaises(notes.RenderError):
            notes.render_pdf("# T\nbody", self.dir / "dest" / "n.pdf", engines=[("a", broken_engine)])
        self.assertEqual(list((self.dir / "dest").iterdir()), [])

    def test_non_pdf_output_is_rejected(self):
        def bad(body, title, pdf, work):
            pdf.write_bytes(b"not a pdf")
        with self.assertRaises(notes.RenderError):
            notes.render_pdf("# T\nbody", self.dir / "dest" / "n.pdf", engines=[("bad", bad)])

    def test_title_from_frontmatter(self):
        self.assertEqual(notes.split_title('---\ntitle: "Hi"\n---\n# Other\nx')[0], "Hi")

    @unittest.skipUnless(notes.chrome_path() and __import__("shutil").which("pandoc"),
                         "pandoc and Chrome are needed")
    def test_real_render(self):
        out = notes.render_pdf("# Real\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", self.dir / "dest" / "real.pdf")
        self.assertTrue(notes.is_pdf(out["path"]))
        self.assertEqual(out["engine"], "chrome")


class FlakyBackend(booking.IcsFolderBackend):
    def __init__(self, folder, mode):
        super().__init__(folder)
        self.mode, self.writes = mode, 0

    def write(self, event):
        self.writes += 1
        if self.mode == "raise":
            raise OSError("calendar unreachable")
        if self.mode == "drop":
            return
        if self.mode == "shift":
            event = dict(event, start=event["start"] + dt.timedelta(hours=1),
                         end=event["end"] + dt.timedelta(hours=1))
        super().write(event)


class BookingTests(Base):
    START = dt.datetime(2026, 11, 10, 14, 0, tzinfo=UTC)

    def book(self, backend, key="k1", **kw):
        return booking.book(self.root, backend, key=key, title="Review, with: notes",
                            start=self.START, minutes=30, **kw)

    def test_receipt_only_after_read_back(self):
        b = booking.IcsFolderBackend(self.root / "cal")
        rec = self.book(b, note="Line one\nline two")
        self.assertEqual(rec["status"], "verified")
        self.assertEqual(rec["uid"], "k1@example.com")
        self.assertEqual(booking.receipt(self.root, "k1")["status"], "verified")
        self.assertEqual(audit.read(self.root)[-1]["action"], "calendar-book")

    def test_verified_booking_is_not_written_twice(self):
        b = FlakyBackend(self.root / "cal", "ok")
        self.book(b)
        self.assertEqual(self.book(b)["status"], "verified")
        self.assertEqual(b.writes, 1)

    def test_event_missing_on_read_back_is_uncertain_and_not_retried(self):
        b = FlakyBackend(self.root / "cal", "drop")
        self.assertEqual(self.book(b)["status"], "uncertain")
        self.book(b)
        self.assertEqual(b.writes, 1)

    def test_changed_event_is_not_verified(self):
        b = FlakyBackend(self.root / "cal", "shift")
        rec = self.book(b)
        self.assertEqual(rec["status"], "uncertain")
        self.assertIn("differs", rec["last"])
        self.assertNotIn("uid", rec)

    def test_backend_error_is_uncertain(self):
        rec = self.book(FlakyBackend(self.root / "cal", "raise"))
        self.assertEqual(rec["status"], "uncertain")
        self.assertIn("unreachable", rec["last"])

    def test_clash_is_refused_without_writing(self):
        b = FlakyBackend(self.root / "cal", "ok")
        self.book(b, key="first")
        rec = self.book(b, key="second")
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(b.writes, 1)

    def test_naive_time_and_bad_length_are_refused(self):
        b = booking.IcsFolderBackend(self.root / "cal")
        with self.assertRaises(ValueError):
            booking.book(self.root, b, key="x", title="t", start=dt.datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            booking.book(self.root, b, key="x", title="t", start=self.START, minutes=1)

    def test_cli_books_and_reports_uncertain_nonzero(self):
        code, out = self.run_cli("book", "--calendar-folder", str(self.root / "cal"), "--key", "c1",
                                 "--title", "Call", "--at", "2026-11-10T14:00:00+00:00")
        self.assertEqual((code, out["status"]), (0, "verified"))


class OutreachTests(Base):
    def make(self, text, name="notes-outreach.md", folder="Q4 prep", age_days=1):
        d = self.root / "area" / folder
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text(text)
        t = time.time() - age_days * 86400
        os.utime(f, (t, t))
        (R.folder(self.root) / "outreach.json").write_text(
            json.dumps(dict(folders=[str(self.root / "area" / "*prep")])))
        return f

    def test_unconfigured_project_reports_nothing(self):
        self.assertEqual(outreach.exceptions(self.root), [])

    def test_unsent_drafts_raise_an_exception(self):
        f = self.make("Nothing has been sent.\n> Hi one\n> Hi two\n")
        found = outreach.exceptions(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "unsent-outreach")
        self.assertIn("2 draft", found[0]["reason"])
        self.assertIn(str(f), found[0]["reason"])
        self.assertEqual(R.report(self.root)["status"], "attention")

    def test_sent_old_and_unmatched_files_are_skipped(self):
        self.make("Nothing has been sent.\nStatus: sent\n")
        self.assertEqual(outreach.exceptions(self.root), [])
        self.make("Nothing has been sent.\n", age_days=30)
        self.assertEqual(outreach.exceptions(self.root), [])
        self.make("Nothing has been sent.\n", name="plain.md")
        self.assertEqual(outreach.exceptions(self.root), [])
        self.make("Some other text\n")
        self.assertEqual(outreach.exceptions(self.root), [])

    def test_scan_changes_no_file(self):
        f = self.make("Nothing has been sent.\n> Hi\n")
        before = (f.read_bytes(), f.stat().st_mtime)
        self.run_cli("outreach", "scan")
        self.assertEqual((f.read_bytes(), f.stat().st_mtime), before)

    def test_limit(self):
        for i in range(4):
            self.make("unsent\n", name="a%d-outreach.md" % i)
        cfg = outreach.load_config(self.root)
        cfg["limit"] = 2
        self.assertEqual(len(outreach.scan(cfg)), 2)


if __name__ == "__main__":
    unittest.main()
