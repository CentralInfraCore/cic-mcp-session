"""End-to-end tests for session_api.search_context_hybrid() against a REAL
Postgres instance.

Job: session-hybrid-search-api-001

This is the FIRST test module that calls a session_api.* function combining
the FTS signal (session_api.search_context()'s plainto_tsquery('simple', ...)
/ ts_rank() expression) and the vector signal
(session_api.search_context_vector()'s cosine-distance `<=>` expression) via
Reciprocal Rank Fusion (RRF) — defined in
output/session-hybrid-search-api-migration.sql, applied FIFTH after
session-postgres-schema.sql, session-chunk-indexer-migration.sql,
session-retrieval-quality-migration.sql, and
session-vector-search-api-migration.sql.

These tests do NOT mock the database connection, the outbox, session_core,
or session_idx tables, and do NOT insert directly into session_core/
session_idx — every fixture row is produced by driving the REAL write-path
chain, exactly like tests/test_session_store/test_session_api.py and
tests/test_session_store/test_vector_search.py:

    insert_envelope() [session_store.envelope_writer]
        -> trg_session_raw_envelopes_enqueue trigger
        -> session_jobs.outbox row (job_type='project_envelope')
        -> run_projection_batch() [session_store.turn_projector]
        -> session_core.sessions / session_core.turns row(s)
        -> trg_session_core_turns_enqueue_index trigger
        -> session_jobs.outbox row (job_type='index_turn')
        -> run_indexing_batch() [session_store.chunk_indexer]
        -> session_core.chunks / session_idx.chunk_fts / session_idx.chunk_embeddings

The fixture for this module is a deliberately constructed three-chunk set
(input.md "4."), chosen so that search_context() (FTS-only) and
search_context_vector() (vector-only) ACTUALLY DISAGREE on the same query
"database lookups":

  - Chunk A (lexically relevant, semantically off-topic): contains the
    literal tokens "database" and "lookups" (split apart, in an unrelated
    context — a grandmother's recipe shoebox / phone-call restaurant
    lookups), so it is the ONLY chunk that satisfies
    plainto_tsquery('simple', 'database lookups') via AND-matching on both
    tokens. It is semantically about cooking/family anecdotes, not query
    performance.
  - Chunk B (semantically relevant, zero lexical overlap): describes
    speeding up database row lookups via a secondary index structure, using
    NEITHER the literal token "database" NOR "lookups" anywhere in the
    text, so it can never satisfy the FTS query at all (proven below — it
    is simply ABSENT from search_context()'s result set). It scores the
    HIGHEST cosine similarity of the three chunks against the query
    embedding (proven empirically while designing this fixture: 0.476 vs.
    Chunk A's 0.305 and Chunk C's 0.003 — see report "Findings" for the
    exact pgvector-computed values).
  - Chunk C (irrelevant control): a sentence about a cat sleeping, with no
    lexical or semantic relationship to the query.

This satisfies input.md's Forbidden Shortcut constraint ("olyan fixture,
ahol a hibrid eredmeny ugyanaz lenne, mint barmelyik egyedi modositase" —
the fixture must be built so the two single methods ACTUALLY produce
different rankings, not coincidentally identical ones): search_context()
returns ONLY chunk A (chunk B never appears at all);
search_context_vector() ranks chunk B above chunk A above chunk C. These are
materially different result sets/orderings, not the same ranking computed
twice.

Reproduction:

    docker run -d --name session-hybrid-search-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55437:5432 pgvector/pgvector:pg16

    # wait for readiness (pg_isready), then apply ALL schema/migration files
    # in order:
    docker exec -i session-hybrid-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql
    docker exec -i session-hybrid-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-chunk-indexer-migration.sql
    docker exec -i session-hybrid-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-retrieval-quality-migration.sql
    docker exec -i session-hybrid-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-vector-search-api-migration.sql
    docker exec -i session-hybrid-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-hybrid-search-api-migration.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55437 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        p_venv/bin/pytest tests/test_session_store/test_hybrid_search.py -v

    docker rm -f session-hybrid-search-test
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from session_store.chunk_indexer import run_indexing_batch
from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.turn_projector import run_projection_batch
from session_store.vector_search import embed_query, to_pgvector_literal

# ---------------------------------------------------------------------------
# Fixture text (input.md "4."): see module docstring for the full design
# rationale of why these three specific texts were chosen.
# ---------------------------------------------------------------------------
QUERY_TEXT = "database lookups"

CHUNK_A_TEXT = (
    "Grandma's old recipe database is just a shoebox of index cards, and "
    "she always complains that her grandkids' phone lookups for restaurant "
    "reviews take forever compared to flipping through her cards."
)
CHUNK_B_TEXT = (
    "Adding a secondary structure on the email column let the query planner "
    "skip straight to matching rows instead of scanning the entire customers "
    "table, making retrieval far faster."
)
CHUNK_C_TEXT = (
    "My cat enjoys sleeping on the windowsill all afternoon while birds "
    "chirp outside."
)


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55437")),
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
            "Cannot reach a real Postgres instance for hybrid_search tests. "
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
        "provider_session_id": "sess-hybrid-001",
        "provider_event_name": "Stop",
        "source": {"kind": "hook", "collector": "log-event.py"},
        "occurred_at": datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        "ingested_at": datetime(2026, 6, 21, 12, 0, 1, tzinfo=timezone.utc),
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


def _call_search_context_vector(pg_config, session_id, query_text, limit=20):
    embedding = embed_query(query_text)
    literal = to_pgvector_literal(embedding)
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, turn_id, text, similarity "
                "FROM session_api.search_context_vector(%s, %s::vector, %s)",
                (session_id, literal, limit),
            )
            return cur.fetchall()


def _call_search_context_hybrid(pg_config, session_id, query_text, limit=20):
    embedding = embed_query(query_text)
    literal = to_pgvector_literal(embedding)
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, turn_id, text, fused_score "
                "FROM session_api.search_context_hybrid(%s, %s, %s::vector, %s)",
                (session_id, query_text, literal, limit),
            )
            return cur.fetchall()


def _build_three_chunk_fixture(pg_config: SessionStoreConfig) -> tuple[int, int, int, object]:
    """Build the three-chunk lexical/semantic/irrelevant fixture through the
    real chain and return (chunk_a_id, chunk_b_id, chunk_c_id, session_id)."""
    _run_chain_for_envelope(
        pg_config,
        provider_event_name="UserPromptSubmit",
        occurred_at=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        payload={"raw_text": CHUNK_A_TEXT},
        idempotency_key="sha256:" + ("1" * 64),
        event_id=str(uuid.uuid4()),
    )
    _run_chain_for_envelope(
        pg_config,
        provider_event_name="Stop",
        occurred_at=datetime(2026, 6, 21, 12, 1, 0, tzinfo=timezone.utc),
        payload={"raw_text": CHUNK_B_TEXT},
        idempotency_key="sha256:" + ("2" * 64),
        event_id=str(uuid.uuid4()),
    )
    _run_chain_for_envelope(
        pg_config,
        provider_event_name="PostToolUse",
        occurred_at=datetime(2026, 6, 21, 12, 2, 0, tzinfo=timezone.utc),
        payload={"raw_text": CHUNK_C_TEXT},
        idempotency_key="sha256:" + ("3" * 64),
        event_id=str(uuid.uuid4()),
    )

    session_id = _get_session_id(pg_config, "sess-hybrid-001")
    assert session_id is not None

    chunk_a_id = _chunk_id_for_text(pg_config, CHUNK_A_TEXT)
    chunk_b_id = _chunk_id_for_text(pg_config, CHUNK_B_TEXT)
    chunk_c_id = _chunk_id_for_text(pg_config, CHUNK_C_TEXT)
    assert chunk_a_id is not None
    assert chunk_b_id is not None
    assert chunk_c_id is not None

    return chunk_a_id, chunk_b_id, chunk_c_id, session_id


# ---------------------------------------------------------------------------
# 1. Fixture sanity check.
# ---------------------------------------------------------------------------
class TestThreeChunkFixtureRealChain:
    def test_fixture_builds_through_real_chain_and_produces_expected_rows(
        self, pg_config: SessionStoreConfig
    ):
        chunk_a_id, chunk_b_id, chunk_c_id, session_id = _build_three_chunk_fixture(pg_config)

        with psycopg.connect(pg_config.conninfo()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM session_core.chunks WHERE session_id = %s",
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
# 2. search_context() alone — proves the FTS-only blind spot: it finds
#    Chunk A (lexical match) but NEVER returns Chunk B at all (no lexical
#    overlap whatsoever with the query).
# ---------------------------------------------------------------------------
class TestSearchContextLexicalOnly:
    def test_finds_chunk_a_but_not_chunk_b(self, pg_config: SessionStoreConfig):
        chunk_a_id, chunk_b_id, chunk_c_id, session_id = _build_three_chunk_fixture(pg_config)

        rows = _call_search_context(pg_config, session_id, QUERY_TEXT)
        chunk_ids = [r[0] for r in rows]

        assert chunk_a_id in chunk_ids, (
            f"FTS query {QUERY_TEXT!r} should match chunk A (literal lexical "
            f"overlap); actual rows={rows!r}"
        )
        assert chunk_b_id not in chunk_ids, (
            f"FTS query {QUERY_TEXT!r} should NOT match chunk B (zero "
            f"lexical overlap by construction); actual rows={rows!r}"
        )
        assert chunk_c_id not in chunk_ids


# ---------------------------------------------------------------------------
# 3. search_context_vector() alone — proves the vector-only side: it ranks
#    Chunk B (semantically relevant) above Chunk A (lexical-only, off-topic)
#    above Chunk C (irrelevant).
# ---------------------------------------------------------------------------
class TestSearchContextVectorSemanticOnly:
    def test_ranks_chunk_b_above_chunk_a_above_chunk_c(self, pg_config: SessionStoreConfig):
        chunk_a_id, chunk_b_id, chunk_c_id, session_id = _build_three_chunk_fixture(pg_config)

        rows = _call_search_context_vector(pg_config, session_id, QUERY_TEXT)
        assert len(rows) == 3
        sim_by_chunk = {r[0]: r[3] for r in rows}

        assert sim_by_chunk[chunk_b_id] > sim_by_chunk[chunk_a_id] > sim_by_chunk[chunk_c_id], (
            f"vector-only ranking should be B > A > C by cosine similarity; "
            f"actual rows={rows!r}"
        )


# ---------------------------------------------------------------------------
# 4. search_context_hybrid() — THIS is the test class that proves the
#    fusion does real work: BOTH chunk A (lexical-only) and chunk B
#    (semantic-only) rank above chunk C (irrelevant control), even though
#    neither single method alone surfaces both A and B as relevant.
# ---------------------------------------------------------------------------
class TestSearchContextHybridFusion:
    def test_ranks_both_chunk_a_and_chunk_b_above_chunk_c(self, pg_config: SessionStoreConfig):
        chunk_a_id, chunk_b_id, chunk_c_id, session_id = _build_three_chunk_fixture(pg_config)

        rows = _call_search_context_hybrid(pg_config, session_id, QUERY_TEXT)
        assert len(rows) == 3
        fused_by_chunk = {r[0]: r[3] for r in rows}

        assert fused_by_chunk[chunk_a_id] > fused_by_chunk[chunk_c_id], (
            f"hybrid fusion should rank chunk A (lexical match) above chunk "
            f"C (irrelevant control); actual rows={rows!r}"
        )
        assert fused_by_chunk[chunk_b_id] > fused_by_chunk[chunk_c_id], (
            f"hybrid fusion should rank chunk B (semantic match) above "
            f"chunk C (irrelevant control), even though chunk B is invisible "
            f"to the FTS side alone; actual rows={rows!r}"
        )

    def test_chunk_b_is_present_in_hybrid_despite_zero_fts_overlap(
        self, pg_config: SessionStoreConfig
    ):
        """Regression guard for the FULL OUTER JOIN: chunk B has NO row on
        the FTS side (see TestSearchContextLexicalOnly above) but must still
        appear in the hybrid result, contributed entirely by the vector
        side's RRF term."""
        chunk_a_id, chunk_b_id, chunk_c_id, session_id = _build_three_chunk_fixture(pg_config)

        rows = _call_search_context_hybrid(pg_config, session_id, QUERY_TEXT)
        chunk_ids = [r[0] for r in rows]
        assert chunk_b_id in chunk_ids, (
            f"chunk B must appear in the hybrid result via the vector-side "
            f"RRF term alone, despite zero FTS overlap; actual rows={rows!r}"
        )

    def test_fused_score_matches_rrf_formula_for_each_chunk(
        self, pg_config: SessionStoreConfig
    ):
        """Proves fused_score is actually computed via RRF (1/(k+rank) per
        side, k=60), not just "some score that happens to order correctly" —
        recomputes each side's rank independently and checks the exact
        fused_score value."""
        chunk_a_id, chunk_b_id, chunk_c_id, session_id = _build_three_chunk_fixture(pg_config)

        fts_rows = _call_search_context(pg_config, session_id, QUERY_TEXT)
        fts_rank_by_chunk = {row[0]: i + 1 for i, row in enumerate(fts_rows)}

        vector_rows = _call_search_context_vector(pg_config, session_id, QUERY_TEXT)
        vector_rank_by_chunk = {row[0]: i + 1 for i, row in enumerate(vector_rows)}

        hybrid_rows = _call_search_context_hybrid(pg_config, session_id, QUERY_TEXT)
        fused_by_chunk = {row[0]: row[3] for row in hybrid_rows}

        k = 60
        for chunk_id in (chunk_a_id, chunk_b_id, chunk_c_id):
            expected = 0.0
            if chunk_id in fts_rank_by_chunk:
                expected += 1.0 / (k + fts_rank_by_chunk[chunk_id])
            if chunk_id in vector_rank_by_chunk:
                expected += 1.0 / (k + vector_rank_by_chunk[chunk_id])
            assert fused_by_chunk[chunk_id] == pytest.approx(expected, rel=1e-9), (
                f"chunk_id={chunk_id} expected RRF fused_score={expected!r}, "
                f"actual={fused_by_chunk[chunk_id]!r}"
            )

    def test_limit_parameter_caps_result_count(self, pg_config: SessionStoreConfig):
        _build_three_chunk_fixture(pg_config)
        session_id = _get_session_id(pg_config, "sess-hybrid-001")
        rows = _call_search_context_hybrid(pg_config, session_id, QUERY_TEXT, limit=1)
        assert len(rows) == 1
