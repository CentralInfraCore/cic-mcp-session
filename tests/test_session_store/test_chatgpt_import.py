"""
Tests for session_store.chatgpt_import against a REAL Postgres instance.

Job: historical-dedupe-idempotency-001

SECURITY BOUNDARY (see jobs/historical-dedupe-idempotency-001/input.md
"KRITIKUS BIZTONSAGI HATAR"): every conversation/mapping/message fixture
below is ENTIRELY FABRICATED, SYNTHETIC test data. None of it originates
from, nor is derived from, any real personal ChatGPT export bundle -- only
the STRUCTURE (field names, the `{id, message, parent, children}` node
shape, the `author.role` / `content.content_type` field names) is taken
from the prior job's structural design report
(jobs/historical-chatgpt-export-importer-001/output/historical-chatgpt-importer-design.md).
All conversation ids, message ids, and message content below (e.g.
"test-conv-0001", "hello world test message") are obviously-fabricated
placeholder values, not real data.

These tests do NOT mock the database connection -- same real-Postgres
pattern as tests/test_session_store/test_envelope_writer.py (pg_config /
_clean_envelopes_table / _count_rows fixtures and helpers are reused
verbatim from that module, NOT reinvented here).

This suite does NOT repeat test_envelope_writer.py's
test_duplicate_idempotency_key_is_noop_not_duplicate (the generic
ON CONFLICT mechanism is already proven there, on a generic envelope).
What THIS suite proves is converter-specific: that converting the SAME
synthetic ChatGPT-export message node twice produces the SAME
idempotency_key (because raw_payload_hash and occurred_at are both
deterministic functions of the node's content), so a re-import of an
unchanged historical message is a no-op at the row-count level -- and
that converting a genuinely DIFFERENT synthetic node does NOT collide.
"""

from __future__ import annotations

from session_store.chatgpt_import import (
    chatgpt_message_to_envelope,
    compute_raw_payload_hash,
)
from session_store.envelope_writer import insert_envelope
from tests.test_session_store.test_envelope_writer import (
    _clean_envelopes_table,  # noqa: F401 (autouse fixture, re-exported on purpose)
    _count_rows,
    _pg_config,
    pg_config,  # noqa: F401 (session-scoped fixture, re-exported on purpose)
)

# ---------------------------------------------------------------------------
# Synthetic ChatGPT export fixtures -- FABRICATED, see module docstring.
# ---------------------------------------------------------------------------


def _synthetic_conversation(conversation_id: str = "test-conv-0001") -> dict:
    """A fabricated conversation-level dict.

    Only the two keys this converter actually reads (design report mapping
    table row 1: "conversation_id / id -> provider_session_id") are
    populated with realistic-shaped but entirely fake values.
    """
    return {
        "conversation_id": conversation_id,
        "id": conversation_id,
        "title": "synthetic test conversation - not real data",
    }


def _synthetic_user_message_node(
    node_id: str = "test-node-0001",
    create_time: float = 1700000000.0,
    text: str = "hello world test message",
) -> dict:
    """A fabricated `mapping`-node with a `user` role message.

    Shape matches design report "Export Bundle Structure" /
    "mapping-bejárás" section: {id, message, parent, children}, with
    message = {author, channel, content, create_time, ...}.
    """
    return {
        "id": node_id,
        "parent": "test-node-0000-root",
        "children": [],
        "message": {
            "id": node_id,
            "author": {"role": "user", "name": None, "metadata": {}},
            "create_time": create_time,
            "update_time": create_time,
            "content": {
                "content_type": "text",
                "parts": [text],
            },
            "status": "finished_successfully",
            "end_turn": True,
            "weight": 1.0,
            "metadata": {
                "can_save": True,
                "is_visually_hidden_from_conversation": False,
            },
            "recipient": "all",
            "channel": None,
        },
    }


def _synthetic_assistant_message_node(
    node_id: str = "test-node-0002",
    create_time: float = 1700000005.0,
    text: str = "synthetic assistant reply for test purposes",
) -> dict:
    """A second, fabricated node -- different role, different create_time,
    different content -- used to prove non-collision with the first node.
    """
    return {
        "id": node_id,
        "parent": "test-node-0001",
        "children": [],
        "message": {
            "id": node_id,
            "author": {"role": "assistant", "name": None, "metadata": {}},
            "create_time": create_time,
            "update_time": create_time,
            "content": {
                "content_type": "text",
                "parts": [text],
            },
            "status": "finished_successfully",
            "end_turn": True,
            "weight": 1.0,
            "metadata": {
                "can_save": True,
                "is_visually_hidden_from_conversation": False,
            },
            "recipient": "all",
            "channel": None,
        },
    }


