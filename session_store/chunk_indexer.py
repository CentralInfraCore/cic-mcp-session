"""
Outbox-worker for session_jobs.outbox (job_type='index_turn') ->
session_core.chunks / session_idx.chunk_fts / session_idx.chunk_embeddings /
session_core.source_refs.

Job: session-chunk-indexer-001 (chunks/fts/embeddings), extended by
session-source-refs-extractor-001 (source_refs).

Source of truth for the table DDL:
  output/session-postgres-schema.sql (session_core.chunks, session_idx.chunk_fts,
  session_idx.chunk_embeddings, session_jobs.outbox, session_core.source_refs)
  output/session-chunk-indexer-migration.sql (the 'index_turn' outbox job_type,
  the AFTER INSERT trigger on session_core.turns that enqueues it, and the
  VECTOR(1536) -> VECTOR(384) dimension correction)
Source of truth for the per-row-transaction outbox-processing pattern this
worker REUSES (not reinvented here):
  session_store/turn_projector.py (_project_one_job / run_projection_batch
  structure: one transaction per outbox row, FOR UPDATE SKIP LOCKED select,
  attempts/max_attempts -> failed/dead_letter bookkeeping identical to
  turn_projector's _mark_failed_or_dead_letter)
Source of truth for the local embedding pattern referenced (not imported, to
avoid pulling the KB-build module's unrelated dependencies into the
session_store runtime path):
  make_source.py:17 EMBEDDING_MODEL / make_source.py:290 create_embeddings()
  (SentenceTransformer.encode(..., normalize_embeddings=True))

Scope: this module ONLY reads pending/failed session_jobs.outbox rows with
job_type='index_turn', reads the referenced session_core.turns row, derives
1+ deterministic text chunks from turns.content, writes session_core.chunks,
session_idx.chunk_fts (to_tsvector), session_idx.chunk_embeddings (local
sentence-transformers model), and session_core.source_refs (deterministic
key-/regex-based provenance extraction — see "Source-ref extraction" below),
and closes the outbox row (done/failed/dead_letter). It does NOT populate
session_idx.ranking_features, does NOT evaluate retrieval quality via
session_api.search_context(), does NOT implement multi-worker
locking/claiming beyond the single-worker-instance assumption
turn_projector.py already documents, and does NOT call any external
LLM/HTTP embedding API — see input.md "Nem cél" / module docstring of
turn_projector.py for the inherited limitation, and report "Risks".

This module has NO production caller in this job (no cron/supervisor/systemd
timer is wired in — see input.md "Nem cél"). Only this job's own pytest
suite (tests/test_session_store/test_chunk_indexer.py) and the CLI entry
point below (`python -m session_store.chunk_indexer`) invoke
run_indexing_batch().

---------------------------------------------------------------------------
Source-ref extraction (session-source-refs-extractor-001)
---------------------------------------------------------------------------

extract_source_refs() is a PURE, deterministic, key-/regex-matching
function — NOT an AI/LLM call, NOT semantic interpretation — that derives
zero or more (ref_kind, ref_value) provenance references from a single
turn's (role, content, text). It is called from _index_one_job(),
immediately AFTER a chunk is inserted (_insert_chunk()), inside that SAME
per-row transaction — there is no new outbox job_type/trigger for this; the
chunk_id FK that session_core.source_refs requires only exists once the
chunk row itself has been inserted in this transaction, so reusing the
existing index_turn per-row transaction is the only place this can happen
without inventing new outbox machinery (input.md "Fontos architekturális
döntés").

Three rules, applied independently (a single turn can produce refs from
more than one rule):

  1. ref_kind='tool_call': role == 'tool' AND content (a JSONB dict) has a
     'tool_name' key -> one row, ref_value = str(content['tool_name']).
     Rationale for the key name: 'tool_name' is the one tool-identifying
     key actually used elsewhere in this codebase's test fixtures/docs as
     the canonical "which tool" field on a tool-shaped payload (input.md
     "2." explicitly names 'tool_name' as the example key) — no other
     candidate key (e.g. 'name', 'tool') appears anywhere in this repo's
     schema/docs, so 'tool_name' is the only non-arbitrary choice available
     without inventing a new convention.
  2. ref_kind='file': content (or its nested 'tool_input' dict, if
     present) has one of FILE_PATH_KEYS -> one row per matching key found,
     ref_value = str(value). Checked on both the top-level content dict AND
     content['tool_input'] (if that nested dict exists), because a
     tool-call-shaped payload conventionally nests its arguments under
     'tool_input' (mirrors how PreToolUse/PostToolUse-shaped hook payloads
     are commonly structured — see turn_projector.PROVIDER_EVENT_NAME_TO_ROLE
     mapping these event names to role='tool') while a plain payload may
     carry the same key at the top level; checking both locations with the
     same fixed key list avoids needing two separate rule definitions.
     FILE_PATH_KEYS = ('file_path', 'path', 'notebook_path') — three
     concrete, observed key spellings (input.md "2." names all three
     explicitly), not a fuzzy "anything that looks like a path" heuristic.
  3. ref_kind='url': a fixed regex (URL_PATTERN, r'https?://\\S+') applied
     to the CHUNK'S text (the already-extracted/chunked text this rule
     receives as its `text` parameter), NOT the raw turns.content payload
     — one row per regex match (re.findall), so a turn with N distinct
     URLs in its text produces N rows. Matching against chunk text (not the
     raw JSONB payload) is the explicit input.md "2." requirement ("a
     CHUNK SZÖVEGÉBEN (nem a raw payload-ban)"); since chunking can split
     one turn's text into multiple pieces, this rule is invoked once per
     chunk piece (inside the existing per-chunk loop in _index_one_job),
     so a URL is correctly attributed to the chunk_id whose text actually
     contains it, never to a different chunk of the same turn.

None of these three rules involves any LLM/AI judgment call, network
request, or non-deterministic state — same input always yields the same
output list, in the same order (tool_call rule first, then file rule, then
url rule, each appending zero or more tuples in a fixed, documented order).

content_hash (session_core.source_refs.content_hash) is sha256(ref_value
.encode("utf-8")).hexdigest(), NOT the chunk's own content_hash and NOT a
hash of the whole turn — input.md "3." explicitly allows "sha256 az
ref_value-ból, vagy indokold más választásodat"; hashing ref_value directly
was chosen (not, say, hashing (ref_kind, ref_value) together) because
ref_value alone is the unique payload of a provenance pointer (a file path,
a URL, a tool name) that a future dedup/lookup pass would want to match on
across rows independent of which ref_kind first observed that exact string
— see report "Decisions Proposed" for the full rationale and the rejected
"hash the whole row" alternative.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache

import psycopg

from session_store.envelope_writer import SessionStoreConfig

logger = logging.getLogger(__name__)

OUTBOX_JOB_TYPE = "index_turn"

# ---------------------------------------------------------------------------
# Source-ref extraction rules (input.md "2. Determinisztikus kinyerési
# szabályok") — see module docstring "Source-ref extraction" for the full
# rationale of each key/pattern choice.
# ---------------------------------------------------------------------------
TOOL_CALL_ROLE = "tool"
TOOL_NAME_KEY = "tool_name"
FILE_PATH_KEYS = ("file_path", "path", "notebook_path")
NESTED_TOOL_INPUT_KEY = "tool_input"
URL_PATTERN = re.compile(r"https?://\S+")

# ---------------------------------------------------------------------------
# Embedding model (input.md: "konzisztencia kedvéért" with make_source.py's
# existing local sentence-transformers convention; NOT mandatory to match,
# but no explicit reason found to diverge — same multilingual MiniLM model
# is appropriate for session turn content, which (like the KB corpus) is
# mixed Hungarian/English per the repo's bilingual convention).
#
# Actual output dimension is queried via a real model.encode() call (see
# report "Decisions Proposed" / "Claim-Evidence Matrix") — NOT assumed from
# documentation — and is 384, not the schema's original VECTOR(1536)
# placeholder. output/session-chunk-indexer-migration.sql performs the
# corresponding ALTER COLUMN ... TYPE VECTOR(384).
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = os.environ.get(
    "SESSION_CHUNK_EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
)
EXPECTED_EMBEDDING_DIM = 384

# ---------------------------------------------------------------------------
# Chunking strategy (input.md "2. Chunking-stratégia", deterministic, NOT
# AI/LLM-based — see report "Decisions Proposed" for the full rationale and
# rejected alternatives).
#
# Text extraction from turns.content (JSONB, provider-/event-shape-
# dependent, set by turn_projector._insert_turn from the envelope payload
# 1:1 — see turn_projector.py module docstring):
#   1. If content is a JSON object and has one of KNOWN_TEXT_KEYS (checked
#      in this fixed order), use the first matching key's value, coerced to
#      str() if not already a string.
#   2. Otherwise (no known key present, or content is not an object), fall
#      back to a deterministic JSON string-serialization of the whole
#      content value (json.dumps with sort_keys=True, so the same input
#      JSONB value always serializes identically — no AI/LLM judgment about
#      "what part is the text").
#
# Splitting into chunks: fixed character-length windows, NOT sentence/regex
# boundaries — chosen over regex sentence-splitting because turn content is
# frequently semi-structured (tool output, JSON blobs, code), where naive
# sentence-boundary regexes (". " etc.) routinely misfire (e.g. splitting
# "v1.2.3" or "e.g." mid-token) in a way that is harder to reason about than
# a fixed-length window. A fixed window is simpler to test deterministically
# and is an accepted FTS/embedding chunking baseline; tuning chunk size/
# overlap for retrieval quality is explicitly out of scope for this
# `experimental` job (see input.md "status indoklás").
#
# CHUNK_SIZE_CHARS / CHUNK_OVERLAP_CHARS values: ~1500 char target window
# (within input.md's documented ~1000-2000 char guidance), 200 char overlap
# so that text spanning a chunk boundary is not entirely lost to either
# side's FTS/embedding context.
# ---------------------------------------------------------------------------
KNOWN_TEXT_KEYS = ("raw_text", "text", "content", "message")
CHUNK_SIZE_CHARS = 1500
CHUNK_OVERLAP_CHARS = 200


def extract_text(content) -> str:
    """Deterministically extract chunkable text from a turns.content JSONB value.

    Pure function, no I/O, no external calls, no AI/LLM judgment — see module
    docstring "Chunking strategy". Never raises; always returns a string
    (possibly empty, e.g. for content={}.) The caller (`_chunk_text`) treats
    an empty extracted string as "skip, zero chunks for this turn" rather
    than inserting an empty session_core.chunks row.
    """
    import json

    if isinstance(content, dict):
        for key in KNOWN_TEXT_KEYS:
            if key in content and content[key] is not None:
                value = content[key]
                return value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        # No known key present: deterministic fallback to the whole object,
        # serialized with sort_keys=True so the same JSONB value always
        # produces the same chunk text across runs.
        return json.dumps(content, sort_keys=True)
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True)


def estimate_token_count(text: str) -> int:
    """Deterministic, documented token-count estimate: whitespace-split length.

    Not a real tokenizer count (e.g. not BPE-aware) — input.md explicitly
    only requires "egyszerű, dokumentált becslés, pl. whitespace-split
    hossz", and this is exactly that: len(text.split()). Pure function, no
    I/O.
    """
    return len(text.split())


def split_into_chunks(text: str) -> list[str]:
    """Split text into fixed-size, overlapping windows.

    Deterministic: for the same input string, this ALWAYS returns the same
    list of chunk strings — no randomness, no AI/LLM call. Window size
    CHUNK_SIZE_CHARS, overlap CHUNK_OVERLAP_CHARS (see module docstring
    "Chunking strategy"). Empty input -> empty list (no chunks created for
    an empty turn). A text shorter than CHUNK_SIZE_CHARS -> exactly one
    chunk (the whole text, no padding).
    """
    if not text:
        return []
    if len(text) <= CHUNK_SIZE_CHARS:
        return [text]

    step = CHUNK_SIZE_CHARS - CHUNK_OVERLAP_CHARS
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + CHUNK_SIZE_CHARS, n)
        chunks.append(text[start:end])
        if end == n:
            break
        start += step
    return chunks


def _extract_file_refs_from_dict(d: dict) -> list[tuple[str, str]]:
    """Scan a single dict (not recursively) for FILE_PATH_KEYS, in fixed order.

    Helper for extract_source_refs() rule 2 — kept separate so the same
    scan logic can be applied to both `content` and `content['tool_input']`
    without duplicating the key-iteration loop.
    """
    refs: list[tuple[str, str]] = []
    for key in FILE_PATH_KEYS:
        if key in d and d[key] is not None:
            refs.append(("file", str(d[key])))
    return refs


def extract_source_refs(role: str, content, text: str) -> list[tuple[str, str]]:
    """Deterministically derive provenance (ref_kind, ref_value) pairs.

    Pure function, no I/O, no AI/LLM call — see module docstring "Source-ref
    extraction" for the full rationale of each rule. Always returns a list
    (possibly empty); never raises for the documented input shapes (a
    non-dict `content` simply yields no tool_call/file rows, since rules 1
    and 2 only look for keys inside a dict).

    Rule order (fixed, deterministic — same input always produces the same
    output list in the same order):
      1. tool_call: role == 'tool' and content (dict) has 'tool_name'.
      2. file: content (dict) and/or content['tool_input'] (nested dict)
         has one of FILE_PATH_KEYS, in FILE_PATH_KEYS order; content-level
         matches are appended before tool_input-level matches.
      3. url: every regex match of URL_PATTERN against `text`, in the order
         they appear in the string (re.findall's documented left-to-right
         order).
    """
    refs: list[tuple[str, str]] = []

    is_dict = isinstance(content, dict)

    # Rule 1: tool_call.
    if role == TOOL_CALL_ROLE and is_dict and content.get(TOOL_NAME_KEY) is not None:
        refs.append(("tool_call", str(content[TOOL_NAME_KEY])))

    # Rule 2: file — checked on content itself, then on content['tool_input']
    # if that nested dict is present (see module docstring rule 2 rationale).
    if is_dict:
        refs.extend(_extract_file_refs_from_dict(content))
        nested = content.get(NESTED_TOOL_INPUT_KEY)
        if isinstance(nested, dict):
            refs.extend(_extract_file_refs_from_dict(nested))

    # Rule 3: url — against the chunk TEXT, not the raw payload.
    for match in URL_PATTERN.findall(text or ""):
        refs.append(("url", match))

    return refs


@lru_cache(maxsize=1)
def _get_embedding_model(model_name: str = EMBEDDING_MODEL):
    """Lazily load and cache the SentenceTransformer model (process-wide).

    Cached via lru_cache so repeated calls within one worker process (e.g.
    one embedding call per chunk across many outbox rows in a batch) do not
    reload the model from disk every time — mirrors make_source.py's
    pattern of loading the model once per run, not once per text.
    """
    from sentence_transformers import SentenceTransformer

    logger.info("loading embedding model: %s", model_name)
    return SentenceTransformer(model_name)


def embed_texts(texts: list[str], model_name: str = EMBEDDING_MODEL):
    """Encode a list of texts with the local sentence-transformers model.

    Returns a list of plain Python float lists (one per input text), ready
    to be passed as a pgvector literal via psycopg. Calls NO external
    LLM/HTTP API — see "Forbidden Shortcuts" / module docstring. Raises
    whatever the underlying SentenceTransformer.encode call raises (caught
    by the caller's per-row transaction handling in _index_one_job, same as
    any other step).
    """
    model = _get_embedding_model(model_name)
    embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32)
    return [[float(x) for x in row] for row in embeddings]


@dataclass(frozen=True)
class IndexingResult:
    """Outcome of a single outbox row indexing attempt."""

    job_id: int
    outcome: str  # 'done' | 'failed' | 'dead_letter'
    chunk_count: int = 0
    source_ref_count: int = 0
    error: str | None = None


def _fetch_pending_jobs(cur: psycopg.Cursor) -> list[tuple]:
    """Select pending/failed index_turn outbox rows for processing.

    Mirrors turn_projector._fetch_pending_jobs exactly (same FOR UPDATE
    SKIP LOCKED rationale under the single-worker-instance assumption — see
    that function's docstring, which applies unchanged here).
    """
    cur.execute(
        """
        SELECT job_id, source_id, attempts, max_attempts
        FROM session_jobs.outbox
        WHERE job_type = %s
          AND status IN ('pending', 'failed')
        ORDER BY created_at ASC
        FOR UPDATE SKIP LOCKED
        """,
        (OUTBOX_JOB_TYPE,),
    )
    return cur.fetchall()


def _fetch_turn(cur: psycopg.Cursor, turn_id: int) -> tuple | None:
    """Fetch the turn row needed for chunking + source-ref extraction.

    `role` is selected alongside turn_id/session_id/content (extended by
    session-source-refs-extractor-001 — input.md "3."): session_core.turns
    .role is the deterministic signal turn_projector.map_role() already
    computed and persisted at projection time ('tool', 'assistant', 'user',
    'system', 'manual', or 'event' — see turn_projector.py module docstring
    "Role mapping"), so extract_source_refs() can use it directly for rule
    1 (tool_call) without re-deriving provider_event_name from anywhere.
    """
    cur.execute(
        """
        SELECT turn_id, session_id, content, role
        FROM session_core.turns
        WHERE turn_id = %s
        """,
        (turn_id,),
    )
    return cur.fetchone()


def _insert_chunk(
    cur: psycopg.Cursor, turn_id: int, session_id, chunk_seq: int, text: str, token_count: int
) -> int:
    cur.execute(
        """
        INSERT INTO session_core.chunks (turn_id, session_id, chunk_seq, text, token_count)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING chunk_id
        """,
        (turn_id, session_id, chunk_seq, text, token_count),
    )
    return cur.fetchone()[0]


def _insert_chunk_fts(cur: psycopg.Cursor, chunk_id: int, text: str) -> None:
    """Insert the FTS row using to_tsvector('simple', text).

    'simple' config chosen (not 'english'/'hungarian') because session turn
    content is mixed-language (Hungarian + English, code, tool output) per
    CLAUDE.md's bilingual convention and turn_projector's provider-agnostic
    content handling — a language-specific stemming config would silently
    misbehave on whichever language it's NOT configured for. 'simple' does
    no stemming/stopword removal, which is a safe, deterministic, language-
    neutral default; session_api.search_context() (existing function) uses
    plainto_tsquery('english', ...) and is documented out of scope for this
    job to change (see input.md "Nem cél" — retrieval-quality evaluation/
    tuning, including the FTS config interplay, is for a future job).
    """
    cur.execute(
        """
        INSERT INTO session_idx.chunk_fts (chunk_id, tsv)
        VALUES (%s, to_tsvector('simple', %s))
        """,
        (chunk_id, text),
    )


def _insert_chunk_embedding(
    cur: psycopg.Cursor, chunk_id: int, embedding: list[float], model_name: str
) -> None:
    cur.execute(
        """
        INSERT INTO session_idx.chunk_embeddings (chunk_id, embedding_model, embedding)
        VALUES (%s, %s, %s)
        """,
        (chunk_id, model_name, embedding),
    )


def _insert_source_ref(cur: psycopg.Cursor, chunk_id: int, ref_kind: str, ref_value: str) -> None:
    """Insert one session_core.source_refs row for a single extracted ref.

    content_hash = sha256(ref_value.encode("utf-8")).hexdigest() — see
    module docstring "Source-ref extraction" for why ref_value alone (not
    the whole row, not the chunk's own content) is hashed.
    """
    content_hash = hashlib.sha256(ref_value.encode("utf-8")).hexdigest()
    cur.execute(
        """
        INSERT INTO session_core.source_refs (chunk_id, ref_kind, ref_value, content_hash)
        VALUES (%s, %s, %s, %s)
        """,
        (chunk_id, ref_kind, ref_value, content_hash),
    )


def _mark_done(cur: psycopg.Cursor, job_id: int) -> None:
    cur.execute(
        """
        UPDATE session_jobs.outbox
        SET status = 'done', updated_at = now()
        WHERE job_id = %s
        """,
        (job_id,),
    )


def _mark_failed_or_dead_letter(
    cur: psycopg.Cursor, job_id: int, attempts: int, max_attempts: int, error: str
) -> str:
    """Identical bookkeeping to turn_projector._mark_failed_or_dead_letter."""
    new_attempts = attempts + 1
    new_status = "dead_letter" if new_attempts >= max_attempts else "failed"
    cur.execute(
        """
        UPDATE session_jobs.outbox
        SET status = %s, attempts = %s, last_error = %s, updated_at = now()
        WHERE job_id = %s
        """,
        (new_status, new_attempts, error, job_id),
    )
    return new_status


def _index_one_job(
    conn: psycopg.Connection, job_id: int, source_id: int, attempts: int, max_attempts: int
) -> IndexingResult:
    """Index a single outbox row (one session_core.turns row) in its own transaction.

    Mirrors turn_projector._project_one_job's per-row-transaction structure
    exactly: one transaction per outbox row, so one bad row (e.g. dangling
    turn_id) cannot poison the batch or roll back already-completed
    indexing of other rows; any exception is caught and turned into a
    failed/dead_letter outbox update rather than propagating.

    Source-ref extraction (session-source-refs-extractor-001): for each
    chunk, IMMEDIATELY after _insert_chunk() returns its chunk_id (same
    per-row transaction, no new outbox job_type — see module docstring
    "Source-ref extraction"), extract_source_refs() is called with that
    chunk's own (role, content, text), and one session_core.source_refs row
    is inserted per returned (ref_kind, ref_value) tuple via
    _insert_source_ref(). A turn with zero extractable refs across all its
    chunks simply inserts zero source_refs rows — not an error.
    """
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                turn = _fetch_turn(cur, source_id)
                if turn is None:
                    raise LookupError(
                        f"session_core.turns row not found for source_id={source_id}"
                    )
                turn_id, session_id, content, role = turn

                text = extract_text(content)
                pieces = split_into_chunks(text)

                chunk_count = 0
                source_ref_count = 0
                for chunk_seq, piece in enumerate(pieces, start=1):
                    token_count = estimate_token_count(piece)
                    chunk_id = _insert_chunk(
                        cur, turn_id, session_id, chunk_seq, piece, token_count
                    )
                    _insert_chunk_fts(cur, chunk_id, piece)
                    [embedding] = embed_texts([piece])
                    _insert_chunk_embedding(cur, chunk_id, embedding, EMBEDDING_MODEL)
                    chunk_count += 1

                    for ref_kind, ref_value in extract_source_refs(role, content, piece):
                        _insert_source_ref(cur, chunk_id, ref_kind, ref_value)
                        source_ref_count += 1

                _mark_done(cur, job_id)
        return IndexingResult(
            job_id=job_id,
            outcome="done",
            chunk_count=chunk_count,
            source_ref_count=source_ref_count,
        )
    except Exception as exc:  # noqa: BLE001 - deliberate: never let one bad
        # row raise out of the batch; always resolve the outbox row instead.
        # Same rationale as turn_projector._project_one_job.
        logger.warning("indexing failed for outbox job_id=%s: %s", job_id, exc)
        with conn.transaction():
            with conn.cursor() as cur:
                outcome = _mark_failed_or_dead_letter(
                    cur, job_id, attempts, max_attempts, str(exc)
                )
        return IndexingResult(job_id=job_id, outcome=outcome, error=str(exc))


def run_indexing_batch(config: SessionStoreConfig | None = None) -> list[IndexingResult]:
    """Run one batch of outbox->chunk/fts/embedding indexing.

    Reads all current pending/failed index_turn outbox rows, indexes each
    into session_core.chunks/session_idx.chunk_fts/session_idx.chunk_embeddings,
    and resolves each outbox row to done/failed/dead_letter. Returns the
    list of per-row results. Never raises on a per-row indexing failure —
    only a connection-level failure (e.g. Postgres unreachable) propagates,
    mirroring turn_projector.run_projection_batch exactly.

    This function calls NO external LLM/HTTP service for chunk-boundary
    decisions (split_into_chunks is pure/deterministic) — only the local
    sentence-transformers model (embed_texts) for embedding generation, run
    in-process, no network call.
    """
    cfg = config or SessionStoreConfig.from_env()
    results: list[IndexingResult] = []

    with psycopg.connect(cfg.conninfo()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                jobs = _fetch_pending_jobs(cur)
        # jobs is materialized above; rows were SKIP LOCKED-selected and the
        # transaction that held that lock has already committed/closed, so
        # each job is now processed in its own short transaction via
        # _index_one_job — identical rationale to
        # turn_projector.run_projection_batch.
        for job_id, source_id, attempts, max_attempts in jobs:
            results.append(_index_one_job(conn, job_id, source_id, attempts, max_attempts))

    return results


def _main() -> int:
    """CLI entry point: `python -m session_store.chunk_indexer`.

    Runs exactly one indexing batch against the Postgres instance configured
    via SESSION_STORE_PG_*/PG* env vars (see SessionStoreConfig.from_env)
    and prints a one-line summary per processed outbox row. Exit code is
    always 0 — per-row failures are expected, recoverable outcomes (failed/
    dead_letter), not CLI errors. This is the documented, runnable CLI
    entry point referenced in the report's reachability section; it does
    NOT by itself prove anything about whether something invokes it on a
    recurring schedule in production (see report "Findings"/"Reachability"),
    mirroring turn_projector._main exactly.
    """
    logging.basicConfig(level=logging.INFO)
    results = run_indexing_batch()
    if not results:
        print("no pending/failed index_turn outbox jobs found")
        return 0
    for r in results:
        if r.error:
            print(f"job_id={r.job_id} outcome={r.outcome} error={r.error!r}")
        else:
            print(
                f"job_id={r.job_id} outcome={r.outcome} chunk_count={r.chunk_count} "
                f"source_ref_count={r.source_ref_count}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
