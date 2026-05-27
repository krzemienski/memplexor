#!/usr/bin/env python3
"""
Functional validation harness for the Memplexor Claude Code plugin.

Runs against the REAL Claude Code CLI via claude-code-sdk subprocess.
No mocks. No stubs. Every assertion either passes or fails based on
artifacts on disk + transcript content.

Usage:
    python3 tests/validate.py            # full run
    python3 tests/validate.py --quick    # smoke only (no LLM calls)
    python3 tests/validate.py --bench    # add timing benchmarks

Exit code: 0 = all PASS, 1 = any FAIL.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

PLUGIN_ROOT = Path("/Users/nick/Desktop/Memplexor").resolve()
SANDBOX_ROOT = Path("/tmp/memplexor-validate")
RESULTS: list[dict] = []


# ---------- pretty output ----------
def banner(s: str):
    print(f"\n{'═' * 72}\n  {s}\n{'═' * 72}")


def record(name: str, passed: bool, evidence: str, duration_ms: Optional[float] = None):
    status = "✓ PASS" if passed else "✗ FAIL"
    dur = f"  ({duration_ms:.0f}ms)" if duration_ms is not None else ""
    print(f"  {status}  {name}{dur}")
    if not passed:
        print(f"        evidence: {evidence}")
    RESULTS.append({"name": name, "passed": passed, "evidence": evidence, "ms": duration_ms})


# ---------- helpers ----------
def make_sample_project(name: str, stack: str = "nextjs") -> Path:
    """Create a realistic sample project under SANDBOX_ROOT/<name>."""
    p = SANDBOX_ROOT / name
    if p.exists():
        shutil.rmtree(p)
    (p / "src").mkdir(parents=True)
    (p / "tests").mkdir()

    if stack == "nextjs":
        (p / "package.json").write_text(json.dumps({
            "name": name,
            "version": "0.1.0",
            "scripts": {
                "build": "next build",
                "test": "vitest",
                "lint": "eslint .",
                "typecheck": "tsc --noEmit",
            },
            "dependencies": {"next": "^14", "react": "^18", "react-dom": "^18"},
            "devDependencies": {"typescript": "^5", "vitest": "^1", "eslint": "^9"},
        }, indent=2))
        (p / "src/page.tsx").write_text("export default function Page(){return <div/>}\n")
    elif stack == "python":
        (p / "pyproject.toml").write_text(
            '[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
            'dependencies = ["fastapi", "uvicorn"]\n\n'
            '[tool.ruff]\nline-length = 100\n'.format(name=name)
        )
        (p / "src/main.py").write_text("def main(): print('hi')\n")

    (p / "README.md").write_text(f"# {name}\n\nSample project for Memplexor validation.\n")
    return p


def seed_memory(project: Path, current_md: str, history_md: str):
    """Pre-seed .claude/memory/ to simulate a project that already ran /setup."""
    mem = project / ".claude" / "memory"
    (mem / "context").mkdir(parents=True, exist_ok=True)
    (mem / "progress").mkdir(parents=True, exist_ok=True)
    (mem / "sessions").mkdir(parents=True, exist_ok=True)
    (mem / "context" / "CURRENT.md").write_text(current_md)
    (mem / "progress" / "HISTORY.md").write_text(history_md)


def run_hook(hook_name: str, payload: dict) -> tuple[int, str, str]:
    """Invoke a hook script directly with a payload on stdin. Returns (exit, stdout, stderr)."""
    script = PLUGIN_ROOT / "hooks" / f"{hook_name}.sh"
    proc = subprocess.run(
        ["bash", str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


async def run_claude(project: Path, prompt: str, timeout: float = 240.0,
                     skip_perms: bool = True) -> tuple[str, list, float]:
    """
    Spawn a real Claude Code session via the Python SDK.
    Returns (final_text, all_messages, wall_clock_seconds).
    """
    from claude_code_sdk import (
        ClaudeSDKClient, ClaudeCodeOptions,
        AssistantMessage, TextBlock, ResultMessage,
    )
    # ToolResultBlock isn't re-exported in all SDK versions; import defensively
    try:
        from claude_code_sdk.types import ToolResultBlock
    except Exception:
        ToolResultBlock = None  # type: ignore

    opts = ClaudeCodeOptions(
        cwd=str(project),
        permission_mode="bypassPermissions" if skip_perms else "acceptEdits",
        extra_args={"plugin-dir": str(PLUGIN_ROOT), "tools": "default"},
        max_turns=20,
    )

    final_text = ""
    messages = []
    t0 = time.monotonic()
    try:
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(prompt)
            # Iterate with explicit per-message exception handling so SDK
            # bugs on uncommon message types (e.g. rate_limit_event) don't
            # abort the whole run.
            it = client.receive_response().__aiter__()
            while True:
                try:
                    msg = await it.__anext__()
                except StopAsyncIteration:
                    break
                except Exception as msg_err:
                    # SDK 0.0.25 sometimes raises on unknown message types
                    # (e.g. "Unknown message type: rate_limit_event").
                    # Skip the bad message and keep streaming.
                    err_str = str(msg_err)
                    if "Unknown message type" in err_str:
                        continue
                    raise
                messages.append(msg)
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            final_text += block.text
                        # Capture tool-result content (where subagent replies surface)
                        elif ToolResultBlock is not None and isinstance(block, ToolResultBlock):
                            content = getattr(block, "content", None)
                            if isinstance(content, str):
                                final_text += "\n" + content
                            elif isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        final_text += "\n" + c.get("text", "")
                # Also capture ResultMessage.result for SDK builds that put the final
                # assistant reply there.
                if isinstance(msg, ResultMessage):
                    result_text = getattr(msg, "result", None)
                    if isinstance(result_text, str):
                        final_text += "\n" + result_text
                    break
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}", messages, time.monotonic() - t0

    return final_text, messages, time.monotonic() - t0


def _run_claude_cli(project: Path, prompt: str, timeout: float = 240.0):
    """
    Direct CLI invocation — `claude --plugin-dir … -p "<prompt>"`.
    Headless mode prints the final assistant text to stdout.
    This is what real users do, and captures responses regardless of whether
    the model went through Agent delegation.
    """
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            ["claude", "--plugin-dir", str(PLUGIN_ROOT),
             "--dangerously-skip-permissions", "-p", prompt],
            cwd=str(project),
            capture_output=True, text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return proc.stdout, [], time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return f"ERROR: timeout after {timeout}s", [], time.monotonic() - t0
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}", [], time.monotonic() - t0


async def _run_claude_no_agent(project: Path, prompt: str, timeout: float = 240.0):
    """CLI invocation wrapper — kept async for symmetry with run_claude."""
    return _run_claude_cli(project, prompt, timeout)


# ---------- TEST GROUP 1: hook unit tests (no LLM) ----------
def test_hooks_smoke():
    banner("GROUP 1 — Hook scripts (subprocess, no LLM)")

    # 1.1 SessionStart with no memory → must produce valid JSON + setup hint
    proj = make_sample_project("hook-empty", "nextjs")
    t0 = time.monotonic()
    rc, out, err = run_hook("session-start", {"cwd": str(proj), "hook_event_name": "SessionStart"})
    dt = (time.monotonic() - t0) * 1000
    try:
        d = json.loads(out)
        ctx = d["hookSpecificOutput"]["additionalContext"]
        ok = (rc == 0
              and d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
              and "Always consult .claude/memory/" in ctx
              and "Run /setup" in ctx)
        record("session-start (no memory) → valid JSON + /setup hint", ok,
               f"rc={rc} keys={list(d.get('hookSpecificOutput',{}).keys())}", dt)
    except Exception as e:
        record("session-start (no memory) → valid JSON + /setup hint", False, f"parse: {e}", dt)

    # 1.2 SessionStart with seeded memory → must inject CURRENT + HISTORY
    proj = make_sample_project("hook-seeded", "nextjs")
    seed_memory(proj,
                current_md="<!-- Generated by Memplexor -->\n# Current Project Context\n\n## Where We Are\n* PLEXOR-MARKER-WHERE\n\n## Open Questions\n* PLEXOR-MARKER-OPEN\n",
                history_md="## 2026-05-01T00:00:00-04:00 — PLEXOR-MARKER-HIST\n* x\n")
    t0 = time.monotonic()
    rc, out, _ = run_hook("session-start", {"cwd": str(proj)})
    dt = (time.monotonic() - t0) * 1000
    try:
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        ok = (rc == 0
              and "PLEXOR-MARKER-WHERE" in ctx
              and "PLEXOR-MARKER-OPEN" in ctx
              and "PLEXOR-MARKER-HIST" in ctx)
        record("session-start (seeded) → injects CURRENT + HISTORY markers", ok,
               f"markers found: where={'PLEXOR-MARKER-WHERE' in ctx} open={'PLEXOR-MARKER-OPEN' in ctx} hist={'PLEXOR-MARKER-HIST' in ctx}", dt)
    except Exception as e:
        record("session-start (seeded) → injects CURRENT + HISTORY markers", False, str(e), dt)

    # 1.3 Malformed payload → must still emit valid JSON, exit 0
    t0 = time.monotonic()
    rc, out, _ = run_hook("session-start", {})
    dt = (time.monotonic() - t0) * 1000
    try:
        d = json.loads(out)
        ok = rc == 0 and "additionalContext" in d.get("hookSpecificOutput", {})
        record("session-start (empty payload) → fails open with valid JSON", ok, f"rc={rc}", dt)
    except Exception as e:
        record("session-start (empty payload) → fails open with valid JSON", False, str(e), dt)

    # 1.4 session-save with no memory dir → silent no-op
    proj = make_sample_project("hook-no-mem", "nextjs")
    t0 = time.monotonic()
    rc, out, err = run_hook("session-save", {"cwd": str(proj), "hook_event_name": "Stop"})
    dt = (time.monotonic() - t0) * 1000
    has_memory_dir = (proj / ".claude" / "memory").exists()
    ok = rc == 0 and not has_memory_dir
    record("session-save (no memory dir) → no-op, doesn't auto-create", ok,
           f"rc={rc} created_dir={has_memory_dir}", dt)

    # 1.5 session-save Stop → preserves rich user sections
    proj = make_sample_project("hook-preserve", "nextjs")
    rich_current = """<!-- Generated by Memplexor -->
