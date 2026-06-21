"""
Query-embedding helper for session_api.search_context_vector().

Job: session-vector-search-api-001

Source of truth for the SQL function this module's output is meant to be
passed into:
  output/session-vector-search-api-migration.sql
  (session_api.search_context_vector(p_session_id, p_query_embedding
  VECTOR(384), p_limit))
Source of truth for the embedding model this module REUSES (does NOT
reload/re-implement):
  session_store/chunk_indexer.py:194 embed_texts() /
  session_store/chunk_indexer.py:180 _get_embedding_model() /
  session_store/chunk_indexer.py:73 EMBEDDING_MODEL /
  session_store/chunk_indexer.py:76 EXPECTED_EMBEDDING_DIM

Scope: this module ONLY converts a single query string into a single
embedding vector (list[float], length 384), by calling
session_store.chunk_indexer.embed_texts() with a one-element list. It does
NOT load a model itself, does NOT call any external LLM/HTTP embedding API
(see input.md "Forbidden Shortcuts" — query embedding via external API is
explicitly TILOS), and does NOT execute any SQL itself — callers are
responsible for passing the returned vector into
session_api.search_context_vector() via psycopg (see `to_pgvector_literal`
below for the parameter-formatting choice).

This module has NO production caller in this job (no MCP server wiring —
see input.md "Nem cél": "az MCP szerver átírása, hogy ezt a függvényt
hívja" is explicitly out of scope). Only this job's own pytest suite
(tests/test_session_store/test_vector_search.py) invokes embed_query().
"""

from __future__ import annotations

from session_store.chunk_indexer import EXPECTED_EMBEDDING_DIM, embed_texts


def embed_query(text: str) -> list[float]:
    """Convert a single query string into its embedding vector.

    Calls session_store.chunk_indexer.embed_texts() (the SAME local
    sentence-transformers model used to embed chunks at index time — see
    that function's docstring) with a one-element list, and returns the
    single resulting vector. Reusing embed_texts() rather than calling
    SentenceTransformer.encode() directly here guarantees the query
    embedding and the indexed chunk embeddings come from the identical
    model-loading path (same _get_embedding_model() lru_cache instance,
    same normalize_embeddings=True setting), which is required for cosine
    similarity between the two to be meaningful at all.

    Returns a plain Python list[float] of length EXPECTED_EMBEDDING_DIM
    (384) — never a numpy array — so it is directly usable as the
    parameter value for psycopg (see `to_pgvector_literal` for the exact
    over-the-wire formatting decision used in this job's tests).

    Raises whatever the underlying embed_texts()/SentenceTransformer.encode
    call raises (no try/except here — same "let it propagate" stance as
    embed_texts() itself).
    """
    [vector] = embed_texts([text])
    return vector


def to_pgvector_literal(vector: list[float]) -> str:
    """Format a Python float list as a pgvector input literal string.

    Decision (documented per input.md "3."): this job formats the vector as
    a pgvector text literal ('[0.1,0.2,...]') passed as a plain TEXT/VARCHAR
    query parameter, rather than depending on the separate `pgvector` PyPI
    package's psycopg adapter (`pgvector.psycopg.register_vector`). Pgvector
    accepts this literal format as input to any `vector` typed column or
    function parameter via an implicit text->vector cast, so no additional
    runtime dependency is required beyond psycopg (already a hard
    dependency of this repo, see requirements.txt) and the already-vendored
    sentence-transformers stack. The `pgvector` package was deliberately
    NOT added to requirements.in for this job — see report "Decisions
    Proposed" for the trade-off (the adapter is more ergonomic for ongoing
    array-typed query params, but this job's only caller is this helper +
    its tests, so the text-literal route avoids a new dependency for a
    single call site).
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


__all__ = ["embed_query", "to_pgvector_literal", "EXPECTED_EMBEDDING_DIM"]
