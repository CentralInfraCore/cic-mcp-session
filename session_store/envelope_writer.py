"""
Write-path for SessionIngressEnvelope -> session_raw.envelopes.

Job: session-raw-event-store-001. Secret-redaction step added by
session-data-protection-001 (see insert_envelope() and
session_store/redaction.py).
Source of truth for the envelope shape:
  output/session-ingress-envelope.schema.yaml (SessionIngressEnvelope)
Source of truth for the table DDL:
  output/session-postgres-schema.sql (session_raw.envelopes)

Scope: this module ONLY inserts a validated SessionIngressEnvelope-shaped
dict into session_raw.envelopes. It does not read, project, embed, or
expose anything via MCP — see session_store/__init__.py and CLAUDE.md
"Nem cél" for the explicit boundary.

This module has NO production caller in this job (session-raw-event-store-001
does not wire it into mcp-server/server.py or any hook/importer — that is
explicitly out of scope, see input.md "Nem cél" / "4b. Reachability
ellenőrzés"). Only this job's own pytest suite (tests/test_session_store/)
calls insert_envelope().
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import psycopg
from psycopg import sql

from session_store.redaction import redact_secrets

# ---------------------------------------------------------------------------
# Required envelope fields (mirrors output/session-ingress-envelope.schema.yaml
# "required" list). event_id/provider/.../idempotency_key are all required at
# the schema level; we re-validate them here at the application layer before
# ever building SQL, since the write-path must not rely solely on the DB to
# catch a malformed envelope.
# ---------------------------------------------------------------------------
REQUIRED_FIELDS = (
    "apiVersion",
    "kind",
    "event_id",
    "provider",
    "provider_session_id",
    "source",
    "occurred_at",
    "ingested_at",
    "payload",
    "raw_payload_hash",
    "trust",
    "canonical",
    "interpreted",
    "idempotency_key",
)

EXPECTED_API_VERSION = "cic.session/v1"
EXPECTED_KIND = "SessionIngressEnvelope"


class EnvelopeValidationError(ValueError):
    """Raised when an envelope fails application-level validation.

    This is the mechanism used for "4. canonical/interpreted elutasítás
    kezelése" — canonical=True / interpreted=True envelopes are rejected
    HERE, before any SQL is built, rather than being allowed to hit the
    DB CHECK constraint (session_raw.envelopes.canonical/interpreted
    CHECK (... = false)) and being caught as a raised DB error. Choosing
    application-level pre-validation over "let the DB CHECK fail and catch
    it" keeps the rejection reason explicit and avoids depending on driver-
    specific exception parsing to explain *why* a row was rejected.
    """


@dataclass(frozen=True)
class SessionStoreConfig:
    """DB connection parameters for the write-path, sourced from env vars.

    No hardcoded connection string per input.md 2. requirement. Falls back
    to common Postgres-client-library defaults (PG* env vars / localhost)
    only when the corresponding env var is entirely unset.
    """

    host: str
    port: int
    dbname: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> "SessionStoreConfig":
        return cls(
            host=os.environ.get("SESSION_STORE_PG_HOST", os.environ.get("PGHOST", "localhost")),
            port=int(os.environ.get("SESSION_STORE_PG_PORT", os.environ.get("PGPORT", "5432"))),
            dbname=os.environ.get("SESSION_STORE_PG_DB", os.environ.get("PGDATABASE", "postgres")),
            user=os.environ.get("SESSION_STORE_PG_USER", os.environ.get("PGUSER", "postgres")),
            password=os.environ.get("SESSION_STORE_PG_PASSWORD", os.environ.get("PGPASSWORD", "")),
        )

    def conninfo(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


def validate_envelope(envelope: Mapping[str, Any]) -> None:
    """Validate required fields and reject canonical/interpreted=True.

    Raises EnvelopeValidationError on any violation. Does not touch the
    database — pure in-memory validation, run BEFORE insert_envelope()
    builds any SQL.
    """
    missing = [f for f in REQUIRED_FIELDS if f not in envelope]
    if missing:
        raise EnvelopeValidationError(
            f"envelope missing required field(s): {', '.join(missing)}"
        )

    if envelope["apiVersion"] != EXPECTED_API_VERSION:
        raise EnvelopeValidationError(
            f"unsupported apiVersion: {envelope['apiVersion']!r} "
            f"(expected {EXPECTED_API_VERSION!r})"
        )

    if envelope["kind"] != EXPECTED_KIND:
        raise EnvelopeValidationError(
            f"unsupported kind: {envelope['kind']!r} (expected {EXPECTED_KIND!r})"
        )

    # Schema-level const:false fields. The envelope schema does not expose
    # these as free booleans semantically, but a caller could still pass a
    # malformed dict — this is exactly the case this validation exists for.
    if envelope.get("canonical") is not False:
        raise EnvelopeValidationError(
            "rejected: canonical must be false on a SessionIngressEnvelope "
            "(canonical=true is reserved for the knowledge layer after "
            "human review/promotion, see dec-thead-0001)"
        )

    if envelope.get("interpreted") is not False:
        raise EnvelopeValidationError(
            "rejected: interpreted must be false on a SessionIngressEnvelope "
            "(semantic interpretation happens downstream in session_core "
            "projection, never on ingress, see dec-thead-0002)"
        )

    source = envelope.get("source")
    if not isinstance(source, Mapping) or "kind" not in source or "collector" not in source:
        raise EnvelopeValidationError(
            "envelope.source must be an object with 'kind' and 'collector'"
        )

    if source["kind"] not in ("hook", "importer", "manual", "api"):
        raise EnvelopeValidationError(
            f"envelope.source.kind must be one of hook/importer/manual/api, "
            f"got {source['kind']!r}"
        )

    if envelope.get("trust") not in ("session_local", "session_derived"):
        raise EnvelopeValidationError(
            f"envelope.trust must be session_local or session_derived, "
            f"got {envelope.get('trust')!r}"
        )


def insert_envelope(
    envelope: Mapping[str, Any],
    config: SessionStoreConfig | None = None,
) -> int | None:
    """Validate and insert a SessionIngressEnvelope into session_raw.envelopes.

    Returns the new row's `id` (BIGSERIAL) on a fresh insert, or None if the
    insert was a no-op due to an idempotency_key collision (ON CONFLICT DO
    NOTHING — see "3. Idempotencia").

    Raises EnvelopeValidationError before ever opening a DB connection if
    the envelope is malformed or declares canonical=true / interpreted=true
    (see "4. canonical/interpreted elutasítás kezelése").

    session-data-protection-001: `envelope["payload"]` is run through
    session_store.redaction.redact_secrets() BEFORE it is bound into the
    INSERT params below -- the PERSISTED row never contains the original
    secret-shaped substrings, only [REDACTED] in their place. This does
    NOT mutate the caller's own `envelope` dict (redact_secrets() returns
    a new structure) and does NOT touch `raw_payload_hash` (see
    redaction.py module docstring for why that hash intentionally still
    reflects the pre-redaction producer-side bytes).
    """
    validate_envelope(envelope)

    cfg = config or SessionStoreConfig.from_env()
    source = envelope["source"]
    redacted_payload = redact_secrets(envelope["payload"])

    insert_stmt = sql.SQL(
        """
        INSERT INTO session_raw.envelopes (
            api_version, kind, event_id, provider, provider_session_id,
            provider_event_name, source_kind, source_collector,
            occurred_at, ingested_at, payload, payload_encoding,
            raw_payload_hash, trust, canonical, interpreted,
            idempotency_key, workstream, schema_notes
        ) VALUES (
            %(api_version)s, %(kind)s, %(event_id)s, %(provider)s, %(provider_session_id)s,
            %(provider_event_name)s, %(source_kind)s, %(source_collector)s,
            %(occurred_at)s, %(ingested_at)s, %(payload)s, %(payload_encoding)s,
            %(raw_payload_hash)s, %(trust)s, %(canonical)s, %(interpreted)s,
            %(idempotency_key)s, %(workstream)s, %(schema_notes)s
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """
    )

    params = {
        "api_version": envelope["apiVersion"],
        "kind": envelope["kind"],
        "event_id": envelope["event_id"],
        "provider": envelope["provider"],
        "provider_session_id": envelope["provider_session_id"],
        "provider_event_name": envelope.get("provider_event_name"),
        "source_kind": source["kind"],
        "source_collector": source["collector"],
        "occurred_at": envelope["occurred_at"],
        "ingested_at": envelope["ingested_at"],
        "payload": psycopg.types.json.Json(redacted_payload),
        "payload_encoding": envelope.get("payload_encoding", "json"),
        "raw_payload_hash": envelope["raw_payload_hash"],
        "trust": envelope["trust"],
        "canonical": envelope["canonical"],
        "interpreted": envelope["interpreted"],
        "idempotency_key": envelope["idempotency_key"],
        "workstream": envelope.get("workstream"),
        "schema_notes": envelope.get("schema_notes"),
    }

    with psycopg.connect(cfg.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(insert_stmt, params)
            row = cur.fetchone()
        conn.commit()

    return row[0] if row else None
