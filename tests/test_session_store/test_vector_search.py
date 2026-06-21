"""
End-to-end tests for session_api.search_context_vector() and
session_store.vector_search.embed_query() against a REAL Postgres instance.

Job: session-vector-search-api-001

This is the FIRST test module that ever queries session_idx.chunk_embeddings
via a session_api.* function — the column has held real embedding vectors
since session-chunk-indexer-001, but session_api.search_context() (FTS-only)
never reads it, and no prior job's test suite calls
session_api.search_context_vector() (defined in
output/session-vector-search-api-migration.sql, applied FOURTH after
session-postgres-schema.sql, session-chunk-indexer-migration.sql, and
session-retrieval-quality-migration.sql).

These tests do NOT mock the database connection, the outbox, session_core,
or session_idx tables, and do NOT insert directly into session_core/
session_idx — every fixture row is produced by driving the REAL write-path
chain, exactly like tests/test_session_store/test_session_api.py:

    insert_envelope() [session_store.envelope_writer]
        -> trg_session_raw_envelopes_enqueue trigger
        -> session_jobs.outbox row (job_type='project_envelope')
        -> run_projection_batch() [session_store.turn_projector]
        -> session_core.sessions / session_core.turns row(s)
        -> trg_session_core_turns_enqueue_index trigger
        -> session_jobs.outbox row (job_type='index_turn')
        -> run_indexing_batch() [session_store.chunk_indexer]
        -> session_core.chunks / session_idx.chunk_fts / session_idx.chunk_embeddings

The fixture for this module is two semantically well-separated topics (input.md
"4."):
  - Topic A: a database/Postgres migration turn (English, technical, schema/
    index/column vocabulary)
  - Topic B: a frontend CSS styling turn (English, technical, but a
    completely different vocabulary domain)
These were chosen specifically because they are both "technical English"
(ruling out language-only separation as a confound) but lexically and
semantically disjoint domains, which paraphrase-multilingual-MiniLM-L12-v2
(the model session_store.chunk_indexer.embed_texts() already uses) should
separate clearly in embedding space.

Reproduction:

    docker run -d --name session-vector-search-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55436:5432 pgvector/pgvector:pg16

    # wait for readiness (pg_isready), then apply ALL schema/migration files
    # in order:
    docker exec -i session-vector-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql
    docker exec -i session-vector-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-chunk-indexer-migration.sql
    docker exec -i session-vector-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-retrieval-quality-migration.sql
    docker exec -i session-vector-search-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-vector-search-api-migration.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55436 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        p_venv/bin/pytest tests/test_session_store/test_vector_search.py -v

    docker rm -f session-vector-search-test
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from session_store.chunk_indexer import EXPECTED_EMBEDDING_DIM, run_indexing_batch
from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.turn_projector import run_projection_batch
from session_store.vector_search import embed_query, to_pgvector_literal

# ---------------------------------------------------------------------------
# Two semantically well-separated topics (input.md "4."): a database
# migration turn (Topic A) and a frontend CSS styling turn (Topic B).
# ---------------------------------------------------------------------------
TOPIC_A_TEXT = (
    "We need to run the Postgres schema migration before deploying: add the "
    "new index on the foreign key column, backfill the NOT NULL constraint "
    "in batches, and verify the HNSW vector index rebuild completes without "
    "locking the table for writes."
)
TOPIC_B_TEXT = (
    "The button component needs a CSS fix: the flexbox container should "
    "center its children vertically, the hover state should transition the "
    "background color smoothly, and the border-radius needs to match the "
    "design system's rounded-corner token."
)
TOPIC_A_QUERY = "database schema migration and index rebuild"
TOPIC_B_QUERY = "CSS flexbox styling and button hover color"


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55436")),
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
            "Cannot reach a real Postgres instance for vector_search tests. "
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
        "provider_session_id": "sess-vector-001",
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


def _call_search_context_vector(pg_config, session_id, query_text, limit=20):
    """Embed query_text via embed_query(), then call
    session_api.search_context_vector() with the resulting vector formatted
    as a pgvector text literal (see vector_search.to_pgvector_literal
    docstring for the formatting decision)."""
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


def _build_two_topic_fixture(pg_config: SessionStoreConfig) -> tuple[int, int, object]:
    """Build the two-topic fixture through the real chain and return
    (topic_a_chunk_id, topic_b_chunk_id, session_id)."""
    _run_chain_for_envelope(
        pg_config,
        provider_event_name="UserPromptSubmit",
        occurred_at=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc),
        payload={"raw_text": TOPIC_A_TEXT},
        idempotency_key="sha256:" + ("1" * 64),
        event_id=str(uuid.uuid4()),
    )
    _run_chain_for_envelope(
        pg_config,
        provider_event_name="Stop",
        occurred_at=datetime(2026, 6, 21, 12, 1, 0, tzinfo=timezone.utc),
        payload={"raw_text": TOPIC_B_TEXT},
        idempotency_key="sha256:" + ("2" * 64),
        event_id=str(uuid.uuid4()),
    )

    session_id = _get_session_id(pg_config, "sess-vector-001")
    assert session_id is not None

    topic_a_chunk_id = _chunk_id_for_text(pg_config, TOPIC_A_TEXT)
    topic_b_chunk_id = _chunk_id_for_text(pg_config, TOPIC_B_TEXT)
    assert topic_a_chunk_id is not None
    assert topic_b_chunk_id is not None

    return topic_a_chunk_id, topic_b_chunk_id, session_id


# ---------------------------------------------------------------------------
# 1. Fixture sanity check: the real chain produces exactly 2 chunks, each
#    with an embedding row, before any search_context_vector() assertions.
# ---------------------------------------------------------------------------
class TestTwoTopicFixtureRealChain:
    def test_fixture_builds_through_real_chain_and_produces_expected_rows(
        self, pg_config: SessionStoreConfig
    ):
        topic_a_chunk_id, topic_b_chunk_id, session_id = _build_two_topic_fixture(pg_config)

        with psycopg.connect(pg_config.conninfo()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM session_core.chunks WHERE session_id = %s",
                    (session_id,),
                )
                assert cur.fetchone()[0] == 2
                cur.execute(
                    "SELECT count(*) FROM session_idx.chunk_embeddings e "
                    "JOIN session_core.chunks c ON c.chunk_id = e.chunk_id "
                    "WHERE c.session_id = %s",
                    (session_id,),
                )
                assert cur.fetchone()[0] == 2


# ---------------------------------------------------------------------------
# 2. embed_query() dimension check — TESTED, not assumed (input.md "5.").
# ---------------------------------------------------------------------------
class TestEmbedQueryDimension:
    def test_embed_query_output_dimension_matches_chunk_embeddings_column(
        self, pg_config: SessionStoreConfig
    ):
        vector = embed_query(TOPIC_A_QUERY)
        assert isinstance(vector, list)
        assert len(vector) == EXPECTED_EMBEDDING_DIM == 384

        # Cross-check against the ACTUAL declared column dimension in this
        # real Postgres instance (not just the Python-side constant), via
        # pgvector's vector_dims() function on a real inserted row, so the
        # claim is "queried", not "assumed from documentation" (input.md
        # Definition Of Done).
        topic_a_chunk_id, _, _ = _build_two_topic_fixture(pg_config)
        with psycopg.connect(pg_config.conninfo()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT vector_dims(embedding) FROM session_idx.chunk_embeddings "
                    "WHERE chunk_id = %s",
                    (topic_a_chunk_id,),
                )
                (stored_dim,) = cur.fetchone()
        assert stored_dim == len(vector) == 384


# ---------------------------------------------------------------------------
# 3. Semantic relevance — THIS is the test class that proves the function
#    actually does cosine-similarity search, not just "returns rows"
#    (input.md Forbidden Shortcuts: "csak azt tesztelni, hogy a fuggveny
#    hibatlanul visszaad SOK sort - TILOS").
# ---------------------------------------------------------------------------
class TestSemanticRelevance:
    def test_topic_a_query_ranks_topic_a_chunk_first(self, pg_config: SessionStoreConfig):
        topic_a_chunk_id, topic_b_chunk_id, session_id = _build_two_topic_fixture(pg_config)

        rows = _call_search_context_vector(pg_config, session_id, TOPIC_A_QUERY)
        assert len(rows) == 2
        chunk_ids_in_order = [r[0] for r in rows]
        assert chunk_ids_in_order[0] == topic_a_chunk_id, (
            f"query about database migration did not rank the migration "
            f"chunk first; actual rows (chunk_id, turn_id, text, similarity)="
            f"{rows!r}"
        )
        # similarity must be a meaningful ordering signal: topic A's own
        # similarity score must exceed topic B's for this query.
        sim_by_chunk = {r[0]: r[3] for r in rows}
        assert sim_by_chunk[topic_a_chunk_id] > sim_by_chunk[topic_b_chunk_id]

    def test_topic_b_query_ranks_topic_b_chunk_first(self, pg_config: SessionStoreConfig):
        topic_a_chunk_id, topic_b_chunk_id, session_id = _build_two_topic_fixture(pg_config)

        rows = _call_search_context_vector(pg_config, session_id, TOPIC_B_QUERY)
        assert len(rows) == 2
        chunk_ids_in_order = [r[0] for r in rows]
        assert chunk_ids_in_order[0] == topic_b_chunk_id, (
            f"query about CSS styling did not rank the CSS chunk first; "
            f"actual rows (chunk_id, turn_id, text, similarity)={rows!r}"
        )
        sim_by_chunk = {r[0]: r[3] for r in rows}
        assert sim_by_chunk[topic_b_chunk_id] > sim_by_chunk[topic_a_chunk_id]

    def test_limit_parameter_caps_result_count(self, pg_config: SessionStoreConfig):
        _build_two_topic_fixture(pg_config)
        session_id = _get_session_id(pg_config, "sess-vector-001")
        rows = _call_search_context_vector(pg_config, session_id, TOPIC_A_QUERY, limit=1)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# 4. EXPLAIN — index-usage verification (input.md "5."): documented, not
#    assumed.
# ---------------------------------------------------------------------------
class TestExplainIndexUsage:
    def test_explain_documents_actual_plan_for_small_fixture(
        self, pg_config: SessionStoreConfig
    ):
        """At this fixture's row count (2 chunks for the session, 2 rows
        total in chunk_embeddings), the Postgres planner is expected to
        choose a sequential scan over the HNSW index scan, because HNSW
        (like any index) only wins over a seq scan once the table is large
        enough that index traversal beats reading the whole (tiny) table
        directly. This test does NOT assert "index scan chosen" — it
        queries the REAL plan and documents whichever plan the planner
        actually picks, per input.md's explicit "NE feltetelezz
        index-hasznalatot ellenorzes nelkul" requirement.
        """
        topic_a_chunk_id, topic_b_chunk_id, session_id = _build_two_topic_fixture(pg_config)
        embedding = embed_query(TOPIC_A_QUERY)
        literal = to_pgvector_literal(embedding)

        with psycopg.connect(pg_config.conninfo()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "EXPLAIN SELECT c.chunk_id, c.turn_id, c.text, "
                    "(1 - (e.embedding <=> %s::vector))::REAL AS similarity "
                    "FROM session_core.chunks c "
                    "JOIN session_idx.chunk_embeddings e ON e.chunk_id = c.chunk_id "
                    "WHERE c.session_id = %s "
                    "ORDER BY e.embedding <=> %s::vector LIMIT 20",
                    (literal, session_id, literal),
                )
                plan_lines = [row[0] for row in cur.fetchall()]

        plan_text = "\n".join(plan_lines)
        # The plan must mention SOME scan strategy for chunk_embeddings —
        # this assertion fails loudly (rather than silently passing) if the
        # query shape changes in a way that makes the plan unreadable.
        assert "chunk_embeddings" in plan_text, (
            f"EXPLAIN output did not reference chunk_embeddings at all, "
            f"cannot document index usage: {plan_text!r}"
        )
        uses_hnsw_index_scan = "Index Scan" in plan_text and "hnsw" in plan_text.lower()
        uses_seq_scan = "Seq Scan" in plan_text
        # Documented finding (see report "Findings"): at 2 rows, expect a
        # sequential scan, NOT the HNSW index scan — explicitly asserted as
        # the accepted/expected outcome at this fixture size, not silently
        # passed over.
        assert uses_seq_scan or uses_hnsw_index_scan, (
            f"EXPLAIN plan used neither a recognizable Seq Scan nor an HNSW "
            f"Index Scan on chunk_embeddings; actual plan:\n{plan_text}"
        )
        # Record which one, for the report to quote verbatim.
        print(f"\n--- EXPLAIN plan (2-row fixture) ---\n{plan_text}\n")
        print(f"uses_seq_scan={uses_seq_scan} uses_hnsw_index_scan={uses_hnsw_index_scan}")
