"""
Converter: ChatGPT export `mapping`-node -> SessionIngressEnvelope dict.

Job: historical-dedupe-idempotency-001

Source of truth for the field mapping (followed 1:1, NOT reinvented here):
  jobs/historical-chatgpt-export-importer-001/output/historical-chatgpt-importer-design.md
  (cic-mcp-factory repo), section "conversations-*.json To SessionIngressEnvelope
  Mapping".
Source of truth for the idempotency_key formula (5-component, with occurred_at):
  jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml
  lines 214-247 (cic-mcp-factory repo).

Scope: this module converts ONE already-extracted ChatGPT export `mapping`-node
(`{id, message, parent, children}`, see design report "Export Bundle Structure")
into ONE valid SessionIngressEnvelope dict. It does NOT:
  - read/parse any conversations-*.json file from disk
  - walk the `mapping` tree (parent/children traversal order is an explicit
    open question, see design report "Nem cél" / "Megjegyzés mapping-bejárásról")
  - call insert_envelope() itself -- the caller is responsible for persistence,
    using session_store.envelope_writer.insert_envelope() (see input.md
    "A converter NE hívjon DB-t direktben").

This module has NO production caller in this job -- only this job's own
pytest suite (tests/test_session_store/test_chatgpt_import.py) calls
chatgpt_message_to_envelope(). A future historical-import job (see design
report "Next Jobs") would wire this into an actual file-reading/tree-walking
importer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

# ASCII unit separator (0x1F), per the idempotency_key formula in
# session-ingress-envelope.schema.yaml lines 220-229 ("joined with the ASCII
# unit separator (0x1F) to avoid ambiguous concatenation collisions").
_UNIT_SEP = "\x1f"

PROVIDER_CHATGPT_EXPORT = "chatgpt-export"

# source.collector identifies the concrete collector/tool instance, per
# schema lines 108-114 ("e.g. script name + version"). No production
# importer exists yet (see module docstring "Nem cél" boundary above), so
# this names the converter module itself, versioned for this job.
SOURCE_COLLECTOR = "chatgpt-import-converter-v1"


def _sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes.

    Mirrors hooks/log-event.py:_sha256_hex (line 249-250) and the same
    canonicalization strategy used there for raw_payload_hash -- no
    existing importable helper exists in session_store/ for this (verified
    by grep, see report "Converter Implementation"), and hooks/log-event.py
    is a standalone CLI script (hyphenated filename, not import-safe from
    session_store/), so the formula is re-implemented here rather than
    imported, following the exact same logic verbatim (not reinvented).
    """
    return hashlib.sha256(data).hexdigest()


