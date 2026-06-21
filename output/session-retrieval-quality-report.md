# session-retrieval-quality-001 Output

## Scope

Ez a job az ELSŐ, ami a `session_api.*` 4 SQL függvényt (`search_context`,
`get_timeline`, `get_context_pack`, `session_status` —
`output/session-postgres-schema.sql:326-400`) valódi adaton, valódi Postgres
ellen futtatja. A két korábbi worker (`turn_projector`, `chunk_indexer`)
write-path-ja már le volt tesztelve önmagában; ez a job a teljes
write-path → read-path láncot teszteli végponttól végpontig, és két konkrét,
gyanús integrációs rést vizsgál tényleges futtatással:

1. FTS nyelvi konfiguráció eltérés (`chunk_indexer`: `to_tsvector('simple',
   ...)` vs. `search_context()`: `plainto_tsquery('english', ...)`)
2. `session_status()` `pending_jobs` aluszámolás gyanúja (`index_turn`
   outbox payload nem tartalmaz `event_id`-t)

Nem cél (lásd input.md): új vektor-keresési SQL függvény, `source_refs`/
`ranking_features` feltöltése, nagy mennyiségű retrieval-minőség benchmark,
az MCP szerver bekötése.

## Inputs Read

- `output/session-postgres-schema.sql` — `session_api.*` 4 függvény teljes
  definíciója
- `session_store/envelope_writer.py` — write-path entry point
  (`insert_envelope`)
- `session_store/turn_projector.py` — `project_envelope` worker
  (`run_projection_batch`)
- `session_store/chunk_indexer.py` — `index_turn` worker
  (`run_indexing_batch`), `to_tsvector('simple', ...)` az `_insert_chunk_fts`
  függvényben (280-286. sor)
- `output/session-chunk-indexer-migration.sql` — `index_turn` outbox
  job_type trigger payload-formátuma (`session_id`/`turn_seq`, NINCS
  `event_id`)
- `tests/test_session_store/test_chunk_indexer.py`,
  `tests/test_session_store/test_turn_projector.py` — meglévő
  teszt-fixture minta (Postgres-konténer, `_pg_config`, truncate-fixture)
- `.cic-context/factory-docs/job-slices.yaml` — `session-retrieval-quality-001`
  bejegyzés (normatív acceptance gates)

## Findings

### 1. FTS nyelvi konfiguráció eltérés — IGAZOLT, és súlyosabb a feltételezettnél

A gyanú nem csak igazolódott, hanem szélesebb kört érintett, mint a job
eredetileg feltételezte: NEM csak a stemming-érzékeny ("run"/"running") eset
hibás, hanem **minden** `search_context()` hívás, beleértve az egzakt szavas
keresést is. Ennek oka:

- `chunk_indexer._insert_chunk_fts()` `to_tsvector('simple', text)`-et hív —
  nem stemmel, a literál token-eket tárolja.
- `search_context()` `plainto_tsquery('english', p_query)`-t hív — az
  `'english'` konfiguráció stemmel.
- Egzakt szó eset: a `"deployment"` query `plainto_tsquery('english',
  'deployment')` hatására `'deploy'` lexémává stemmelődik (lásd Evidence),
  de a tsvector oldalon a literál `'deployment'` token van — a `@@` soha
  nem egyezik.
- Stemming-érzékeny eset: a `"run"` query `'run'` lexémává válik (maga már
  base form), de a chunk csak `"running"`-ot tartalmaz literálisan — nem
  egyeznek.
- Kétnyelvű korpusz hatása: az `'english'` konfiguráció a magyar szövegen is
  futna, ha a tsquery oldalon marad — `to_tsvector('english', 'futás
  közben...')` a `"futás"`-t `'futá'`-ra csonkítja (hibás suffix-levágás egy
  nyelvre, amire az angol stemmer nincs felkészítve).

**Döntés**: a `search_context()` tsquery oldalát `'simple'`-re igazítottam
(nem a `chunk_indexer` tsvector oldalát `'english'`-re), mert a session
korpusz explicit kétnyelvű (magyar+angol) — egy egynyelvű stemmer konfiguráció
bármelyik irányba aktívan torzítaná a másik nyelv tokenjeit. A `'simple'/
'simple'` párosítás az egyetlen, amelyik egyformán korrekt (ha
exact-match-only is) mindkét nyelven.

### 2. `session_status()` `pending_jobs` aluszámolás — IGAZOLT

