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

## Assistant compatibility and platform coverage

Claude and Codex call the same CLI and read the same JSON. Neither provider chooses a different policy. This release includes no subscription quota adapters, model-generated note calls, browser integration or automatic assistant execution.

The tests run on macOS for this release. The lock implementation includes POSIX and Windows paths. Windows execution has not been verified on a Windows machine. File locks are designed for one local filesystem, rather than concurrent edits across network shares or cloud-sync replicas.

The private deployment also has mail, calendar, job maintenance, ledger and desktop integrations. Those integrations are not installed by this package. The launch film is an explanation of that broader workflow, rather than a recording or benchmark of this release.
