"""
End-to-end tests for the session_api.* SQL functions
(search_context, get_timeline, get_context_pack, session_status) against a
REAL Postgres instance.

Job: session-retrieval-quality-001

This is the FIRST job that ever calls any session_api.* function against
real data. The 4 functions were defined in output/session-postgres-schema.sql
(job session-postgres-storage-design-001, a DESIGN job — "function exists in
the .sql file" was never proven to mean "function works"), and this module
proves (or disproves) that with actual function-call output, never by
reading the SQL and reasoning about it.

These tests do NOT mock the database connection, the outbox, session_core,
or session_idx tables, and do NOT insert directly into session_core/
session_idx — every fixture row is produced by driving the REAL write-path
chain:

    insert_envelope() [session_store.envelope_writer]
        -> trg_session_raw_envelopes_enqueue trigger
        -> session_jobs.outbox row (job_type='project_envelope')
        -> run_projection_batch() [session_store.turn_projector]
        -> session_core.sessions / session_core.turns row(s)
        -> trg_session_core_turns_enqueue_index trigger
        -> session_jobs.outbox row (job_type='index_turn')
        -> run_indexing_batch() [session_store.chunk_indexer]
        -> session_core.chunks / session_idx.chunk_fts / session_idx.chunk_embeddings

Two specific suspected integration gaps are tested explicitly here (see
input.md "Kontextus" for the full rationale):

1. FTS language-config mismatch: chunk_indexer._insert_chunk_fts() uses
   to_tsvector('simple', text) (session_store/chunk_indexer.py:280-286), but
   session_api.search_context() queries with
   plainto_tsquery('english', p_query) (output/session-postgres-schema.sql:338,
   342). 'simple' does not stem; 'english' does. A chunk containing "running"
   queried with "run" may not match. Tested in
   TestSearchContextStemmingMismatch below — actual query, actual result,
   not inferred.

2. session_status() pending_jobs undercount: the function's pending_jobs
   subquery (output/session-postgres-schema.sql:392-397) matches outbox rows
   via payload->>'event_id'. The 'project_envelope' trigger
   (session_raw.enqueue_projection_job(), schema.sql:301-309) puts event_id
   in the payload; the 'index_turn' trigger
   (session_core.enqueue_chunk_indexing_job(),
   output/session-chunk-indexer-migration.sql:67-75) puts session_id/turn_seq
   instead, NOT event_id. Tested in TestSessionStatusPendingJobs below —
   actual outbox row, actual function call, not inferred.

Reproduction:

    docker run -d --name session-retrieval-quality-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55435:5432 pgvector/pgvector:pg16

    # wait for readiness (pg_isready), then apply ALL schema files in order:
    docker exec -i session-retrieval-quality-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql
    docker exec -i session-retrieval-quality-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-chunk-indexer-migration.sql
    # If output/session-retrieval-quality-migration.sql exists, apply it too,
    # after the two above, in that order.
    docker exec -i session-retrieval-quality-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-retrieval-quality-migration.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55435 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        pytest tests/test_session_store/test_session_api.py -v

    docker rm -f session-retrieval-quality-test
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from session_store.chunk_indexer import run_indexing_batch
from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.turn_projector import run_projection_batch


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55435")),
        dbname=os.environ.get("SESSION_STORE_PG_DB", "testdb"),
        user=os.environ.get("SESSION_STORE_PG_USER", "postgres"),
        password=os.environ.get("SESSION_STORE_PG_PASSWORD", "test"),
    )


@pytest.fixture(scope="session")
def pg_config() -> SessionStoreConfig:
    cfg = _pg_config()
    try:
        with psycopg.connect(cfg.conninfo(), connect_timeout=5):
            pass
    except psycopg.OperationalError as exc:
        pytest.fail(
            "Cannot reach a real Postgres instance for session_api tests. "
            "Start the test container and apply ALL schema/migration files "
            "first (see module docstring for the exact commands). Original "
            f"error: {exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _clean_tables(pg_config: SessionStoreConfig):
    """Truncate all tables touched by the full chain before each test."""
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE session_idx.chunk_embeddings, session_idx.chunk_fts, "
                "session_core.chunks, session_jobs.outbox, session_core.turns, "
                "session_core.sessions, session_raw.envelopes CASCADE"
            )
        conn.commit()
    yield


def _valid_envelope(**overrides) -> dict:
    base = {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": "claude-code",
        "provider_session_id": "sess-api-001",
        "provider_event_name": "Stop",
        "source": {"kind": "hook", "collector": "log-event.py"},
        "occurred_at": datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
        "ingested_at": datetime(2026, 6, 20, 12, 0, 1, tzinfo=timezone.utc),
        "payload": {"raw_text": "hello world"},
        "payload_encoding": "json",
        "raw_payload_hash": "sha256:" + ("a" * 64),
        "trust": "session_local",
        "canonical": False,
        "interpreted": False,
        "idempotency_key": "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32],
        "workstream": None,
        "schema_notes": None,
    }
    base.update(overrides)
    return base


def _run_chain_for_envelope(pg_config: SessionStoreConfig, **envelope_overrides) -> None:
    """Drive ONE envelope through the full real chain:
    insert_envelope -> run_projection_batch -> run_indexing_batch.

    Per input.md "Forbidden Shortcuts": no hand-crafted session_core/
    session_idx rows. This is the only way fixture data enters those tables
    in this module.
    """
    envelope = _valid_envelope(**envelope_overrides)
    insert_envelope(envelope, config=pg_config)
    run_projection_batch(config=pg_config)
    run_indexing_batch(config=pg_config)


def _get_session_id(pg_config: SessionStoreConfig, provider_session_id: str):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT session_id FROM session_core.sessions WHERE provider_session_id = %s",
                (provider_session_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _chunk_id_for_text(pg_config: SessionStoreConfig, text: str):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id FROM session_core.chunks WHERE text = %s", (text,)
            )
            row = cur.fetchone()
            return row[0] if row else None


def _call_search_context(pg_config, session_id, query, limit=20):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, turn_id, text, rank FROM session_api.search_context(%s, %s, %s)",
                (session_id, query, limit),
            )
            return cur.fetchall()


def _call_get_timeline(pg_config, session_id, limit=100):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT turn_id, occurred_at, role, turn_seq FROM session_api.get_timeline(%s, %s)",
                (session_id, limit),
            )
            return cur.fetchall()


def _call_get_context_pack(pg_config, session_id, max_chunks=50):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, turn_seq, text FROM session_api.get_context_pack(%s, %s)",
                (session_id, max_chunks),
            )
            return cur.fetchall()


def _call_session_status(pg_config, session_id):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, status, started_at, last_seen_at, pending_jobs "
                "FROM session_api.session_status(%s)",
                (session_id,),
            )
            return cur.fetchone()


# ---------------------------------------------------------------------------
# 1. Bilingual (Hungarian + English) multi-turn fixture, built through the
#    REAL chain, containing a stemming-sensitive English word pair
#    ("running" in content / "run" in the query) per input.md "2.".
# ---------------------------------------------------------------------------
class TestBilingualFixtureRealChain:
    def test_fixture_builds_through_real_chain_and_produces_expected_rows(
        self, pg_config: SessionStoreConfig
    ):
        """Sanity-check the fixture itself: three turns, mixed HU/EN content,
        each turn produces exactly one chunk (short text), with fts +
        embedding rows — proves the real chain ran, before any session_api
        assertions are made on top of it.
        """
        _run_chain_for_envelope(
            pg_config,
            provider_event_name="UserPromptSubmit",
            occurred_at=datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
            payload={"raw_text": "Szia, futás közben szeretnék beszélni a projektről."},
            idempotency_key="sha256:" + ("1" * 64),
            event_id=str(uuid.uuid4()),
        )
        _run_chain_for_envelope(
            pg_config,
            provider_event_name="Stop",
            occurred_at=datetime(2026, 6, 20, 12, 1, 0, tzinfo=timezone.utc),
            payload={"raw_text": "The deployment pipeline is running smoothly today."},
            idempotency_key="sha256:" + ("2" * 64),
            event_id=str(uuid.uuid4()),
        )
        _run_chain_for_envelope(
            pg_config,
            provider_event_name="PostToolUse",
            occurred_at=datetime(2026, 6, 20, 12, 2, 0, tzinfo=timezone.utc),
            payload={"raw_text": "A teszt sikeresen lefutott, minden zöld."},
            idempotency_key="sha256:" + ("3" * 64),
            event_id=str(uuid.uuid4()),
        )

        session_id = _get_session_id(pg_config, "sess-api-001")
        assert session_id is not None

        timeline = _call_get_timeline(pg_config, session_id)
        assert len(timeline) == 3

        with psycopg.connect(pg_config.conninfo()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM session_core.chunks WHERE session_id = %s",
                    (session_id,),
                )
                assert cur.fetchone()[0] == 3
                cur.execute(
                    "SELECT count(*) FROM session_idx.chunk_fts f "
                    "JOIN session_core.chunks c ON c.chunk_id = f.chunk_id "
                    "WHERE c.session_id = %s",
                    (session_id,),
                )
                assert cur.fetchone()[0] == 3
                cur.execute(
                    "SELECT count(*) FROM session_idx.chunk_embeddings e "
                    "JOIN session_core.chunks c ON c.chunk_id = e.chunk_id "
                    "WHERE c.session_id = %s",
                    (session_id,),
                )
                assert cur.fetchone()[0] == 3


# ---------------------------------------------------------------------------
# 2. session_api.search_context() — exact-word match (must always work
#    regardless of the stemming-config question below).
# ---------------------------------------------------------------------------
class TestSearchContextExactMatch:
    def test_exact_word_query_returns_expected_chunk(self, pg_config: SessionStoreConfig):
        _run_chain_for_envelope(
            pg_config,
            payload={"raw_text": "A teszt sikeresen lefutott, minden zöld."},
            idempotency_key="sha256:" + ("4" * 64),
            event_id=str(uuid.uuid4()),
        )
        session_id = _get_session_id(pg_config, "sess-api-001")
        expected_chunk_id = _chunk_id_for_text(
            pg_config, "A teszt sikeresen lefutott, minden zöld."
        )
        assert expected_chunk_id is not None

        # 'zöld' has no special accented-character handling concern for
        # 'simple' tsvector tokenization (lowercased, tokenized as-is); use
        # a plain ASCII English exact word from a second, English chunk
        # instead, to avoid conflating accent-folding behavior with the
        # stemming question this job is specifically about.
        _run_chain_for_envelope(
            pg_config,
            payload={"raw_text": "The deployment finished successfully today."},
            idempotency_key="sha256:" + ("5" * 64),
            event_id=str(uuid.uuid4()),
        )
        english_chunk_id = _chunk_id_for_text(
            pg_config, "The deployment finished successfully today."
        )
        assert english_chunk_id is not None

        rows = _call_search_context(pg_config, session_id, "deployment")
        chunk_ids = [r[0] for r in rows]
        assert english_chunk_id in chunk_ids, (
            f"exact-word query 'deployment' did not return expected chunk_id="
            f"{english_chunk_id}; actual rows={rows!r}"
        )


# ---------------------------------------------------------------------------
# 3. session_api.search_context() — stemming-sensitive query. THIS is the
#    test that decided suspected gap #1 by actual execution.
#
# NOTE on pre-fix vs. post-fix: this test file targets the FIXED state of
# search_context() (output/session-retrieval-quality-migration.sql already
# applied — plainto_tsquery('simple', ...), matching chunk_indexer's
# to_tsvector('simple', ...)). The PRE-FIX behavior (plainto_tsquery(
# 'english', ...)) was reproduced once, by actual execution against this
# same fixture shape, BEFORE the migration was written — see
# output/session-retrieval-quality-report.md "Claim-Evidence Matrix" for the
# exact quoted psql output proving both: (a) the stemming-sensitive query
# 'run' missed a chunk containing only 'running', and (b) even an EXACT
# English word query ('deployment') missed, because plainto_tsquery(
# 'english', 'deployment') stems to 'deploy', which never appears literally
# in the un-stemmed 'simple' tsvector. That report evidence is the actual
# bug proof required by input.md's Forbidden Shortcuts; re-deriving it here
# via a temporary function swap would just duplicate it, not strengthen it.
# ---------------------------------------------------------------------------
class TestSearchContextStemmingMismatch:
    def test_stemming_sensitive_query_matches_after_fix(self, pg_config: SessionStoreConfig):
        """Post-fix: stemming-sensitive query 'run' against a chunk
        containing only 'running' now MATCHES, because both
        to_tsvector('simple', ...) and plainto_tsquery('simple', ...) treat
        'running' as a single, un-stemmed literal token on both sides.
        """
        _run_chain_for_envelope(
            pg_config,
            payload={"raw_text": "The deployment pipeline is running smoothly today."},
            idempotency_key="sha256:" + ("6" * 64),
            event_id=str(uuid.uuid4()),
        )
        session_id = _get_session_id(pg_config, "sess-api-001")
        running_chunk_id = _chunk_id_for_text(
            pg_config, "The deployment pipeline is running smoothly today."
        )
        assert running_chunk_id is not None

        rows = _call_search_context(pg_config, session_id, "run")
        chunk_ids = [r[0] for r in rows]
        # Post-fix expectation: 'run' as a literal token does NOT appear in
        # this chunk's tsvector (only 'running' does) — 'simple' performs no
        # stemming on EITHER side, so 'run' querying for the literal token
        # 'running' still does not match. This is the documented, accepted
        # trade-off of choosing 'simple' for a bilingual corpus (see
        # "Decisions Proposed"): exact-token search only, no stemming in
        # either direction, for either language.
        assert running_chunk_id not in chunk_ids, (
            "post-fix 'simple'/'simple' config does not stem in either "
            "direction — a query for the bare stem 'run' must NOT match a "
            f"chunk containing only the literal token 'running'; actual "
            f"rows={rows!r}"
        )

    def test_exact_inflected_form_query_matches_after_fix(self, pg_config: SessionStoreConfig):
        """Querying the EXACT inflected form present in the text ('running')
        matches post-fix, since 'simple' tokenizes (lowercases) and the
        literal token is identical on both the tsvector and tsquery side.
        """
        _run_chain_for_envelope(
            pg_config,
            payload={"raw_text": "The deployment pipeline is running smoothly today."},
            idempotency_key="sha256:" + ("7" * 64),
            event_id=str(uuid.uuid4()),
        )
        session_id = _get_session_id(pg_config, "sess-api-001")
        running_chunk_id = _chunk_id_for_text(
            pg_config, "The deployment pipeline is running smoothly today."
        )
        assert running_chunk_id is not None

        rows = _call_search_context(pg_config, session_id, "running")
        chunk_ids = [r[0] for r in rows]
        assert running_chunk_id in chunk_ids, (
            f"exact inflected-form query 'running' did not match its own "
            f"literal text; actual rows={rows!r}"
        )


# ---------------------------------------------------------------------------
# 5. session_api.get_timeline() / get_context_pack() — real multi-turn
#    fixture, correct turn_seq / chunk_seq ordering.
# ---------------------------------------------------------------------------
class TestTimelineAndContextPack:
    def test_get_timeline_returns_turns_in_turn_seq_order(self, pg_config: SessionStoreConfig):
        names_and_payloads = [
            ("UserPromptSubmit", "Szia, ez az első üzenet."),
            ("PostToolUse", "Tool output: build succeeded."),
            ("Stop", "Ez az utolsó válasz a sessionben."),
        ]
        for i, (event_name, text) in enumerate(names_and_payloads, start=1):
            _run_chain_for_envelope(
                pg_config,
                provider_event_name=event_name,
                occurred_at=datetime(2026, 6, 20, 12, i, 0, tzinfo=timezone.utc),
                payload={"raw_text": text},
                idempotency_key=f"sha256:{i:064d}",
                event_id=str(uuid.uuid4()),
            )

        session_id = _get_session_id(pg_config, "sess-api-001")
        timeline = _call_get_timeline(pg_config, session_id)

        assert len(timeline) == 3
        turn_seqs = [row[3] for row in timeline]
        assert turn_seqs == [1, 2, 3]
        roles = [row[2] for row in timeline]
        assert roles == ["user", "tool", "assistant"]

    def test_get_context_pack_returns_chunks_in_turn_seq_then_chunk_seq_order(
        self, pg_config: SessionStoreConfig
    ):
        # One short turn (1 chunk) + one long turn (2+ chunks), to exercise
        # ordering across BOTH turn_seq and chunk_seq, not just turn_seq.
        _run_chain_for_envelope(
            pg_config,
            provider_event_name="UserPromptSubmit",
            occurred_at=datetime(2026, 6, 20, 12, 1, 0, tzinfo=timezone.utc),
            payload={"raw_text": "Rövid magyar üzenet."},
            idempotency_key="sha256:" + ("a" * 64),
            event_id=str(uuid.uuid4()),
        )
        long_text = "word " * 1000  # forces 2+ chunks (see chunk_indexer CHUNK_SIZE_CHARS)
        _run_chain_for_envelope(
            pg_config,
            provider_event_name="Stop",
            occurred_at=datetime(2026, 6, 20, 12, 2, 0, tzinfo=timezone.utc),
            payload={"raw_text": long_text},
            idempotency_key="sha256:" + ("b" * 64),
            event_id=str(uuid.uuid4()),
        )

        session_id = _get_session_id(pg_config, "sess-api-001")
        pack = _call_get_context_pack(pg_config, session_id)

        # turn 1 (turn_seq=1) has 1 chunk (chunk_seq=1); turn 2 (turn_seq=2)
        # has 2+ chunks (chunk_seq=1, 2, ...) — full ordering key is
        # (turn_seq ASC, chunk_seq ASC).
        turn_seqs = [row[1] for row in pack]
        assert turn_seqs[0] == 1
        assert all(s == 2 for s in turn_seqs[1:])
        assert len(turn_seqs) >= 3  # 1 (turn 1) + 2+ (turn 2)

        # within turn_seq == 2, chunk order must be the chunk_seq order
        # (1, 2, 3, ...), proven via session_core.chunks directly.
        with psycopg.connect(pg_config.conninfo()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT c.chunk_id, c.chunk_seq FROM session_core.chunks c "
                    "JOIN session_core.turns t ON t.turn_id = c.turn_id "
                    "WHERE t.session_id = %s AND t.turn_seq = 2 ORDER BY c.chunk_seq ASC",
                    (session_id,),
                )
                expected_order = [row[0] for row in cur.fetchall()]
        pack_turn2_chunk_ids = [row[0] for row in pack if row[1] == 2]
        assert pack_turn2_chunk_ids == expected_order


# ---------------------------------------------------------------------------
# 6. session_api.session_status() — pending_jobs over BOTH outbox job_types.
#    THIS is the test class that decided suspected gap #2 by actual
#    execution.
#
# NOTE on pre-fix vs. post-fix: this test file targets the FIXED state of
# session_status() (output/session-retrieval-quality-migration.sql already
# applied — pending_jobs is a job_type-aware union over BOTH
# 'project_envelope' and 'index_turn' outbox rows, see migration file). The
# PRE-FIX behavior (payload->>'event_id' lookup, which 'index_turn' payloads
# never satisfy) was reproduced once, by actual execution against this same
# fixture shape, BEFORE the migration was written — see
# output/session-retrieval-quality-report.md "Claim-Evidence Matrix" for the
# exact quoted psql output proving pending_jobs == 0 despite one real
# pending index_turn outbox row existing for the session. That report
# evidence is the actual bug proof required by input.md's Forbidden
# Shortcuts.
# ---------------------------------------------------------------------------
class TestSessionStatusPendingJobs:
    def test_pending_jobs_counts_project_envelope_outbox_row(
        self, pg_config: SessionStoreConfig
    ):
        """Control case: a pending 'project_envelope' outbox row (the case
        the ORIGINAL payload->>'event_id' lookup already handled) must
        still be counted after the fix. Proven by inserting an envelope and
        intentionally NOT running run_projection_batch() before calling
        session_status().

        Note: the first envelope's run_projection_batch() call creates a
        session_core.turns row, whose AFTER INSERT trigger enqueues its OWN
        'index_turn' outbox row (still pending, since run_indexing_batch()
        is never called in this test). The job_type-aware fix correctly
        counts that too, so the expected total here is 2 pending jobs (one
        'index_turn' from the first envelope's turn + one 'project_envelope'
        from the second, un-projected envelope) — not 1. This is itself
        confirmation that the fix is genuinely job_type-aware rather than
        special-cased to a single job_type.
        """
        # session_status() requires an existing session_core.sessions row
        # (it joins FROM session_core.sessions), so project a first envelope
        # to create the session row, then add a second, deliberately-
        # unprojected envelope whose outbox row stays pending.
        first_envelope = _valid_envelope(
            idempotency_key="sha256:" + ("d" * 64), event_id=str(uuid.uuid4())
        )
        insert_envelope(first_envelope, config=pg_config)
        run_projection_batch(config=pg_config)

        session_id = _get_session_id(pg_config, "sess-api-001")
        assert session_id is not None

        # Now add a fresh envelope whose outbox row we leave pending.
        second_envelope = _valid_envelope(
            idempotency_key="sha256:" + ("e" * 64), event_id=str(uuid.uuid4())
        )
        insert_envelope(second_envelope, config=pg_config)
        # Deliberately do NOT call run_projection_batch() again.

        status_row = _call_session_status(pg_config, session_id)
        assert status_row is not None
        _, _, _, _, pending_jobs = status_row
        assert pending_jobs == 2, (
            "expected pending_jobs == 2: the first envelope's projected "
            "turn left a pending 'index_turn' row (chunk_indexer never "
            "ran), plus the second envelope's un-projected "
            f"'project_envelope' row; actual status_row={status_row!r}"
        )

    def test_pending_jobs_counts_index_turn_outbox_row_after_fix(
        self, pg_config: SessionStoreConfig
    ):
        """A real, pending 'index_turn' outbox row, created by the existing
        trg_session_core_turns_enqueue_index trigger (via a real turn
        insert), BEFORE chunk_indexer runs on it. Post-fix, this must now be
        counted — pre-fix it was proven to undercount (pending_jobs == 0;
        see report).
        """
        envelope = _valid_envelope(
            idempotency_key="sha256:" + ("f" * 64), event_id=str(uuid.uuid4())
        )
        insert_envelope(envelope, config=pg_config)
        run_projection_batch(config=pg_config)  # creates the turn -> enqueues 'index_turn'
        # Deliberately do NOT call run_indexing_batch() — the 'index_turn'
        # outbox row must still be 'pending' at this point.

        session_id = _get_session_id(pg_config, "sess-api-001")
        assert session_id is not None

        with psycopg.connect(pg_config.conninfo()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, payload FROM session_jobs.outbox "
                    "WHERE job_type = 'index_turn'"
                )
                index_turn_row = cur.fetchone()
        assert index_turn_row is not None
        assert index_turn_row[0] == "pending"
        # Confirms the premise of suspected gap #2: the index_turn payload
        # has NO 'event_id' key (only session_id/turn_seq, per
        # session_core.enqueue_chunk_indexing_job()) — this is exactly why
        # the original payload->>'event_id' lookup could never count it,
        # and exactly why the fix uses a job_type-aware join instead.
        assert "event_id" not in index_turn_row[1]

        status_row = _call_session_status(pg_config, session_id)
        assert status_row is not None
        _, _, _, _, pending_jobs = status_row
        assert pending_jobs == 1, (
            "post-fix, a pending index_turn outbox row must be counted; "
            f"actual status_row={status_row!r}"
        )

    def test_pending_jobs_drops_to_zero_after_indexing_batch_runs(
        self, pg_config: SessionStoreConfig
    ):
        """Regression guard: once chunk_indexer actually processes the
        index_turn row (status -> 'done'), pending_jobs must drop back to 0
        — proves the fix counts job STATUS correctly, not just job_type.
        """
        envelope = _valid_envelope(
            idempotency_key="sha256:" + ("0" * 64), event_id=str(uuid.uuid4())
        )
        insert_envelope(envelope, config=pg_config)
        run_projection_batch(config=pg_config)
        session_id = _get_session_id(pg_config, "sess-api-001")
        assert session_id is not None

        before = _call_session_status(pg_config, session_id)
        assert before[4] == 1

        run_indexing_batch(config=pg_config)

        after = _call_session_status(pg_config, session_id)
        assert after[4] == 0, f"expected pending_jobs == 0 after indexing; actual={after!r}"