A gyanú igazolódott pontosan a leírt mechanizmus szerint: a `pending_jobs`
subquery `o.payload->>'event_id' IN (...)`-re támaszkodott, ami a
`project_envelope` outbox sorokra igaz (a payload tartalmaz `event_id`-t —
lásd `session_raw.enqueue_projection_job()`), de az `index_turn` outbox
sorokra (payload csak `session_id`/`turn_seq` — lásd
`session_core.enqueue_chunk_indexing_job()`) sosem teljesül, mert a
`payload->>'event_id'` ott `NULL`. Egy valódi, pending `index_turn` sor
mellett a `session_status()` `pending_jobs = 0`-t adott vissza — teljes
aluszámolás, nem csak részleges.

**Döntés**: a `pending_jobs` számítást egy explicit, `job_type`-onkénti
unió-ra cseréltem (`project_envelope` → join `session_raw.envelopes` a
`source_id`-n; `index_turn` → join `session_core.turns` a `source_id`-n),
ahelyett hogy egy közös `payload`-mintát feltételeztem volna minden
job_type-ra. Ez explicit korlátozással jár: egy jövőbeli új `job_type`
hozzáadásakor a függvényt is bővíteni kell egy új union-branch-csel (lásd
Risks).

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| A kétnyelvű, valódi-láncon-átmenő fixture (insert_envelope → run_projection_batch → run_indexing_batch) sikeresen épül, 3 turn / 3 chunk / 3 fts / 3 embedding sorral | proven | `pytest tests/test_session_store/test_session_api.py::TestBilingualFixtureRealChain::test_fixture_builds_through_real_chain_and_produces_expected_rows` → `PASSED` | valódi Postgres (pgvector/pgvector:pg16, port 55435), teljes lánc, nincs kézi INSERT session_core/session_idx táblákba | alacsony — a write-path workerek már önállóan tesztelve, ez csak az összekapcsolásukat bizonyítja |
| `search_context()` PRE-FIX egzakt szóra ("deployment") NEM talál (a stemming-mismatch minden query-t érint, nem csak az inflektált alakot) | proven | psql/python repro (`prefix_repro` scratch DB, schema+chunk-indexer-migráció, NINCS retrieval-quality-migráció): `search_context('deployment') PRE-FIX result: []`, `search_context('run') PRE-FIX result: []`, `search_context('running') PRE-FIX result: []` | tényleges `session_api.search_context()` SQL-függvény hívás, valódi adaton, a fix migráció előtt | — |
| `plainto_tsquery('english', 'deployment')` → `'deploy'` lexéma (ezért nem egyezik a `to_tsvector('simple', ...)`-ben tárolt literál `'deployment'`-tel) | proven | `psql`: `SELECT plainto_tsquery('english', 'deployment');` → `'deploy'` | direkt psql lekérdezés | — |
| `plainto_tsquery('english', 'run')` → `'run'` lexéma; a chunk csak `"running"`-ot tartalmaz literálisan a `'simple'` tsvectorban, ezért nem egyezik | proven | `psql`: `SELECT to_tsvector('simple', 'The deployment pipeline is running smoothly today.');` → `'deployment':2 'is':4 'pipeline':3 'running':5 'smoothly':6 'the':1 'today':7`; `SELECT plainto_tsquery('english', 'run');` → `'run'` | direkt psql lekérdezés, majd a fenti python repro `search_context('run')` hívás | — |
| `to_tsvector('english', ...)` torzítja a magyar szöveget (pl. `'futás'` → `'futá'`), ezért a `'english'` konfiguráció NEM lett volna védhető döntés a chunk_indexer oldalra sem | proven | `psql`: `SELECT to_tsvector('english', 'Szia, futás közben szeretnék beszélni a projektről.');` → `'beszélni':5 'futá':2 'közben':3 'projektről':7 'szeretnék':4 'szia':1` (a `futás` `s`-e levágva) | direkt psql lekérdezés | — |
| Additív migráció (`search_context()`: `plainto_tsquery('simple', ...)`) után a stemming-érzékeny query (`"run"`) DOKUMENTÁLTAN továbbra sem talál a `"running"`-ot tartalmazó chunk-ra (várt, dokumentált trade-off, nem hiba) | proven | `pytest ...::TestSearchContextStemmingMismatch::test_stemming_sensitive_query_matches_after_fix` → `PASSED` (assertálja hogy a chunk NINCS a `run` query eredményei között, mert a `'simple'` config sem stemmel) | valódi Postgres, fix migráció alkalmazva, tényleges függvényhívás | a `'simple'` döntés elfogadja, hogy nincs stemmelt retrieval semelyik nyelven sem — lásd Risks |
| Fix után az egzakt inflektált alak (`"running"`) lekérdezése talál | proven | `pytest ...::TestSearchContextStemmingMismatch::test_exact_inflected_form_query_matches_after_fix` → `PASSED` | ua. | — |
| Fix után az egzakt szó (`"deployment"`) lekérdezése talál | proven | `pytest ...::TestSearchContextExactMatch::test_exact_word_query_returns_expected_chunk` → `PASSED` | ua. | — |
| `get_timeline()` helyes `turn_seq` sorrendet ad vissza 3-turnos multi-turn fixture-rel (`[1, 2, 3]`, role-ok `["user", "tool", "assistant"]`) | proven | `pytest ...::TestTimelineAndContextPack::test_get_timeline_returns_turns_in_turn_seq_order` → `PASSED` | valódi Postgres, valódi lánc | alacsony |
| `get_context_pack()` helyes `(turn_seq ASC, chunk_seq ASC)` sorrendet ad vissza egy rövid (1 chunk) + egy hosszú (2+ chunk) turnnal | proven | `pytest ...::TestTimelineAndContextPack::test_get_context_pack_returns_chunks_in_turn_seq_then_chunk_seq_order` → `PASSED` | valódi Postgres, valódi lánc | alacsony |
| `session_status()` PRE-FIX `pending_jobs` aluszámol egy valódi, pending `index_turn` outbox sor mellett (`pending_jobs = 0`, miközben 1 valódi pending sor létezik) | proven | python repro (`prefix_repro` scratch DB): `outbox rows: [('index_turn', 'pending', {'turn_seq': 1, 'session_id': '...'}), ('project_envelope', 'done', {...})]` → `session_status() PRE-FIX result: (..., 0)` | tényleges `session_api.session_status()` SQL-függvény hívás, valódi adaton, a fix migráció előtt | — |
| `index_turn` outbox payload tényleg nem tartalmaz `event_id`-t (a gyanú előfeltétele) | proven | python repro outbox dump: `{'turn_seq': 1, 'session_id': '9b8e376e-...'}` — nincs `event_id` kulcs | direkt SQL `SELECT payload FROM session_jobs.outbox WHERE job_type='index_turn'` | — |
| Additív migráció (`session_status()`: job_type-aware union) után egy valódi, pending `index_turn` sor `pending_jobs = 1`-et ad | proven | `pytest ...::TestSessionStatusPendingJobs::test_pending_jobs_counts_index_turn_outbox_row_after_fix` → `PASSED` | valódi Postgres, fix migráció alkalmazva, tényleges függvényhívás | — |
| Fix után a `pending_jobs` érték 0-ra csökken, amint a `run_indexing_batch()` lefut az adott `index_turn` sorra (a fix a STÁTUSZT követi, nem csak a job_type meglétét) | proven | `pytest ...::TestSessionStatusPendingJobs::test_pending_jobs_drops_to_zero_after_indexing_batch_runs` → `PASSED` | ua. | — |
| Fix után a `project_envelope` job_type továbbra is helyesen számolódik (regresszió-mentes a kontroll esetre) | proven | `pytest ...::TestSessionStatusPendingJobs::test_pending_jobs_counts_project_envelope_outbox_row` → `PASSED` (pending_jobs == 2, mert az első envelope projektált turnja maga is hagy egy pending `index_turn` sort — ez maga is bizonyítja, hogy a fix valóban job_type-aware, nem csak egy job_type-ra hardkódolt) | ua. | — |
| A két meglévő worker write-path teszt-suite-ja (test_turn_projector.py, test_chunk_indexer.py) regresszió-mentes az új migráció után | proven | `pytest tests/test_session_store/ -v` → `30 passed in 17.60s` | teljes suite futtatás, ugyanaz a Postgres instance, mindhárom SQL fájl alkalmazva | — |
| Minden `session_api.*` függvény `file:line` hivatkozása dokumentálva | proven | lásd "Decisions Proposed" alatti táblázat | direkt fájlolvasás | — |

