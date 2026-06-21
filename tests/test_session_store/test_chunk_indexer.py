"""
End-to-end tests for session_store.chunk_indexer against a REAL Postgres
instance.

Job: session-chunk-indexer-001

These tests do NOT mock the database connection, the outbox, session_core,
or session_idx tables, and do NOT mock the embedding model. They require a
live Postgres reachable via the SESSION_STORE_PG_* env vars (see
session_store.envelope_writer.SessionStoreConfig.from_env), with BOTH
output/session-postgres-schema.sql AND
output/session-chunk-indexer-migration.sql already applied, in that order.

The full chain under test, end to end:

    insert_envelope() [session_store.envelope_writer, EXISTING write-path]
        -> trg_session_raw_envelopes_enqueue trigger
        -> session_jobs.outbox row (job_type='project_envelope')
        -> run_projection_batch() [session_store.turn_projector, EXISTING worker]
        -> session_core.turns row (inserted)
        -> trg_session_core_turns_enqueue_index trigger (THIS job's migration)
        -> session_jobs.outbox row (job_type='index_turn')
        -> run_indexing_batch() [session_store.chunk_indexer, THIS job]
        -> session_core.chunks row(s)
        -> session_idx.chunk_fts row(s)
        -> session_idx.chunk_embeddings row(s)
        -> session_jobs.outbox row marked 'done'

Reproduction (see also output/session-chunk-indexer-report.md):

    docker run -d --name session-chunk-indexer-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \\
        -p 55434:5432 pgvector/pgvector:pg16

    # wait for readiness (pg_isready), then apply BOTH schema files in order:
    docker exec -i session-chunk-indexer-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-postgres-schema.sql
    docker exec -i session-chunk-indexer-test \\
        psql -U postgres -d testdb -v ON_ERROR_STOP=1 \\
        < output/session-chunk-indexer-migration.sql

    SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55434 \\
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \\
    SESSION_STORE_PG_PASSWORD=test \\
        pytest tests/test_session_store/test_chunk_indexer.py -v

    docker rm -f session-chunk-indexer-test
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from session_store.chunk_indexer import (
    CHUNK_OVERLAP_CHARS,
    CHUNK_SIZE_CHARS,
    EMBEDDING_MODEL,
    EXPECTED_EMBEDDING_DIM,
    extract_source_refs,
    extract_text,
    run_indexing_batch,
    split_into_chunks,
)
from session_store.envelope_writer import SessionStoreConfig, insert_envelope
from session_store.turn_projector import run_projection_batch


def _pg_config() -> SessionStoreConfig:
    return SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55434")),
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
            "Cannot reach a real Postgres instance for chunk_indexer tests. "
            "Start the test container and apply BOTH schema files first "
            "(see module docstring for the exact commands). Original error: "
            f"{exc}"
        )
    return cfg


@pytest.fixture(autouse=True)
def _clean_tables(pg_config: SessionStoreConfig):
    """Truncate all tables touched by this test module before each test."""
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE session_idx.chunk_embeddings, session_idx.chunk_fts, "
                "session_core.source_refs, session_core.chunks, session_jobs.outbox, "
                "session_core.turns, session_core.sessions, session_raw.envelopes CASCADE"
            )
        conn.commit()
    yield


def _valid_envelope(**overrides) -> dict:
    base = {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": "claude-code",
        "provider_session_id": "sess-chunk-001",
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


def _make_turn(pg_config: SessionStoreConfig, **envelope_overrides) -> int:
    """Insert an envelope and run the turn_projector to produce a real
    session_core.turns row, returning its turn_id.

    Per input.md "5. Tesztek": "felhasználva a meglévő insert_envelope() +
    turn_projector.run_projection_batch() láncot, hogy valódi turn keletkezzen".
    """
    envelope = _valid_envelope(**envelope_overrides)
    insert_envelope(envelope, config=pg_config)
    run_projection_batch(config=pg_config)

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT turn_id FROM session_core.turns ORDER BY turn_id DESC LIMIT 1"
            )
            return cur.fetchone()[0]


def _index_turn_outbox_job_ids(pg_config: SessionStoreConfig) -> list[int]:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id FROM session_jobs.outbox "
                "WHERE job_type = 'index_turn' ORDER BY job_id ASC"
            )
            return [row[0] for row in cur.fetchall()]


def _outbox_row(pg_config: SessionStoreConfig, job_id: int) -> tuple:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, attempts, max_attempts, last_error "
                "FROM session_jobs.outbox WHERE job_id = %s",
                (job_id,),
            )
            return cur.fetchone()


def _chunks_for_turn(pg_config: SessionStoreConfig, turn_id: int) -> list[tuple]:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, chunk_seq, text, token_count FROM session_core.chunks "
                "WHERE turn_id = %s ORDER BY chunk_seq ASC",
                (turn_id,),
            )
            return cur.fetchall()


def _fts_row(pg_config: SessionStoreConfig, chunk_id: int) -> tuple | None:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, tsv FROM session_idx.chunk_fts WHERE chunk_id = %s",
                (chunk_id,),
            )
            return cur.fetchone()


def _embedding_row(pg_config: SessionStoreConfig, chunk_id: int) -> tuple | None:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, embedding_model, vector_dims(embedding) "
                "FROM session_idx.chunk_embeddings WHERE chunk_id = %s",
                (chunk_id,),
            )
            return cur.fetchone()


# ---------------------------------------------------------------------------
# 1. Pure unit coverage for the deterministic chunking helpers (no DB
#    needed, but kept in this module since it documents the contract the
#    e2e tests below rely on).
# ---------------------------------------------------------------------------
def test_extract_text_uses_known_keys_in_documented_order():
    assert extract_text({"raw_text": "abc"}) == "abc"
    assert extract_text({"text": "def", "content": "ignored"}) == "def"
    assert extract_text({"content": "ghi"}) == "ghi"
    assert extract_text({"message": "jkl"}) == "jkl"


def test_extract_text_falls_back_to_sorted_json_for_unknown_shape():
    result = extract_text({"foo": 1, "bar": 2})
    assert result == '{"bar": 2, "foo": 1}'
    # deterministic: same input -> same output every time
    assert extract_text({"foo": 1, "bar": 2}) == result


def test_split_into_chunks_is_deterministic_and_handles_short_text():
    text = "short content"
    assert split_into_chunks(text) == [text]
    assert split_into_chunks(text) == split_into_chunks(text)
    assert split_into_chunks("") == []


def test_split_into_chunks_produces_multiple_overlapping_windows():
    text = "x" * (CHUNK_SIZE_CHARS * 2 + 100)
    chunks = split_into_chunks(text)
    assert len(chunks) >= 2
    assert all(len(c) <= CHUNK_SIZE_CHARS for c in chunks)
    # reassembled length accounts for overlap between consecutive windows
    step = CHUNK_SIZE_CHARS - CHUNK_OVERLAP_CHARS
    assert len(chunks) == -(-(len(text) - CHUNK_SIZE_CHARS) // step) + 1


# ---------------------------------------------------------------------------
# 2. Full end-to-end chain: turn -> trigger -> index_turn outbox row ->
#    chunk_indexer run -> chunks/chunk_fts/chunk_embeddings rows -> done.
# ---------------------------------------------------------------------------
def test_full_chain_turn_to_chunks_fts_embeddings_and_outbox_done(
    pg_config: SessionStoreConfig,
):
    turn_id = _make_turn(pg_config, payload={"raw_text": "hello chunked world"})

    job_ids = _index_turn_outbox_job_ids(pg_config)
    assert len(job_ids) == 1
    job_id = job_ids[0]
    status_before, attempts_before, _, _ = _outbox_row(pg_config, job_id)
    assert status_before == "pending"
    assert attempts_before == 0

    results = run_indexing_batch(config=pg_config)

    assert len(results) == 1
    assert results[0].job_id == job_id
    assert results[0].outcome == "done"
    assert results[0].error is None
    assert results[0].chunk_count == 1

    status_after, _, _, last_error_after = _outbox_row(pg_config, job_id)
    assert status_after == "done"
    assert last_error_after is None

    chunks = _chunks_for_turn(pg_config, turn_id)
    assert len(chunks) == 1
    chunk_id, chunk_seq, text, token_count = chunks[0]
    assert chunk_seq == 1
    assert text == "hello chunked world"
    assert token_count == 3  # whitespace-split estimate

    fts_row = _fts_row(pg_config, chunk_id)
    assert fts_row is not None
    assert fts_row[0] == chunk_id

    embedding_row = _embedding_row(pg_config, chunk_id)
    assert embedding_row is not None
    assert embedding_row[1] == EMBEDDING_MODEL
    assert embedding_row[2] == EXPECTED_EMBEDDING_DIM


# ---------------------------------------------------------------------------
# 3. Error handling: outbox row referencing a non-existent turn_id must end
#    up failed/dead_letter, never raise unhandled, never stay pending.
# ---------------------------------------------------------------------------
def test_dangling_turn_id_marks_outbox_failed_not_crash_not_stuck_pending(
    pg_config: SessionStoreConfig,
):
    nonexistent_turn_id = 999_999_999
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_jobs.outbox
                    (job_type, source_table, source_id, payload)
                VALUES ('index_turn', 'session_core.turns', %s, '{}'::jsonb)
                RETURNING job_id
                """,
                (nonexistent_turn_id,),
            )
            job_id = cur.fetchone()[0]
        conn.commit()

    # Must not raise — run_indexing_batch absorbs per-row errors.
    results = run_indexing_batch(config=pg_config)

    assert len(results) == 1
    assert results[0].job_id == job_id
    assert results[0].outcome in ("failed", "dead_letter")
    assert results[0].error is not None
    assert "999999999" in results[0].error or str(nonexistent_turn_id) in results[0].error

    status, attempts, _, last_error = _outbox_row(pg_config, job_id)
    assert status in ("failed", "dead_letter")
    assert status != "pending"
    assert attempts == 1
    assert last_error is not None

    # No chunk rows were created for the dangling reference.
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_core.chunks")
            assert cur.fetchone()[0] == 0


