# session-postgres-storage-design-001 Output

## Scope

Ez egy **DESIGN job**, nem futtatott migráció. A kimenet egy SQL DRAFT
(`output/session-postgres-schema.sql`) és ez a report. Nincs valódi Postgres
instance, nincs migrációs teszt, nincs futtatott DDL-validáció — minden
`proven` állítás a DDL fájl tényleges, idézett tartalmára vonatkozik, NEM
egy élő adatbázison futtatott bizonyítékra (lásd "Nem cél" az input.md-ben).

A job tárgya mind az 5 Postgres schema (`session_raw`, `session_core`,
`session_idx`, `session_jobs`, `session_api`) vázlatos felépítése legalább
egy konkrét táblával/funkcióval mindegyikhez, a trigger/outbox határ
explicit kimondása, az index-stratégia, a particionálási döntés v1-re, és a
worker-felelősségi lista.

## Inputs Read

- `cic-mcp-session/output/session-ingress-envelope.schema.yaml` — a
  `SessionIngressEnvelope` teljes kontraktusa (299 sor), TELJESEN elolvasva
  a DDL megírása előtt.
- `cic-mcp-factory/.cic-context/factory-docs/job-slices.yaml` —
  `session-postgres-storage-design-001` bejegyzés (sor 141-167):
  `acceptance_gates`, `required_evidence`, `forbidden_shortcuts`.
- `cic-mcp-factory/.cic-context/factory-docs/architecture.md` — "## Schema
  szeparacio" (sor 116-156) és "## Inheritance / partitioning allaspont"
  (sor 158-185) szekciók.
- `cic-mcp-factory/.cic-context/factory-docs/execution-phases.md` — "## Phase
  3 - DB-backed Session Runtime" (sor 89-110).
- `cic-mcp-factory/.cic-context/corpus/normalized/thead-review-2026-06-20.yaml`
  — `dec-thead-0003` (PKL nem skálázódik, sor 44-50), `dec-thead-0004`
  (Postgres mint első backend, sor 51-58), `sup-0002` (PKL csak
  export/snapshot, sor 78-81).
- `cic-mcp-factory/.cic-context/factory-docs/acceptance-contract.md` —
  "## Universal Output Contract" (sor 23-50) és "## Artifact Contract" SQL
  alszekció (sor 71-78).

## Findings

A `SessionIngressEnvelope` 13 required mezőt definiál (`apiVersion`, `kind`,
`event_id`, `provider`, `provider_session_id`, `source`, `occurred_at`,
`ingested_at`, `payload`, `raw_payload_hash`, `trust`, `canonical`,
`interpreted`, `idempotency_key`) plusz 3 opcionális mezőt
(`provider_event_name`, `workstream`, `schema_notes`) és egy beágyazott
objektumot (`source.kind`, `source.collector`). A DDL ezt a 13+3+2 mezőt
egy-egy oszlopra képezi le a `session_raw.envelopes` táblában — nincs JSONB
"catch-all" mező a definiált envelope-mezőkre (a `payload` maga JSONB, mert
az envelope-kontraktus is JSONB-ként definiálja, lásd schema sor 143-154).

Az `architecture.md` "Schema szeparacio" szekciója 9 schema-t listáz
(`session_raw/core/idx/jobs/api` + a más rétegekhez tartozó
`gateway_core/api`, `shared_core`, `knowledge_core`) — ennek a jobnak csak
az első 5 a tárgya, a többi más capability-jobok hatóköre.

