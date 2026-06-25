#!/usr/bin/env python3
"""Shared, SANDBOXED ingest plumbing for the 6 Claude Code session hooks.

Job: session-ingest-hook-sandboxed-001.

This module is the common core behind the 6 thin hook entry-points
(UserPromptSubmit.py, PostToolUse.py, PostToolUseFailure.py, Stop.py,
SessionStart.py, SessionEnd.py). Each entry-point is a few lines that just
calls run_hook() with its own identity; ALL real logic lives here so it can
be unit-tested once.

----------------------------------------------------------------------------
THE single most important constraint (inherited from hooks/log-event.py)
----------------------------------------------------------------------------
A Claude Code hook BLOCKS the real session turn it is attached to until the
hook process exits. An uncaught exception or a non-zero exit can stall or
deny the user's actual work. Therefore run_hook() wraps EVERYTHING in one
broad try/except and ALWAYS returns 0 — no failure (bad stdin, missing
field, unreadable transcript, unwritable outbox, missing dependency) is ever
allowed to propagate. This mirrors log-event.py:main()'s guarantee.

----------------------------------------------------------------------------
SANDBOX constraint (input.md, THE most important prohibition of this job)
----------------------------------------------------------------------------
These hooks NEVER write to a real ~/.claude/settings.json or
~/.claude-personal/settings*.json, and NEVER write to the production
Postgres. The ONLY sink is a SANDBOXED, disposable append-only outbox file
(NDJSON), whose path is resolved from CIC_SESSION_INGEST_OUTBOX (set by the
job's sandboxed settings.json) or a sandbox default UNDER this hooks/
directory. A separate, human-gated "go-live" step (see
output/session-ingest-hook-go-live-checklist.md) is what would later drain
this outbox into the real session_store.envelope_writer.insert_envelope()
path — this module deliberately does NOT do that.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

# Repo root on sys.path so a lazy `from session_store.transcript_reader
# import ...` works when the hook is launched from anywhere (same trick as
# hooks/log-event.py). Done at import time, but import of session_store
# itself stays LAZY inside run_hook()'s try/except (failure isolation).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

PROVIDER = "claude-code"
_UNIT_SEP = "\x1f"

# Sandbox defaults — ALWAYS under this hooks/ dir, NEVER under ~/.claude.
_DEFAULT_OUTBOX = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".sandbox-outbox", "outbox.ndjson"
)
_DEFAULT_LOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".sandbox-outbox", "hooks.errors.log"
)


def resolve_outbox_path() -> str:
    """SANDBOXED outbox path. CIC_SESSION_INGEST_OUTBOX wins; else a sandbox
    default under hooks/.sandbox-outbox/. Never ~/.claude/anything.
    """
    return os.environ.get("CIC_SESSION_INGEST_OUTBOX", _DEFAULT_OUTBOX)


def resolve_log_path() -> str:
    return os.environ.get("CIC_SESSION_HOOK_LOG_PATH", _DEFAULT_LOG)


def _log_error(message: str) -> None:
    """Best-effort error logging to stderr AND a sandbox log file. Never
    raises (both sinks individually wrapped) — a broken log path must not
    defeat the always-exit-0 guarantee.
    """
    line = f"[{datetime.now(timezone.utc).isoformat()}] {message}"
    try:
        print(line, file=sys.stderr)
    except Exception:
        pass
    try:
        log_path = resolve_log_path()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _now_iso_utc() -> str:
    """RFC3339 UTC, second precision, trailing 'Z' — same precision as
    log-event.py:_now_iso_utc (matches the idempotency_key hash basis)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _idempotency_key(
    provider_session_id: str,
    provider_event_name: str | None,
    occurred_at: str,
    raw_payload_hash: str,
) -> str:
    """EXACT formula reused from log-event.py / session-ingress-envelope
    schema: provider US session US event US occurred_at US hash, 0x1F-joined,
    sha256'd once. Reused verbatim, NOT reinvented."""
    basis = _UNIT_SEP.join(
        [
            PROVIDER,
            provider_session_id,
            provider_event_name or "",
            occurred_at,
            raw_payload_hash,
        ]
    )
    return "sha256:" + _sha256_hex(basis.encode("utf-8"))


def build_event_envelope(hook_json: dict, collector_name: str) -> dict:
    """Map a Claude Code hook stdin JSON dict into a SessionIngressEnvelope
    dict — same field mapping as hooks/log-event.py:build_envelope(), with
    the collector name parameterized per hook script. Pure function, no I/O.
    """
    provider_session_id = hook_json.get("session_id") or "unknown-session"
    provider_event_name = hook_json.get("hook_event_name")
    occurred_at = _now_iso_utc()

    payload = hook_json
    canonical_payload_bytes = json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    raw_payload_hash = "sha256:" + _sha256_hex(canonical_payload_bytes)

    return {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": PROVIDER,
        "provider_session_id": provider_session_id,
        "provider_event_name": provider_event_name,
        "source": {"kind": "hook", "collector": collector_name},
        "occurred_at": occurred_at,
        "ingested_at": _now_iso_utc(),
        "payload": payload,
        "payload_encoding": "json",
        "raw_payload_hash": raw_payload_hash,
        "trust": "session_local",
        "canonical": False,
        "interpreted": False,
        "idempotency_key": _idempotency_key(
            provider_session_id, provider_event_name, occurred_at, raw_payload_hash
        ),
        "workstream": os.environ.get("CIC_JOB_ID") or None,
        "schema_notes": None,
    }


