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
Jelenleg a `base-repo` `mcp/main` template-jéből bootstrapelt MCP-szerver scaffold van benne,
saját session-specifikus implementáció (ingress envelope, Postgres storage) még nincs.

## Kapcsolódó dokumentáció

- [`cic-mcp-factory` factory-docs](https://github.com/CentralInfraCore/cic-mcp-factory) — a komponens
  tervezési alapja (`architecture.md`, `acceptance-contract.md`, `execution-phases.md`)
- [`cic-mcp-knowledge`](https://github.com/CentralInfraCore/cic-mcp-knowledge) — a canonical réteg,
  amire ez a komponens sosem promote-ol automatikusan