def test_dangling_turn_id_reaches_dead_letter_after_max_attempts(
    pg_config: SessionStoreConfig,
):
    nonexistent_turn_id = 888_888_888
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_jobs.outbox
                    (job_type, source_table, source_id, payload, max_attempts)
                VALUES ('index_turn', 'session_core.turns', %s, '{}'::jsonb, 2)
                RETURNING job_id
                """,
                (nonexistent_turn_id,),
            )
            job_id = cur.fetchone()[0]
        conn.commit()

    results_1 = run_indexing_batch(config=pg_config)
    assert results_1[0].outcome == "failed"
    status_1, attempts_1, _, _ = _outbox_row(pg_config, job_id)
    assert status_1 == "failed"
    assert attempts_1 == 1

    results_2 = run_indexing_batch(config=pg_config)
    assert results_2[0].outcome == "dead_letter"
    status_2, attempts_2, _, _ = _outbox_row(pg_config, job_id)
    assert status_2 == "dead_letter"
    assert attempts_2 == 2

    results_3 = run_indexing_batch(config=pg_config)
    assert results_3 == []


# ---------------------------------------------------------------------------
# 4. Long content -> 2+ chunks, correct chunk_seq sequence.
# ---------------------------------------------------------------------------
def test_long_content_produces_multiple_chunks_with_correct_seq(
    pg_config: SessionStoreConfig,
):
    long_text = "word " * 1000  # well over CHUNK_SIZE_CHARS
    turn_id = _make_turn(pg_config, payload={"raw_text": long_text})

    results = run_indexing_batch(config=pg_config)
    assert len(results) == 1
    assert results[0].outcome == "done"
    assert results[0].chunk_count >= 2

    chunks = _chunks_for_turn(pg_config, turn_id)
    assert len(chunks) == results[0].chunk_count
    seqs = [c[1] for c in chunks]
    assert seqs == list(range(1, len(chunks) + 1))  # strictly 1, 2, 3, ...

    # every chunk has a matching fts + embedding row
    for chunk_id, _, _, _ in chunks:
        assert _fts_row(pg_config, chunk_id) is not None
        assert _embedding_row(pg_config, chunk_id) is not None


# ---------------------------------------------------------------------------
# 5. Embedding dimension check via vector_dims(), explicit assert against
#    the declared column dimension (input.md "5." last bullet).
# ---------------------------------------------------------------------------
def test_embedding_dimension_matches_declared_column_dimension(
    pg_config: SessionStoreConfig,
):
    turn_id = _make_turn(pg_config, payload={"raw_text": "dimension check text"})
    run_indexing_batch(config=pg_config)

    chunks = _chunks_for_turn(pg_config, turn_id)
    assert len(chunks) == 1
    chunk_id = chunks[0][0]

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            # Declared column dimension, read from the catalog via pgvector's
            # own typmod-derived dimension reporting (atttypmod), independent
            # of vector_dims() on the row itself.
            cur.execute(
                """
                SELECT atttypmod
                FROM pg_attribute
                WHERE attrelid = 'session_idx.chunk_embeddings'::regclass
                  AND attname = 'embedding'
                """
            )
            declared_dim = cur.fetchone()[0]

            cur.execute(
                "SELECT vector_dims(embedding) FROM session_idx.chunk_embeddings "
                "WHERE chunk_id = %s",
                (chunk_id,),
            )
            actual_dim = cur.fetchone()[0]

    assert actual_dim == EXPECTED_EMBEDDING_DIM
    assert actual_dim == declared_dim


# ---------------------------------------------------------------------------
# 6. extract_source_refs() pure-function unit coverage (no DB needed) — see
#    module docstring "Source-ref extraction" for the rule rationale.
# ---------------------------------------------------------------------------
def test_extract_source_refs_tool_call_rule():
    refs = extract_source_refs("tool", {"tool_name": "Read"}, "irrelevant chunk text")
    assert refs == [("tool_call", "Read")]


def test_extract_source_refs_file_rule_top_level_and_nested():
    refs = extract_source_refs(
        "tool",
        {"file_path": "/a.py", "tool_input": {"path": "/b.py", "notebook_path": "/c.ipynb"}},
        "no urls",
    )
    assert refs == [("file", "/a.py"), ("file", "/b.py"), ("file", "/c.ipynb")]


def test_extract_source_refs_url_rule_matches_chunk_text_not_payload():
    refs = extract_source_refs(
        "assistant",
        {"raw_text": "payload has no url"},
        "but the chunk text has https://example.com/x and https://example.org/y",
    )
    assert refs == [
        ("url", "https://example.com/x"),
        ("url", "https://example.org/y"),
    ]


def test_extract_source_refs_returns_empty_list_for_nothing_extractable():
    refs = extract_source_refs("user", {"raw_text": "just plain text, nothing special"}, "x")
    assert refs == []


# ---------------------------------------------------------------------------
# 7. Four-case end-to-end fixture via the REAL insert_envelope() chain
#    (input.md "4. Négy-eseti teszt-fixture"): each case inserts a real
#    envelope, runs the full chain (turn_projector -> chunk_indexer), and
#    asserts the actual session_core.source_refs rows produced — proven via
#    real SQL queries, not just "the function ran without raising".
# ---------------------------------------------------------------------------
def _source_refs_for_chunk(pg_config: SessionStoreConfig, chunk_id: int) -> list[tuple]:
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ref_kind, ref_value, content_hash FROM session_core.source_refs "
                "WHERE chunk_id = %s ORDER BY source_ref_id ASC",
                (chunk_id,),
            )
            return cur.fetchall()


def test_case_a_tool_call_payload_produces_tool_call_source_ref(
    pg_config: SessionStoreConfig,
):
    """Case A: role resolves to 'tool' (provider_event_name='PostToolUse'),
    content carries tool_name -> one ref_kind='tool_call' row."""
    turn_id = _make_turn(
        pg_config,
        provider_event_name="PostToolUse",
        payload={"raw_text": "ran a tool", "tool_name": "Read"},
    )
    results = run_indexing_batch(config=pg_config)
    assert len(results) == 1
    assert results[0].outcome == "done"
    assert results[0].source_ref_count == 1

    chunks = _chunks_for_turn(pg_config, turn_id)
    assert len(chunks) == 1
    chunk_id = chunks[0][0]

    refs = _source_refs_for_chunk(pg_config, chunk_id)
    assert len(refs) == 1
    ref_kind, ref_value, content_hash = refs[0]
    assert ref_kind == "tool_call"
    assert ref_value == "Read"
    assert content_hash == hashlib.sha256(ref_value.encode("utf-8")).hexdigest()


def test_case_b_file_path_payload_produces_file_source_ref(
    pg_config: SessionStoreConfig,
):
    """Case B: tool_input.file_path present -> one ref_kind='file' row."""
    turn_id = _make_turn(
        pg_config,
        provider_event_name="PostToolUse",
        payload={
            "raw_text": "edited a file",
            "tool_input": {"file_path": "/workspace/session_store/chunk_indexer.py"},
        },
    )
    results = run_indexing_batch(config=pg_config)
    assert len(results) == 1
    assert results[0].outcome == "done"
    assert results[0].source_ref_count == 1

    chunks = _chunks_for_turn(pg_config, turn_id)
    chunk_id = chunks[0][0]

    refs = _source_refs_for_chunk(pg_config, chunk_id)
    assert len(refs) == 1
    ref_kind, ref_value, content_hash = refs[0]
    assert ref_kind == "file"
    assert ref_value == "/workspace/session_store/chunk_indexer.py"
    assert content_hash == hashlib.sha256(ref_value.encode("utf-8")).hexdigest()


def test_case_c_url_in_text_produces_url_source_ref(
    pg_config: SessionStoreConfig,
):
    """Case C: turn text contains a URL -> one ref_kind='url' row, matched
    against the chunk TEXT (not the raw payload structure)."""
    turn_id = _make_turn(
        pg_config,
        payload={"raw_text": "see the docs at https://example.com/docs for details"},
    )
    results = run_indexing_batch(config=pg_config)
    assert len(results) == 1
    assert results[0].outcome == "done"
    assert results[0].source_ref_count == 1

    chunks = _chunks_for_turn(pg_config, turn_id)
    chunk_id = chunks[0][0]

    refs = _source_refs_for_chunk(pg_config, chunk_id)
    assert len(refs) == 1
    ref_kind, ref_value, content_hash = refs[0]
    assert ref_kind == "url"
    assert ref_value == "https://example.com/docs"
    assert content_hash == hashlib.sha256(ref_value.encode("utf-8")).hexdigest()


def test_case_d_nothing_extractable_produces_zero_source_refs_no_error(
    pg_config: SessionStoreConfig,
):
    """Case D: control case — plain text, no tool_name/file key/URL ->
    zero session_core.source_refs rows, no exception, outbox row still
    marked 'done' (not failed)."""
    turn_id = _make_turn(
        pg_config,
        payload={"raw_text": "just a plain assistant reply, nothing to extract here"},
    )
    results = run_indexing_batch(config=pg_config)
    assert len(results) == 1
    assert results[0].outcome == "done"
    assert results[0].error is None
    assert results[0].source_ref_count == 0

    chunks = _chunks_for_turn(pg_config, turn_id)
    chunk_id = chunks[0][0]

    refs = _source_refs_for_chunk(pg_config, chunk_id)
    assert refs == []

    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM session_core.source_refs WHERE chunk_id = %s",
                (chunk_id,),
            )
            assert cur.fetchone()[0] == 0
