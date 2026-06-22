#!/usr/bin/env python3
"""
Claude Code hook collector -> SessionIngressEnvelope -> session_raw.envelopes.

Job: session-hook-collector-001

This is the FIRST real producer of SessionIngressEnvelope rows in this repo.
Every envelope inserted by prior jobs (session-raw-event-store-001 and all
jobs built on top of it) was a hand-constructed test fixture
(_valid_envelope() in tests/test_session_store/*.py) — this script is what
those fixtures stood in for: a Claude Code hook script invoked by the
Claude Code CLI itself, reading the hook's stdin JSON, mapping it
deterministically into a SessionIngressEnvelope dict, and calling the
EXISTING session_store.envelope_writer.insert_envelope() (this script does
NOT reimplement or modify insert_envelope()/validate_envelope() — see that
module for the write-path/validation logic).

----------------------------------------------------------------------------
THE single most important constraint (input.md, NOT negotiable)
----------------------------------------------------------------------------
Claude Code hooks BLOCK the actual tool call / session turn they are
attached to until the hook process exits. A non-zero/blocking exit code
(or an uncaught exception that crashes the interpreter before reaching
sys.exit) on, say, PreToolUse, can deny or stall the user's real tool call.
A bad Postgres connection must NEVER be able to do that.

Therefore:
  - main() wraps EVERYTHING (stdin read, JSON parse, envelope construction,
    insert_envelope() call) in a single broad try/except.
  - ANY exception (DB connection refused, DB timeout, validation error,
    malformed stdin JSON, missing fields, anything) is caught, logged to
    stderr (Claude Code hook stderr is observable to the user/developer but
    does NOT itself block anything), and the script still exits 0.
  - There is no code path in this script that exits non-zero. Exit code is
    ALWAYS 0, success or failure, by design — see "Decisions Proposed" /
    "Forbidden Shortcuts" in output/session-hook-collector-report.md for
    the full rationale (this mirrors the existing, already-deployed
    cic-factory pattern in tools/hooks/log-event.py: "Always exits 0 -
    never blocks the agent").

----------------------------------------------------------------------------
Claude Code hook stdin JSON contract (source: Claude Code docs, "Hooks"
reference - own knowledge, no live fetch performed by this job; explicitly
cited as such in the report's "Decisions Proposed" section, not invented
ad hoc)
----------------------------------------------------------------------------
Common fields on EVERY hook invocation's stdin JSON:
  - session_id          str  - the Claude Code session identifier
  - transcript_path      str  - path to the session transcript file
  - cwd                   str  - the working directory the session is in
  - hook_event_name        str  - one of PreToolUse / PostToolUse /
                                    UserPromptSubmit / Stop / SubagentStop /
                                    Notification / SessionStart / SessionEnd

Event-specific fields:
  - PreToolUse / PostToolUse:
      tool_name           str   - the tool being invoked
      tool_input          dict  - the tool's input parameters
      tool_response       dict  - (PostToolUse only) the tool's result
  - UserPromptSubmit:
      prompt              str   - the raw text the user submitted
  - Stop / SubagentStop:
      stop_hook_active    bool  - true if a Stop hook is already running
                                    (loop guard, mirrored from the existing
                                    log-event.py pattern, see below)

This script does not assume any OTHER fields exist; every field access
below uses .get() with a safe default, and a hook JSON that is missing
fields this script doesn't itself require maps to a smaller, but still
valid, SessionIngressEnvelope (e.g. provider_event_name may end up None on
a malformed/unknown event - validate_envelope() does not require it).

----------------------------------------------------------------------------
hook JSON -> SessionIngressEnvelope field mapping
----------------------------------------------------------------------------
See output/session-hook-collector-report.md, "Findings" section, for the
full mapping table with the schema/test-fixture provenance for each
SessionIngressEnvelope field. Summary:

  apiVersion            = "cic.session/v1"            (constant)
  kind                   = "SessionIngressEnvelope"     (constant)
  event_id                = uuid4(), generated here       (NOT from hook JSON
                                                             - hook JSON has no
                                                             per-event UUID)
  provider                 = "claude-code"                  (constant)
  provider_session_id       = hook_json["session_id"]
  provider_event_name        = hook_json["hook_event_name"]
  source.kind                 = "hook"                        (constant)
  source.collector              = "log-event.py"                (this
                                                                    script's
                                                                    own
                                                                    filename
                                                                    - matches
                                                                    every
                                                                    existing
                                                                    test
                                                                    fixture's
                                                                    source.collector
                                                                    value)
  occurred_at                    = now() UTC, ISO-8601 with 'Z',
                                     second precision           (hook JSON
                                                                   carries no
                                                                   provider-side
                                                                   timestamp;
                                                                   collector
                                                                   observation
                                                                   time is used,
                                                                   per the
                                                                   envelope
                                                                   schema's
                                                                   documented
                                                                   fallback:
                                                                   "provider-side
                                                                   timestamp if
                                                                   available,
                                                                   else collector
                                                                   observation
                                                                   time")
  ingested_at                     = now() UTC, ISO-8601 with 'Z',
                                      second precision           (computed
                                                                    immediately
                                                                    after
                                                                    occurred_at,
                                                                    so the two
                                                                    are equal or
                                                                    ingested_at
                                                                    is later by
                                                                    sub-second
                                                                    amounts only)
  payload                           = the FULL raw hook JSON dict,
                                        unmodified              (see "Decisions
                                                                   Proposed" -
                                                                   chosen over a
                                                                   partial subset
                                                                   so this script
                                                                   does not have
                                                                   to anticipate
                                                                   every future
                                                                   hook_event_name's
                                                                   field shape;
                                                                   matches the
                                                                   schema's
                                                                   "structurally
                                                                   preserved, not
                                                                   summarized"
                                                                   requirement)
  payload_encoding                   = "json"                    (constant)
  raw_payload_hash                    = "sha256:" + sha256(canonical
                                          JSON serialization of payload)
  trust                                = "session_local"          (constant)
  canonical                             = False                    (constant,
                                                                       schema
                                                                       const:false)
  interpreted                           = False                    (constant,
                                                                       schema
                                                                       const:false)
  idempotency_key                        = sha256(...)               (see
                                                                         "Decisions
                                                                         Proposed"
                                                                         /
                                                                         output/session-ingress-envelope.schema.yaml
                                                                         lines
                                                                         214-247
                                                                         for the
                                                                         exact,
                                                                         already-
                                                                         normative
                                                                         formula
                                                                         this
                                                                         script
                                                                         reuses
                                                                         verbatim)
  workstream                              = os.environ.get("CIC_JOB_ID")
                                              or None                (best-effort,
                                                                        optional
                                                                        field;
                                                                        mirrors
                                                                        the
                                                                        existing
                                                                        log-event.py
                                                                        CIC_JOB_ID
                                                                        convention)
  schema_notes                             = None                    (no
                                                                         truncation/
                                                                         partial-capture
                                                                         condition
                                                                         this
                                                                         script
                                                                         needs to
                                                                         flag)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone

# This script is invoked as a standalone process by the Claude Code hook
# runner (stdin/stdout, no shared Python process with the rest of this
# repo) - sys.path is adjusted so it can import session_store directly when
# run from the repo root, exactly like tests/test_session_store/*.py do.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

COLLECTOR_NAME = "log-event.py"
LOG_PATH = os.environ.get(
    "SESSION_HOOK_LOG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "log-event.errors.log"),
)


def _log_error(message: str) -> None:
    """Best-effort error logging to stderr AND a local file.

    Never raises. Both sinks are individually wrapped so that a broken
    stderr (unlikely, but not impossible under unusual process setups) or
    an unwritable log file cannot escalate into an uncaught exception that
    would defeat the entire point of this function.
    """
    line = f"[{datetime.now(timezone.utc).isoformat()}] {message}"
    try:
        print(line, file=sys.stderr)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _now_iso_utc() -> str:
    """Current UTC time, RFC3339, second precision, trailing 'Z'.

    Second precision (not microsecond) matches the idempotency_key formula
    in output/session-ingress-envelope.schema.yaml, which requires
    occurred_at to be "normalized to RFC3339 UTC with second precision
    before hashing" - using the same precision for the stored occurred_at
    value (not just the hash input) keeps the stored timestamp and the
    hashed timestamp string identical, avoiding a subtle mismatch between
    what is persisted and what was hashed.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_envelope(hook_json: dict) -> dict:
    """Deterministically map a Claude Code hook stdin JSON dict into a
    SessionIngressEnvelope dict (see module docstring for the full field
    mapping table and provenance).

    Pure function, no I/O, no DB access - kept separate from main() so it
    can be unit-tested/inspected independently of stdin/DB plumbing, and so
    main()'s try/except has the smallest possible surface that still
    covers every failure mode (a bug in this function is still caught by
    main()'s broad except, see "THE single most important constraint").
    """
    provider_session_id = hook_json.get("session_id") or "unknown-session"
    provider_event_name = hook_json.get("hook_event_name")

    occurred_at = _now_iso_utc()
    ingested_at = _now_iso_utc()

    # payload = the full raw hook JSON, unmodified - see module docstring
    # "hook JSON -> SessionIngressEnvelope field mapping" / "Decisions
    # Proposed" for why the full dict (not a hand-picked subset) is kept.
    payload = hook_json

    # raw_payload_hash: sha256 over a deterministic (sort_keys=True) JSON
    # serialization of the payload, so the same payload value always
    # produces the same hash regardless of the original stdin key order.
    canonical_payload_bytes = json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    raw_payload_hash = "sha256:" + _sha256_hex(canonical_payload_bytes)

    # idempotency_key: EXACT formula from
    # output/session-ingress-envelope.schema.yaml (lines 214-247) -
    # provider + US + provider_session_id + US + (provider_event_name or
    # "") + US + occurred_at + US + raw_payload_hash, joined with ASCII
    # unit separator 0x1F, then sha256'd once. See module docstring
    # "Decisions Proposed" for why this script reuses that formula verbatim
    # rather than inventing a new one.
    provider = "claude-code"
    unit_sep = "\x1f"
    idempotency_input = unit_sep.join(
        [
            provider,
            provider_session_id,
            provider_event_name or "",
            occurred_at,
            raw_payload_hash,
        ]
    )
    idempotency_key = "sha256:" + _sha256_hex(idempotency_input.encode("utf-8"))

    return {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": provider,
        "provider_session_id": provider_session_id,
        "provider_event_name": provider_event_name,
        "source": {"kind": "hook", "collector": COLLECTOR_NAME},
        "occurred_at": occurred_at,
        "ingested_at": ingested_at,
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


def main() -> int:
    """Entry point. ALWAYS returns 0 - see module docstring "THE single
    most important constraint". Every failure mode (bad stdin, DB
    unreachable, validation error, anything) is caught here and only
    logged, never propagated as a blocking exit code or an uncaught
    exception.
    """
    try:
        raw_stdin = sys.stdin.read()
    except Exception as exc:
        _log_error(f"failed to read stdin: {exc!r}")
        return 0

    try:
        hook_json = json.loads(raw_stdin) if raw_stdin.strip() else {}
        if not isinstance(hook_json, dict):
            raise ValueError(f"hook stdin JSON is not an object: {type(hook_json)!r}")
    except Exception as exc:
        _log_error(f"failed to parse hook stdin JSON: {exc!r}")
        return 0

    try:
        envelope = build_envelope(hook_json)
    except Exception as exc:
        _log_error(f"failed to build SessionIngressEnvelope: {exc!r}")
        return 0

    try:
        # Imported lazily, INSIDE the try block, so that even a missing
        # dependency (e.g. psycopg not installed in whatever Python runs
        # this hook) cannot raise before this function's own try/except is
        # active - see "THE single most important constraint".
        from session_store.envelope_writer import insert_envelope

        insert_envelope(envelope)
    except Exception as exc:
        # Covers: DB unreachable/connection refused, validation error
        # raised by validate_envelope() (e.g. malformed envelope),
        # ImportError, or any other failure from the write-path. This is
        # the exact scenario input.md calls out by name: "egy rossz
        # Postgres-kapcsolat kepes tonkretenni a felhasznalo tenyleges
        # Claude Code session-jet" - this except clause is what prevents
        # that.
        _log_error(
            f"insert_envelope() failed (non-blocking, hook still exits 0): {exc!r}"
        )
        return 0

    return 0


if __name__ == "__main__":
    # Always exit 0 regardless of main()'s return value, as a final
    # defense-in-depth measure - main() itself already only ever returns 0,
    # but sys.exit(main()) here makes that guarantee explicit and visible
    # at the call site rather than implicit in main()'s body alone.
    sys.exit(main())
