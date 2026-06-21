"""
session_store: write-path and projection-worker package for cic-mcp-session.

Scope:
- session_store.envelope_writer (job: session-raw-event-store-001) persists
  SessionIngressEnvelope instances into the session_raw.envelopes table.
- session_store.turn_projector (job: session-turn-projector-001) consumes
  session_jobs.outbox project_envelope jobs and projects
  session_raw.envelopes rows into session_core.sessions/session_core.turns.

No MCP server wiring, no embedding/session_idx population, no
chunk/source_refs/manifests projection here — see CLAUDE.md "Nem cél" /
the individual job input.md files for explicit out-of-scope items.
"""
