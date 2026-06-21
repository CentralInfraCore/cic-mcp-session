# session-source-refs-api-001 Output

## Scope

Ez a job megírja a `cic-mcp-session` repo session rétegének ELSŐ `session_api`
függvényét, ami a `session_core.source_refs` táblát olvassa:
`session_api.get_source_refs(p_session_id UUID, p_ref_kind TEXT DEFAULT NULL,
p_limit INTEGER DEFAULT 100)`. A `source_refs` táblát a
`session-source-refs-extractor-001` job (`session_store/chunk_indexer.py:
extract_source_refs()`) tölti fel `tool_call`/`file`/`url` provenance-sorokkal, de
eddig SEMMI nem olvasta `session_api`-n keresztül — az architektúra szabálya
(`architecture.md`: "Az MCP szerver ne tablakat turkaljon. Stabil API fuggvenyeket
hivjon") megköveteli ezt a hidat, ez a job zárja be a hiányt a `source_refs`
táblára.

A scope-on belül:
- additív SQL migráció (`output/session-source-refs-api-migration.sql`), a
  meglévő 5 SQL fájl (séma + 4 migráció) felülírása nélkül
- a session-scoping join megválasztása (`source_refs.chunk_id -> chunks.session_id`,
  egy hop, mivel a `source_refs` táblának nincs direkt `session_id` oszlopa)
- két-session-es fixture a VALÓDI `insert_envelope -> turn_projector ->
  chunk_indexer` láncon keresztül
- mindhárom bizonyítási eset (NULL-filter, kind-filter, session-scoping) tényleges
  SQL-kimenettel
- a teljes meglévő `tests/test_session_store/` suite regresszió-mentes futtatása

Nem cél (lásd input.md "Nem cél"): MCP szerver átírása, recall-/pontosság-mérés
valós session-adaton, `session_idx.ranking_features` feltöltése,
`search_context()`/`search_context_hybrid()` bővítése `source_refs`-szel.

## Inputs Read

- `output/session-postgres-schema.sql` — TELJESEN elolvasva: `session_core.source_refs`
  DDL (180-187. sor, nincs direkt `session_id` oszlop, csak `chunk_id` FK), és a 4
  meglévő `session_api.*` függvény (`search_context` 326-345, `get_timeline` 347-362,
  `get_context_pack` 364-379, `session_status` 381-400. sor) stílusmintája —
  `LANGUAGE sql STABLE`, `RETURNS TABLE`, paraméter-default-ok, a `search_context()`
  egy-hopos `chunks.session_id = p_session_id` szűrés-mintáját követtem
- `session_store/chunk_indexer.py` — TELJESEN elolvasva: `extract_source_refs()`
  (278-317. sor), a `ref_kind` lehetséges értékei (`tool_call`, `file`, `url`,
  139-143. sor konstansok: `TOOL_CALL_ROLE`, `TOOL_NAME_KEY`, `FILE_PATH_KEYS`,
  `NESTED_TOOL_INPUT_KEY`, `URL_PATTERN`)
- `tests/test_session_store/test_chunk_indexer.py` — TELJESEN elolvasva: a 4-eseti
  `source_refs` fixture-minta (Eset A `test_case_a_tool_call_payload_...` 489-513,
  Eset B `test_case_b_file_path_payload_...` 516-541, Eset C
  `test_case_c_url_in_text_...` 544-566, Eset D `test_case_d_nothing_extractable_...`
  569-597. sor) — a saját két-session-es fixture-öm Eset A/B/C payload-jait
  szó szerint újrahasználja
- `tests/test_session_store/test_session_api.py` — TELJESEN elolvasva: a
  `_valid_envelope`/`_run_chain_for_envelope`/`_get_session_id`/`_clean_tables`
  helper-minta, amit a saját teszt-modulom követ
- `output/session-chunk-indexer-migration.sql`, `output/session-retrieval-quality-migration.sql`,
  `output/session-vector-search-api-migration.sql`, `output/session-hybrid-search-api-migration.sql`
  — a fej-kommentekből az alkalmazási sorrend (séma → chunk-indexer →
  retrieval-quality → vector-search-api → hybrid-search-api → ÚJ source-refs-api)
- `.cic-context/factory-docs/job-slices.yaml` `session-source-refs-api-001` bejegyzés
  — a goal/forbidden_shortcuts megegyezik az input.md-vel, nincs eltérés

## Findings

**1. Valódi Postgres lánc.** `pgvector/pgvector:pg16` konténer indítva
(`docker run -d --name session-source-refs-api-test -p 55435:5432 ...`),
`pg_isready` 3 másodperc után "accepting connections". Mind az 5 meglévő SQL fájl
egymás után, hibamentesen alkalmazva (`ON_ERROR_STOP=1`, mindegyik végén
`CREATE FUNCTION`/`CREATE TRIGGER`/`CREATE TABLE` sikeres kimenettel, nincs hiba):
`session-postgres-schema.sql` → `session-chunk-indexer-migration.sql` →
`session-retrieval-quality-migration.sql` → `session-vector-search-api-migration.sql` →
`session-hybrid-search-api-migration.sql` → **`session-source-refs-api-migration.sql`**
(ÚJ), utolsóként, ugyanazon az instance-en. Az új migráció psql kimenete:

```
CREATE FUNCTION
COMMENT
```

**2. `session_api.get_source_refs()` definíció.**
`output/session-source-refs-api-migration.sql:36-58`. A `source_refs` táblának
nincs direkt `session_id` oszlopa (`session-postgres-schema.sql:180-187`), csak
`chunk_id` FK — a `session_core.chunks` táblának VISZONT van direkt `session_id`
oszlopa (`session-postgres-schema.sql:167-178`), ezért a scoping join
`source_refs -> chunks` egy hop, ugyanazt a denormalizációt használva, mint a
meglévő `session_api.search_context()` (`session-postgres-schema.sql:339-341`).
A `p_ref_kind IS NULL OR r.ref_kind = p_ref_kind` mintát követi a függvény —
NULL paraméter = minden kind, nem-NULL = pontos egyezés. A `session_id` szűrés
MINDIG aktív (nem opcionális), ez a cross-session szivárgás elleni garancia
(lásd "Forbidden Shortcuts").

**3. Két-session-es fixture a valódi láncon.** Session 1
(`provider_session_id="sess-source-refs-001"`) 3 turn-t kapott, az
`extract_source_refs()` Eset A/B/C payload-jaival szó szerint:
- Eset A (`tool_call`): `provider_event_name="PostToolUse"`,
  `payload={"raw_text": "ran a tool", "tool_name": "Read"}`
- Eset B (`file`): `provider_event_name="PostToolUse"`,
  `payload={"raw_text": "edited a file", "tool_input": {"file_path":
  "/workspace/session_store/chunk_indexer.py"}}`
- Eset C (`url`): `payload={"raw_text": "see the docs at
  https://example.com/docs for details"}`

Session 2 (`provider_session_id="sess-source-refs-002"`) 1 turn-t kapott, `file`
kind-dal, de Session 1-től ELTÉRŐ `ref_value`-vel:
`payload={"raw_text": "edited a different file", "tool_input": {"file_path":
"/workspace/session_store/turn_projector.py"}}`.

Mindhárom Session 1 envelope és a Session 2 envelope a valódi
`insert_envelope() -> run_projection_batch() -> run_indexing_batch()` láncon
ment át (`session_store.envelope_writer`, `session_store.turn_projector`,
`session_store.chunk_indexer` — nem mockolt, nem kézzel beszúrt sor). A fixture
épülése igazolva (`tests/test_session_store/test_session_source_refs_api.py::
test_fixture_builds_through_real_chain_and_produces_three_kinds_for_session1`):
Session 1-re pontosan 3 `source_refs` sor (egy/kind), Session 2-re pontosan 1.

**4. Bizonyítás — NULL-filter (mind a 3 kind, Session 2 kizárva).** Valódi
psql lekérdezés ugyanazon az instance-en, `session1_id =
4f75cfab-e490-4981-90c4-49d40b0f95b9`:

```sql
SELECT source_ref_id, ref_kind, ref_value FROM session_api.get_source_refs('4f75cfab-e490-4981-90c4-49d40b0f95b9', NULL) ORDER BY source_ref_id;
```

```
 source_ref_id | ref_kind  |                 ref_value
---------------+-----------+-------------------------------------------
            21 | tool_call | Read
            22 | file      | /workspace/session_store/chunk_indexer.py
            23 | url       | https://example.com/docs
(3 rows)
```

Mind a 3 ref_kind visszajött, Session 2 `ref_value`-ja
(`/workspace/session_store/turn_projector.py`) NEM jelenik meg a sorok között.

**5. Bizonyítás — kind-filter (csak `file`).** Ugyanaz az instance,
`session1_id` ugyanaz:

```sql
SELECT source_ref_id, ref_kind, ref_value FROM session_api.get_source_refs('4f75cfab-e490-4981-90c4-49d40b0f95b9', 'file') ORDER BY source_ref_id;
```

```
 source_ref_id | ref_kind |                 ref_value
---------------+----------+-------------------------------------------
            22 | file     | /workspace/session_store/chunk_indexer.py
(1 row)
```

Pontosan 1 sor jött vissza, `ref_kind='file'`, a `tool_call` és `url` sorok
NEM jelennek meg.

**6. Bizonyítás — session-scoping (Session 2 lekérdezése nem látja Session 1
sorait).** Ugyanaz az instance, `session2_id =
74f63925-3f73-48db-b25e-c049e361e33e`:

```sql
SELECT source_ref_id, ref_kind, ref_value FROM session_api.get_source_refs('74f63925-3f73-48db-b25e-c049e361e33e', NULL) ORDER BY source_ref_id;
```

```
 source_ref_id | ref_kind |                 ref_value
---------------+----------+--------------------------------------------
            24 | file     | /workspace/session_store/turn_projector.py
(1 row)
```

Pontosan 1 sor (Session 2 saját file-referenciája), Session 1 mindhárom
`ref_value`-ja (`Read`, `/workspace/session_store/chunk_indexer.py`,
`https://example.com/docs`) NEM jelenik meg — a session-scoping join helyesen
zár ki cross-session adatot.

**7. Automatizált teszt-suite, ugyanezekkel az esetekkel.**
`tests/test_session_store/test_session_source_refs_api.py` 5 teszttel ismétli
meg ugyanezt programozottan (`_call_get_source_refs()` helper, valódi psycopg
kapcsolaton):

```
tests/test_session_store/test_session_source_refs_api.py::test_fixture_builds_through_real_chain_and_produces_three_kinds_for_session1 PASSED
tests/test_session_store/test_session_source_refs_api.py::test_null_filter_returns_all_three_kinds_for_session1_excludes_session2 PASSED
tests/test_session_store/test_session_source_refs_api.py::test_kind_filter_returns_only_file_rows PASSED
tests/test_session_store/test_session_source_refs_api.py::test_session_scoping_session2_query_excludes_session1_rows PASSED
tests/test_session_store/test_session_source_refs_api.py::test_limit_parameter_caps_returned_rows PASSED
```

**8. Regresszió-ellenőrzés.** A TELJES `tests/test_session_store/` suite
lefuttatva ugyanazon az instance-en (séma + mind a 6 migráció alkalmazva), a
saját 5 új teszttel együtt:

```
============================= test session starts ==============================
collected 59 items
...
============================= 59 passed in 24.77s ==============================
```

Minden `chunk_indexer`/`envelope_writer`/`turn_projector`/`session_api`/
`vector_search`/`hybrid_search`/`worker_loop` teszt zöld — nincs regresszió.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Mind az 5 meglévő SQL fájl + az ÚJ migráció hibamentesen alkalmazható egymás után egy valódi Postgres instance-en | proven | psql `ON_ERROR_STOP=1` kimenet mind a 6 fájlra, utolsó: `CREATE FUNCTION` / `COMMENT` | tényleges `docker exec ... psql` futtatás, valódi pgvector/pg16 konténer | alacsony — additív, nincs DROP/ALTER a meglévő objektumokon |
| `session_api.get_source_refs(p_session_id, p_ref_kind, p_limit)` létezik, a meglévő `session_api.*` stílusmintát követi | proven | `output/session-source-refs-api-migration.sql:36-58`, `LANGUAGE sql STABLE`, `RETURNS TABLE`, default paraméterek, mintaegyezés `search_context()`-tel (`session-postgres-schema.sql:326-345`) | fájl:sor hivatkozás + sikeres `CREATE FUNCTION` psql kimenet | alacsony |
| Két-session-es fixture létrehozva a VALÓDI `insert_envelope()` láncon (Session 1: 3 kind, Session 2: 1 eltérő ref_value) | proven | `test_fixture_builds_through_real_chain_and_produces_three_kinds_for_session1` PASSED, Session 1 count=3, Session 2 count=1 | pytest futtatás valódi Postgres ellen, nem mockolt | alacsony |
| NULL-filter: mind a 3 kind visszajön Session 1-re, Session 2 sora kizárva | proven | psql kimenet 3 sorral (`tool_call`/`file`/`url`), Session 2 `ref_value` nem szerepel | tényleges `SELECT ... FROM session_api.get_source_refs(session1_id, NULL)` psql lekérdezés | alacsony |
| Kind-filter: csak a `file` kind sora jön vissza | proven | psql kimenet 1 sorral, `ref_kind='file'` | tényleges `SELECT ... FROM session_api.get_source_refs(session1_id, 'file')` psql lekérdezés | alacsony |
| Session-scoping: Session 2 lekérdezése csak saját sorát adja vissza, Session 1 sorai nem szivárognak át | proven | psql kimenet 1 sorral (`turn_projector.py` ref_value), Session 1 mindhárom ref_value-ja hiányzik | tényleges `SELECT ... FROM session_api.get_source_refs(session2_id, NULL)` psql lekérdezés | alacsony — ez a legkritikusabb bizonyítás (cross-session leak guard), explicit negatív assertion is van rá a tesztben |
| `p_limit` paraméter érvényesül | proven | `test_limit_parameter_caps_returned_rows` PASSED, `limit=1` → 1 sor 3 elérhető helyett | pytest futtatás valódi Postgres ellen | alacsony |
| Teljes meglévő `tests/test_session_store/` suite regresszió-mentes | proven | `59 passed in 24.77s`, minden modul (chunk_indexer, envelope_writer, turn_projector, session_api, vector_search, hybrid_search, worker_loop) zöld | tényleges pytest futtatás, ugyanazon instance-en, az új migrációval együtt | alacsony |
| `session_id`-szűrés nélküli (cross-session-szivárgó) függvény NEM készült | proven | a migráció egyetlen függvénye is kötelezően `WHERE c.session_id = p_session_id`-t tartalmaz, nincs alternatív/bypass függvény a fájlban | kódolvasás + a session-scoping teszt explicit negatív assertion-jei | alacsony |

## Decisions Proposed

- **Egy-hopos join (`source_refs -> chunks`), NEM két-hopos
  (`source_refs -> chunks -> turns -> sessions`)** — mivel a `session_core.chunks`
  táblának VAN direkt `session_id` oszlopa (denormalizált, lásd
  `session-postgres-schema.sql:171`), a `search_context()` függvény is ezt
  használja. Konzisztens a meglévő mintával, és egy join-nal kevesebb.
- **`ORDER BY source_ref_id ASC`** — a meglévő 4 függvény mindegyike valamilyen
  determinisztikus sorrendet ad (turn_seq, chunk_seq, rank), a `get_source_refs()`
  a beszúrási sorrendet (`source_ref_id`, `BIGSERIAL`) választja, mivel nincs
  természetes "rangsor" provenance-referenciák között — ez determinisztikus és
  stabil ismételt hívások között.
- **`status_after_merge: experimental`** (a Target szekció szerint) — egy
  konstruált, két-session-es fixture-rel bizonyítva, `candidate`-hez nagyobb,
  valós session-adaton végzett kiértékelés kellene (lásd input.md "Target" és
  "status indoklás").

## Rejected / Out Of Scope

- MCP szerver átírása, hogy a `get_source_refs()`-et hívja — külön, jövőbeli
  capability-job (lásd input.md "Nem cél")
- recall-/pontosság-mérés valós session-adaton — ehhez a job konstruált
  fixture-je nem elégséges alap, külön job kellene nagyobb, valós adattal
- `session_idx.ranking_features` feltöltése — nem ehhez a jobhoz tartozik,
  más capability-szelet
- `search_context()`/`search_context_hybrid()` bővítése, hogy `source_refs`-et
  is visszaadjon — explicit jövőbeli döntés, ha egyáltalán szükséges lesz,
  nem ennek a jobnak a feladata

## Risks

- **Konstruált fixture, nem valós session-adat.** A két session mindössze
  4 turn-t/4 source_refs sort tartalmaz — ez bizonyítja a függvény
  HELYESSÉGÉT (filter logika, scoping), de NEM bizonyít semmit teljesítményről
  nagy `source_refs` táblán (nincs `EXPLAIN`/index-stratégia vizsgálat ebben
  a jobban, mivel a migráció nem ad hozzá új indexet — a meglévő
  `chunks.chunk_id` PK és a `source_refs.chunk_id` FK-index a meglévő
  `session-postgres-schema.sql`-ből származik, ezt a job nem módosította).
- **`p_limit` nincs felső korlátra védve** (pl. nincs `LEAST(p_limit, 1000)`
  típusú clamp) — ugyanez a minta a meglévő 4 függvénynél is, nem ÚJ
  kockázat, de érdemes egy jövőbeli "API hardening" jobban egységesen
  kezelni az összes `session_api.*` függvényre.
- **`status_after_merge: experimental`** — lásd "Decisions Proposed", a
  `candidate` státuszhoz nagyobb mintán végzett kiértékelés szükséges, ez
  explicit limitáció, nem hiba.

## Definition Of Done Check

- [x] additív migráció alkalmazva, kimenet idézve — Findings 1.
- [x] `get_source_refs()` létezik, a meglévő `session_api.*` stílusmintát követi,
      fájl:sor hivatkozással — Findings 2. (`output/session-source-refs-api-migration.sql:36-58`)
- [x] két-session-es fixture létrehozva a VALÓDI láncon — Findings 3.
- [x] NULL-filter eset bizonyítva (mind a 3 kind, Session 2 kizárva), kimenet idézve — Findings 4.
- [x] specifikus kind-filter eset bizonyítva, kimenet idézve — Findings 5.
- [x] session-scoping eset bizonyítva (Session 2 lekérdezése nem látja Session 1
      sorait), kimenet idézve — Findings 6.
- [x] teljes meglévő teszt-suite lefuttatva, regresszió-mentesség bizonyítva — Findings 8.
- [x] claim-evidence tábla kitöltve, nem üres — lásd fent

## Next Jobs

- MCP szerver bekötése: egy jövőbeli capability-job, ami a `cic-mcp-session`
  MCP tool-jai közé felveszi a `get_source_refs()` hívást (jelenleg explicit
  "Nem cél" ebben a jobban)
- Nagyobb, valós session-adaton végzett kiértékelés a `status_after_merge:
  candidate` előléptetéshez
- Ha jövőbeli igény mutatkozik rá: `search_context()`/`search_context_hybrid()`
  bővítése, hogy a `source_refs`-et is visszaadja a chunk mellett (ez most
  explicit "Nem cél")
