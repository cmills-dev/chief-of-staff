# Changes

## 0.2.0

The core now includes a permission policy. The only hard denies are payments and file deletion. Outbound messages are draft-only. One role may commit and merge `--no-ff` into a configured main branch, other roles keep a review-first path, and push is denied for every role. A `.chief/policy.json` file changes the defaults.

Draft-only email saves a draft through Apple Mail on macOS or a portable `.eml` file, never sends, and writes an audit entry. The attention report lists drafts that still wait.

PDF-only notes render a markdown draft with pandoc and headless Chrome, with a LaTeX fallback. They refuse `.md` output and leave no markdown beside the PDF.

Calendar booking reads an event back before it records a receipt, and never writes an uncertain item twice. An outreach scan reports notes in configured folders that were drafted and never sent.

Later items: a booking backend for a live calendar service, a free-slot source for choosing a time, reminders and collectors that follow a booking, and an undo command for drafts and bookings. The bundled calendar backend uses `.ics` files. Windows execution of the new features is unverified.

## 0.1.0

The repository now includes an installable local core for protected decisions, confirmed commitments and evidence checks. Changed or missing artifacts surface as stale evidence. Explicit reaffirmation keeps the previous completion receipt.

A shared command interface works from Claude, Codex or a terminal. A locked local watcher coalesces changes, retains its cursor on failure and retries with backoff. Report writes and watching start disabled.

This release does not include inbox, calendar, school, desktop or scheduled maintenance integrations from the private deployment. It makes no model calls. Tests cover local behaviour on macOS. Windows execution remains unverified.