## Decisions Proposed

### `session_api.*` függvények file:line hivatkozása (input.md "6.")

| Függvény | Eredeti definíció | Módosítva-e ebben a jobban |
|---|---|---|
| `session_api.search_context` | `output/session-postgres-schema.sql:326-345` | IGEN — `output/session-retrieval-quality-migration.sql:51-69` (CREATE OR REPLACE, `plainto_tsquery('simple', ...)`) |
| `session_api.get_timeline` | `output/session-postgres-schema.sql:347-362` | NEM — tesztelve, helyesnek bizonyult, nem módosítva |
| `session_api.get_context_pack` | `output/session-postgres-schema.sql:364-379` | NEM — tesztelve, helyesnek bizonyult, nem módosítva |
| `session_api.session_status` | `output/session-postgres-schema.sql:381-400` | IGEN — `output/session-retrieval-quality-migration.sql:92-122` (CREATE OR REPLACE, job_type-aware union) |

### 1. FTS konfiguráció: `'simple'`/`'simple'`, nem `'english'`/`'english'`

Két irány állt rendelkezésre: (a) a `chunk_indexer` tsvector oldalát
`'english'`-re váltani, hogy egyezzen a `search_context()` tsquery
oldalával, vagy (b) a `search_context()` tsquery oldalát `'simple'`-re
váltani, hogy egyezzen a `chunk_indexer` tsvector oldalával. A (b)
megoldást választottam, mert:

