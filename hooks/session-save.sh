#!/usr/bin/env bash
# Memplexor session save hook (Stop + PreCompact).
# - Appends to HISTORY.md
# - Rewrites CURRENT.md atomically
# - Writes per-session note
# Fails open: any internal error → exit 0.

set -u

PAYLOAD="$(cat 2>/dev/null || true)"

# --- parse payload ------------------------------------------------------------
project_root=""
transcript_path=""
hook_event=""
if command -v python3 >/dev/null 2>&1 && [ -n "$PAYLOAD" ]; then
  parsed="$(printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("cwd") or d.get("project_dir") or d.get("workspace_dir") or "")
    print(d.get("transcript_path") or "")
    print(d.get("hook_event_name") or d.get("hookEventName") or "")
except Exception:
    print(""); print(""); print("")
' 2>/dev/null)"
  project_root="$(printf '%s\n' "$parsed" | sed -n '1p')"
  transcript_path="$(printf '%s\n' "$parsed" | sed -n '2p')"
  hook_event="$(printf '%s\n' "$parsed" | sed -n '3p')"
fi
[ -z "$project_root" ] && project_root="${CLAUDE_PROJECT_DIR:-$PWD}"
[ ! -d "$project_root" ] && exit 0

[ -z "$hook_event" ] && hook_event="Stop"

# --- skip if memory not initialized -------------------------------------------
MEM_DIR="$project_root/.claude/memory"
if [ ! -d "$MEM_DIR" ]; then
  # Don't auto-create — user must opt in via /setup
  exit 0
fi

mkdir -p "$MEM_DIR/context" "$MEM_DIR/progress" "$MEM_DIR/sessions" 2>/dev/null || exit 0

# --- timestamps ---------------------------------------------------------------
now_iso="$(date +%Y-%m-%dT%H:%M:%S%z 2>/dev/null | sed 's/\([+-][0-9][0-9]\)\([0-9][0-9]\)$/\1:\2/')"
[ -z "$now_iso" ] && now_iso="$(date +%Y-%m-%dT%H:%M:%S)"
file_stamp="$(date +%Y-%m-%dT%H-%M-%S 2>/dev/null)"
[ -z "$file_stamp" ] && file_stamp="session"

# --- title based on event -----------------------------------------------------
case "$hook_event" in
  PreCompact|preCompact|pre_compact)
    title="pre-compact save"
    ;;
  Stop|stop)
    title="session stop"
    ;;
  *)
    title="session save ($hook_event)"
    ;;
esac

# --- duplicate suppression: if last HISTORY entry has same title within 60s, skip
HISTORY="$MEM_DIR/progress/HISTORY.md"
if [ -f "$HISTORY" ]; then
  last_line="$(grep '^## ' "$HISTORY" 2>/dev/null | tail -1)"
  if [ -n "$last_line" ] && printf '%s' "$last_line" | grep -qF "$title"; then
    # check if within 60s
    last_ts="$(printf '%s' "$last_line" | sed -n 's/^## \([0-9T:+-]*\) .*/\1/p')"
    if [ -n "$last_ts" ] && command -v python3 >/dev/null 2>&1; then
      skip="$(python3 -c "
import datetime as d
try:
    a = d.datetime.fromisoformat('$last_ts'.replace('Z','+00:00'))
    b = d.datetime.fromisoformat('$now_iso'.replace('Z','+00:00'))
    delta = abs((b - a).total_seconds())
    print('skip' if delta < 60 else 'go')
except Exception:
    print('go')
" 2>/dev/null)"
      [ "$skip" = "skip" ] && exit 0
    fi
  fi
fi

# --- gather conservative summary ---------------------------------------------
summary_files=""
summary_extra=""
if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
  if command -v python3 >/dev/null 2>&1; then
    # Extract: (a) Write/Edit/Read file_paths, (b) Bash commands (1st line, ≤80c),
    # (c) Agent subtask descriptions. Returns up to 15 distinct activity bullets.
    summary_files="$(python3 -c "
import json, sys
acts = []
seen = set()
def add(s):
    if s and s not in seen:
        seen.add(s)
        acts.append(s)
try:
    with open('$transcript_path') as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            msg = e.get('message') or {}
            content = msg.get('content') or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict): continue
                if block.get('type') != 'tool_use': continue
                name = block.get('name', '')
                inp = block.get('input') or {}
                if name in ('Write','Edit','MultiEdit','NotebookEdit'):
                    p = inp.get('file_path') or inp.get('path') or inp.get('filePath')
                    if p: add(f'* edit: {p}')
                elif name == 'Read':
                    p = inp.get('file_path') or inp.get('path')
                    if p: add(f'* read: {p}')
                elif name == 'Bash':
                    cmd = (inp.get('command','') or '').strip().split('\n',1)[0]
                    if cmd: add(f'* bash: {cmd[:80]}')
                elif name == 'Agent':
                    desc = inp.get('description','') or ''
                    if desc: add(f'* agent: {desc[:80]}')
except Exception:
    pass
for a in acts[-15:]:
    print(a)
" 2>/dev/null)"
  fi
fi

[ -z "$summary_files" ] && summary_files="* (no tool activity captured)"