# ---------------------------------------------------------------------------
# 1. Converter produces a valid, insertable envelope
# ---------------------------------------------------------------------------
def test_converter_output_is_insertable(pg_config):
    conversation = _synthetic_conversation()
    node = _synthetic_user_message_node()

    envelope = chatgpt_message_to_envelope(conversation, node)

    assert envelope["provider"] == "chatgpt-export"
    assert envelope["provider_session_id"] == "test-conv-0001"
    assert envelope["provider_event_name"] == "user"
    assert envelope["occurred_at"] == "2023-11-14T22:13:20Z"
    assert envelope["payload"] == node["message"]
    assert envelope["raw_payload_hash"] == compute_raw_payload_hash(node["message"])

    new_id = insert_envelope(envelope, config=pg_config)

    assert new_id is not None
    assert _count_rows(pg_config) == 1


# ---------------------------------------------------------------------------
# 2. Converter-specific dedupe proof: re-converting the SAME synthetic node
#    yields the SAME idempotency_key, and re-importing it is a row-count
#    no-op (NOT a repeat of the generic ON CONFLICT test -- this asserts on
#    the converter's own determinism, not just insert_envelope()'s SQL).
# ---------------------------------------------------------------------------
def test_reimporting_unchanged_synthetic_message_does_not_duplicate(pg_config):
    conversation = _synthetic_conversation()
    node = _synthetic_user_message_node()

    first_envelope = chatgpt_message_to_envelope(conversation, node)
    first_id = insert_envelope(first_envelope, config=pg_config)
    assert first_id is not None
    assert _count_rows(pg_config) == 1

    # Re-import simulation: convert the SAME synthetic node again, as a
    # fresh independent dict (simulating a second export run reading the
    # same historical message from disk again -- a NEW Python dict, not
    # the same object reference).
    second_envelope = chatgpt_message_to_envelope(
        _synthetic_conversation(), _synthetic_user_message_node()
    )

    # Converter-specific claim: same synthetic input -> same idempotency_key,
    # even though event_id and ingested_at necessarily differ between calls.
    assert second_envelope["idempotency_key"] == first_envelope["idempotency_key"]
    assert second_envelope["event_id"] != first_envelope["event_id"]

    second_id = insert_envelope(second_envelope, config=pg_config)

    assert second_id is None  # ON CONFLICT DO NOTHING -> no new row
    assert _count_rows(pg_config) == 1  # still exactly one row, no duplicate


# ---------------------------------------------------------------------------
# 3. A genuinely different synthetic node (different role + create_time +
#    content) does NOT collide with the first -- gets its own row.
# ---------------------------------------------------------------------------
def test_different_synthetic_message_gets_separate_row(pg_config):
    conversation = _synthetic_conversation()

    user_envelope = chatgpt_message_to_envelope(
        conversation, _synthetic_user_message_node()
    )
    assistant_envelope = chatgpt_message_to_envelope(
        conversation, _synthetic_assistant_message_node()
    )

    assert user_envelope["idempotency_key"] != assistant_envelope["idempotency_key"]

    first_id = insert_envelope(user_envelope, config=pg_config)
    second_id = insert_envelope(assistant_envelope, config=pg_config)

    assert first_id is not None
    assert second_id is not None
    assert first_id != second_id
    assert _count_rows(pg_config) == 2


# ---------------------------------------------------------------------------
# 4. Different conversation_id (different provider_session_id) with
#    otherwise-identical message content also does NOT collide -- proves
#    provider_session_id is genuinely part of the key, not just a label.
# ---------------------------------------------------------------------------
def test_same_message_content_different_conversation_does_not_collide(pg_config):
    envelope_a = chatgpt_message_to_envelope(
        _synthetic_conversation("test-conv-0001"), _synthetic_user_message_node()
    )
    envelope_b = chatgpt_message_to_envelope(
        _synthetic_conversation("test-conv-9999"), _synthetic_user_message_node()
    )

    assert envelope_a["idempotency_key"] != envelope_b["idempotency_key"]

    insert_envelope(envelope_a, config=pg_config)
    insert_envelope(envelope_b, config=pg_config)

    assert _count_rows(pg_config) == 2
