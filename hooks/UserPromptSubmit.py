#!/usr/bin/env python3
"""Claude Code `UserPromptSubmit` hook -> SANDBOXED session ingest outbox.

Job: session-ingest-hook-sandboxed-001. Thin entry-point: all logic lives in
hooks/_ingest_sandbox.py:run_hook(). Fires when the user submits a prompt; enqueues the event envelope.

ALWAYS exits 0 (never blocks the user's real turn). Writes ONLY to the
sandboxed outbox (CIC_SESSION_INGEST_OUTBOX or a sandbox default) — NEVER to
~/.claude/settings.json, ~/.claude-personal/settings*.json, or production
Postgres. See output/session-ingest-hook-go-live-checklist.md for the
human-gated live-wiring step this script deliberately does NOT perform.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _ingest_sandbox import run_hook  # noqa: E402

COLLECTOR_NAME = "UserPromptSubmit.py"

if __name__ == "__main__":
    sys.exit(run_hook(COLLECTOR_NAME, extract_turns=False))
