"""
Secret-redaction for raw envelope payloads, applied just before the
session_raw.envelopes insert (job: session-data-protection-001).

Scope: this module recursively scans a JSON-shaped value (the kind of
structure envelope.payload already is -- str/int/float/bool/None/dict/list)
and replaces any substring matching one of SECRET_PATTERNS with
REDACTED_PLACEHOLDER. It does NOT implement a general-purpose secret
scanner/service (input.md "Nem cél": "teljes, ipari secret-scanning
megoldás" is explicitly out of scope) -- a small, NAMED, extensible regex
list is sufficient per input.md "Feladat" 2.

Extensibility path: add a new (name, compiled re.Pattern) tuple to
SECRET_PATTERNS below. Each pattern is matched independently and
substituted via re.sub, so patterns do not need to be mutually exclusive
or ordered by specificity -- a string that happens to match two patterns
still ends up fully redacted either way.

What this module deliberately does NOT touch: envelope.raw_payload_hash.
That hash is computed by the PRODUCER (the hook script / importer) over
the ORIGINAL, pre-redaction bytes, before the envelope ever reaches
insert_envelope() -- see envelope_writer.py:217 (the hash is read straight
from the envelope dict, never recomputed here). Redacting the persisted
payload does not retroactively change what the producer already hashed;
this is a documented, intentional asymmetry (see
output/session-data-protection.md "Decisions Proposed"), not an oversight.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED_PLACEHOLDER = "[REDACTED]"

# Extensible list: (name, compiled regex). Add new entries here to widen
# coverage -- no other code path needs to change. `name` is documentation
# only (not used for substitution), kept so a future patch can target one
# pattern (e.g. for testing) without re-deriving what each regex matches.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_personal_access_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("generic_bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9\-_.=]{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def _redact_string(value: str) -> str:
    redacted = value
    for _, pattern in SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED_PLACEHOLDER, redacted)
    return redacted


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret-shaped substrings in a JSON-shaped value.

    Returns a NEW structure (str/dict/list are never mutated in place) --
    int/float/bool/None pass through unchanged (a secret cannot be a
    Python int/float/bool/None in a JSON payload; only string leaves are
    ever matched against SECRET_PATTERNS).
    """
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {key: redact_secrets(val) for key, val in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value