# Current Project Context

**Last updated:** 2026-05-01T00:00:00-04:00

## Where We Are
* PLEXOR-WHERE-VERBATIM

## Recent Work
* PLEXOR-RECENT-VERBATIM

## Next Steps
* PLEXOR-NEXT-VERBATIM

## Open Questions
* PLEXOR-OPEN-VERBATIM

## Important Files
* PLEXOR-IMPORTANT-VERBATIM

## Warnings or Constraints
* PLEXOR-WARN-VERBATIM
"""
    seed_memory(proj, current_md=rich_current, history_md="## 2026-05-01T00:00:00-04:00 — init\n* x\n")
    t0 = time.monotonic()
    rc, _, _ = run_hook("session-save", {"cwd": str(proj), "hook_event_name": "Stop"})
    dt = (time.monotonic() - t0) * 1000
    cur_after = (proj / ".claude/memory/context/CURRENT.md").read_text()
    markers = ["PLEXOR-WHERE-VERBATIM", "PLEXOR-RECENT-VERBATIM", "PLEXOR-NEXT-VERBATIM",
               "PLEXOR-OPEN-VERBATIM", "PLEXOR-IMPORTANT-VERBATIM", "PLEXOR-WARN-VERBATIM"]
    missing = [m for m in markers if m not in cur_after]
    ok = rc == 0 and not missing
    record("session-save preserves all 6 content-bearing sections", ok,
           f"missing markers: {missing}", dt)

    # 1.6 session-save dup suppression — 2 rapid invocations → only 1 H2 added
    proj = make_sample_project("hook-dup", "nextjs")
    seed_memory(proj, current_md="<!-- gen -->\n# x\n\n## Open Questions\n* a\n",
                history_md="## 2026-05-01T00:00:00-04:00 — old\n* x\n")
    h_before = (proj / ".claude/memory/progress/HISTORY.md").read_text().count("\n## ")
    run_hook("session-save", {"cwd": str(proj), "hook_event_name": "Stop"})
    time.sleep(0.5)
    run_hook("session-save", {"cwd": str(proj), "hook_event_name": "Stop"})
    h_after = (proj / ".claude/memory/progress/HISTORY.md").read_text().count("\n## ")
    record("session-save 60s dup suppression on same-title Stop", h_after - h_before == 1,
           f"H2 delta = {h_after - h_before} (expected 1)")

    # 1.7 different titles within 60s → both written
    proj = make_sample_project("hook-distinct", "nextjs")
    seed_memory(proj, current_md="<!-- gen -->\n# x\n\n## Open Questions\n* a\n",
                history_md="## 2026-05-01T00:00:00-04:00 — old\n* x\n")
    h_before = (proj / ".claude/memory/progress/HISTORY.md").read_text().count("\n## ")
    run_hook("session-save", {"cwd": str(proj), "hook_event_name": "Stop"})
    time.sleep(0.5)
    run_hook("session-save", {"cwd": str(proj), "hook_event_name": "PreCompact"})
    h_after = (proj / ".claude/memory/progress/HISTORY.md").read_text().count("\n## ")
    record("session-save distinct titles → both written", h_after - h_before == 2,
           f"H2 delta = {h_after - h_before} (expected 2)")

    # 1.8 No leftover .tmp.* atomic-write files
    proj = make_sample_project("hook-atomic", "nextjs")
    seed_memory(proj, current_md="<!-- gen -->\n# x\n\n## Open Questions\n* a\n",
                history_md="## 2026-05-01T00:00:00-04:00 — old\n* x\n")
    for _ in range(5):
        run_hook("session-save", {"cwd": str(proj), "hook_event_name": "Stop"})
    leftovers = list((proj / ".claude/memory/context").glob(".tmp.*"))
    record("atomic writes leave no .tmp.* files", len(leftovers) == 0,
           f"leftovers={leftovers}")


# ---------- TEST GROUP 2: manifest validation ----------
def test_manifest():
    banner("GROUP 2 — Plugin manifest + claude plugin validate")

    # 2.1 manifest exists + parses
    manifest = PLUGIN_ROOT / ".claude-plugin/plugin.json"
    ok = manifest.is_file()
    record("plugin.json exists", ok, str(manifest))
    if ok:
        try:
            m = json.loads(manifest.read_text())
            keys = set(m.keys())
            need = {"name", "version", "description"}
            record("plugin.json has required keys", need.issubset(keys),
                   f"keys={keys} missing={need - keys}")
        except Exception as e:
            record("plugin.json parses", False, str(e))

    # 2.2 hooks.json exists + registers 3 events
    hooks_json = PLUGIN_ROOT / "hooks/hooks.json"
    try:
        h = json.loads(hooks_json.read_text())
        events = set(h.get("hooks", {}).keys())
        need = {"SessionStart", "Stop", "PreCompact"}
        record("hooks.json registers SessionStart/Stop/PreCompact",
               need.issubset(events), f"events={events}")
    except Exception as e:
        record("hooks.json parses", False, str(e))

    # 2.3 each command has frontmatter with description + allowed-tools
    for cmd in (PLUGIN_ROOT / "commands").glob("*.md"):
        text = cmd.read_text()
        ok = (text.startswith("---\n")
              and "\n---\n" in text[4:]
              and "description:" in text[:1000]
              and "allowed-tools:" in text[:1000])
        record(f"command {cmd.name} has valid frontmatter", ok, f"head={text[:80]!r}")

    # 2.4 hook scripts are executable
    for s in ["session-start.sh", "session-save.sh"]:
        path = PLUGIN_ROOT / "hooks" / s
        ok = os.access(path, os.X_OK)
        record(f"hook {s} is executable", ok, f"mode={oct(path.stat().st_mode)}")

    # 2.5 `claude plugin validate` exit code
    t0 = time.monotonic()
    proc = subprocess.run(
        ["claude", "plugin", "validate", str(PLUGIN_ROOT)],
        capture_output=True, text=True, timeout=30,
    )
    dt = (time.monotonic() - t0) * 1000
    ok = proc.returncode == 0 and "Validation passed" in proc.stdout
    record("`claude plugin validate` passes", ok, f"rc={proc.returncode} stdout={proc.stdout[:120]!r}", dt)


# ---------- TEST GROUP 3: live SDK runs (real LLM) ----------
async def test_live_sessions():
    banner("GROUP 3 — Live Claude Code subprocesses via Python SDK")

    # 3.1 SessionStart injection: empty project → orientation appears
    proj = make_sample_project("sdk-empty", "nextjs")
    t0 = time.monotonic()
    text, msgs, sec = await run_claude(
        proj,
        "What text was injected by the SessionStart hook? Look for a section titled "
        "'Project Memory (Memplexor)' in your system context. If you see it, quote the "
        "entire block verbatim inside triple backticks. If not, say so explicitly.",
        timeout=180,
    )
    dt = sec * 1000
    ok = ("Project Memory (Memplexor)" in text
          and "Always consult .claude/memory/" in text)
    record("SDK: SessionStart hook injects orientation in empty project", ok,
           f"snippet={text[:200]!r}", dt)

    # 3.2 SessionStart injection: seeded → marker round-trips through additionalContext
    proj = make_sample_project("sdk-seeded", "nextjs")
    seed_memory(
        proj,
        current_md=(
            "<!-- Generated by Memplexor -->\n# Current Project Context\n\n"
            "**Last updated:** 2026-05-27T19:00:00-04:00\n\n"
            "## Where We Are\n* Stack: Next.js + React + TypeScript\n* MEMPLEXOR-SDK-MARKER-XYZ123\n\n"
            "## Open Questions\n* App Router or Pages Router?\n"
        ),
        history_md="## 2026-05-27T19:00:00-04:00 — setup complete\n* MEMPLEXOR-HIST-MARKER-ABC789\n",
    )
    t0 = time.monotonic()
    text, _, sec = await run_claude(
        proj,
        "Look in your system context for a 'Project Memory (Memplexor)' block. "
        "Inside it there are two unique tokens beginning with 'MEMPLEXOR-' — quote both verbatim.",
        timeout=180,
    )
    dt = sec * 1000
    ok = "MEMPLEXOR-SDK-MARKER-XYZ123" in text and "MEMPLEXOR-HIST-MARKER-ABC789" in text
    record("SDK: SessionStart injects seeded CURRENT + HISTORY markers", ok,
           f"current_marker_seen={'MEMPLEXOR-SDK-MARKER-XYZ123' in text} hist_marker_seen={'MEMPLEXOR-HIST-MARKER-ABC789' in text}", dt)

    # 3.3 Stop hook fires after subprocess exits → check on-disk artifacts
    proj = make_sample_project("sdk-stop", "nextjs")
    seed_memory(proj, current_md="<!-- gen -->\n# x\n\n## Open Questions\n* a\n",
                history_md="## 2026-05-01T00:00:00-04:00 — init\n* x\n")
    h_before = (proj / ".claude/memory/progress/HISTORY.md").read_text().count("\n## ")
    s_before = len(list((proj / ".claude/memory/sessions").glob("*.md")))
    text, _, sec = await run_claude(
        proj,
        "Say hello in one sentence. Do not use any tools.",
        timeout=120,
    )
    # give the Stop hook a moment after subprocess exit
    await asyncio.sleep(0.5)
    h_after = (proj / ".claude/memory/progress/HISTORY.md").read_text().count("\n## ")
    s_after = len(list((proj / ".claude/memory/sessions").glob("*.md")))
    ok = (h_after - h_before) == 1 and (s_after - s_before) == 1
    record("SDK: Stop hook appends HISTORY + writes session note after subprocess exit", ok,
           f"history Δ={h_after - h_before} sessions Δ={s_after - s_before}", sec * 1000)

    # 3.4 memory-status command body — Claude reads the spec and produces a status report
    # Use the lower-level _run helper with no Agent tool, so the subprocess can't delegate
    # and forces the inline reply to appear in the AssistantMessage stream.
    text, _, sec = await _run_claude_no_agent(
        proj,
        "Read " + str(PLUGIN_ROOT / "commands/memory-status.md") + " then execute its "
        "instructions against the current project (" + str(proj) + "). Output ONLY the "
        "Markdown status report — no preamble, no explanation, no tool delegation.",
        timeout=240,
    )
    ok = ("Memory Status" in text
          and "Current context" in text
          and "History entries" in text)
    record("SDK: subprocess can execute memory-status command from spec", ok,
           f"snippet={text[:300]!r}", sec * 1000)


# ---------- TEST GROUP 4: benchmarks ----------
async def test_benchmarks():
    banner("GROUP 4 — Benchmarks")

    # B1 — Hook script latency (cold)
    proj = make_sample_project("bench-cold", "nextjs")
    seed_memory(proj, current_md="<!-- gen -->\n# x\n\n## Open Questions\n* a\n",
                history_md="## 2026-05-01T00:00:00-04:00 — init\n* x\n")
    durations = []
    for _ in range(10):
        t0 = time.monotonic()
        run_hook("session-start", {"cwd": str(proj)})
        durations.append((time.monotonic() - t0) * 1000)
    durations.sort()
    median = durations[len(durations) // 2]
    p95 = durations[int(len(durations) * 0.95) - 1]
    ok = p95 < 200
    record(f"bench: session-start latency p50={median:.0f}ms p95={p95:.0f}ms (target p95<200ms)",
           ok, f"samples={durations}")

    # B2 — session-save latency
    durations = []
    for _ in range(10):
        # use distinct titles to avoid dup suppression
        evt = "Stop" if _ % 2 == 0 else "PreCompact"
        t0 = time.monotonic()
        run_hook("session-save", {"cwd": str(proj), "hook_event_name": evt})
        durations.append((time.monotonic() - t0) * 1000)
        time.sleep(0.1)
    durations.sort()
    median = durations[len(durations) // 2]
    p95 = durations[int(len(durations) * 0.95) - 1]
    ok = p95 < 500
    record(f"bench: session-save latency p50={median:.0f}ms p95={p95:.0f}ms (target p95<500ms)",
           ok, f"samples={durations}")

    # B3 — additionalContext size (token cost proxy)
    rc, out, _ = run_hook("session-start", {"cwd": str(proj)})
    if rc == 0:
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        char_len = len(ctx)
        approx_tokens = char_len // 4
        ok = approx_tokens < 3000
        record(f"bench: injected context size ~{approx_tokens} tokens ({char_len} chars) (target <3000)",
               ok, "")


# ---------- main ----------
async def main():
    args = set(sys.argv[1:])
    quick = "--quick" in args
    bench = "--bench" in args
    full = not quick

    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)

    test_manifest()
    test_hooks_smoke()
    if full:
        await test_live_sessions()
    if bench:
        await test_benchmarks()

    banner("SUMMARY")
    passed = sum(1 for r in RESULTS if r["passed"])
    failed = sum(1 for r in RESULTS if not r["passed"])
    print(f"  {passed} passed, {failed} failed, {len(RESULTS)} total\n")

    if failed:
        print("Failures:")
        for r in RESULTS:
            if not r["passed"]:
                print(f"  ✗ {r['name']}")
                print(f"      {r['evidence']}")

    # write JSON report
    report = SANDBOX_ROOT / "report.json"
    report.write_text(json.dumps({
        "passed": passed,
        "failed": failed,
        "total": len(RESULTS),
        "results": RESULTS,
    }, indent=2))
    print(f"\nReport: {report}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
