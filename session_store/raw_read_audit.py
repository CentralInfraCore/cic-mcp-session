"""
Audited read-path for session_raw.envelopes.

Job: session-data-protection-001, "Feladat" 5 ("rollback_conversation()
megerősítése + audit-log olvasásra").

Scope: this module provides the ONE intended entry point for reading
session_raw.envelopes rows for an out-of-band purpose (an admin query, the
historical importer inspecting already-imported data, etc.) -- every call
writes exactly one session_audit.raw_reads row, in the SAME transaction as
the SELECT, so a read is never silently un-audited. This module does NOT
read session_raw.envelopes for the worker pipeline's own internal use
(turn_projector.py/chunk_indexer.py read session_jobs.outbox + the ONE
referenced row by source_id as part of normal projection -- that is the
documented write-path's own consumption, not an out-of-band "someone is
looking at raw data" read, see output/session-data-protection.md "Decisions
Proposed" for why those call sites are NOT routed through this module).

rollback_conversation() (session_store/rollback.py:72) remains the
UNCHANGED, REUSED deletion primitive for session_raw.envelopes -- this
module is reused ALONGSIDE it, not a replacement or modification of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from session_store.envelope_writer import SessionStoreConfig


@dataclass(frozen=True)
class RawReadResult:
    """One log_and_read_raw_envelopes() call's outcome: the rows actually
    read, plus the read_id of the session_audit.raw_reads row written for
    this call (so a caller/test can directly join back to the audit row
    it caused, rather than re-querying by timestamp).
    """

    rows: list[dict[str, Any]]
    read_id: int


def log_and_read_raw_envelopes(
    *,
    reader: str,
    read_kind: str,
    provider: str | None = None,
    provider_session_id: str | None = None,
    config: SessionStoreConfig | None = None,
) -> RawReadResult:
    """Read session_raw.envelopes rows (optionally scoped to one
    (provider, provider_session_id) pair, mirroring rollback_conversation()'s
    own scoping key -- session_store/rollback.py:72-77) AND write exactly
    one session_audit.raw_reads row recording the read, in a SINGLE
    transaction.

    `reader` and `read_kind` are required (not optional/defaulted) --
    input.md "Feladat" 5 requires the audit log to record WHO read and for
    WHAT purpose; a caller that cannot state either should not be reading
    raw envelopes through this entry point.

    If `provider`/`provider_session_id` are both None, reads ALL
    session_raw.envelopes rows (an unscoped "admin browses everything"
    read) -- still audited, with provider/provider_session_id NULL in the
    audit row (see migration comment).
    """
    cfg = config or SessionStoreConfig.from_env()

    with psycopg.connect(cfg.conninfo()) as conn:
        with conn.cursor() as cur:
            if provider is not None or provider_session_id is not None:
                cur.execute(
                    """
                    SELECT id, provider, provider_session_id, payload,
                           raw_payload_hash, occurred_at
                    FROM session_raw.envelopes
                    WHERE provider = %s AND provider_session_id = %s
                    ORDER BY id ASC
                    """,
                    (provider, provider_session_id),
                )
            else:
                cur.execute(
                    """
                    SELECT id, provider, provider_session_id, payload,
                           raw_payload_hash, occurred_at
                    FROM session_raw.envelopes
                    ORDER BY id ASC
                    """
                )
            rows = cur.fetchall()
            columns = ["id", "provider", "provider_session_id", "payload",
                       "raw_payload_hash", "occurred_at"]
            result_rows = [dict(zip(columns, row)) for row in rows]

            cur.execute(
                """
                INSERT INTO session_audit.raw_reads
                    (reader, read_kind, provider, provider_session_id, rows_returned)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING read_id
                """,
                (reader, read_kind, provider, provider_session_id, len(result_rows)),
            )
            read_id = cur.fetchone()[0]
        conn.commit()

    return RawReadResult(rows=result_rows, read_id=read_id)
