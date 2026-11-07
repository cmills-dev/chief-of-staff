# Local core

The local core keeps decisions, commitments and evidence in a project's `.chief/` directory.

It is a standard-library Python package. Run it from the source checkout or install it with `python3 -m pip install .`. Each example below uses the installed `chief` command. Add `--project /path/to/project` before the command to select another project.

## Start with a preview

```sh
chief init
chief status
chief reconcile --dry-run
```

Initialisation creates empty state. It enables neither automatic report writes nor the watcher. Explicit commands still record the instructions you give them. `status` returns JSON without changing task state. Reconcile renders a current report. It persists `.chief/report.json` and `.chief/note.txt` only after `chief enable`.

Keep `.chief/` out of public source control. It contains project-specific instructions, sources and file fingerprints. The source repository already ignores it.

## Promote work explicitly

```sh
chief task draft --title "Check the export" --owner "Example User" --actor "Example User" --source chat:instruction
```

The command returns a `CT-` identifier. Use that returned identifier in the commands below, replacing `CT-IDENTIFIER`.

```sh
chief task promote CT-IDENTIFIER --actor "Example User" --source chat:instruction --confirmed
chief task block CT-IDENTIFIER --reason "Input is missing" --next "Choose the input file" --actor "Example User" --source chat:instruction
chief status
chief task resume CT-IDENTIFIER --actor "Example User" --source chat:instruction
```

Scratch drafts stay outside confirmed commitments. Promotion needs a named responsible person or system. Multiple names appear as a responsibility conflict. A blocked task needs a reason and a next decision. Resume changes its state back to open, with a recorded source.

## Preserve decisions and reasons

```sh
chief decision record --id COS-EXPORT --decision "Keep the accepted export format" --reason "Downstream tools depend on it" --actor "Example User" --source chat:instruction --confirmed
chief decision protect --id COS-EXPORT --task task:CT-IDENTIFIER --actor "Example User" --source chat:instruction --confirmed
```

The linked work cannot complete while that decision needs review. Review records are retained. An explicit review can permit closure:

```sh
chief decision review --id COS-EXPORT --outcome allow-close --reason "The checked result keeps the accepted format" --actor "Example User" --source chat:instruction --confirmed
```

Permission alone is not completion evidence. Unknown or malformed decision references hold completion. Corrupt or missing state stays visible and is not replaced with an empty record. Initialisation refuses a partial project state, so restoring a lost file is a separate repair step.

The confirmation flag asserts that a human instruction exists. It does not authenticate the person running the command. An assistant should record a real instruction and its source, rather than infer permission from a model response.

## Check evidence against current files

```sh
chief decision snapshot --task task:CT-IDENTIFIER --path schema.sql --assumption "The accepted table shape is current" --actor "Example User" --source chat:instruction --confirmed
chief task complete CT-IDENTIFIER --evidence result.txt --actor "Example User" --source chat:instruction --confirmed
```

Evidence paths are relative to the selected project. Missing files, symlinks, sensitive filenames and paths outside the project are rejected. Snapshots bind an assumption to the current file bytes. Tests and human review establish whether the assumption is true.

A changed completed artifact appears as stale evidence. Recheck it, then run `task complete` again with confirmation and current evidence. The earlier receipt stays in `completion_history`. A declared assumption snapshot still needs its own explicit refresh. Reaffirming completion does not bypass a protected decision or a stale declared snapshot.

## React to local changes

```sh
chief enable
chief enable --watch
chief watch
```

One `watch` command performs one tick. The first enabled tick records a baseline, rather than claiming a successful reconcile. Call it once per minute with your scheduler, or run a bounded foreground loop:

```sh
chief watch --ticks 120 --interval 60
```

Changes settle for at least 30 seconds before reconciliation. Continuous changes are processed after at most five minutes. The watcher checks task and decision records, recorded artifact files and an hourly age event. It excludes its own reports and cursor from the source digest.

An interrupted or failed reconcile keeps its earlier successful cursor and retries with backoff, up to 30 minutes. Changes that arrive during a run remain pending. One shared local lock prevents overlapping manual and watch reconciles. File and byte limits bound the scan.

```sh
chief disable --watch
chief disable
```

Disabling report writes also prevents a live watch tick. A dry run does not advance the cursor. The watcher reads no inbox, calls no model and executes no arbitrary shell commands. It writes attention reports. Task completion and decision changes remain explicit commands.

