"""
Tests for session_store.rollback against a REAL Postgres instance: imports
2 distinct synthetic conversations, rolls back exactly ONE of them, and
proves -- with real SQL queries -- that every table the rolled-back
conversation touched is empty AND every table the untouched conversation
touched is unchanged.

Job: historical-import-rollback-tool-001

SECURITY BOUNDARY (same pattern as tests/test_session_store/
test_historical_import_runner.py / test_chatgpt_import.py): every
conversation imported by this test module is ENTIRELY FABRICATED, synthetic
content built in-process by historical_import_runner's own
_write_synthetic_bundle() fixture (reused verbatim, NOT reinvented here --
see input.md Sources). No real, personal export-bundle is read, imported,
or rolled back anywhere in this test module.

These tests do NOT mock the database connection -- same real-Postgres
pattern as test_envelope_writer.py / test_historical_import_runner.py
(pg_config / _clean_envelopes_table / _count_rows fixtures reused, NOT
reinvented here).

Pipeline coverage note: historical_import_runner.run() only writes
session_raw.envelopes (via insert_envelope()). The downstream projection
(session_core.sessions/turns, via turn_projector.run_projection_batch())
and indexing (session_core.chunks/source_refs, session_idx.chunk_fts/
chunk_embeddings, via chunk_indexer.run_indexing_batch()) stages are driven
here too, via worker_loop.run_loop() (bounded, 1 iteration) -- reusing the
EXISTING worker entry point, not reimplementing projection/indexing logic.
chunk_indexer.run_indexing_batch() additionally needs a local
sentence-transformers model to populate session_idx.chunk_embeddings, which
is NOT installed in this job's minimal .venv-host (see job setup notes --
"nincs szükség... embedding-modellre ehhez a jobhoz"). To still prove the
cascade chain end-to-end for chunk_embeddings/ranking_features/manifests
(none of which has any production writer at all yet -- ranking_features and
manifests have NO writer anywhere in this codebase as of this job, verified
by grep, see output/historical-import-rollback-tool.md "Findings"), this
test module inserts synthetic rows DIRECTLY via SQL, anchored by real
chunk_id/session_id values produced by the real import+projection+indexing
run above -- this is test fixture setup for a table with no existing
writer, not a reimplementation of any existing import/projection/indexing
logic (chatgpt_import.py / envelope_writer.py / historical_import_runner.py
/ turn_projector.py / chunk_indexer.py are all called unmodified, verbatim,
for every table that DOES have a real writer).
"""

from __future__ import annotations

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig
from session_store.historical_import_runner import run
from session_store.rollback import rollback_conversation
from session_store.worker_loop import run_loop
from tests.test_session_store.test_envelope_writer import (
    _clean_envelopes_table,  # noqa: F401 (autouse fixture, re-exported on purpose)
    pg_config,  # noqa: F401 (session-scoped fixture, re-exported on purpose)
)
from tests.test_session_store.test_historical_import_runner import (
    _write_synthetic_bundle,
)

PROVIDER_CHATGPT_EXPORT = "chatgpt-export"


@pytest.fixture(autouse=True)
def _clean_session_core_tables(pg_config: SessionStoreConfig):
    """Truncate session_core.sessions (CASCADE) before each test in this
    module, in addition to the session_raw.envelopes truncation
    test_envelope_writer._clean_envelopes_table already does (re-exported
    above).

    test_envelope_writer._clean_envelopes_table only truncates
    session_raw.envelopes, which has NO foreign key to session_core.sessions
    (see module docstring / output/session-postgres-schema.sql lines
    105-108) -- so without this fixture, session_core/session_idx rows left
    over from a PRIOR test run (in this module or any other module sharing
    the same real Postgres instance) would still be present, breaking this
    module's "0 rows before == nothing to begin with" / "manifests_pkey
    UniqueViolation on re-insert" assumptions. Truncating session_core.
    sessions CASCADE removes it and everything that cascades from it
    (turns/chunks/source_refs/manifests/chunk_fts/chunk_embeddings/
    ranking_features) in one statement -- exactly the same cascade chain
    rollback_conversation() itself relies on, used here only for test
    isolation, not as part of the code under test.
    """
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE session_core.sessions CASCADE")
        conn.commit()
    yield


# ---------------------------------------------------------------------------
# Tables touched by ONE rolled-back conversation, per input.md "4." -- every
# one of these is queried separately for BOTH the rolled-back conversation
# and the untouched conversation, in the same test.
# ---------------------------------------------------------------------------