def build_turn_envelope(turn: Any, collector_name: str) -> dict:
    """Map ONE transcript_reader.Turn into a SessionIngressEnvelope dict.

    The Turn fields mirror SessionIngressEnvelope vocabulary by design (see
    session_store/transcript_reader.py:Turn docstring). idempotency_key is
    derived from the turn's OWN stable, content-based turn_id (not a fresh
    occurred_at), so re-reading the same transcript line always yields the
    same idempotency_key — the downstream drain can dedup re-ingested turns.
    """
    provider_session_id = turn.provider_session_id or "unknown-session"
    occurred_at = turn.occurred_at or _now_iso_utc()
    provider_event_name = "transcript.turn"

    payload = {
        "turn_id": turn.turn_id,
        "role": turn.role,
        "text": turn.text,
        "tool_use": turn.tool_use,
        "tool_result": turn.tool_result,
        "turn_payload": turn.payload,
    }
    canonical_payload_bytes = json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    raw_payload_hash = "sha256:" + _sha256_hex(canonical_payload_bytes)

    # Stable idempotency: turn_id is already a content-stable sha256 id.
    idempotency_key = "sha256:" + _sha256_hex(
        _UNIT_SEP.join([PROVIDER, provider_session_id, "transcript.turn", turn.turn_id]).encode(
            "utf-8"
        )
    )

    return {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": PROVIDER,
        "provider_session_id": provider_session_id,
        "provider_event_name": provider_event_name,
        "source": {"kind": "transcript", "collector": collector_name},
        "occurred_at": occurred_at,
        "ingested_at": _now_iso_utc(),
        "payload": payload,
        "payload_encoding": "json",
        "raw_payload_hash": raw_payload_hash,
        "trust": "session_local",
        "canonical": False,
        "interpreted": False,
        "idempotency_key": idempotency_key,
        "workstream": os.environ.get("CIC_JOB_ID") or None,
        "schema_notes": None,
    }


def _append_outbox(envelope: dict, outbox_path: str) -> None:
    """Append ONE envelope as a single NDJSON line to the SANDBOX outbox.
    Creates the parent dir if needed. The only persistence side effect of
    these hooks — never touches Postgres, never touches ~/.claude.
    """
    os.makedirs(os.path.dirname(outbox_path), exist_ok=True)
    with open(outbox_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n")


def _offset_store_path(outbox_path: str, transcript_path: str) -> str:
    """Per-transcript byte-offset file, kept in a sandbox sibling dir of the
    outbox so incremental Stop reads resume where they left off (and don't
    re-enqueue already-seen turns). Filename is a hash of transcript_path so
    arbitrary paths map to a safe filename.
    """
    key = _sha256_hex(transcript_path.encode("utf-8"))[:32]
    return os.path.join(os.path.dirname(outbox_path), ".offsets", f"{key}.offset")


def _read_offset(offset_path: str) -> int:
    try:
        with open(offset_path, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def _write_offset(offset_path: str, offset: int) -> None:
    os.makedirs(os.path.dirname(offset_path), exist_ok=True)
    with open(offset_path, "w", encoding="utf-8") as f:
        f.write(str(offset))


def _enqueue_transcript_turns(
    hook_json: dict, collector_name: str, outbox_path: str
) -> int:
    """On Stop: read NEW turns from transcript_path (incrementally, resuming
    from the saved byte offset) via the prerequisite
    session_store.transcript_reader, and enqueue each as a turn envelope.
    Returns the number of turns enqueued. Raises on failure — the caller
    (run_hook) is responsible for catching, per failure isolation.
    """
    transcript_path = hook_json.get("transcript_path")
    if not transcript_path:
        return 0

    # Lazy import — a missing/broken transcript_reader must not raise before
    # run_hook()'s try/except is active.
    from session_store.transcript_reader import read_transcript_incremental

    offset_path = _offset_store_path(outbox_path, transcript_path)
    since_offset = _read_offset(offset_path)
    turns, new_offset = read_transcript_incremental(transcript_path, since_offset)

    for turn in turns:
        _append_outbox(build_turn_envelope(turn, collector_name), outbox_path)

    _write_offset(offset_path, new_offset)
    return len(turns)


def run_hook(collector_name: str, extract_turns: bool = False) -> int:
    """Entry point for every hook script. ALWAYS returns 0.

    1. read stdin, parse JSON (tolerant: empty/blank stdin -> {});
    2. build + enqueue the event envelope into the SANDBOX outbox;
    3. if extract_turns (the Stop hook) AND a transcript_path is present,
       incrementally read + enqueue transcript turns.
    EVERY step is inside one broad try/except — any exception is logged to
    the sandbox log and swallowed, never propagated. Never blocks the user.
    """
    try:
        outbox_path = resolve_outbox_path()

        try:
            raw_stdin = sys.stdin.read()
        except Exception as exc:
            _log_error(f"[{collector_name}] failed to read stdin: {exc!r}")
            return 0

        try:
            hook_json = json.loads(raw_stdin) if raw_stdin.strip() else {}
            if not isinstance(hook_json, dict):
                raise ValueError(f"hook stdin JSON is not an object: {type(hook_json)!r}")
        except Exception as exc:
            _log_error(f"[{collector_name}] failed to parse stdin JSON: {exc!r}")
            return 0

        try:
            _append_outbox(build_event_envelope(hook_json, collector_name), outbox_path)
        except Exception as exc:
            _log_error(
                f"[{collector_name}] failed to enqueue event envelope "
                f"(non-blocking, hook still exits 0): {exc!r}"
            )

        if extract_turns:
            try:
                _enqueue_transcript_turns(hook_json, collector_name, outbox_path)
            except Exception as exc:
                _log_error(
                    f"[{collector_name}] failed to enqueue transcript turns "
                    f"(non-blocking, hook still exits 0): {exc!r}"
                )

        return 0
    except Exception as exc:  # absolute last-resort guard
        try:
            _log_error(f"[{collector_name}] unexpected top-level failure: {exc!r}")
        except Exception:
            pass
        return 0
