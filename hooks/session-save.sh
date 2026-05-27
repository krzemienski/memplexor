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
  # Extract last ~50 tool_use file paths from the JSONL transcript
  if command -v python3 >/dev/null 2>&1; then
    summary_files="$(python3 -c "
import json, sys
paths = []
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
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use':
                    inp = block.get('input') or {}
                    p = inp.get('file_path') or inp.get('path') or inp.get('filePath')
                    if p and p not in paths:
                        paths.append(p)
except Exception:
    pass
for p in paths[-15:]:
    print('* ' + p)
" 2>/dev/null)"
  fi
fi

[ -z "$summary_files" ] && summary_files="* (transcript not available)"

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

# Preserve user "Open Questions" + "Warnings" sections if present
preserved_questions=""
preserved_warnings=""
if [ -f "$CURRENT" ]; then
  preserved_questions="$(awk '
    /^## Open Questions/ { flag=1; next }
    flag && /^## / { flag=0 }
    flag { print }
  ' "$CURRENT" | sed '/./,$!d' | awk 'BEGIN{RS=""} {print; exit}')"
  preserved_warnings="$(awk '
    /^## Warnings or Constraints/ { flag=1; next }
    flag && /^## / { flag=0 }
    flag { print }
  ' "$CURRENT" | sed '/./,$!d' | awk 'BEGIN{RS=""} {print; exit}')"
fi
[ -z "$preserved_questions" ] && preserved_questions="* (none recorded)"
[ -z "$preserved_warnings" ] && preserved_warnings="* (none recorded)"

cat <<EOF | atomic_write "$CURRENT" || true
# Current Project Context

<!-- Generated by Memplexor session-save hook. Edits to "Open Questions" and "Warnings or Constraints" are preserved. -->

**Last updated:** $now_iso
**Last event:** $hook_event ($title)

## Where We Are

* Session checkpoint written by Memplexor.
* See \`.claude/memory/progress/HISTORY.md\` for chronological log.
* See \`.claude/memory/sessions/$file_stamp.md\` for this session's note.

## Recent Work

$summary_files

## Next Steps

* Resume work referencing this CURRENT.md.
* Run \`/memory-status\` to inspect memory health.

## Open Questions

$preserved_questions

## Important Files

$summary_files

## Warnings or Constraints

$preserved_warnings
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
