"""
Scoped rollback for ONE already-imported conversation, keyed exclusively by
(provider, provider_session_id).

Job: historical-import-rollback-tool-001

Source of truth for the cascade chain this function relies on (NOT
reimplemented here):
  output/session-postgres-schema.sql
    - session_core.sessions (line ~137-152): the row this function's first
      DELETE targets. `sessions_provider_session_unique UNIQUE (provider,
      provider_session_id)` (line 151) is what makes (provider,
      provider_session_id) a valid, unambiguous targeting key.
    - session_core.turns/chunks/source_refs/manifests (lines ~154-197) and
      session_idx.chunk_fts/chunk_embeddings/ranking_features (lines
      ~209-233): ALL reference session_id with `ON DELETE CASCADE`, directly
      or transitively (turns -> chunks -> source_refs/chunk_fts/
      chunk_embeddings/ranking_features). Deleting the session_core.sessions
      row is therefore sufficient to remove every one of these rows; this
      module does NOT issue a separate DELETE for any of them.
    - session_raw.envelopes (lines 48-103): has NO foreign key to
      session_core.sessions (the raw event store is independent of the
      projection -- see table comment, lines 105-108) and is therefore NOT
      touched by the cascade above. This is the one table this module DOES
      issue a second, explicit DELETE for.

Scope: this module ONLY deletes rows belonging to ONE (provider,
provider_session_id) pair. It does NOT expose a general-purpose "delete
rows matching an arbitrary condition" API, and it does NOT implement a
full-table TRUNCATE wrapper -- see input.md "Nem cél" / "Forbidden
Shortcuts". rollback_conversation()'s signature is structurally limited to
exactly this one pair so that a caller cannot accidentally widen the scope
of a deletion.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from session_store.envelope_writer import SessionStoreConfig


@dataclass(frozen=True)
class RollbackResult:
    """Outcome of one rollback_conversation() call.

    sessions_deleted: number of rows removed from session_core.sessions by
        the first DELETE (0 or 1, since (provider, provider_session_id) is
        UNIQUE -- sessions_provider_session_unique). The CASCADE-deleted
        rows in turns/chunks/source_refs/manifests/chunk_fts/
        chunk_embeddings/ranking_features are NOT individually counted here
        (Postgres does not report cascaded row counts back to the
        DELETE ... statement that triggered them); sessions_deleted is the
        authoritative signal for "was there a session to roll back", and the
        cascade chain audit (see module docstring) is what proves those
        descendant rows are removed whenever sessions_deleted == 1.
    envelopes_deleted: number of rows removed from session_raw.envelopes by
        the second, separate DELETE (no FK/cascade involved -- this table
        has no foreign key to session_core.sessions, so its row count is
        independent of sessions_deleted and can be 0, 1, or >1 -- a
        conversation with N imported nodes has N envelopes rows, one per
        mapping-node, see session_store/chatgpt_import.py /
        session_store/historical_import_runner.py).
    """

    sessions_deleted: int
    envelopes_deleted: int


def rollback_conversation(
    provider: str,
    provider_session_id: str,
    *,
    config: SessionStoreConfig | None = None,
) -> RollbackResult:
    """Delete ONE conversation, identified ONLY by (provider, provider_session_id).

    Two explicit, sequential DELETE statements in a SINGLE transaction
    (one psycopg connection context manager, committed once at the end):

      1. DELETE FROM session_core.sessions WHERE provider = %s AND
         provider_session_id = %s
         -- the ON DELETE CASCADE chain documented in output/session-
         -- postgres-schema.sql (turns -> chunks -> source_refs/chunk_fts/
         -- chunk_embeddings/ranking_features, plus manifests directly off
         -- sessions) removes every descendant row of this session. This
         -- function does NOT issue a second DELETE for any of those
         -- tables -- see module docstring "Forbidden Shortcuts" reference.

      2. DELETE FROM session_raw.envelopes WHERE provider = %s AND
         provider_session_id = %s
         -- issued separately because session_raw.envelopes has no foreign
         -- key to session_core.sessions (the raw event store is
         -- intentionally independent of the projection -- see
         -- output/session-postgres-schema.sql lines 105-108) and is
         -- therefore NOT covered by the cascade in step 1.

    Why ONE transaction (not two independent commits): a partial rollback
    (e.g. sessions row removed, but a connection drop before the envelopes
    DELETE runs) would leave the raw event store inconsistent with the
    projection it once produced -- the import path's own per-row commit
    style (envelope_writer.insert_envelope(), one connection per row) is
    appropriate for a long-running, resumable import, but a single,
    targeted rollback of one already-fully-imported conversation has no
    equivalent "resume" semantics to fall back on, so atomicity here is
    strictly preferable: either both deletes happen, or neither does.

    Idempotent / safe to re-call: if `provider`/`provider_session_id` does
    not match any session_core.sessions row (already rolled back, or never
    imported), the first DELETE affects 0 rows -- this is NOT an error
    condition, no exception is raised, and the function proceeds to the
    second DELETE (which is independently idempotent for the same reason)
    and returns RollbackResult(sessions_deleted=0, envelopes_deleted=0) (or
    envelopes_deleted>0 if envelopes rows somehow outlived their session,
    e.g. a prior partial/manual cleanup).

    This function's signature is INTENTIONALLY limited to exactly
    (provider, provider_session_id) -- there is no `where`/`filter`
    parameter and no "delete everything" mode. This is a structural
    constraint, not just a documentation note: a caller cannot widen the
    scope of a single call to this function beyond one conversation.
    """
    cfg = config or SessionStoreConfig.from_env()

    with psycopg.connect(cfg.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM session_core.sessions
                WHERE provider = %s AND provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            sessions_deleted = cur.rowcount

            cur.execute(
                """
                DELETE FROM session_raw.envelopes
                WHERE provider = %s AND provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            envelopes_deleted = cur.rowcount

        conn.commit()

    return RollbackResult(
        sessions_deleted=sessions_deleted,
        envelopes_deleted=envelopes_deleted,
    )
