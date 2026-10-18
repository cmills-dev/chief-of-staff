# Changes

## 0.1.0

The repository now includes an installable local core for protected decisions, confirmed commitments and evidence checks. Changed or missing artifacts surface as stale evidence. Explicit reaffirmation keeps the previous completion receipt.

A shared command interface works from Claude, Codex or a terminal. A locked local watcher coalesces changes, retains its cursor on failure and retries with backoff. Report writes and watching start disabled.

This release does not include inbox, calendar, school, desktop or scheduled maintenance integrations from the private deployment. It makes no model calls. Tests cover local behaviour on macOS. Windows execution remains unverified.
