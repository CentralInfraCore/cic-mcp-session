"""
Tests for session_store.transcript_reader.

Job: session-transcript-reader-001

These tests do NOT require Postgres, a live MCP server, or any network
access — transcript_reader.py is a pure file-input parser. The fixture
JSONL lines below are FIXTURE data: they follow the EXACT line shape
confirmed against a real, live Claude Code transcript file on this
machine (see output/session-transcript-reader.md "Inputs Read" for the
real file path and the verbatim line dumps that this fixture's shape is
based on), but the session id, uuids, file paths, and message content are
synthetic/anonymized so this fixture can be committed to the repo.
"""

from __future__ import annotations

import json
import os

from session_store.transcript_reader import Turn, read_transcript_incremental

FIXTURE_LINES_INITIAL = [
    {
        "parentUuid": None,
        "isSidechain": False,
        "promptId": "prompt-0001",
        "type": "user",
        "message": {
            "role": "user",
            "content": "What is the current status of the deploy?",
        },
        "uuid": "uuid-user-0001",
        "timestamp": "2026-06-20T10:00:00.000Z",
        "userType": "external",
        "cwd": "/home/test/project",
        "sessionId": "sess-fixture-0001",
        "version": "2.1.148",
        "gitBranch": "main",
    },
    {
        "parentUuid": "uuid-user-0001",
        "isSidechain": False,
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "id": "msg-0001",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check the deploy status."}
            ],
            "stop_reason": "tool_use",
        },
        "requestId": "req-0001",
        "uuid": "uuid-assistant-0001",
        "timestamp": "2026-06-20T10:00:02.000Z",
        "sessionId": "sess-fixture-0001",
    },
    {
        "parentUuid": "uuid-assistant-0001",
        "isSidechain": False,
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "id": "msg-0001",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-0001",
                    "name": "Bash",
                    "input": {"command": "kubectl get pods", "description": "Check pods"},
                }
            ],
            "stop_reason": "tool_use",
        },
        "requestId": "req-0001",
        "uuid": "uuid-assistant-0002",
        "timestamp": "2026-06-20T10:00:03.000Z",
        "sessionId": "sess-fixture-0001",
    },
    {
        "parentUuid": "uuid-assistant-0002",
        "isSidechain": False,
        "promptId": "prompt-0001",
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": "toolu-0001",
                    "type": "tool_result",
                    "content": "pod/web-1 Running\npod/web-2 Running",
                    "is_error": False,
                }
            ],
        },
        "uuid": "uuid-user-0002",
        "timestamp": "2026-06-20T10:00:04.000Z",
        "toolUseResult": {"stdout": "pod/web-1 Running\npod/web-2 Running", "stderr": ""},
        "sourceToolAssistantUUID": "uuid-assistant-0002",
        "sessionId": "sess-fixture-0001",
    },
]

# Two NEW lines appended after the initial 4, for the idempotency test:
# one more user message and one more assistant text reply.
FIXTURE_LINES_APPENDED = [
    {
        "parentUuid": "uuid-user-0002",
        "isSidechain": False,
        "promptId": "prompt-0002",
        "type": "user",
        "message": {
            "role": "user",
            "content": "Great, thanks.",
        },
        "uuid": "uuid-user-0003",
        "timestamp": "2026-06-20T10:00:10.000Z",
        "sessionId": "sess-fixture-0001",
    },
    {
        "parentUuid": "uuid-user-0003",
        "isSidechain": False,
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "id": "msg-0002",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "You're welcome."}],
            "stop_reason": "end_turn",
        },
        "requestId": "req-0002",
        "uuid": "uuid-assistant-0003",
        "timestamp": "2026-06-20T10:00:12.000Z",
        "sessionId": "sess-fixture-0001",
    },
]

# A non-conversational line type that must be skipped (e.g. "summary").
FIXTURE_NON_TURN_LINE = {"type": "summary", "aiTitle": "Deploy status check", "sessionId": "sess-fixture-0001"}