def _count(cur: psycopg.Cursor, sql: str, params: tuple) -> int:
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _conversation_row_counts(
    pg_config: SessionStoreConfig, provider: str, provider_session_id: str
) -> dict[str, int]:
    """Real psql-equivalent row counts for ALL 9 tables, scoped to ONE
    (provider, provider_session_id) conversation.

    Every query below joins down from session_core.sessions (or, for
    session_raw.envelopes, filters directly on provider/provider_session_id)
    -- this is independent verification SQL, not a call into
    rollback_conversation() itself or any of its internals.
    """
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            counts = {}

            counts["sessions"] = _count(
                cur,
                """
                SELECT count(*) FROM session_core.sessions
                WHERE provider = %s AND provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            counts["turns"] = _count(
                cur,
                """
                SELECT count(*) FROM session_core.turns t
                JOIN session_core.sessions s ON s.session_id = t.session_id
                WHERE s.provider = %s AND s.provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            counts["chunks"] = _count(
                cur,
                """
                SELECT count(*) FROM session_core.chunks c
                JOIN session_core.sessions s ON s.session_id = c.session_id
                WHERE s.provider = %s AND s.provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            counts["source_refs"] = _count(
                cur,
                """
                SELECT count(*) FROM session_core.source_refs sr
                JOIN session_core.chunks c ON c.chunk_id = sr.chunk_id
                JOIN session_core.sessions s ON s.session_id = c.session_id
                WHERE s.provider = %s AND s.provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            counts["manifests"] = _count(
                cur,
                """
                SELECT count(*) FROM session_core.manifests m
                JOIN session_core.sessions s ON s.session_id = m.session_id
                WHERE s.provider = %s AND s.provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            counts["chunk_fts"] = _count(
                cur,
                """
                SELECT count(*) FROM session_idx.chunk_fts f
                JOIN session_core.chunks c ON c.chunk_id = f.chunk_id
                JOIN session_core.sessions s ON s.session_id = c.session_id
                WHERE s.provider = %s AND s.provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            counts["chunk_embeddings"] = _count(
                cur,
                """
                SELECT count(*) FROM session_idx.chunk_embeddings e
                JOIN session_core.chunks c ON c.chunk_id = e.chunk_id
                JOIN session_core.sessions s ON s.session_id = c.session_id
                WHERE s.provider = %s AND s.provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            counts["ranking_features"] = _count(
                cur,
                """
                SELECT count(*) FROM session_idx.ranking_features rf
                JOIN session_core.chunks c ON c.chunk_id = rf.chunk_id
                JOIN session_core.sessions s ON s.session_id = c.session_id
                WHERE s.provider = %s AND s.provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
            counts["envelopes"] = _count(
                cur,
                """
                SELECT count(*) FROM session_raw.envelopes
                WHERE provider = %s AND provider_session_id = %s
                """,
                (provider, provider_session_id),
            )
    return counts


def _seed_manifests_and_ranking_features(pg_config: SessionStoreConfig) -> None:
    """Insert synthetic session_core.manifests / session_idx.ranking_features
    rows for EVERY session_core.sessions / session_core.chunks row currently
    present, anchored by their real session_id/chunk_id.

    Neither table has any production writer yet (verified by grep -- see
    module docstring and output/historical-import-rollback-tool.md
    "Findings"), so this is the only way to exercise the cascade chain for
    them; the session_id/chunk_id values used here are REAL, already
    committed by the actual import+projection+indexing pipeline run earlier
    in the test, not fabricated/random ids.
    """
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT session_id FROM session_core.sessions")
            session_ids = [row[0] for row in cur.fetchall()]
            for session_id in session_ids:
                cur.execute(
                    """
                    INSERT INTO session_core.manifests
                        (session_id, manifest_version, summary)
                    VALUES (%s, 1, %s)
                    """,
                    (session_id, psycopg.types.json.Json({"note": "synthetic test manifest"})),
                )

            cur.execute("SELECT chunk_id FROM session_core.chunks")
            chunk_ids = [row[0] for row in cur.fetchall()]
            for chunk_id in chunk_ids:
                cur.execute(
                    """
                    INSERT INTO session_idx.ranking_features
                        (chunk_id, recency_score, importance_score, feature_vector)
                    VALUES (%s, 0.5, 0.5, %s)
                    """,
                    (chunk_id, psycopg.types.json.Json({"note": "synthetic test feature"})),
                )
        conn.commit()


def _seed_chunk_embeddings(pg_config: SessionStoreConfig) -> None:
    """Insert a synthetic session_idx.chunk_embeddings row for every
    session_core.chunks row, since this job's minimal .venv-host has no
    sentence-transformers model installed (see module docstring) and
    chunk_indexer.run_indexing_batch() would otherwise raise on the real
    embed_texts() call. The embedding values are fabricated unit vectors --
    only their PRESENCE/ABSENCE after rollback is asserted, never their
    content.
    """
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chunk_id FROM session_core.chunks")
            chunk_ids = [row[0] for row in cur.fetchall()]
            fake_vector = [0.0] * 384
            for chunk_id in chunk_ids:
                cur.execute(
                    """
                    INSERT INTO session_idx.chunk_embeddings
                        (chunk_id, embedding_model, embedding)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (chunk_id) DO NOTHING
                    """,
                    (chunk_id, "synthetic-test-model", fake_vector),
                )
        conn.commit()


def test_rollback_removes_only_targeted_conversation_all_tables(
    tmp_path, pg_config: SessionStoreConfig
):
    """The Definition Of Done's core claim, end-to-end:

    1. Import 2 DISTINCT synthetic conversations (different bundle dirs, so
       each has its own unique conversation_id -> provider_session_id),
       through the real, unmodified historical_import_runner.run().
    2. Drive projection + indexing (worker_loop.run_loop(), 1 bounded
       iteration) so BOTH conversations populate session_core.sessions/
       turns/chunks, session_idx.chunk_fts, and session_core.source_refs
       for real (chunk_embeddings/ranking_features/manifests seeded
       separately -- see module docstring "Pipeline coverage note").
    3. rollback_conversation() on ONLY conversation A.
    4. Assert, with independent SQL (not reusing rollback_conversation's own
       queries), that EVERY one of the 9 tables is empty for conversation A
       and UNCHANGED for conversation B.
    """
    # --- two DISTINCT synthetic bundles -> two distinct conversation_ids ---
    tmp_path_a = tmp_path / "bundle_a"
    tmp_path_b = tmp_path / "bundle_b"
    tmp_path_a.mkdir()
    tmp_path_b.mkdir()
    bundle_dir_a = _write_synthetic_bundle(tmp_path_a, conversations_per_shard=1)
    bundle_dir_b = _write_synthetic_bundle(tmp_path_b, conversations_per_shard=1)

    # Re-namespace bundle B's conversation ids so they cannot collide with
    # bundle A's (both _write_synthetic_bundle() calls otherwise produce the
    # same conversation_id naming scheme, e.g. "synthetic-conv-000-000").
    import json
    from pathlib import Path

    for shard_path in sorted(Path(bundle_dir_b).glob("conversations-*.json")):
        conversations = json.loads(shard_path.read_text(encoding="utf-8"))
        for conv in conversations:
            new_id = conv["conversation_id"].replace(
                "synthetic-conv-", "synthetic-conv-bundleB-"
            )
            conv["conversation_id"] = new_id
            conv["id"] = new_id
            old_prefix = conv["title"].split(" - not real data")[0]
            conv["title"] = old_prefix.replace("synthetic-conv-", "synthetic-conv-bundleB-") + (
                " - not real data"
            )
        shard_path.write_text(json.dumps(conversations), encoding="utf-8")

    # conversation_id of the FIRST conversation in each bundle's first shard
    # is deterministic given _write_synthetic_bundle()'s naming scheme.
    provider_session_id_a = "synthetic-conv-000-000"
    provider_session_id_b = "synthetic-conv-bundleB-000-000"

    # --- import both bundles via the REAL, unmodified runner ---
    result_a = run(bundle_dir_a, config=pg_config)
    result_b = run(bundle_dir_b, config=pg_config)
    assert result_a.total_rows_inserted > 0
    assert result_b.total_rows_inserted > 0

    # --- drive projection + indexing for both (real worker entry point,
    #     bounded to exactly 1 iteration -- this is enough since the whole
    #     backlog was enqueued synchronously by the inserts above) ---
    loop_results = run_loop(max_iterations=1, interval_seconds=0, config=pg_config)
    assert len(loop_results) == 1
    assert loop_results[0].projection_count > 0
    # indexing_count may legitimately be 0 here if chunk_indexer's embed
    # step is not reachable in this environment -- this assertion only
    # checks the loop ran, not that indexing succeeded (see next assert).

    # chunk_indexer.run_indexing_batch() needs sentence-transformers, which
    # this job's minimal .venv-host does not install (see module docstring
    # "Pipeline coverage note"). If chunks were NOT created by indexing
    # (because the embed call raised and the per-row transaction caught it,
    # see chunk_indexer._index_one_job's outcome=failed/dead_letter path),
    # seed them directly so chunks/source_refs/chunk_fts still exist to
    # exercise the cascade -- this only fires when indexing genuinely could
    # not run in this environment, verified below by an explicit check.
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM session_core.chunks")
            existing_chunk_count = cur.fetchone()[0]

    if existing_chunk_count == 0:
        _seed_chunks_source_refs_and_fts_directly(pg_config)

    _seed_chunk_embeddings(pg_config)
    _seed_manifests_and_ranking_features(pg_config)

    # --- sanity check: BOTH conversations have non-zero rows everywhere,
    #     BEFORE rollback, so the post-rollback "0" assertions actually mean
    #     something (not just "never had any rows to begin with") ---
    counts_a_before = _conversation_row_counts(
        pg_config, PROVIDER_CHATGPT_EXPORT, provider_session_id_a
    )
    counts_b_before = _conversation_row_counts(
        pg_config, PROVIDER_CHATGPT_EXPORT, provider_session_id_b
    )
    for table, count in counts_a_before.items():
        assert count > 0, f"conversation A table {table!r} has 0 rows BEFORE rollback (bad fixture)"
    for table, count in counts_b_before.items():
        assert count > 0, f"conversation B table {table!r} has 0 rows BEFORE rollback (bad fixture)"

    # --- the call under test: roll back ONLY conversation A ---
    rollback_result = rollback_conversation(
        PROVIDER_CHATGPT_EXPORT, provider_session_id_a, config=pg_config
    )
    assert rollback_result.sessions_deleted == 1
    assert rollback_result.envelopes_deleted == counts_a_before["envelopes"]

    # --- proof 1: EVERY table is now empty for the ROLLED-BACK conversation ---
    counts_a_after = _conversation_row_counts(
        pg_config, PROVIDER_CHATGPT_EXPORT, provider_session_id_a
    )
    for table, count in counts_a_after.items():
        assert count == 0, f"conversation A table {table!r} still has {count} row(s) after rollback"

    # --- proof 2: EVERY table is UNCHANGED for the UNTOUCHED conversation ---
    counts_b_after = _conversation_row_counts(
        pg_config, PROVIDER_CHATGPT_EXPORT, provider_session_id_b
    )
    assert counts_b_after == counts_b_before

    # --- re-calling rollback_conversation() on the SAME, now-already-rolled-
    #     back conversation is idempotent: 0/0, no exception ---
    second_call = rollback_conversation(
        PROVIDER_CHATGPT_EXPORT, provider_session_id_a, config=pg_config
    )
    assert second_call.sessions_deleted == 0
    assert second_call.envelopes_deleted == 0


def _seed_chunks_source_refs_and_fts_directly(pg_config: SessionStoreConfig) -> None:
    """Fallback seeding ONLY for the case where chunk_indexer's real
    embed_texts() call could not run in this environment (no
    sentence-transformers installed -- see module docstring). Inserts one
    chunk + one chunk_fts + one source_refs row per session_core.turns row
    that has none yet, anchored to REAL turn_id/session_id values already
    committed by the real projection run. This is fixture setup for an
    embedding-model-less environment, not a reimplementation of
    chunk_indexer's chunking/extraction logic (the inserted text/ref are
    fixed placeholders, not derived via split_into_chunks()/
    extract_source_refs()).
    """
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.turn_id, t.session_id
                FROM session_core.turns t
                LEFT JOIN session_core.chunks c ON c.turn_id = t.turn_id
                WHERE c.chunk_id IS NULL
                """
            )
            turns_without_chunks = cur.fetchall()

            for turn_id, session_id in turns_without_chunks:
                cur.execute(
                    """
                    INSERT INTO session_core.chunks
                        (turn_id, session_id, chunk_seq, text, token_count)
                    VALUES (%s, %s, 1, %s, 1)
                    RETURNING chunk_id
                    """,
                    (turn_id, session_id, "synthetic test chunk text"),
                )
                chunk_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO session_idx.chunk_fts (chunk_id, tsv)
                    VALUES (%s, to_tsvector('simple', %s))
                    """,
                    (chunk_id, "synthetic test chunk text"),
                )

                cur.execute(
                    """
                    INSERT INTO session_core.source_refs
                        (chunk_id, ref_kind, ref_value, content_hash)
                    VALUES (%s, 'file', 'synthetic/test/path.txt', 'deadbeef')
                    """,
                    (chunk_id,),
                )
        conn.commit()


# ---------------------------------------------------------------------------
# Unit-level coverage for the no-op / idempotent path, independent of the
# big end-to-end test above (does not require an import to have happened at
# all -- proves rollback_conversation() never raises for an unknown pair).
# ---------------------------------------------------------------------------
def test_rollback_unknown_conversation_is_noop_not_error(pg_config: SessionStoreConfig):
    result = rollback_conversation(
        "chatgpt-export", "conversation-that-was-never-imported", config=pg_config
    )

    assert result.sessions_deleted == 0
    assert result.envelopes_deleted == 0
