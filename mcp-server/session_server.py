#!/usr/bin/env python3
"""
Session MCP server for cic-mcp-session — the FIRST session-specific MCP
server in this repo.

Job: session-mcp-tools-001 (search_session_context), EXTENDED by job
session-mcp-tools-remaining-001 (the 6 tools added below:
search_session_context_fts / search_session_context_vector /
get_session_timeline / get_session_context_pack / get_session_status /
get_session_source_refs).

IMPORTANT — distinct from mcp-server/server.py: that module is the cic-graph
KB-graph server (token search, node lookup, focus_pack, etc.) — a totally
unrelated concept that builds its index from kb_data/pkl artifacts. This
module is NOT a modification of that file and does NOT import from it. This
module exposes session_api.* SQL functions (search_context_hybrid, plus —
as of session-mcp-tools-remaining-001 — search_context, search_context_vector,
get_timeline, get_context_pack, session_status, get_source_refs) to an MCP
client, via a single FastMCP instance named "cic-session" (not "cic-graph").

Source of truth for the SQL functions this module calls (NOT reimplemented
here — see "Forbidden Shortcuts" in input.md, no RRF/FTS/vector/provenance-
join logic is rewritten in Python):
  output/session-hybrid-search-api-migration.sql
  session_api.search_context_hybrid(p_session_id UUID, p_query TEXT,
  p_query_embedding VECTOR(384), p_limit INTEGER DEFAULT 20)
  RETURNS TABLE (chunk_id BIGINT, turn_id BIGINT, text TEXT,
  fused_score DOUBLE PRECISION)

  output/session-retrieval-quality-migration.sql
  session_api.search_context(p_session_id UUID, p_query TEXT,
  p_limit INTEGER DEFAULT 20)
  RETURNS TABLE (chunk_id BIGINT, turn_id BIGINT, text TEXT, rank REAL)
  session_api.session_status(p_session_id UUID)
  RETURNS TABLE (session_id UUID, status TEXT, started_at TIMESTAMPTZ,
  last_seen_at TIMESTAMPTZ, pending_jobs BIGINT)

  output/session-postgres-schema.sql
  session_api.get_timeline(p_session_id UUID, p_limit INTEGER DEFAULT 100)
  RETURNS TABLE (turn_id BIGINT, occurred_at TIMESTAMPTZ, role TEXT,
  turn_seq INTEGER)
  session_api.get_context_pack(p_session_id UUID,
  p_max_chunks INTEGER DEFAULT 50)
  RETURNS TABLE (chunk_id BIGINT, turn_seq INTEGER, text TEXT)

  output/session-vector-search-api-migration.sql
  session_api.search_context_vector(p_session_id UUID,
  p_query_embedding VECTOR(384), p_limit INTEGER DEFAULT 20)
  RETURNS TABLE (chunk_id BIGINT, turn_id BIGINT, text TEXT,
  similarity REAL)

  output/session-source-refs-api-migration.sql
  session_api.get_source_refs(p_session_id UUID, p_ref_kind TEXT DEFAULT NULL,
  p_limit INTEGER DEFAULT 100)
  RETURNS TABLE (source_ref_id BIGINT, chunk_id BIGINT, turn_id BIGINT,
  ref_kind TEXT, ref_value TEXT, content_hash TEXT)

Source of truth for the query-embedding helper this module reuses (NOT
reimplemented here):
  session_store/vector_search.py:embed_query() / to_pgvector_literal()

Source of truth for the DB connection config this module reuses (NOT
hardcoded here):
  session_store/envelope_writer.py:SessionStoreConfig.from_env()

Scope: this module wraps 7 session_api.* functions total as thin MCP tools
(search_context_hybrid from session-mcp-tools-001, plus the 6 added by
session-mcp-tools-remaining-001: search_context, search_context_vector,
get_timeline, get_context_pack, session_status, get_source_refs). It does
not implement authentication/rate-limiting, and is NOT wired into
.mcp.json.tpl or any live Claude Code MCP config by either job — see job
reports output/session-mcp-tools-report.md ("Deploy státusz") and
output/session-mcp-tools-remaining-report.md ("Findings" / explicit "nincs
deploy-olva" statement).

This module has NO production caller in either job (no .mcp.json.tpl entry,
no orchestrator/gateway wiring — see input.md "Nem cél" / job reports
"Reachability"). Only each job's own manual verification (direct function
call + actual mcp.list_tools()/mcp.call_tool() dispatch, see job reports
"Findings") and any future job's pytest suite would invoke these tools.
"""