def compute_raw_payload_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 hash of the deterministic JSON serialization of `payload`.

    Same canonicalization as hooks/log-event.py build_envelope() (lines
    278-281): sort_keys=True, ensure_ascii=False, so an identical payload
    value always produces the same hash regardless of original key order
    (this property is what the dedupe proof in this job's test relies on:
    re-converting the same synthetic message node must yield the same
    raw_payload_hash, then the same idempotency_key).

    Returns the "sha256:<hex>" form required by
    session-ingress-envelope.schema.yaml's raw_payload_hash pattern
    (line 167: "^sha256:[a-f0-9]{64}$").
    """
    canonical_bytes = json.dumps(
        payload, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + _sha256_hex(canonical_bytes)


def compute_idempotency_key(
    *,
    provider: str,
    provider_session_id: str,
    provider_event_name: str | None,
    occurred_at: str,
    raw_payload_hash: str,
) -> str:
    """Compute idempotency_key per the schema's 5-component formula.

    EXACT formula from session-ingress-envelope.schema.yaml lines 220-229:

        idempotency_key = sha256(
          provider + "\\x1f" +
          provider_session_id + "\\x1f" +
          (provider_event_name or "") + "\\x1f" +
          occurred_at + "\\x1f" +
          raw_payload_hash
        )

    occurred_at MUST already be normalized to RFC3339 UTC with second
    precision before being passed in here (schema lines 230-231) -- this
    function does not re-normalize it, see normalize_occurred_at().

    Field order and the (provider_event_name or "") substitution mirror
    hooks/log-event.py build_envelope() (lines 290-301) verbatim -- same
    formula, not reinvented, just applied to ChatGPT-export-derived inputs
    instead of Claude Code hook-derived inputs.
    """
    idempotency_input = _UNIT_SEP.join(
        [
            provider,
            provider_session_id,
            provider_event_name or "",
            occurred_at,
            raw_payload_hash,
        ]
    )
    return "sha256:" + _sha256_hex(idempotency_input.encode("utf-8"))


def normalize_occurred_at(create_time: float | int) -> str:
    """Convert a ChatGPT export epoch-float `create_time` to RFC3339 UTC,
    second precision, trailing 'Z'.

    Per the design report mapping table: "ChatGPT epoch-float timestamp ->
    RFC3339 UTC conversion required at importer level" and the schema's
    idempotency_key description (line 230): "occurred_at MUST be normalized
    to RFC3339 UTC with second precision before hashing." Second precision
    (not microsecond) matches hooks/log-event.py:_now_iso_utc()'s strategy
    of using the same precision for both the stored value and the hash
    input, avoiding a mismatch between what's persisted and what's hashed.
    """
    dt = datetime.fromtimestamp(float(create_time), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def chatgpt_message_to_envelope(
    conversation: Mapping[str, Any],
    node: Mapping[str, Any],
    provider_session_id: str | None = None,
) -> dict:
    """Convert one ChatGPT export `mapping`-node into a SessionIngressEnvelope dict.

    Args:
        conversation: the conversation-level dict (only `conversation_id`/`id`
            is read from it here -- see design report mapping table row 1).
        node: one `mapping[node_id]` entry, shape `{id, message, parent,
            children}` (design report "Export Bundle Structure" /
            "mapping-node kulcsai"). `node["message"]` MUST be present and
            itself contain `author.role`, `create_time` (epoch float), and
            the rest of the message object -- system/tool nodes with a
            None `message` are the caller's concern to filter out before
            calling this function (this converter does not silently skip
            them, since "Nem cél" leaves tree-walking/filtering policy to
            the future importer).
        provider_session_id: optional override for provider_session_id; if
            omitted, falls back to conversation["conversation_id"] or
            conversation["id"] (mapping table row 1: "conversation_id / id
            (conversation-objektum szintű) -> provider_session_id").

    Returns:
        A dict matching every required field of SessionIngressEnvelope
        (session-ingress-envelope.schema.yaml "required" list, lines
        18-32). Does NOT call insert_envelope() or touch the database --
        the caller is responsible for persistence (input.md "A converter
        NE hívjon DB-t direktben").

    Field mapping (1:1 from historical-chatgpt-importer-design.md
    "conversations-*.json To SessionIngressEnvelope Mapping" table):
      - provider              = "chatgpt-export" (constant; already in the
                                 schema's `provider` examples, line 70)
      - provider_session_id   = conversation["conversation_id"] or ["id"]
      - provider_event_name   = message["author"]["role"] (design report's
                                 "Javasolt választás", row 4 / Decisions
                                 Proposed item 1)
      - occurred_at           = message["create_time"], epoch float ->
                                 RFC3339 UTC, second precision
      - payload                = the FULL message object, as-is (design
                                 report row 5 / Decisions Proposed item 3 --
                                 "NEM csak content.parts")
      - raw_payload_hash      = sha256 of payload's canonical JSON form
      - idempotency_key        = 5-component schema formula
    """
    message = node["message"]
    author = message["author"]
    role = author["role"]

    session_id = provider_session_id or conversation.get(
        "conversation_id"
    ) or conversation.get("id")
    if not session_id:
        raise ValueError(
            "conversation has neither 'conversation_id' nor 'id' -- cannot "
            "derive provider_session_id (design report mapping table row 1)"
        )

    occurred_at = normalize_occurred_at(message["create_time"])

    # payload = full message object, as-is (schema "stored AS-IS" guarantee,
    # lines 144-154; design report Decisions Proposed item 3). dict(...) so
    # the returned envelope does not alias the caller's node["message"].
    payload = dict(message)

    raw_payload_hash = compute_raw_payload_hash(payload)

    idempotency_key = compute_idempotency_key(
        provider=PROVIDER_CHATGPT_EXPORT,
        provider_session_id=str(session_id),
        provider_event_name=role,
        occurred_at=occurred_at,
        raw_payload_hash=raw_payload_hash,
    )

    return {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        # event_id is wrap-time identity, NOT part of idempotency_key (schema
        # lines 53-62) -- a fresh uuid4 per call is correct even on re-import
        # of the same logical message; dedupe is governed by idempotency_key.
        "event_id": str(uuid.uuid4()),
        "provider": PROVIDER_CHATGPT_EXPORT,
        "provider_session_id": str(session_id),
        "provider_event_name": role,
        "source": {"kind": "importer", "collector": SOURCE_COLLECTOR},
        "occurred_at": occurred_at,
        # ingested_at = wrap-time (now), per schema lines 131-138 ("When the
        # envelope was constructed/wrapped by the collector"). Distinct from
        # occurred_at, which is the historical, source-stable timestamp.
        "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload": payload,
        "payload_encoding": "json",
        "raw_payload_hash": raw_payload_hash,
        "trust": "session_local",
        "canonical": False,
        "interpreted": False,
        "idempotency_key": idempotency_key,
        "workstream": None,
        "schema_notes": None,
    }
