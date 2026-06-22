#!/usr/bin/env python3
"""
Session MCP server for cic-mcp-session — the FIRST session-specific MCP
server in this repo.

Job: session-mcp-tools-001

IMPORTANT — distinct from mcp-server/server.py: that module is the cic-graph
KB-graph server (token search, node lookup, focus_pack, etc.) — a totally
unrelated concept that builds its index from kb_data/pkl artifacts. This
module is NOT a modification of that file and does NOT import from it. This
module exposes exactly one session_api.* SQL function
(session_api.search_context_hybrid(), defined in
output/session-hybrid-search-api-migration.sql, job session-hybrid-search-
api-001) to an MCP client, via a NEW FastMCP instance named "cic-session"
(not "cic-graph").

Source of truth for the SQL function this module calls (NOT reimplemented
here — see "Forbidden Shortcuts" in input.md, no RRF logic is rewritten in
Python):
  output/session-hybrid-search-api-migration.sql
  session_api.search_context_hybrid(p_session_id UUID, p_query TEXT,
  p_query_embedding VECTOR(384), p_limit INTEGER DEFAULT 20)
  RETURNS TABLE (chunk_id BIGINT, turn_id BIGINT, text TEXT,
  fused_score DOUBLE PRECISION)

Source of truth for the query-embedding helper this module reuses (NOT
reimplemented here):
  session_store/vector_search.py:embed_query() / to_pgvector_literal()

Source of truth for the DB connection config this module reuses (NOT
hardcoded here):
  session_store/envelope_writer.py:SessionStoreConfig.from_env()

Scope: this module ONLY wraps search_context_hybrid() as a single MCP tool.
It does not add tools for any other session_api.* function (search_context,
search_context_vector, get_timeline, get_context_pack, session_status,
get_source_refs — see input.md "Nem cél"), does not implement
authentication/rate-limiting, and is NOT wired into .mcp.json.tpl or any
live Claude Code MCP config by this job — see job report
output/session-mcp-tools-report.md, "Deploy státusz" for the explicit
"not deployed" statement.

This module has NO production caller in this job (no .mcp.json.tpl entry,
no orchestrator/gateway wiring — see input.md "Nem cél" / job report
"Reachability"). Only this job's own manual verification (direct function
call + actual mcp.list_tools()/mcp.call_tool() dispatch, see job report
"Findings") and any future job's pytest suite would invoke
search_session_context().
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