from __future__ import annotations

import psycopg
from mcp.server.fastmcp import FastMCP

from session_store.envelope_writer import SessionStoreConfig
from session_store.vector_search import embed_query, to_pgvector_literal

mcp = FastMCP("cic-session")


@mcp.tool()
def search_session_context(session_id: str, query: str, limit: int = 20) -> list[dict]:
    """Hybrid (FTS + vector, RRF-fused) search over one session's chunks.

    Thin MCP wrapper around the EXISTING, already-tested SQL function
    session_api.search_context_hybrid() (output/session-hybrid-search-api-
    migration.sql) — this function does NOT reimplement the RRF fusion
    logic; it only:
      1. converts `query` into a query embedding via
         session_store.vector_search.embed_query() (reused, not
         reimplemented), formatted via to_pgvector_literal() for the
         psycopg/pgvector text->vector cast,
      2. calls session_api.search_context_hybrid(p_session_id, p_query,
         p_query_embedding, p_limit) via psycopg, using a connection built
         from SessionStoreConfig.from_env() (env-driven, no hardcoded
         connection string),
      3. returns the rows as a list of dicts with keys chunk_id, turn_id,
         text, fused_score.

    Args:
        session_id: The session_core.sessions.session_id (UUID, as a string).
        query: The natural-language query text. Used for BOTH the FTS side
            (passed through verbatim as p_query) and the vector side (first
            embedded via embed_query()).
        limit: Max rows to return (passed through as p_limit, default 20 —
            mirrors session_api.search_context_hybrid()'s own default).

    Returns:
        list[dict]: each dict has keys chunk_id (int), turn_id (int),
        text (str), fused_score (float), ordered by fused_score DESC (the
        same order session_api.search_context_hybrid() itself returns).
    """
    embedding = embed_query(query)
    literal = to_pgvector_literal(embedding)

    config = SessionStoreConfig.from_env()
    with psycopg.connect(config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, turn_id, text, fused_score "
                "FROM session_api.search_context_hybrid(%s, %s, %s::vector, %s)",
                (session_id, query, literal, limit),
            )
            rows = cur.fetchall()

    return [
        {
            "chunk_id": row[0],
            "turn_id": row[1],
            "text": row[2],
            "fused_score": row[3],
        }
        for row in rows
    ]


@mcp.tool()
def search_session_context_fts(session_id: str, query: str, limit: int = 20) -> list[dict]:
    """FTS-only (no vector, no RRF fusion) search over one session's chunks.

    Thin MCP wrapper around the EXISTING, already-tested SQL function
    session_api.search_context() (output/session-retrieval-quality-
    migration.sql) — this function does NOT reimplement the
    plainto_tsquery('simple', ...) full-text-search logic; it only:
      1. calls session_api.search_context(p_session_id, p_query, p_limit)
         via psycopg, using a connection built from
         SessionStoreConfig.from_env() (env-driven, no hardcoded
         connection string),
      2. returns the rows as a list of dicts with keys chunk_id, turn_id,
         text, rank.

    Args:
        session_id: The session_core.sessions.session_id (UUID, as a string).
        query: The natural-language query text, passed through verbatim as
            p_query (FTS-only — NOT embedded, unlike
            search_session_context_vector/search_session_context).
        limit: Max rows to return (passed through as p_limit, default 20 —
            mirrors session_api.search_context()'s own default).

    Returns:
        list[dict]: each dict has keys chunk_id (int), turn_id (int),
        text (str), rank (float), ordered by rank DESC (the same order
        session_api.search_context() itself returns).
    """
    config = SessionStoreConfig.from_env()
    with psycopg.connect(config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, turn_id, text, rank "
                "FROM session_api.search_context(%s, %s, %s)",
                (session_id, query, limit),
            )
            rows = cur.fetchall()

    return [
        {
            "chunk_id": row[0],
            "turn_id": row[1],
            "text": row[2],
            "rank": row[3],
        }
        for row in rows
    ]