A `dec-thead-0003`/`dec-thead-0004`/`sup-0002` döntések megerősítik a
job Kontextus-szekciójának állítását: a PKL nem skálázódik 100-1000 MB
session-adatra, Postgres az első DB-backend, a PKL legfeljebb
export/snapshot lehet. A DDL ezt úgy tartja be, hogy egyetlen táblája sem
egy globális `chunks.pkl`-t modellez élő store-ként (lásd Claim-Evidence
Matrix, "no global chunks.pkl" sor).

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Mind az 5 schema létezik a DDL-ben legalább egy konkrét táblával/funkcióval | proven | `session-postgres-schema.sql:39` `CREATE SCHEMA IF NOT EXISTS session_raw;` + `:48` `CREATE TABLE session_raw.envelopes (`; `:135` `CREATE SCHEMA IF NOT EXISTS session_core;` + `:137` `CREATE TABLE session_core.sessions (`; `:207` `CREATE SCHEMA IF NOT EXISTS session_idx;` + `:209` `CREATE TABLE session_idx.chunk_fts (`; `:268` `CREATE SCHEMA IF NOT EXISTS session_jobs;` + `:274` `CREATE TABLE session_jobs.outbox (`; `:324` `CREATE SCHEMA IF NOT EXISTS session_api;` + `:326` `CREATE OR REPLACE FUNCTION session_api.search_context(` | manuális sor-idézés a DDL fájlból | alacsony — a fájl léte/szerkezete ellenőrzött, de szintaxis nincs Postgres-en futtatva (lásd Risks) |
| `session_raw.envelopes` 1:1 leképezi a `SessionIngressEnvelope` minden mezőjét | proven | lásd a teljes envelope-mező → DDL-oszlop táblázatot lent ("Envelope Field Mapping") | mezőnév-egyezés ellenőrzése a schema YAML és a DDL oszloplista között | alacsony — a leképezés szándékos és teljes, de típus-konverzió (pl. JSON Schema `format: uuid` → Postgres `UUID`) nincs futtatva validálva |
| `idempotency_key` UNIQUE constraint a `session_raw`-on | proven | `session-postgres-schema.sql:102` `CONSTRAINT envelopes_idempotency_key_unique UNIQUE (idempotency_key)` | sor-idézés | alacsony |
| `canonical` és `interpreted` mezők pinned false-ra a session_raw táblában (envelope `const: false` tükrözése) | proven | `session-postgres-schema.sql:84-85` `canonical BOOLEAN NOT NULL DEFAULT false CHECK (canonical = false),` és `:86-87` `interpreted BOOLEAN NOT NULL DEFAULT false CHECK (interpreted = false),` | sor-idézés | alacsony — DEFAULT+CHECK kombináció, de nincs futtatva tényleges INSERT teszt ami próbálna `true`-t beszúrni |
| Trigger NEM hív külső LLM/HTTP-t, csak outbox-ba ír | proven | `session-postgres-schema.sql:301-310` `enqueue_projection_job()` function body: egyetlen `INSERT INTO session_jobs.outbox (...) VALUES (...)`, nincs benne `http`/`dblink`/külső hívás; trigger maga `:311-314` | function body manuális olvasása — nincs benne semmilyen extension-alapú HTTP/LLM hívás | közepes — a forbidden_shortcuts betartása jelen DDL-ben strukturális, de semmi nem akadályozza meg, hogy egy JÖVŐBELI módosítás HTTP hívást adjon a triggerhez; ez review-fegyelem kérdése, nem DB-szintű kényszer |
| Index-stratégia lefedi session_id, metadata, FTS, vector lookup-ot | proven | session_id: `session-postgres-schema.sql:239-243`; metadata: `:246-247` GIN index; FTS: `:250-251` GIN index `tsv`-n; vector: `:255-256` HNSW index `vector_cosine_ops`-szal | sor-idézés | alacsony — szintaxis valószínűsíthetően helyes (standard pgvector/GIN minta), de nincs `EXPLAIN`/futtatott terv |
| Particionálás/inheritance döntés v1-re explicit kimondva és indokolva | proven | lásd "Decisions Proposed" — explicit döntés: nincs particionálás v1-ben | jelen report szövege + DDL-ben nincs `PARTITION BY` klózus sehol (ellenőrizve: `grep PARTITION` a fájlban 0 hit) | alacsony |
| Worker-felelősségi lista megadva | proven | lásd "Decisions Proposed" / worker szekció | jelen report szövege | alacsony |
| `session_api.*` 4 stabil függvényt ad (`search_context`, `get_timeline`, `get_context_pack`, `session_status`) az `architecture.md` elvárás szerint | proven | `session-postgres-schema.sql:326` `search_context`, `:347` `get_timeline`, `:364` `get_context_pack`, `:381` `session_status` | sor-idézés, függvénynevek egyezése az `architecture.md:152-155` SQL hívásmintával | alacsony — a függvénytest SQL-szintaxisa nincs futtatva validálva |
| Nincs globális `chunks.pkl` mint élő store | proven | a teljes DDL fájl egyetlen táblája sem PKL-alapú; `session_core.chunks` Postgres tábla (`:167-178`), nem fájl | a fájl tartalmának manuális átnézése — nincs `pkl`/`pickle` említés a DDL-ben | alacsony |

