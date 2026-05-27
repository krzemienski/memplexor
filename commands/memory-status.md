---
description: Report Memplexor memory health — checks .claude/memory/ artifacts, counts entries, recommends next action. Read-only.
allowed-tools: Bash, Read, Glob
argument-hint: "[--verbose] [--json] [--help]"
---

# /memory-status — diagnostic

Read-only health check for Memplexor memory in the current project.

## Arguments

- `--verbose` → include full file paths and tail snippets
- `--json` → emit machine-readable JSON instead of markdown
- `--help` → print usage + exit

## Step 1 — Probe artifacts

Check existence:

| Artifact | Check |
|---|---|
| `.claude/memory/` | dir exists |
| `.claude/memory/context/CURRENT.md` | file exists |
| `.claude/memory/progress/HISTORY.md` | file exists |
| `.claude/memory/sessions/` | dir exists |
| `.claude/skills/project-guidelines/SKILL.md` | file exists |

## Step 2 — Extract data

- **Last updated:** grep `^**Last updated:**` in CURRENT.md
- **History entries:** count `^## ` lines in HISTORY.md
- **Recent history headings:** `tail -n+1 HISTORY.md | grep '^## ' | tail -5`
- **Session note count:** count `.md` files in `sessions/`
- **Latest session:** newest file in `sessions/` (by name = ISO timestamp)

## Step 3 — Hook config

Plugin hooks are registered via `${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json`. Report
that hooks are "configured (plugin-level)" if the plugin is loaded — otherwise
warn.

## Step 4 — Output (markdown default)

```
Memory Status:
* Setup: complete | incomplete
* Current context: present, last updated <ts> | missing
* History entries: <N>
* Session notes: <N> (latest: <name>)
* Project guidelines skill: present | missing
* Hooks: configured | not detected

Recent history:
* <heading 1>
* <heading 2>
* ...

Recommended next action:
* No action needed.   (if all present)
* Run /setup          (if memory missing)
* Run /memory-refresh (if skill stale)
```

If `--verbose`: also dump file paths and the first 10 lines of CURRENT.md.

If `--json`: emit

```json
{
  "setup_complete": true,
  "current_md": {"present": true, "last_updated": "<ts>", "path": "..."},
  "history": {"entries": 12, "recent": ["...", "..."], "path": "..."},
  "sessions": {"count": 5, "latest": "...", "dir": "..."},
  "skill": {"present": true, "path": "..."},
  "hooks": {"configured": true},
  "recommendation": "none" | "run /setup" | "run /memory-refresh"
}
```

## Safety

- Never write files.
- Never edit memory.
- Report `incomplete` if any required artifact is missing.
