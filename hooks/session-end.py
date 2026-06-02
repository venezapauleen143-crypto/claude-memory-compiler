"""
SessionEnd hook - captures conversation transcript for memory extraction.

When a Claude Code session ends, this hook:
1. Extracts conversation context from the transcript
2. Saves a forced summary to daily log (even if flush.py thinks nothing is worth saving)
3. Spawns flush.py for deeper knowledge extraction

Note: the transcript itself is NOT archived here — Claude Code already keeps it
permanently at ~/.claude/projects/*.jsonl.

The hook itself does NO API calls - only local file I/O for speed (<10s).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Recursion guard: if we were spawned by flush.py (which calls Agent SDK,
# which runs Claude Code, which would fire this hook again), exit immediately.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
STATE_DIR = SCRIPTS_DIR

logging.basicConfig(
    filename=str(SCRIPTS_DIR / "flush.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [hook] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

MAX_TURNS = 30
MAX_CONTEXT_CHARS = 15_000
MIN_TURNS_TO_FLUSH = 3  # lowered from 5 to catch shorter sessions


def extract_conversation_context(transcript_path: Path) -> tuple[str, int]:
    """Read JSONL transcript and extract last ~N conversation turns as markdown."""
    turns: list[str] = []

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                role = entry.get("role", "")
                content = entry.get("content", "")

            if role not in ("user", "assistant"):
                continue

            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if isinstance(content, str) and content.strip():
                label = "User" if role == "user" else "Assistant"
                turns.append(f"**{label}:** {content.strip()}\n")

    recent = turns[-MAX_TURNS:]
    context = "\n".join(recent)

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1:]

    return context, len(recent)


def save_forced_summary(context: str, turn_count: int, session_id: str):
    """Always save a summary to daily log, regardless of what flush.py decides."""
    today = datetime.now(timezone.utc).astimezone()
    daily_file = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"
    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    # Create a brief summary from the context
    # Extract first user message and last assistant message as bookends
    lines = context.split("\n")
    user_lines = [l for l in lines if l.startswith("**User:**")]
    asst_lines = [l for l in lines if l.startswith("**Assistant:**")]

    first_topic = user_lines[0][:200] if user_lines else "(no user messages)"
    last_response = asst_lines[-1][:200] if asst_lines else "(no assistant messages)"

    summary = (
        f"### Session {today.strftime('%H:%M')} ({turn_count} turns, {len(context)} chars)\n\n"
        f"**開始話題:** {first_topic}\n"
        f"**最後回應:** {last_response}\n"
        f"**Session ID:** {session_id}\n\n"
    )

    if daily_file.exists():
        existing = daily_file.read_text(encoding="utf-8")
        daily_file.write_text(existing + summary, encoding="utf-8")
    else:
        header = f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n"
        daily_file.write_text(header + summary, encoding="utf-8")

    logging.info("Forced summary saved to %s (%d turns)", daily_file.name, turn_count)


def main() -> None:
    # Read hook input from stdin
    try:
        raw_input = sys.stdin.read()
        logging.info("RAW STDIN: %s", raw_input[:2000])
        try:
            hook_input: dict = json.loads(raw_input)
        except json.JSONDecodeError:
            fixed_input = re.sub(r'(?<!\\)\\(?!["\\])', r'\\\\', raw_input)
            hook_input = json.loads(fixed_input)
    except (json.JSONDecodeError, ValueError, EOFError) as e:
        logging.error("Failed to parse stdin: %s", e)
        return

    logging.info("PARSED KEYS: %s", list(hook_input.keys()))
    session_id = hook_input.get("session_id", "unknown")
    source = hook_input.get("source", "unknown")
    transcript_path_str = hook_input.get("transcript_path", "")

    logging.info("SessionEnd fired: session=%s source=%s", session_id, source)

    if not transcript_path_str or not isinstance(transcript_path_str, str):
        logging.info("SKIP: no transcript path")
        return

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        logging.info("SKIP: transcript missing: %s", transcript_path_str)
        return

    # NOTE: transcript 歸檔不需要，Claude Code 已永久保存在 ~/.claude/projects/*.jsonl

    # Extract conversation context in the hook (fast, no API calls)
    try:
        context, turn_count = extract_conversation_context(transcript_path)
    except Exception as e:
        logging.error("Context extraction failed: %s", e)
        return

    if not context.strip():
        logging.info("SKIP: empty context")
        return

    if turn_count < MIN_TURNS_TO_FLUSH:
        logging.info("SKIP: only %d turns (min %d)", turn_count, MIN_TURNS_TO_FLUSH)
        return

    # === NEW: Always save a forced summary to daily log ===
    try:
        save_forced_summary(context, turn_count, session_id)
    except Exception as e:
        logging.warning("Failed to save forced summary: %s", e)

    # Write context to a temp file for the background process
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    context_file = STATE_DIR / f"session-flush-{session_id}-{timestamp}.md"
    try:
        context_file.write_text(context, encoding="utf-8")
    except Exception as e:
        logging.error("Failed to write context file %s: %s", context_file, e)
        return

    # Spawn flush.py as a background process
    flush_script = SCRIPTS_DIR / "flush.py"

    cmd = [
        "uv",
        "run",
        "--directory",
        str(ROOT),
        "python",
        str(flush_script),
        str(context_file),
        session_id,
    ]

    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        logging.info("Spawned flush.py for session %s (%d turns, %d chars)", session_id, turn_count, len(context))
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)


if __name__ == "__main__":
    main()