## Permission policy

```sh
chief policy --text "pay the supplier invoice"
chief policy --outbound send
chief policy --role chief --branch main --git "git merge --no-ff feature/report"
chief policy --role build --branch main --git "git merge --no-ff feature/report"
```

The command prints a JSON verdict. It exits 0 when the request is allowed and 2 when it is denied. A hook or wrapper in your assistant can call it before running a command.

The hard denies are payments and file deletion. Other requests are allowed. Outbound verbs (`send`, `reply`, `forward`, `post`, `publish`) are denied while `outbound` is `draft-only`, which is the default. A draft is always allowed.

For git, push, rebase, reset, clean, amend, squash merges, branch deletion and worktree removal are denied for every role. The Chief role (`merge_roles`) may add, commit, branch, apply and merge with `--no-ff`. Any other role needs review before a commit or merge reaches the main branch.

`.chief/policy.json` is optional. Every key is optional:

```json
{
  "outbound": "draft-only",
  "main_branch": "main",
  "merge_roles": ["chief"],
  "extra_patterns": {"payment": ["\\bwire\\b"]}
}
```

`patterns` replaces the default pattern lists. `extra_patterns` adds to them. The policy is a check that your tooling calls. It does not sandbox a process.

## Draft-only email

```sh
chief draft save --to someone@example.com --subject "Weekly summary" --body-file body.txt
chief draft list
chief draft done DRAFT-KEY
```

On macOS, `--backend auto` saves into the Drafts mailbox through Mail. If Mail is unavailable or refuses, the draft becomes an `.eml` file with an `X-Unsent: 1` header in `.chief/drafts/`. Use `--backend eml` and `--eml-folder` to choose the portable file backend on any system. Mail opens the file as a draft that is ready to send.

The module has no send path. Each save adds a `draft-email` entry to `.chief/audit.jsonl` with the recipients and subject, and no body. The same draft is not saved twice while it waits. A draft stops waiting when you run `done`, when its `.eml` file is moved away, or after seven days. `chief status` lists waiting drafts under `drafts_waiting`, and the note prints them under "Drafts waiting for you".

## PDF-only notes

```sh
chief note render --src /tmp/draft.md --out notes/summary.pdf
```

Keep the markdown draft in a temporary folder. The command refuses an output that is not a `.pdf`, a draft that sits in the destination folder, a markdown file with the same name beside the PDF, and an empty note. Pandoc builds an HTML page and headless Chrome prints it. Set `CHIEF_CHROME` to choose the browser. If Chrome fails, pandoc with `xelatex` renders the same draft. If every engine fails, nothing is written.

## Verified calendar bookings

```sh
chief book --calendar-folder calendar --key review-1 --title "Review" --at 2026-11-10T14:00:00+00:00 --minutes 30
```

The command writes an event, reads it back by its key and compares title, start and end. Only a match records a `verified` receipt in `.chief/bookings.json`. A mismatch or a missing event is `uncertain` and the command exits 1. A clash with an existing event is `failed`. The same key is never written twice, because a second write could duplicate the event. Use a new key after you check the calendar.

The bundled backend keeps one `.ics` file per event in a folder. A calendar app can import or subscribe to that folder. Another calendar needs a small backend class with `write`, `read_back` and optionally `conflicts`.

## Unsent outreach

Create `.chief/outreach.json`:

```json
{"folders": ["~/notes/*prep"], "file_glob": "*outreach*.md", "max_age_days": 14, "limit": 5}
```

`chief outreach scan` lists matching files that say nothing was sent and carry no sent marker. The scan only reads. `chief status` raises each file as an `unsent-outreach` exception. With no configuration, nothing is scanned.

## Assistant compatibility and platform coverage

Claude and Codex call the same CLI and read the same JSON. Neither provider chooses a different policy. This release includes no subscription quota adapters, model-generated note calls, browser integration or automatic assistant execution.

The tests run on macOS for this release. The lock implementation includes POSIX and Windows paths. Windows execution has not been verified on a Windows machine. File locks are designed for one local filesystem, rather than concurrent edits across network shares or cloud-sync replicas.

The private deployment also has mail, calendar, job maintenance, ledger and desktop integrations. Those integrations are not installed by this package. The launch film is an explanation of that broader workflow, rather than a recording or benchmark of this release.