@mcp.tool()
def search_session_context_vector(session_id: str, query: str, limit: int = 20) -> list[dict]:
    """Vector-only (cosine similarity, no FTS, no RRF fusion) search over
    one session's chunks.

    Thin MCP wrapper around the EXISTING, already-tested SQL function
    session_api.search_context_vector() (output/session-vector-search-api-
    migration.sql) — this function does NOT reimplement the cosine-distance
    logic; it only:
      1. converts `query` into a query embedding via
         session_store.vector_search.embed_query() (reused, not
         reimplemented — the SAME helper search_session_context() already
         uses), formatted via to_pgvector_literal() for the psycopg/pgvector
         text->vector cast,
      2. calls session_api.search_context_vector(p_session_id,
         p_query_embedding, p_limit) via psycopg, using a connection built
         from SessionStoreConfig.from_env() (env-driven, no hardcoded
         connection string),
      3. returns the rows as a list of dicts with keys chunk_id, turn_id,
         text, similarity.

    Args:
        session_id: The session_core.sessions.session_id (UUID, as a string).
        query: The natural-language query text. Embedded via embed_query()
            before being passed to the SQL function (the SQL function takes
            a READY VECTOR(384), not text).
        limit: Max rows to return (passed through as p_limit, default 20 —
            mirrors session_api.search_context_vector()'s own default).

    Returns:
        list[dict]: each dict has keys chunk_id (int), turn_id (int),
        text (str), similarity (float), ordered by cosine similarity DESC
        (the same order session_api.search_context_vector() itself
        returns).
    """
    embedding = embed_query(query)
    literal = to_pgvector_literal(embedding)

    config = SessionStoreConfig.from_env()
    with psycopg.connect(config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, turn_id, text, similarity "
                "FROM session_api.search_context_vector(%s, %s::vector, %s)",
                (session_id, literal, limit),
            )
            rows = cur.fetchall()

    return [
        {
            "chunk_id": row[0],
            "turn_id": row[1],
            "text": row[2],
            "similarity": row[3],
        }
        for row in rows
    ]


@mcp.tool()
def get_session_timeline(session_id: str, limit: int = 100) -> list[dict]:
    """Chronological turn timeline for one session.

    Thin MCP wrapper around the EXISTING, already-tested SQL function
    session_api.get_timeline() (output/session-postgres-schema.sql) — this
    function does NOT reimplement the turn-ordering logic; it only:
      1. calls session_api.get_timeline(p_session_id, p_limit) via psycopg,
         using a connection built from SessionStoreConfig.from_env()
         (env-driven, no hardcoded connection string),
      2. returns the rows as a list of dicts with keys turn_id, occurred_at,
         role, turn_seq.

    Args:
        session_id: The session_core.sessions.session_id (UUID, as a string).
        limit: Max rows to return (passed through as p_limit, default 100 —
            mirrors session_api.get_timeline()'s own default).

    Returns:
        list[dict]: each dict has keys turn_id (int), occurred_at
        (datetime), role (str), turn_seq (int), ordered by turn_seq ASC
        (the same order session_api.get_timeline() itself returns).
    """
    config = SessionStoreConfig.from_env()
    with psycopg.connect(config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT turn_id, occurred_at, role, turn_seq "
                "FROM session_api.get_timeline(%s, %s)",
                (session_id, limit),
            )
            rows = cur.fetchall()

    return [
        {
            "turn_id": row[0],
            "occurred_at": row[1],
            "role": row[2],
            "turn_seq": row[3],
        }
        for row in rows
    ]


@mcp.tool()
def get_session_context_pack(session_id: str, max_chunks: int = 50) -> list[dict]:
    """Ordered chunk context pack for one session.

    Thin MCP wrapper around the EXISTING, already-tested SQL function
    session_api.get_context_pack() (output/session-postgres-schema.sql) —
    this function does NOT reimplement the turn_seq/chunk_seq ordering
    logic; it only:
      1. calls session_api.get_context_pack(p_session_id, p_max_chunks) via
         psycopg, using a connection built from
         SessionStoreConfig.from_env() (env-driven, no hardcoded
         connection string),
      2. returns the rows as a list of dicts with keys chunk_id, turn_seq,
         text.

    Args:
        session_id: The session_core.sessions.session_id (UUID, as a string).
        max_chunks: Max rows to return (passed through as p_max_chunks,
            default 50 — mirrors session_api.get_context_pack()'s own
            default).

    Returns:
        list[dict]: each dict has keys chunk_id (int), turn_seq (int),
        text (str), ordered by (turn_seq ASC, chunk_seq ASC) (the same
        order session_api.get_context_pack() itself returns).
    """
    config = SessionStoreConfig.from_env()
    with psycopg.connect(config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, turn_seq, text "
                "FROM session_api.get_context_pack(%s, %s)",
                (session_id, max_chunks),
            )
            rows = cur.fetchall()

    return [
        {
            "chunk_id": row[0],
            "turn_seq": row[1],
            "text": row[2],
        }
        for row in rows
    ]