# --- atomic write helper ------------------------------------------------------
atomic_write() {
  # $1 = target path, stdin = content
  target="$1"
  dir="$(dirname "$target")"
  tmp="$(mktemp "$dir/.tmp.XXXXXX" 2>/dev/null)" || return 1
  cat > "$tmp"
  mv -f "$tmp" "$target" || { rm -f "$tmp"; return 1; }
  return 0
}

# --- 1. append to HISTORY.md --------------------------------------------------
{
  printf '\n## %s — %s\n\n' "$now_iso" "$title"
  printf '* Hook event: %s\n' "$hook_event"
  [ -n "$transcript_path" ] && printf '* Transcript: `%s`\n' "$transcript_path"
  printf '* Files touched (recent):\n'
  printf '%s\n' "$summary_files" | sed 's/^/    /'
} >> "$HISTORY" 2>/dev/null

# --- 2. rewrite CURRENT.md atomically -----------------------------------------
CURRENT="$MEM_DIR/context/CURRENT.md"

# Preserve user-facing sections. If the section exists and has *real* content
# (not the "(none recorded)" / "(transcript not available)" placeholders), keep it.
extract_section() {
  # $1 = section heading (e.g. "## Open Questions"), $2 = file
  awk -v h="$1" '
    $0 == h { flag=1; next }
    flag && /^## / { flag=0 }
    flag { print }
  ' "$2" | sed '/./,$!d' | awk 'BEGIN{RS=""} {print; exit}'
}

is_placeholder() {
  # returns 0 if the given block is empty / only whitespace / a known placeholder
  s="$(printf '%s' "$1" | tr -d ' \t\n')"
  [ -z "$s" ] && return 0
  # Match common stub patterns: "* (none ...)", "* (... not available)", "* (add ...)", "* (... TBD)"
  case "$s" in
    "*(none"*|"*(nonerecorded)"|"*(transcriptnotavailable)"|"*(transcriptunavailable)"|"*(notoolactivitycaptured)") return 0;;
    "*(addyourshere"*|"*(refineonnextsessionstart)") return 0;;
    "*(capturedecisionsmanuallyvia\`/memory-save\`)") return 0;;
  esac
  # Heuristic: single bullet starting with "(" and ending with ")" → placeholder
  case "$s" in
    "*("*")"|"-("*")"|"("*")") return 0;;
  esac
  return 1
}

preserved_where=""
preserved_recent=""
preserved_next=""
preserved_questions=""
preserved_important=""
preserved_warnings=""
if [ -f "$CURRENT" ]; then
  preserved_where="$(extract_section "## Where We Are" "$CURRENT")"
  preserved_recent="$(extract_section "## Recent Work" "$CURRENT")"
  preserved_next="$(extract_section "## Next Steps" "$CURRENT")"
  preserved_questions="$(extract_section "## Open Questions" "$CURRENT")"
  preserved_important="$(extract_section "## Important Files" "$CURRENT")"
  preserved_warnings="$(extract_section "## Warnings or Constraints" "$CURRENT")"
fi

# Defaults for sections that the save hook owns when no user content exists
default_where="* Session checkpoint written by Memplexor.
* See \`.claude/memory/progress/HISTORY.md\` for chronological log.
* See \`.claude/memory/sessions/${file_stamp}.md\` for this session's note."

default_recent="$summary_files"
default_next="* Resume work referencing this CURRENT.md.
* Run \`/memory-status\` to inspect memory health."
default_questions="* (none recorded)"
default_important="$summary_files"
default_warnings="* (none recorded)"

# Use preserved content if non-placeholder, else use default
section_where="$preserved_where"; is_placeholder "$section_where" && section_where="$default_where"
section_recent="$preserved_recent"; is_placeholder "$section_recent" && section_recent="$default_recent"
section_next="$preserved_next"; is_placeholder "$section_next" && section_next="$default_next"
section_questions="$preserved_questions"; is_placeholder "$section_questions" && section_questions="$default_questions"
section_important="$preserved_important"; is_placeholder "$section_important" && section_important="$default_important"
section_warnings="$preserved_warnings"; is_placeholder "$section_warnings" && section_warnings="$default_warnings"

cat <<EOF | atomic_write "$CURRENT" || true
# Current Project Context

<!-- Generated by Memplexor session-save hook. User-authored sections preserved when they contain real content. -->

**Last updated:** $now_iso
**Last event:** $hook_event ($title)

## Where We Are

$section_where

## Recent Work

$section_recent

## Next Steps

$section_next

## Open Questions

$section_questions

## Important Files

$section_important

## Warnings or Constraints

$section_warnings
EOF

# --- 3. write per-session note ------------------------------------------------
SESSION_FILE="$MEM_DIR/sessions/${file_stamp}.md"
cat <<EOF > "$SESSION_FILE" 2>/dev/null
# Session Notes

**Timestamp:** $now_iso
**Hook Event:** $hook_event
**Title:** $title
**Transcript Source:** ${transcript_path:-(none)}

## Summary

Automatic checkpoint written by Memplexor on $hook_event.

## Files Touched

$summary_files

## Decisions Made

* (capture decisions manually via \`/memory-save\`)

## Next Steps

* (refine on next session start)
EOF

exit 0
