#!/usr/bin/env bash
# Memplexor SessionStart hook
# Reads project memory and emits hookSpecificOutput.additionalContext JSON.
# Fails open: any error → silent exit 0 (never block Claude Code).

set -u

# --- payload ingestion ---------------------------------------------------------
PAYLOAD="$(cat 2>/dev/null || true)"

# Resolve project root: payload.cwd → $CLAUDE_PROJECT_DIR → pwd
project_root=""
if command -v python3 >/dev/null 2>&1 && [ -n "$PAYLOAD" ]; then
  project_root="$(printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("cwd") or d.get("project_dir") or d.get("workspace_dir") or "")
except Exception:
    print("")
' 2>/dev/null)"
fi
[ -z "$project_root" ] && project_root="${CLAUDE_PROJECT_DIR:-$PWD}"
[ ! -d "$project_root" ] && project_root="$PWD"

MEM_DIR="$project_root/.claude/memory"
CURRENT="$MEM_DIR/context/CURRENT.md"
HISTORY="$MEM_DIR/progress/HISTORY.md"

# --- build context ------------------------------------------------------------
header="Always consult .claude/memory/ before starting work; orient yourself first."

if [ ! -f "$CURRENT" ] && [ ! -f "$HISTORY" ]; then
  body="No project memory found at .claude/memory/. Run /setup to initialize."
else
  current_block=""
  if [ -f "$CURRENT" ]; then
    current_block="$(head -c 8000 "$CURRENT" 2>/dev/null)"
  fi
  history_block=""
  if [ -f "$HISTORY" ]; then
    # Last 3 entries (entries are H2 headings starting with "## ")
    history_block="$(awk '
      /^## / { count++; if (count > 3) exit }
      count >= 1 { print }
    ' "$HISTORY" 2>/dev/null | head -c 4000)"
  fi

  body=""
  [ -n "$current_block" ] && body="### Current Project Context

$current_block"
  if [ -n "$history_block" ]; then
    [ -n "$body" ] && body="$body

"
    body="${body}### Recent Progress (last 3 entries)

$history_block"
  fi
fi

additional_context="# Project Memory (Memplexor)

$header

$body"

# --- emit JSON ----------------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  python3 -c '
import json, sys
ctx = sys.stdin.read()
out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
print(json.dumps(out))
' <<<"$additional_context" 2>/dev/null || exit 0
else
  # Fallback: best-effort hand-encoded JSON
  esc=$(printf '%s' "$additional_context" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk 'BEGIN{ORS="\\n"} {print}')
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$esc"
fi

exit 0