@mcp.tool()
def get_session_status(session_id: str) -> dict:
    """Session status snapshot (status, timestamps, pending_jobs count).

    Thin MCP wrapper around the EXISTING, already-tested SQL function
    session_api.session_status() (output/session-retrieval-quality-
    migration.sql) — this function does NOT reimplement the job_type-aware
    pending_jobs union logic; it only:
      1. calls session_api.session_status(p_session_id) via psycopg, using
         a connection built from SessionStoreConfig.from_env() (env-driven,
         no hardcoded connection string),
      2. returns the single row as a dict with keys session_id, status,
         started_at, last_seen_at, pending_jobs.

    Args:
        session_id: The session_core.sessions.session_id (UUID, as a string).

    Returns:
        dict with keys session_id (str), status (str), started_at
        (datetime), last_seen_at (datetime), pending_jobs (int). Returns an
        empty dict if no matching session_core.sessions row exists (the SQL
        function itself returns zero rows in that case — no Python-side
        existence check is added here, same "let the SQL function's own
        cardinality decide" stance as the other wrappers in this module).
    """
    config = SessionStoreConfig.from_env()
    with psycopg.connect(config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, status, started_at, last_seen_at, pending_jobs "
                "FROM session_api.session_status(%s)",
                (session_id,),
            )
            row = cur.fetchone()

    if row is None:
        return {}

    return {
        "session_id": str(row[0]),
        "status": row[1],
        "started_at": row[2],
        "last_seen_at": row[3],
        "pending_jobs": row[4],
    }


@mcp.tool()
def get_session_source_refs(
    session_id: str, ref_kind: str | None = None, limit: int = 100
) -> list[dict]:
    """Provenance references (tool_call/file/url) for one session.

    Thin MCP wrapper around the EXISTING, already-tested SQL function
    session_api.get_source_refs() (output/session-source-refs-api-
    migration.sql) — this function does NOT reimplement the
    source_refs->chunks session-scoping join or the ref_kind filter logic;
    it only:
      1. calls session_api.get_source_refs(p_session_id, p_ref_kind,
         p_limit) via psycopg, using a connection built from
         SessionStoreConfig.from_env() (env-driven, no hardcoded
         connection string),
      2. returns the rows as a list of dicts with keys source_ref_id,
         chunk_id, turn_id, ref_kind, ref_value, content_hash.

    Args:
        session_id: The session_core.sessions.session_id (UUID, as a string).
        ref_kind: Optional filter — one of 'tool_call', 'file', 'url'.
            None (default) returns all kinds, passed through as p_ref_kind
            (mirrors session_api.get_source_refs()'s own NULL-means-all
            semantics — no Python-side filtering is performed).
        limit: Max rows to return (passed through as p_limit, default 100 —
            mirrors session_api.get_source_refs()'s own default).

    Returns:
        list[dict]: each dict has keys source_ref_id (int), chunk_id (int),
        turn_id (int), ref_kind (str), ref_value (str), content_hash (str),
        ordered by source_ref_id ASC (the same order
        session_api.get_source_refs() itself returns).
    """
    config = SessionStoreConfig.from_env()
    with psycopg.connect(config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_ref_id, chunk_id, turn_id, ref_kind, ref_value, "
                "content_hash FROM session_api.get_source_refs(%s, %s, %s)",
                (session_id, ref_kind, limit),
            )
            rows = cur.fetchall()

    return [
        {
            "source_ref_id": row[0],
            "chunk_id": row[1],
            "turn_id": row[2],
            "ref_kind": row[3],
            "ref_value": row[4],
            "content_hash": row[5],
        }
        for row in rows
    ]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
