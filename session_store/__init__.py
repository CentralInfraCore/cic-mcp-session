"""
session_store: write-path package for cic-mcp-session.

Scope (job: session-raw-event-store-001): persist SessionIngressEnvelope
instances into the session_raw.envelopes table. No read-path, no
projection-worker, no MCP server wiring here — see CLAUDE.md "Nem cél"
for explicit out-of-scope items.
"""