## Decisions Proposed

1. **Particionálás/inheritance v1-ben: NEM kell.** Az `architecture.md`
   elve ("ne használj inheritance-t/particionálást csak azért mert lehet")
   alapján v1-ben a `session_raw.envelopes`, `session_core.*` táblák
   particionálás NÉLKÜL indulnak. Indoklás: a job Kontextus-szekciója és a
   `dec-thead-0003` "100-1000 MB session-adat" méretrendet jelöl meg mint
   azt a skálát, amit a PKL NEM bír — ez NEM az a méret, ahol egy
   single-table Postgres tábla particionálás nélkül problémás lenne (tipikus
   particionálási küszöb production Postgres-ben jellemzően több tíz-száz
   GB / tábla, illetve sok millió-tíz millió sor felett válik indokolttá).
   Ha/amikor a `provider`/dátum szerinti particionálás indokolttá válik
   (pl. `session_raw.envelopes` havi/provider szerinti particionálása), az
   egy KÉSŐBBI iteráció döntése, konkrét volumetria-mérés alapján, nem
   előre feltételezett szükséglet alapján. A DDL-ben ezért nincs
   `PARTITION BY` klózus sehol — ezt explicit ellenőriztem (`grep PARTITION`
   0 hit a fájlban).
2. **Trigger/outbox határ**: a `session_raw.envelopes` táblára kötött
   `trg_session_raw_envelopes_enqueue` trigger (`session-postgres-schema.sql:311-314`)
   KIZÁRÓLAG egy `INSERT INTO session_jobs.outbox` műveletet végez
   (`:301-309`, a function body). Minden további feldolgozás — projection
   `session_core`-ba, embedding-generálás `session_idx.chunk_embeddings`-be
   — egy KÜLSŐ worker felelőssége, ami a `session_jobs.outbox` táblát
   pollozza/konzumálja (`status IN ('pending','failed')`, lásd
   `:290-292` parciális index). Ez explicit betartja a forbidden_shortcut
   "trigger calls external LLM/HTTP — TILOS" szabályt: a trigger function
   teste (`:301-309`) szintaktikailag NEM tartalmaz semmilyen HTTP/LLM
   hívást, csak egy szinkron, DB-n belüli INSERT-et.
3. **`session_api.*` mint az egyetlen MCP-hívási felület.** A 4 függvény
   (`search_context`, `get_timeline`, `get_context_pack`, `session_status`)
   read-only (`LANGUAGE sql STABLE`), `session_core`/`session_idx` táblákat
   JOIN-olnak belsőleg, de a hívó (MCP szerver) sosem lát direkt
   tábla-referenciát — ez az `architecture.md` "Az MCP szerver ne tablakat
   turkaljon" elvét tartja be.
4. **Embedding dimenzió (1536) placeholder**, NEM végleges döntés — ez egy
   külön, jövőbeli job (`session-chunk-indexer-001`, lásd
   `execution-phases.md` Phase 3 "Elso capability-k") felelőssége, hogy
   véglegesítse a tényleges embedding modellt és dimenziószámot.

## Rejected / Out Of Scope

- Valódi Postgres instance felállítása vagy a DDL tényleges lefuttatása —
  explicit "Nem cél" az input.md-ben.
- Migrációs framework (Alembic/Flyway) bekötése — explicit "Nem cél".
- `mcp-server/server.py` átírása, hogy az `session_api.*` függvényeket
  hívja — külön job (`session-search-api-001`).
- Hook/importer tényleges írása a `session_raw`-ba — külön jobok
  (`session-hook-collector-001` / `session-raw-event-store-001`).
- `gateway_core.*`, `gateway_api.*`, `shared_core.*`, `knowledge_core.*`
  schema-k — az `architecture.md` ezeket is listázza, de más rétegek
  (gateway/shared/knowledge) hatókörébe tartoznak, nem ennek a jobnak.
