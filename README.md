# cic-mcp-session

Session-scope esemény-/idővonal-/chunk-tár és provenance réteg a CIC agent-kontextus (`cic-mcp-*` család) számára.

A `cic-mcp-*` család trust-domain rétegezésében ez a komponens egyetlen beszélgetés/session
kontextusát tárolja és szolgálja ki — **nem** canonical tudás, **nem** cross-session memória.

## Mi ez és mi nem

**Igen:**
- `SessionIngressEnvelope` ingest
- raw event store
- turn/timeline projection
- chunk store
- source/provenance refs
- metadata index, full-text search, vector search
- session-scope context pack
- stabil SQL/API/MCP read tools

**Nem:**
- canonical tudás
- shared memory
- cross-session graph
- végleges döntésbányászat
- human review nélküli promotion

A komponensek közti pontos határt lásd: [CLAUDE.md](CLAUDE.md).

## Státusz

`experimental` — a repo a `cic-mcp-factory` job-lifecycle-én keresztül épül fel, kapacitás-jobonként.
A teljes session-specifikus adatfolyam (ingress envelope, Postgres write-path, projection,
chunk/embedding indexelés, FTS/vektor/hibrid retrieval, MCP read tool-ok) MÁR megépült és valódi
Postgres ellen bizonyítva van (~17 capability-job, lásd `output/session-*-report.md`):

- `SessionIngressEnvelope` write-path: `session_store/envelope_writer.py:165` (`insert_envelope`)
  — `output/session-raw-event-store-report.md`
- valódi hook-producer: `hooks/log-event.py:303-304` (`insert_envelope()`-et hív) —
  `output/session-hook-collector-report.md`
- raw event store: `session_raw.envelopes` (`output/session-postgres-schema.sql`) — ugyanaz a riport
- worker loop (turn projection + chunk indexelés, ütemezve): `session_store/turn_projector.py:300`
  (`run_projection_batch`), `session_store/chunk_indexer.py:378` (`run_indexing_batch`),
  `session_store/worker_loop.py:65,93` (`run_one_iteration`/`run_loop`) —
  `output/session-turn-projector-report.md`, `output/session-chunk-indexer-report.md`,
  `output/session-worker-scheduler-report.md`
- FTS / vektor / hibrid keresés: `session_api.search_context()`, `search_context_vector()`,
  `search_context_hybrid()` (`output/session-retrieval-quality-migration.sql`,
  `output/session-vector-search-api-migration.sql`, `output/session-hybrid-search-api-migration.sql`)
  — `output/session-vector-search-api-report.md`, `output/session-hybrid-search-api-report.md`
- `session_api` réteg (timeline, context pack, status, source refs):
  `output/session-postgres-schema.sql` + `output/session-source-refs-api-migration.sql` —
  `output/session-source-refs-api-report.md`
- 7 tool-os MCP szerver: `mcp-server/session_server.py` (`search_session_context` +
  `search_session_context_fts`/`search_session_context_vector`/`get_session_timeline`/
  `get_session_context_pack`/`get_session_status`/`get_session_source_refs`) —
  `output/session-mcp-tools-report.md`, `output/session-mcp-tools-remaining-report.md`
- host-natív MCP indítás (`.venv-host`, nem a Docker-builder `p_venv`): `.mcp.json.tpl` —
  `output/session-mcp-venv-fix-report.md`

A dokumentált korlátok: a fenti komponensek production reachability-je jelenleg `scaffold`
szintű — a write-path-ot és a worker-loop-ot semmilyen permanens, deployolt infrastruktúra
(cron/systemd timer) nem hívja élesben (lásd `output/session-worker-scheduler-report.md`
"Risks"), és a `cic-session` MCP szerver nincs bekötve semelyik éles orchestrátor/Claude Code
session `.mcp.json`-jába (lásd `output/session-mcp-config-wiring-report.md`). A
`status: experimental` ezt a reachability-rést jelzi, NEM a session-specifikus implementáció
hiányát.

## Kapcsolódó dokumentáció

- [`cic-mcp-factory` factory-docs](https://github.com/CentralInfraCore/cic-mcp-factory) — a komponens
  tervezési alapja (`architecture.md`, `acceptance-contract.md`, `execution-phases.md`)
- [`cic-mcp-knowledge`](https://github.com/CentralInfraCore/cic-mcp-knowledge) — a canonical réteg,
  amire ez a komponens sosem promote-ol automatikusan