- A session korpusz explicit kétnyelvű (CLAUDE.md bilingual convention,
  `chunk_indexer.py:274-279` saját indoklása ugyanezért választotta a
  `'simple'`-t a tsvector oldalon).
- Bizonyítottan (lásd Claim-Evidence Matrix) az `'english'` konfiguráció
  aktívan torzítja a magyar tokeneket (`futás` → `futá`).
- A `chunk_indexer.py` módosítása write-path kódváltozás lenne, ami nagyobb
  kockázat (worker mögötti, már élesben tesztelt logika), mint egy SQL
  függvény `CREATE OR REPLACE`-e.

**Elfogadott trade-off**: a `'simple'/'simple'` párosítás nem stemmel
SEMELYIK nyelven — a `"run"` query nem fog egyezni a `"running"`-ot
tartalmazó chunk-kal a fix UTÁN sem (lásd
`test_stemming_sensitive_query_matches_after_fix`, ami EZT a viselkedést
asszertálja, nem az ellenkezőjét). Ez dokumentált, szándékos korlátozás,
nem újabb hiba — lásd Risks.

### 2. `pending_jobs`: job_type-aware union, nem egy közös payload-minta

A `payload->>'event_id'` lookup azt feltételezte, hogy minden jövőbeli
outbox `job_type` payload-ja tartalmaz egy `event_id` kulcsot — ez hamis
feltételezés volt már a job indulásakor (az `index_turn` job_type-ra sosem
volt igaz). A job_type-aware union megoldás minden job_type-ot a saját
forrás-táblájához és saját session-azonosító útjához köti
(`project_envelope` → `session_raw.envelopes.provider_session_id`,
`index_turn` → `session_core.turns.session_id`), ahelyett hogy egy közös
payload-mezőt feltételezne.

## Rejected / Out Of Scope

- `chunk_indexer.py` tsvector konfigurációjának módosítása (`'english'`-re
  váltás) — elvetve, lásd "Decisions Proposed" 1.
- Nyelv-felismerés alapú, per-chunk FTS konfiguráció (pl. `langdetect`
  alapján `'hungarian'` vs `'english'` tsvector/tsquery választás) — ez egy
  retrieval-quality-tuning feladat, amit a "status indoklás" (input.md
  Target szekció) explicit egy jövőbeli, `candidate`-hez vezető nagyobb
  kiértékelésre utal, nem ide
- `session_idx.chunk_embeddings`-et lekérdező hibrid/cosine-similarity
  vektor-keresési API — explicit Nem cél, `session-vector-search-api-001`
  jövőbeli job
- `session_core.source_refs`/`session_idx.ranking_features` feltöltése —
  explicit Nem cél
- Nagy mennyiségű, valós session-adaton végzett retrieval-minőség
  benchmark — explicit Nem cél, `candidate`-hez kellene
- Az MCP szerver (`mcp-server/server.py`) átírása, hogy a `session_api.*`
  függvényeket hívja — explicit Nem cél

## Risks