def _write_jsonl(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _append_jsonl(path: str, records: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def test_first_read_extracts_expected_turns(tmp_path):
    """Initial read from offset 0 extracts one Turn per user/assistant-text
    line plus ONE merged Turn for the tool_use/tool_result pair (not two
    separate turns) — proving the pairing collapses to a single record."""
    path = str(tmp_path / "transcript.jsonl")
    _write_jsonl(path, FIXTURE_LINES_INITIAL)

    turns, offset = read_transcript_incremental(path, since_offset=0)

    # 4 lines in: user, assistant(text), assistant(tool_use), user(tool_result).
    # tool_use + tool_result merge into ONE turn => 3 turns total.
    assert len(turns) == 3
    assert offset == os.path.getsize(path)

    roles = [t.role for t in turns]
    assert roles == ["user", "assistant", "tool"]

    assert turns[0].text == "What is the current status of the deploy?"
    assert turns[1].text == "Let me check the deploy status."

    tool_turn = turns[2]
    assert tool_turn.tool_use is not None
    assert tool_turn.tool_use["id"] == "toolu-0001"
    assert tool_turn.tool_use["name"] == "Bash"
    assert tool_turn.tool_result is not None
    assert tool_turn.tool_result["tool_use_id"] == "toolu-0001"
    assert "pod/web-1 Running" in tool_turn.tool_result["content"]


def test_turn_ids_are_stable_content_based_not_random(tmp_path):
    """Reading the SAME unchanged file twice from offset 0 produces
    IDENTICAL turn_id values — proving turn_id is content-derived, not
    uuid4()-based (uuid4 would differ on every call)."""
    path = str(tmp_path / "transcript.jsonl")
    _write_jsonl(path, FIXTURE_LINES_INITIAL)

    turns_a, _ = read_transcript_incremental(path, since_offset=0)
    turns_b, _ = read_transcript_incremental(path, since_offset=0)

    ids_a = [t.turn_id for t in turns_a]
    ids_b = [t.turn_id for t in turns_b]
    assert ids_a == ids_b
    assert all(tid.startswith("sha256:") for tid in ids_a)
    # all distinct from each other within one read
    assert len(set(ids_a)) == len(ids_a)


def test_tool_use_and_tool_result_pairing_by_tool_use_id(tmp_path):
    """tool_use (assistant line) and tool_result (later user line) are
    paired into a single Turn keyed by tool_use.id == tool_result.tool_use_id,
    even though they originate from two physically different JSONL lines."""
    path = str(tmp_path / "transcript.jsonl")
    _write_jsonl(path, FIXTURE_LINES_INITIAL)

    turns, _ = read_transcript_incremental(path, since_offset=0)
    tool_turns = [t for t in turns if t.role == "tool"]
    assert len(tool_turns) == 1
    assert tool_turns[0].tool_use["id"] == tool_turns[0].tool_result["tool_use_id"]


def test_incremental_read_after_append_returns_only_new_turns(tmp_path):
    """THE idempotency proof (input.md 'Idempotencia'):
    1. read from offset 0 -> N turns, offset O1
    2. append 2 NEW lines
    3. read again from O1 -> EXACTLY 2 new turns, NOT N+2
    """
    path = str(tmp_path / "transcript.jsonl")
    _write_jsonl(path, FIXTURE_LINES_INITIAL)

    first_turns, offset_1 = read_transcript_incremental(path, since_offset=0)
    n = len(first_turns)
    assert n == 3  # sanity check, matches test_first_read_extracts_expected_turns

    _append_jsonl(path, FIXTURE_LINES_APPENDED)

    second_turns, offset_2 = read_transcript_incremental(path, since_offset=offset_1)

    assert len(second_turns) == 2  # EXACTLY 2 new turns, not n + 2
    assert offset_2 > offset_1
    assert [t.role for t in second_turns] == ["user", "assistant"]
    assert second_turns[0].text == "Great, thanks."
    assert second_turns[1].text == "You're welcome."

    # The 2 new turn_ids must not collide with any of the first 3.
    first_ids = {t.turn_id for t in first_turns}
    second_ids = {t.turn_id for t in second_turns}
    assert first_ids.isdisjoint(second_ids)


def test_non_turn_line_types_are_skipped(tmp_path):
    """A line whose top-level `type` is not user/assistant (e.g. "summary")
    is skipped, not turned into a spurious Turn, and does not break offset
    tracking for subsequent real turn lines."""
    path = str(tmp_path / "transcript.jsonl")
    _write_jsonl(path, [FIXTURE_NON_TURN_LINE] + FIXTURE_LINES_INITIAL)

    turns, offset = read_transcript_incremental(path, since_offset=0)

    assert len(turns) == 3
    assert offset == os.path.getsize(path)


def test_second_read_from_final_offset_returns_no_turns(tmp_path):
    """Reading again from the LAST offset (no new lines appended) returns
    an empty turn list and an unchanged offset — the steady-state
    idempotency case."""
    path = str(tmp_path / "transcript.jsonl")
    _write_jsonl(path, FIXTURE_LINES_INITIAL)

    _, offset_1 = read_transcript_incremental(path, since_offset=0)
    turns_again, offset_again = read_transcript_incremental(path, since_offset=offset_1)

    assert turns_again == []
    assert offset_again == offset_1


def test_turn_dataclass_fields_match_session_ingress_envelope_vocabulary():
    """Turn exposes provider_session_id / occurred_at / payload fields
    using the SAME names as SessionIngressEnvelope (see
    output/session-ingress-envelope.schema.yaml), proving the 1:1 mapping
    claimed in output/session-transcript-reader.md
    'SessionIngressEnvelope illesztés'."""
    turn = Turn(
        turn_id="sha256:" + "0" * 64,
        role="user",
        provider_session_id="sess-fixture-0001",
        occurred_at="2026-06-20T10:00:00.000Z",
        text="hello",
        payload={"text": "hello"},
    )
    assert hasattr(turn, "provider_session_id")
    assert hasattr(turn, "occurred_at")
    assert hasattr(turn, "payload")
    assert turn.payload == {"text": "hello"}
