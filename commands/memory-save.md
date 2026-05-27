---
description: Manually checkpoint a milestone — append to HISTORY.md, update CURRENT.md, optionally write per-session note.
allowed-tools: Bash, Read, Write, Edit, Grep
argument-hint: "[note text...] [--title \"Title\"] [--session-note] [--help]"
---

# /memory-save — manual checkpoint

Save a user-driven checkpoint into Memplexor memory.

## Arguments

Parse `$ARGUMENTS`:
- Everything that isn't a recognized flag → the freeform `note` text
- `--title "Some title"` → custom H2 title (default: derived from first line of note)
- `--session-note` → also write a `sessions/<ISO>.md` file
- `--help` → print usage + exit

If no note text is provided AND no `--help`: ask Claude to summarize the *current
conversation state* in ≤6 bullets — what was decided, what changed, what's next.
Use that as the note.

## Step 1 — Validate memory exists

If `.claude/memory/` is missing → tell user to run `/setup` first. Exit.

## Step 2 — Resolve title

- `--title "X"` → use X
- else → first line of the note, truncated to 80 chars, leading verb preserved
- fallback → `manual checkpoint`

## Step 3 — Secret scan

Refuse to write any line matching:
- `(api[_-]?key|password|secret|token|bearer)\s*[:=]`
- common key prefixes: `sk-`, `ghp_`, `gho_`, `xoxb-`, `AKIA`

If found, print which line was rejected and abort. Do not partially write.

## Step 4 — Append HISTORY.md

```markdown

## <ISO-8601> — <title>

<note body, bullet-formatted>
```

Append-only. No dup check for manual saves — user knows what they're doing.

## Step 5 — Update CURRENT.md

Atomic rewrite. Update:
- `**Last updated:**` line
- `## Recent Work` → prepend bullet summarizing the checkpoint
- `## Next Steps` → if the note contains "next:" / "todo:" / "next step:",
  promote those into bullets here

Preserve `## Open Questions` and `## Warnings or Constraints` verbatim.

## Step 6 — Optional session note (--session-note)

Write `.claude/memory/sessions/<ISO-date>.md` with:

```markdown
# Session Notes

**Timestamp:** <ISO>
**Hook Event:** manual (/memory-save)
**Title:** <title>

## Summary

<note body>

## Decisions Made

* <each bullet from note>

## Next Steps

* <extracted from note, if any>
```

## Step 7 — Report

```
Saved:
* Appended checkpoint to .claude/memory/progress/HISTORY.md
* Updated .claude/memory/context/CURRENT.md
* (Wrote .claude/memory/sessions/<file>.md)   [if --session-note]

Checkpoint:
* <title>
* <first line of note>
```

## Examples

```
/memory-save Finished auth refactor; next: integration tests for token refresh
/memory-save --title "v1.2 released" --session-note Shipped 1.2 with new login flow.
```

## Safety

- Secret patterns abort the write.
- Atomic rewrite of CURRENT.md.
- HISTORY.md is append-only.
- Stay inside `$PWD`.
