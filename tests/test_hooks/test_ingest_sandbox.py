"""Real, executed evidence for session-ingest-hook-sandboxed-001.

Each hook is invoked AS A REAL SUBPROCESS — stdin JSON in, exit code
observed — exactly the way the Claude Code CLI invokes a hook (separate
process, no shared interpreter). No mocks. The ONLY sink is a SANDBOX outbox
file in tmp_path (CIC_SESSION_INGEST_OUTBOX) — never ~/.claude, never
Postgres.

Proves:
  - all 6 hooks enqueue a SessionIngressEnvelope into the sandbox outbox and
    exit 0;
  - the Stop hook additionally reads + enqueues transcript turns via the
    prerequisite session_store.transcript_reader (real, incremental);
  - failure isolation: deliberately broken inputs still exit 0, nothing
    propagates;
  - incremental offset: re-firing Stop on an unchanged transcript enqueues
    no duplicate turns;
  - source-level grep: no hook source writes to ~/.claude / .claude-personal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_DIR = _REPO_ROOT / "hooks"

SIX_HOOKS = [
    "UserPromptSubmit",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SessionStart",
    "SessionEnd",
]


def _run_hook(script: str, stdin_text: str, outbox: Path, env_extra=None):
    """Invoke a hook script as the Claude Code CLI would: separate process,
    stdin payload, CIC_SESSION_INGEST_OUTBOX pointing at the sandbox.
    """
    env = dict(os.environ)
    env["CIC_SESSION_INGEST_OUTBOX"] = str(outbox)
    env["CIC_SESSION_HOOK_LOG_PATH"] = str(outbox.parent / "hooks.errors.log")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(_HOOKS_DIR / f"{script}.py")],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc


def _read_outbox(outbox: Path) -> list[dict]:
    if not outbox.exists():
        return []
    return [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------
# 1. All 6 hooks enqueue an event envelope and exit 0
# --------------------------------------------------------------------------
@pytest.mark.parametrize("script", SIX_HOOKS)
def test_hook_enqueues_event_envelope_and_exits_zero(script, tmp_path):
    outbox = tmp_path / "outbox.ndjson"
    stdin = json.dumps(
        {
            "session_id": "FIXTURE-sess-ingest-001",
            "hook_event_name": script,
            "cwd": "/tmp/fixture",
        }
    )
    proc = _run_hook(script, stdin, outbox)

    assert proc.returncode == 0, f"{script} did not exit 0: {proc.stderr}"
    records = _read_outbox(outbox)
    assert len(records) >= 1
    env = records[0]
    assert env["kind"] == "SessionIngressEnvelope"
    assert env["provider"] == "claude-code"
    assert env["provider_session_id"] == "FIXTURE-sess-ingest-001"
    assert env["provider_event_name"] == script
    assert env["source"]["collector"] == f"{script}.py"
    assert env["trust"] == "session_local"
    assert env["canonical"] is False


# --------------------------------------------------------------------------
# 2. Stop hook reads + enqueues transcript turns (real transcript_reader)
# --------------------------------------------------------------------------
def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(o) for o in lines) + "\n")


def test_stop_hook_extracts_transcript_turns(tmp_path):
    outbox = tmp_path / "outbox.ndjson"
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(
        transcript,
        [
            {
                "type": "user",
                "uuid": "u-1",
                "timestamp": "2026-06-25T10:00:00Z",
                "sessionId": "FIXTURE-sess-ingest-001",
                "message": {"role": "user", "content": "first synthetic prompt"},
            },
            {
                "type": "assistant",
                "uuid": "a-1",
                "timestamp": "2026-06-25T10:00:05Z",
                "sessionId": "FIXTURE-sess-ingest-001",
                "message": {"role": "assistant", "content": "first synthetic reply"},
            },
        ],
    )
    stdin = json.dumps(
        {
            "session_id": "FIXTURE-sess-ingest-001",
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        }
    )
    proc = _run_hook("Stop", stdin, outbox)
    assert proc.returncode == 0, proc.stderr

    records = _read_outbox(outbox)
    turn_records = [r for r in records if r["provider_event_name"] == "transcript.turn"]
    assert len(turn_records) == 2, f"expected 2 turns, got {len(turn_records)}"
    texts = {r["payload"]["text"] for r in turn_records}
    assert "first synthetic prompt" in texts
    assert "first synthetic reply" in texts
    # turn envelopes carry a stable, content-derived turn_id
    assert all(r["payload"]["turn_id"].startswith("sha256:") for r in turn_records)


# --------------------------------------------------------------------------
# 3. Incremental offset — re-firing Stop enqueues no duplicate turns
# --------------------------------------------------------------------------
def test_stop_hook_incremental_no_duplicate_turns(tmp_path):
    outbox = tmp_path / "outbox.ndjson"
    transcript = tmp_path / "transcript.jsonl"
    line = {
        "type": "user",
        "uuid": "u-1",
        "timestamp": "2026-06-25T10:00:00Z",
        "sessionId": "FIXTURE-sess-ingest-001",
        "message": {"role": "user", "content": "only synthetic prompt"},
    }
    _write_transcript(transcript, [line])
    stdin = json.dumps(
        {
            "session_id": "FIXTURE-sess-ingest-001",
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        }
    )
    _run_hook("Stop", stdin, outbox)
    _run_hook("Stop", stdin, outbox)  # fire again on unchanged transcript

    records = _read_outbox(outbox)
    turn_records = [r for r in records if r["provider_event_name"] == "transcript.turn"]
    assert len(turn_records) == 1, "incremental offset should prevent re-enqueue"


# --------------------------------------------------------------------------
# 4. Failure isolation — broken inputs still exit 0, nothing propagates
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "stdin_text",
    [
        "this is not json at all {{{",          # malformed JSON
        "",                                       # empty stdin
        json.dumps([1, 2, 3]),                    # JSON but not an object
        json.dumps({"no_session_or_event": True}),  # missing expected fields
    ],
)
def test_failure_isolation_broken_event_input(stdin_text, tmp_path):
    outbox = tmp_path / "outbox.ndjson"
    proc = _run_hook("UserPromptSubmit", stdin_text, outbox)
    # The whole point: a broken hook input NEVER blocks the user's turn.
    assert proc.returncode == 0, f"hook must exit 0 even on broken input: {proc.stderr}"


def test_failure_isolation_stop_bad_transcript_path(tmp_path):
    outbox = tmp_path / "outbox.ndjson"
    stdin = json.dumps(
        {
            "session_id": "FIXTURE-sess-ingest-001",
            "hook_event_name": "Stop",
            "transcript_path": str(tmp_path / "does-not-exist.jsonl"),
        }
    )
    proc = _run_hook("Stop", stdin, outbox)
    assert proc.returncode == 0, proc.stderr
    # the event envelope is still enqueued even though turn extraction failed
    records = _read_outbox(outbox)
    assert any(r["provider_event_name"] == "Stop" for r in records)


# --------------------------------------------------------------------------
# 5. Sink guard — the ONLY write target is a sandbox path, never real config
# --------------------------------------------------------------------------
def test_default_sinks_are_sandbox_never_real_claude_config(monkeypatch):
    """With NO env override, the resolved outbox + log paths must live under
    this hooks/.sandbox-outbox dir — never under a real ~/.claude or
    ~/.claude-personal config directory. This is the runtime invariant the
    job's most important prohibition demands, checked on the actual resolver.
    """
    sys.path.insert(0, str(_HOOKS_DIR))
    import _ingest_sandbox  # noqa: E402

    monkeypatch.delenv("CIC_SESSION_INGEST_OUTBOX", raising=False)
    monkeypatch.delenv("CIC_SESSION_HOOK_LOG_PATH", raising=False)

    for resolved in (_ingest_sandbox.resolve_outbox_path(), _ingest_sandbox.resolve_log_path()):
        norm = os.path.normpath(resolved)
        assert ".sandbox-outbox" in norm
        assert "/.claude/" not in norm and not norm.endswith("/.claude")
        assert "/.claude-personal/" not in norm
        assert "settings.json" not in os.path.basename(norm)


def test_no_hook_performs_write_to_real_claude_config():
    """Source-level guard: no hook line that performs a filesystem WRITE
    (open(...,'w'/'a'), makedirs, .write) may target a real .claude config
    path. Mentions in comments/docstrings (documenting the prohibition) are
    allowed; only actual write operations are flagged.
    """
    write_markers = ("open(", "makedirs", ".write(")
    claude_markers = (".claude/settings", ".claude-personal/settings", "~/.claude")
    offenders = []
    for py in _HOOKS_DIR.glob("*.py"):
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]  # drop trailing comment
            if any(w in code for w in write_markers) and any(
                c in code for c in claude_markers
            ):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, f"hook must never WRITE real config: {offenders}"
