---
description: Archive old Memplexor session notes and history entries. Never deletes by default — moves to .claude/memory/archive/.
allowed-tools: Bash, Read, Write, Edit, Glob
argument-hint: "[--keep-sessions N] [--keep-history N] [--archive] [--summarize] [--dry-run] [--help]"
---

# /memory-prune — retention

Keep memory compact and useful. Archive, don't delete.

## Arguments

- `--keep-sessions N` → keep newest N session notes in `sessions/` (default: 20)
- `--keep-history N` → keep newest N H2 entries in HISTORY.md (default: 100)
- `--archive` → move older sessions/history into `.claude/memory/archive/`
  (default behavior; the flag exists for explicitness)
- `--summarize` → also write a one-paragraph summary of archived items into the
  archive file
- `--dry-run` → print plan only, write nothing
- `--help` → print usage + exit

## Step 1 — Validate

`.claude/memory/` must exist. Else: tell user to run `/setup`.

`mkdir -p .claude/memory/archive` if not present.

## Step 2 — Session prune

List `.claude/memory/sessions/*.md`, sort by filename (ISO date = lexical sort).

Newest N → keep in place. Rest → move to `.claude/memory/archive/sessions/`
preserving filenames.

If `--summarize`: write `.claude/memory/archive/sessions-YYYY-MM.md` with one
paragraph per archived month listing titles + dates.

## Step 3 — History prune

Parse HISTORY.md into a list of (timestamp, title, body) entries by H2 headings.

Keep newest N entries in place. Append older ones to
`.claude/memory/archive/history-archive.md` (append-only too).

Then **atomically rewrite** HISTORY.md as: front-matter comment + the kept
entries only.

If `--summarize`: prepend a "## archived through <date>" summary block to
HISTORY.md noting how many entries moved.

## Step 4 — Append a pruning entry to HISTORY.md

```markdown

## <ISO-8601> — memory pruned

* Sessions: kept <N>, archived <M>
* History: kept <N>, archived <M>
* Archive location: .claude/memory/archive/
```

## Step 5 — Report

```
Pruned:
* Archived <M> session notes to .claude/memory/archive/sessions/
* Archived <M> history entries to .claude/memory/archive/history-archive.md
* (Wrote summaries to ...)                       [if --summarize]

Preserved:
* <N> most recent session notes
* <N> most recent HISTORY.md entries
* CURRENT.md (untouched)

No files were deleted.
```

If `--dry-run`: show planned moves + counts; write nothing.

## Safety

- **NEVER delete files.** Only move + append.
- CURRENT.md is never touched by prune.
- Atomic rewrites for HISTORY.md.
- Stay inside `$PWD`.
- If anything in the plan would touch a file outside `.claude/memory/` → refuse.
