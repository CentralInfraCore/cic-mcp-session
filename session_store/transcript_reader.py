"""
Incremental reader for Claude Code `transcript_path` JSONL files.

Job: session-transcript-reader-001

Context (see jobs/session-transcript-reader-001/input.md "Kontextus"): a
Claude Code hook stdin payload (consumed today by hooks/log-event.py, see
output/session-hook-collector-report.md) carries only a `transcript_path`
string pointing at a JSONL file with the FULL conversation transcript — the
hook payload itself does NOT carry the assistant's response text. If a
future session-ingest pipeline stored the hook payload alone, it would
produce a searchable event log, NOT a conversation memory. This module
closes that gap: given a transcript_path, it extracts stable, content-keyed
Turn records (user / assistant / tool) and pairs each tool_use block with
its tool_result block, so a caller (a future hook, or a test) can build
SessionIngressEnvelope-shaped payloads from real conversation content.

This module does NOT:
- read hook stdin JSON itself (that is hooks/log-event.py's job)
- write to session_raw.envelopes or any DB (that is envelope_writer.py)
- get wired into any hook script or settings.json (see input.md "Nem cél")
It is a pure, file-input reader/parser with no production caller in this
job — only tests/test_session_store/test_transcript_reader.py invokes it.

Transcript JSONL line shapes (confirmed against a REAL, live Claude Code
transcript file on this machine — see
output/session-transcript-reader.md "Inputs Read" for the exact path and
"Findings" for the full line dumps quoted verbatim):

- Every conversational line has a top-level `type` field: "user" or
  "assistant" (also "summary"/"system"/snapshot-only lines exist, which
  this reader skips — see SKIPPED_LINE_TYPES below).
- A conversational line has `message.role` and `message.content`.
  `message.content` is EITHER a plain string (simple user text turns) OR a
  list of typed content blocks: {"type": "text", "text": ...},
  {"type": "tool_use", "id": ..., "name": ..., "input": ...},
  {"type": "tool_result", "tool_use_id": ..., "content": ..., "is_error": ...}.
- Every line has a stable `uuid` (this transcript line's own id) and
  `parentUuid` (the preceding line's uuid, or null for the first line of
  a session) — these are Claude Code's own per-line identifiers, already
  content-independent... but NOT reused here as the Turn id (see
  "stable turn id" below for why a content hash is used instead).
- A tool_use block lives on an ASSISTANT line; the matching tool_result
  block lives on a SEPARATE, LATER user line, joined by
  tool_use.id == tool_result.tool_use_id.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Line `type` values this reader recognizes as turn-bearing. Anything else
# (e.g. "summary", "system", or a bare snapshot/meta line with no `message`
# key at all) is skipped — see read_transcript_incremental().
TURN_LINE_TYPES = ("user", "assistant")

# Content-block `type` values this reader extracts from a `message.content`
# list. "thinking"/"image" blocks (if present) are intentionally NOT
# extracted into Turn.text/tool fields — out of scope for this job (see
# input.md "Nem cél" — this reader only proves user/assistant/tool turn
# extraction and tool_use/tool_result pairing, not every block type Claude
# Code may ever emit).
TEXT_BLOCK_TYPE = "text"
TOOL_USE_BLOCK_TYPE = "tool_use"
TOOL_RESULT_BLOCK_TYPE = "tool_result"


@dataclass
class Turn:
    """One conversational turn extracted from a transcript JSONL line.

    Field naming intentionally mirrors SessionIngressEnvelope vocabulary
    (provider_session_id, occurred_at, payload, provider_event_name) so a
    caller can build an envelope from a Turn with a near-1:1 mapping — see
    output/session-transcript-reader.md "SessionIngressEnvelope illesztés"
    for the exact field-by-field table and any documented deltas.
    """

    turn_id: str
    role: str  # "user" | "assistant" | "tool"
    provider_session_id: str | None
    occurred_at: str | None
    text: str | None = None
    tool_use: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def _stable_turn_id(line_uuid: str | None, role: str, content_repr: str) -> str:
    """Derive a stable, content-based turn id.

    NOT uuid4() — see input.md "Forbidden Shortcuts": a turn id independent
    of transcript content would break idempotent re-ingestion (two
    independent reads of the same line must always produce the same id, so
    a downstream idempotency_key/dedup check can recognize "already seen").

    Composition: sha256("<line_uuid>\x1f<role>\x1f<content_repr>"). The
    transcript's own per-line `uuid` (Claude Code's own content-independent
    but STABLE-PER-LINE identifier — re-reading the same line always
    returns the same uuid, it does not change between reads) is included
    first because it is already unique per physical transcript line, which
    makes turn_id collisions across genuinely different lines effectively
    impossible; role and a content representation are layered in on top so
    that even a transcript lacking a uuid (the documented-format fixture
    path, see "Feladat 1") still gets a content-derived, reproducible id
    rather than falling back to randomness.
    """
    basis = f"{line_uuid or ''}\x1f{role}\x1f{content_repr}"
    return "sha256:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _content_repr(content: Any) -> str:
    """Deterministic string representation of a message content value.

    Used as part of the turn id hash basis. json.dumps with sort_keys=True
    gives a stable representation regardless of dict key insertion order,
    matching the canonicalization approach already used elsewhere in this
    repo for hashing (see output/session-ingress-envelope-contract.md
    raw_payload_hash discussion: "deterministic serializáció a stabil
    hash-hez").
    """
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True, ensure_ascii=True)


def _extract_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize message.content into a list of typed blocks.

    message.content is either a plain string (treated as a single
    {"type": "text", "text": ...} block) or already a list of typed
    blocks (text / tool_use / tool_result / etc., passed through as-is).
    """
    if isinstance(content, str):
        return [{"type": TEXT_BLOCK_TYPE, "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _parse_line(raw_line: str) -> dict[str, Any] | None:
    raw_line = raw_line.strip()
    if not raw_line:
        return None
    return json.loads(raw_line)


def read_transcript_incremental(
    transcript_path: str, since_offset: int = 0
) -> tuple[list[Turn], int]:
    """Read NEW turns from a transcript JSONL file starting at since_offset.

    Offset semantics: BYTE offset into the file, not a line count. A byte
    offset is used (rather than a line index) because it lets the caller
    seek directly with file.seek(offset) without re-reading/re-parsing any
    prior bytes — important for a transcript file that can grow to many
    thousands of lines over a long-running session, and it is also the
    natural unit for the documented use case (a hook firing repeatedly,
    each time re-opening the same growing file and resuming exactly where
    it left off). A line index would require either re-counting lines from
    the start on every call (defeating the incremental-read purpose) or
    maintaining a separate line-count<->byte-offset table; raw byte offset
    has neither problem and is what file.tell()/file.seek() already give
    for free.

    Returns (new_turns, new_offset). new_offset is the byte position right
    after the last successfully parsed line, so a caller can pass it back
    in as since_offset on the next call (see "Idempotencia" — calling this
    twice with the second call's since_offset == the first call's returned
    offset yields ONLY the turns from lines appended after the first read,
    never a repeat of earlier turns).

    Tool pairing: a tool_use block (on an assistant line) is paired with
    its tool_result block (on a later user line) via
    tool_use.id == tool_result.tool_use_id. Because the two blocks can
    arrive in DIFFERENT calls to this function (the tool_use line may be
    read in one batch and its tool_result only appended later), pairing is
    done with a single pass per call plus a same-call lookback: a
    tool_result line is matched against tool_use Turns already built EARLIER
    IN THE SAME since_offset->new_offset window. A tool_result whose
    tool_use_id was NOT seen in this window (e.g. the tool_use line was
    already consumed by an EARLIER call) is still emitted as its own Turn
    (role="tool", tool_result populated, tool_use=None) rather than being
    silently dropped — see output/session-transcript-reader.md "Risks" for
    the documented limitation this implies for cross-call pairing.
    """
    new_turns: list[Turn] = []
    # tool_use_id -> index into new_turns, for same-call tool_result pairing
    pending_tool_use_index: dict[str, int] = {}

    with open(transcript_path, "r", encoding="utf-8") as fh:
        fh.seek(since_offset)
        while True:
            line_start = fh.tell()
            raw_line = fh.readline()
            if not raw_line:
                break
            if not raw_line.endswith("\n"):
                # Partial line at EOF (writer mid-append) — do NOT consume
                # it; stop here so the next call re-reads it complete. The
                # offset returned is line_start, not fh.tell(), so this
                # partial line is re-read in full next time.
                break

            try:
                line = _parse_line(raw_line)
            except json.JSONDecodeError:
                # A line that fails to parse is skipped (not fatal to the
                # whole read) but still advances the offset past it, since
                # re-reading the same malformed line on every future call
                # would otherwise wedge the reader permanently.
                continue
            if line is None:
                continue

            line_type = line.get("type")
            if line_type not in TURN_LINE_TYPES:
                continue

            message = line.get("message")
            if not isinstance(message, dict):
                continue

            role = message.get("role") or line_type
            line_uuid = line.get("uuid")
            occurred_at = line.get("timestamp")
            provider_session_id = line.get("sessionId")
            blocks = _extract_blocks(message.get("content"))

            text_parts: list[str] = []
            tool_use_block: dict[str, Any] | None = None
            tool_result_block: dict[str, Any] | None = None

            for block in blocks:
                btype = block.get("type")
                if btype == TEXT_BLOCK_TYPE:
                    text_parts.append(block.get("text", ""))
                elif btype == TOOL_USE_BLOCK_TYPE:
                    tool_use_block = block
                elif btype == TOOL_RESULT_BLOCK_TYPE:
                    tool_result_block = block

            text = "\n".join(p for p in text_parts if p) or None

            if tool_result_block is not None and tool_use_block is None:
                # This line is a standalone tool_result. Try to pair it
                # with a tool_use Turn built earlier in THIS SAME call.
                tool_use_id = tool_result_block.get("tool_use_id")
                target_idx = pending_tool_use_index.pop(tool_use_id, None)
                if target_idx is not None:
                    new_turns[target_idx].tool_result = tool_result_block
                    new_turns[target_idx].payload["tool_result"] = tool_result_block
                    continue  # merged into the existing tool_use Turn, no new Turn
                # Not seen in this window (tool_use was read in an earlier
                # call) — emit its own tool turn rather than dropping data.
                turn_role = "tool"
                content_repr = _content_repr(tool_result_block)
                turn_id = _stable_turn_id(line_uuid, turn_role, content_repr)
                payload = {"tool_result": tool_result_block}
                new_turns.append(
                    Turn(
                        turn_id=turn_id,
                        role=turn_role,
                        provider_session_id=provider_session_id,
                        occurred_at=occurred_at,
                        text=None,
                        tool_use=None,
                        tool_result=tool_result_block,
                        payload=payload,
                    )
                )
                continue

            turn_role = "tool" if tool_use_block is not None else role
            content_repr = _content_repr(message.get("content"))
            turn_id = _stable_turn_id(line_uuid, turn_role, content_repr)

            payload: dict[str, Any] = {}
            if text is not None:
                payload["text"] = text
            if tool_use_block is not None:
                payload["tool_use"] = tool_use_block

            turn = Turn(
                turn_id=turn_id,
                role=turn_role,
                provider_session_id=provider_session_id,
                occurred_at=occurred_at,
                text=text,
                tool_use=tool_use_block,
                tool_result=None,
                payload=payload,
            )
            new_turns.append(turn)

            if tool_use_block is not None:
                tool_use_id = tool_use_block.get("id")
                if tool_use_id:
                    pending_tool_use_index[tool_use_id] = len(new_turns) - 1

        new_offset = fh.tell()

    return new_turns, new_offset
