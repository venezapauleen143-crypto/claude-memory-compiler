"""
SessionStart hook - injects knowledge base + memory files into every conversation.

Two jobs:
1. Inject knowledge base index + recent daily log (original function)
2. Inject ALL feedback_*.md + latest project_*.md content from unified-memory/
   so the new 菜逼 doesn't need to manually read them

Configure in .claude/settings.json:
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "command": "uv run python hooks/session-start.py"
        }]
    }
}
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Paths
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
MEMORY_DIR = Path.home() / ".claude" / "unified-memory"

MAX_CONTEXT_CHARS = 40_000  # increased to fit memory files
MAX_LOG_LINES = 30


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


def get_memory_files() -> str:
    """Read all feedback_*.md and the latest project_*.md from unified-memory/."""
    if not MEMORY_DIR.exists():
        return "(no memory directory)"

    parts = []

    # Read ALL feedback files
    feedback_files = sorted(MEMORY_DIR.glob("feedback_*.md"))
    for f in feedback_files:
        try:
            content = f.read_text(encoding="utf-8").strip()
            parts.append(f"### {f.stem}\n{content}")
        except Exception:
            pass

    # Read the LATEST project file (by filename date)
    project_files = sorted(MEMORY_DIR.glob("project_2*.md"), reverse=True)
    if project_files:
        try:
            latest = project_files[0]
            content = latest.read_text(encoding="utf-8").strip()
            parts.append(f"### {latest.stem} (latest)\n{content}")
        except Exception:
            pass

    # Read user files
    user_files = sorted(MEMORY_DIR.glob("user_*.md"))
    for f in user_files:
        try:
            content = f.read_text(encoding="utf-8").strip()
            parts.append(f"### {f.stem}\n{content}")
        except Exception:
            pass

    return "\n\n".join(parts) if parts else "(no memory files)"


def build_context() -> str:
    """Assemble the context to inject into the conversation."""
    parts = []

    # Today's date
    today = datetime.now(timezone.utc).astimezone()
    parts.append(f"## Today\n{today.strftime('%A, %B %d, %Y')}")

    # Memory files (feedback + project + user) — injected FIRST so 菜逼 has all rules
    memory_content = get_memory_files()
    parts.append(f"## Memory Files (auto-loaded)\n\n{memory_content}")

    # Knowledge base index
    if INDEX_FILE.exists():
        index_content = INDEX_FILE.read_text(encoding="utf-8")
        parts.append(f"## Knowledge Base Index\n\n{index_content}")
    else:
        parts.append("## Knowledge Base Index\n\n(empty - no articles compiled yet)")

    # Recent daily log
    recent_log = get_recent_log()
    parts.append(f"## Recent Daily Log\n\n{recent_log}")

    context = "\n\n---\n\n".join(parts)

    # Truncate if too long
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"

    return context


def main():
    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
