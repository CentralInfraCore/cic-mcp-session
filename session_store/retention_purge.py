"""
Time-based retention purge for session_raw.envelopes.

Job: session-raw-retention-purge-001 -- the follow-up named explicitly by
session-data-protection-001's retention policy
(output/session-data-protection-retention-policy.md, "Next Jobs": "Egy KOVETO
job, amely ... a session_audit.raw_purges audit-tabla + utemezett purge-job ...
TENYLEGESEN implementalja").

What this module IS:
- A single, callable entry point, purge_expired_raw_envelopes(), that deletes
  session_raw.envelopes rows older than a retention window measured on
  **occurred_at** (the event's actual time), NEVER ingested_at -- the policy
  doc is explicit that occurred_at is the relevant "how old is this data"
  axis, not when it happened to be written.
- Default retention 90 days (the policy default), overridable per call or via
  the SESSION_RAW_RETENTION_DAYS env var.
- The DELETE and its session_audit.raw_purges audit row run in ONE
  transaction (mirroring session_store/raw_read_audit.py and
  session_store/rollback.py:72 rollback_conversation()): a purge is never
  silently un-audited, and the recorded rows_deleted is the count the DELETE
  actually removed (DELETE ... RETURNING), not a pre-count estimate.
- A dry_run mode that counts what WOULD be deleted and removes nothing /
  writes no audit row.

What this module is NOT (deliberately out of scope -- see input.md "Nem cel"):
- It is NOT a daemon / scheduler. Installing a cron / systemd timer that calls
  this function on a schedule is a hosting/operator decision, not part of this
  capability (hence status experimental, not candidate). The caller owns the
  schedule and the connection lifecycle.
- It does NOT touch session_core.* (the projected turns/chunks layer) -- that
  is a separate, later retention decision. This purge is scoped to
  session_raw.envelopes ONLY.
- It does NOT replace or reimplement rollback_conversation()
  (session_store/rollback.py:72), which remains the UNCHANGED, targeted,
  (provider, provider_session_id)-keyed deletion primitive (e.g. a GDPR
  erase-this-conversation request). The two coexist: rollback_conversation()
  is targeted + immediate; this purge is time-based + automatic housekeeping.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import psycopg

from session_store.envelope_writer import SessionStoreConfig

#: Policy default retention window, per
#: output/session-data-protection-retention-policy.md ("90 nap").
DEFAULT_RETENTION_DAYS = 90

#: Env var that overrides the default (an operator/scheduler can set the
#: window without code changes). A per-call retention_days argument wins over
#: this; this wins over DEFAULT_RETENTION_DAYS.
RETENTION_DAYS_ENV = "SESSION_RAW_RETENTION_DAYS"


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of one purge_expired_raw_envelopes() call.

    rows_deleted   -- rows actually removed from session_raw.envelopes
                      (0 on a dry run, where nothing is removed; the count of
                      rows that WOULD be removed is `would_delete`).
    would_delete   -- on a dry run, the count matched by the cutoff predicate;
                      on a real run, equal to rows_deleted.
    cutoff         -- the now() - interval boundary; rows with
                      occurred_at < cutoff were (or would be) deleted. Captured
                      in the SAME statement/transaction as the delete/count, so
                      it provably matches the predicate used.
    retention_days -- the window actually used (after resolving overrides).
    dry_run        -- whether this was a no-op preview.
    purge_id       -- session_audit.raw_purges.purge_id of the audit row
                      written for this purge; None on a dry run (a dry run is a
                      preview, not a purge event, so it writes no audit row --
                      every row in session_audit.raw_purges is a real deletion).
    """

    rows_deleted: int
    would_delete: int
    cutoff: datetime
    retention_days: int
    dry_run: bool
    purge_id: int | None


def resolve_retention_days(retention_days: int | None = None) -> int:
    """Resolve the retention window: explicit arg > env > default.

    Raises ValueError on a negative window (a negative retention would delete
    future-dated rows, which is never intended) or a non-integer env value.
    """
    if retention_days is None:
        raw = os.environ.get(RETENTION_DAYS_ENV)
        if raw is None or raw.strip() == "":
            retention_days = DEFAULT_RETENTION_DAYS
        else:
            try:
                retention_days = int(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{RETENTION_DAYS_ENV} must be an integer, got {raw!r}"
                ) from exc
    if retention_days < 0:
        raise ValueError(f"retention_days must be >= 0, got {retention_days}")
    return retention_days


def purge_expired_raw_envelopes(
    *,
    purger: str,
    retention_days: int | None = None,
    dry_run: bool = False,
    config: SessionStoreConfig | None = None,
) -> PurgeResult:
    """Delete session_raw.envelopes rows whose occurred_at is older than the
    retention window, recording one session_audit.raw_purges audit row in the
    SAME transaction as the DELETE.

    `purger` is required (not defaulted) -- the audit row must record WHO/what
    ran the purge (free text, e.g. an operator name or "retention_cron"),
    mirroring session_audit.raw_reads.reader.

    On a real run, the DELETE predicate is
        occurred_at < now() - make_interval(days => retention_days)
    -- occurred_at ONLY, never ingested_at. now() is the transaction timestamp
    (constant within the transaction), and the cutoff is captured in the same
    CTE as the DELETE, so the audit row's `cutoff` is exactly the boundary the
    DELETE used.

    On a dry run, nothing is deleted and no audit row is written; `would_delete`
    reports how many rows the same predicate currently matches.
    """
    cfg = config or SessionStoreConfig.from_env()
    days = resolve_retention_days(retention_days)

    with psycopg.connect(cfg.conninfo()) as conn:
        with conn.cursor() as cur:
            if dry_run:
                # Count-only preview. Same predicate, no DELETE, no audit row.
                cur.execute(
                    """
                    WITH cutoff AS (
                        SELECT now() - make_interval(days => %s) AS ts
                    )
                    SELECT
                        (SELECT ts FROM cutoff),
                        (SELECT count(*) FROM session_raw.envelopes
                         WHERE occurred_at < (SELECT ts FROM cutoff))
                    """,
                    (days,),
                )
                cutoff, would_delete = cur.fetchone()
                # No commit needed (read-only), but rollback to be explicit
                # that a dry run leaves zero side effects.
                conn.rollback()
                return PurgeResult(
                    rows_deleted=0,
                    would_delete=int(would_delete),
                    cutoff=cutoff,
                    retention_days=days,
                    dry_run=True,
                    purge_id=None,
                )

            # Real purge: capture cutoff, DELETE, and count removed rows in one
            # statement, then write the audit row in the SAME transaction.
            cur.execute(
                """
                WITH cutoff AS (
                    SELECT now() - make_interval(days => %s) AS ts
                ),
                deleted AS (
                    DELETE FROM session_raw.envelopes
                    WHERE occurred_at < (SELECT ts FROM cutoff)
                    RETURNING 1
                )
                SELECT (SELECT ts FROM cutoff), (SELECT count(*) FROM deleted)
                """,
                (days,),
            )
            cutoff, rows_deleted = cur.fetchone()
            rows_deleted = int(rows_deleted)

            cur.execute(
                """
                INSERT INTO session_audit.raw_purges
                    (purger, retention_days, cutoff, rows_deleted)
                VALUES (%s, %s, %s, %s)
                RETURNING purge_id
                """,
                (purger, days, cutoff, rows_deleted),
            )
            purge_id = cur.fetchone()[0]
        conn.commit()

    return PurgeResult(
        rows_deleted=rows_deleted,
        would_delete=rows_deleted,
        cutoff=cutoff,
        retention_days=days,
        dry_run=False,
        purge_id=purge_id,
    )