- Particionálás v1-ben (lásd "Decisions Proposed" #1) — explicit elhalasztva,
  nem elutasítva.

## Risks

- **Szintaxis-kockázat**: a DDL fájl léte és szerkezeti teljessége
  ellenőrzött (manuális átnézéssel), de SOHA nem futott Postgres-en. Az
  `pgvector`/`hnsw`/`vector_cosine_ops` szintaxis (sor 255-256) és a
  `plpgsql` trigger function (sor 301-309) szintaktikai helyessége
  valószínűsíthető (standard, dokumentált minták), de nincs `EXPLAIN`/
  tényleges `CREATE` validáció — ezért minden ide vonatkozó claim
  `proven`-ként szerepel a "DDL fájlban tényleg jelen van" értelemben, NEM
  "futtatva validálva van" értelemben (lásd a Status-definíció az
  input.md-ben).
- **Embedding-dimenzió bizonytalan**: az 1536-os placeholder
  (`session-postgres-schema.sql:223`) félreértésre adhat okot, mintha
  végleges modellválasztás lenne — explicit jelölve placeholder-ként a
  "Decisions Proposed" #4-ben.
- **Trigger-fegyelem jövőbeli kockázata**: a jelenlegi trigger
  szándékosan minimális (csak INSERT), de semmilyen DB-szintű mechanizmus
  nem akadályozza meg, hogy egy jövőbeli módosítás HTTP-hívást adjon hozzá
  — ez code-review fegyelem kérdése marad, nem DDL-kényszer (lásd
  Claim-Evidence Matrix "Trigger NEM hív..." sor, Risk: közepes).
- **`session_status` függvény komplexitása**: a `pending_jobs` subquery
  (`:389-394`) egy `provider_session_id` alapú join-t végez a
  `session_raw.envelopes` és `session_jobs.outbox` között a `payload->>'event_id'`
  JSONB-kulcs alapján — ez működő, de nem indexelt útvonal, teljesítmény-
  optimalizálás egy jövőbeli candidate-szintű jobban válik szükségessé.

## Definition Of Done Check

- [x] `output/session-postgres-schema.sql` tartalmazza mind az 5 schema-t
      legalább egy konkrét táblával/funkcióval, idézve a reportban — lásd
      Claim-Evidence Matrix 1. sor.
- [x] `session_raw` táblája 1:1 leképezi a `SessionIngressEnvelope` minden
      mezőjét — idézve melyik envelope-mező melyik DDL-oszlopnak/JSONB-kulcsnak
      felel meg — lásd "Envelope Field Mapping" táblázat lent.
- [x] `idempotency_key` UNIQUE constraint a `session_raw`-on, idézve a DDL
      releváns sorát — `session-postgres-schema.sql:102`.
- [x] trigger/outbox határ explicit definiálva, idézve melyik konkrét lépés
      megy trigger-be és melyik worker-be — lásd "Decisions Proposed" #2.
- [x] index-stratégia konkrét `CREATE INDEX` parancsokkal (session_id,
      metadata, FTS, vector) — lásd Claim-Evidence Matrix "Index-stratégia" sor.
- [x] particionálás/inheritance döntés v1-re explicit kimondva és indokolva
      — lásd "Decisions Proposed" #1.
- [x] worker-felelősségi lista (mi marad kívül a Postgres-en) — lásd alább,
      önálló szekció.
- [x] claim-evidence tábla kitöltve, nem üres — lásd fent, 10 sor.

### Envelope Field Mapping (`SessionIngressEnvelope` → `session_raw.envelopes`)

| Envelope mező | DDL oszlop/JSONB-kulcs | DDL sor |
|---|---|---|
| `apiVersion` | `api_version TEXT ... CHECK (api_version = 'cic.session/v1')` | `session-postgres-schema.sql:53` |
| `kind` | `kind TEXT ... CHECK (kind = 'SessionIngressEnvelope')` | `:54` |
| `event_id` | `event_id UUID NOT NULL` | `:57` |
| `provider` | `provider TEXT NOT NULL CHECK (length(provider) >= 1)` | `:61` |
| `provider_session_id` | `provider_session_id TEXT NOT NULL CHECK (length(provider_session_id) >= 1)` | `:62` |
| `provider_event_name` (optional) | `provider_event_name TEXT` | `:63` |
| `source.kind` | `source_kind session_raw.source_kind NOT NULL` (ENUM `hook/importer/manual/api`) | `:45`, `:66` |
| `source.collector` | `source_collector TEXT NOT NULL CHECK (length(source_collector) >= 1)` | `:67` |
| `occurred_at` | `occurred_at TIMESTAMPTZ NOT NULL` | `:70` |
| `ingested_at` | `ingested_at TIMESTAMPTZ NOT NULL` | `:71` |
| `payload` | `payload JSONB NOT NULL` | `:74` |
| `payload_encoding` | `payload_encoding session_raw.payload_encoding NOT NULL DEFAULT 'json'` (ENUM `json/text/base64`) | `:46`, `:75` |
| `raw_payload_hash` | `raw_payload_hash TEXT NOT NULL CHECK (raw_payload_hash ~ '^sha256:[a-f0-9]{64}$')` | `:76-77` |
| `trust` | `trust session_raw.trust_level NOT NULL` (ENUM `session_local/session_derived`) | `:44`, `:83` |
| `canonical` | `canonical BOOLEAN NOT NULL DEFAULT false CHECK (canonical = false)` | `:84-85` |
| `interpreted` | `interpreted BOOLEAN NOT NULL DEFAULT false CHECK (interpreted = false)` | `:86-87` |
| `idempotency_key` | `idempotency_key TEXT NOT NULL CHECK (...) ` + `UNIQUE` constraint | `:92-93`, `:102` |
| `workstream` (optional) | `workstream TEXT` | `:96` |
| `schema_notes` (optional) | `schema_notes TEXT` | `:97` |

Minden required envelope-mező lefedett. A két opcionális mező
(`provider_event_name`, `workstream`, `schema_notes` — 3 darab) is lefedett,
NULL-able oszlopként.

### Worker-felelősségi lista (mi marad kívül a Postgres-en)

- **Embedding-generálás**: a `session_idx.chunk_embeddings.embedding` oszlop
  (`session-postgres-schema.sql:223`) értékét egy KÜLSŐ worker tölti fel —
  maga az embedding-modell hívása (LLM/embedding API) sosem fut DB-n belül.
- **LLM/AI-feldolgozás általában**: bármilyen szemantikai értelmezés
  (decision/claim extraction, summarizáció) a `session_jobs.outbox`-ot
  konzumáló worker felelőssége, NEM trigger.
- **Import parser**: a historical export (ChatGPT JSONL stb.) → envelope
  konverzió egy külön job hatóköre (`historical-chatgpt-export-importer-001`,
  Phase 5), nem ez a DDL.
- **Batch rebuild**: a `session_idx` materializált/cache nézeteinek
  újraépítése (ha lesznek ilyenek) worker-oldali batch job, nem DB-belüli
  `REFRESH MATERIALIZED VIEW` trigger-lánc.
- **Provider adapter**: a provider-specifikus payload-interpretáció
  (Claude Code hook formátum, ChatGPT export formátum stb.) a
  hook/importer/collector réteg felelőssége, NEM a session_raw tábla vagy
  bármilyen DB-oldali logika — a `session_raw.envelopes.payload` mezőbe már
  egy kész `SessionIngressEnvelope.payload` kerül.

## Next Jobs

- `session-raw-event-store-001` — a `session_raw.envelopes` tábla tényleges
  létrehozása egy valódi Postgres instance-on, write-path implementáció.
- `session-turn-projector-001` — a `session_jobs.outbox`-ot konzumáló
  worker, ami `session_raw.envelopes` → `session_core.turns/chunks`
  projekciót végzi.
- `session-chunk-indexer-001` — embedding-generálás és FTS-tsvector
  feltöltés workerként, az embedding-modell/dimenzió véglegesítésével.
- `session-search-api-001` — `mcp-server/server.py` átírása, hogy a
  `session_api.*` függvényeket hívja (ez a DDL már kész felülettel
  szolgál ehhez).
- Egy jövőbeli `candidate`-szintű job, ami ezt a DDL-t egy valódi Postgres
  instance-on lefuttatja és bizonyítja, hogy a táblák/indexek/trigger-ek
  hibátlanul létrejönnek (ez emeli a státuszt `experimental`-ról
  `candidate`-re, lásd input.md "status indoklás").