1. **`'simple'/'simple'` FTS config nem stemmel semelyik nyelven** —
   elfogadott trade-off a kétnyelvű korpusz miatt, de azt jelenti, hogy a
   retrieval pontossága angol nyelvű, inflektált lekérdezésekre alacsonyabb
   lenne, mint egy egynyelvű, `'english'`-konfigurációjú rendszeren. Ha a
   jövőben a session korpusz túlnyomóan angol nyelvűvé válna, ezt a döntést
   újra kellene értékelni — ez egy `candidate`-hez vezető, nagyobb
   retrieval-quality kiértékelés feladata (lásd input.md "status
   indoklás").
2. **`session_status()` job_type-aware union nem auto-extensible** — egy
   jövőbeli, harmadik outbox `job_type` bevezetésekor a `pending_jobs`
   számítást explicit ki kell bővíteni egy új union-branch-csel; ha ez
   kimarad, ugyanaz az aluszámolási hiba megismétlődik az új job_type-ra.
   Nincs gépi kikényszerítés (pl. egy `CHECK` vagy teszt), ami ezt
   kikényszerítené egy új job_type bevezetésekor — csak emberi review
   észlelheti.
3. **Chunk-méret/retrieval-hangolás nem értékelt** — ez a job kizárólag
   korrektségi/integrációs validáció (a 4 függvény tényleg visszaadja-e a
   helyes sorokat helyes sorrendben/szűréssel), nem egy valós-méretű,
   sokszáz-turnos session-en végzett relevancia-/ranking-minőség kiértékelés
   — ahogy a Target "status indoklás" is jelzi, ez `candidate`-hez kellene.
4. **`ts_rank` érték nem validált** — a `search_context()` `rank` oszlopa
   (relevancia-pontszám) nincs explicit assertálva semelyik tesztben (csak
   azt teszteljük, hogy a megfelelő `chunk_id` szerepel-e az
   eredményhalmazban) — több, hasonlóan releváns chunk közötti sorrendezést
   ez a job nem vizsgálta.
5. **A `prefix_repro` scratch DB-vel végzett pre-fix bizonyítás külön
   adatbázisban történt** (nem a `feature/session-retrieval-quality-001`
   branch végleges `testdb`-jén), mert a fő `testdb`-n a fix migráció már
   alkalmazva volt a teszt-suite fejlesztésekor — ez nem csökkenti a
   bizonyíték erejét (ugyanaz a schema+migráció sorrend, ugyanaz a valódi
   lánc), de dokumentálandó eltérés a reprodukciós módszerben.

## Definition Of Done Check

- [x] kétnyelvű, valódi-láncon-átmenő fixture létrehozva és dokumentálva —
  `TestBilingualFixtureRealChain`, `PASSED`
- [x] `search_context()` egzakt-szó teszt lefuttatva, kimenet idézve —
  `TestSearchContextExactMatch`, lásd Claim-Evidence Matrix
- [x] `search_context()` stemming-érzékeny teszt lefuttatva, kimenet idézve,
  a TÉNYLEGES eredmény explicit kimondva — PRE-FIX: MISS minden query-re
  (még egzaktra is), lásd Claim-Evidence Matrix
- [x] additív migráció implementálva és újra-tesztelve, kimenet idézve —
  `output/session-retrieval-quality-migration.sql` 1. szekció,
  `TestSearchContextStemmingMismatch`/`TestSearchContextExactMatch` PASSED
  a fix után
- [x] `get_timeline()`/`get_context_pack()` teszt lefuttatva, helyes
  sorrend asszertálva, kimenet idézve — `TestTimelineAndContextPack`,
  `PASSED`
- [x] `session_status()` `pending_jobs` teszt lefuttatva `index_turn`
  outbox-sorral, a TÉNYLEGES eredmény explicit kimondva — PRE-FIX:
  aluszámolás (`pending_jobs = 0` 1 valódi pending sor mellett)
- [x] additív migráció implementálva és újra-tesztelve, kimenet idézve —
  `output/session-retrieval-quality-migration.sql` 2. szekció,
  `TestSessionStatusPendingJobs` PASSED a fix után
- [x] minden `session_api.*` függvény `file:line` hivatkozása dokumentálva —
  lásd "Decisions Proposed" táblázat
- [x] claim-evidence tábla kitöltve, nem üres

## Next Jobs

- `session-vector-search-api-001` — `session_idx.chunk_embeddings`
  hibrid/cosine-similarity lekérdező API (explicit Nem cél itt)
- Egy jövőbeli, nagyobb retrieval-quality kiértékelési job, ami a
  `'simple'/'simple'` FTS trade-offot, a chunk-méret/overlap hangolást és a
  `ts_rank` relevancia-sorrendezést egy valós-méretű, többszáz-turnos
  session-en méri — ez kellene a `candidate` státuszhoz (lásd Target
  "status indoklás")
- Ha a jövőben új outbox `job_type` kerül bevezetésre: a
  `session_status().pending_jobs` union-t bővíteni kell egy új branch-csel
  (lásd Risks 2.) — ezt a következő, az adott job_type-ot bevezető capability
  jobnak kell elvégeznie, nem egy külön session-retrieval jobnak
