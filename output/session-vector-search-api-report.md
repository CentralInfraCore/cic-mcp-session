# session-vector-search-api-001 Output

## Scope

Ez a job megírta az ELSŐ SQL függvényt, ami tényleg lekérdezi a
`session_idx.chunk_embeddings` táblát: `session_api.search_context_vector()`,
cosine-similarity vektor-keresés a meglévő HNSW indexen (`<=>` operátor,
`vector_cosine_ops`). A query-szöveg → vektor átalakítást egy Python helper
(`session_store/vector_search.py:embed_query()`) végzi, ami a MEGLÉVŐ
`session_store/chunk_indexer.py:embed_texts()`-et hívja (nem ír újra
modell-betöltő/-hívó kódot). A munka a teljes láncot (schema + chunk-indexer
migráció + retrieval-quality migráció + az új migráció) egy valódi
`pgvector/pgvector:pg16` Postgres instance ellen, egyetlen menetben futtatta
le és validálta.

Nem cél (a job specifikációja szerint, be nem vállalva): hibrid (FTS+vektor)
ranking, `session_core.source_refs`/`session_idx.ranking_features` feltöltés,
nagy mennyiségű valós adaton végzett teljesítmény-/index-skálázódási teszt,
az MCP szerver átírása, permanens futtatási infrastruktúra.

## Inputs Read

- `output/session-postgres-schema.sql` — `session_idx.chunk_embeddings` DDL,
  `idx_session_idx_chunk_embeddings_hnsw` index, `session_api.search_context()`
  minta (320-345. sor)
- `output/session-chunk-indexer-migration.sql` — `VECTOR(1536)` →
  `VECTOR(384)` korrekció, `index_turn` outbox job_type
- `output/session-retrieval-quality-migration.sql` — additív
  `CREATE OR REPLACE FUNCTION` minta `session_api.search_context()`-en
- `session_store/chunk_indexer.py` — `embed_texts()` (194. sor),
  `_get_embedding_model()` (180. sor), `EMBEDDING_MODEL` (73. sor),
  `EXPECTED_EMBEDDING_DIM` (76. sor)
- `tests/test_session_store/test_session_api.py` — valódi-láncon-átmenő
  fixture minta (insert_envelope → turn_projector → chunk_indexer)
- `session_store/envelope_writer.py` — `SessionStoreConfig`, `insert_envelope()`

## Findings

1. **A `chunk_embeddings` tábla addig tényleg soha nem volt lekérdezve.** A
   `session-chunk-indexer-001` óta minden chunk-hoz írunk embedding sort, de
   `session_api.search_context()` kizárólag FTS-t (`tsvector`/`tsquery`)
   használ — ez a job az első, ami `SELECT ... FROM session_idx.chunk_embeddings`
   tartalmazó SQL-t futtat valódi adaton.
2. **A pgvector Python adapter (`pgvector` PyPI package) nincs telepítve és
   nem is szükséges.** A `<=>` operátor és a `VECTOR(384)` paraméter egy
   sima pgvector text-literal stringként (`'[0.1,0.2,...]'::vector`) átadva
   psycopg natív `TEXT`/string paraméterként működik — nincs szükség új
   futásidejű dependency-re a meglévő `psycopg[binary]` mellett. Lásd
   `session_store/vector_search.py:to_pgvector_literal()` docstring a teljes
   indoklásért.
3. **A repo-nak nem volt `p_venv`-je** (a `CLAUDE.md` szerint `make deps`
   Dockerrel hozná létre) — ezt a job hozta létre lokálisan
   (`python3 -m venv p_venv && pip install -r requirements.txt`), mert a
   `requirements.txt` már tartalmazza a szükséges `psycopg[binary]==3.3.4` és
   `sentence-transformers==5.6.0` csomagokat, csak még senki nem telepítette
   ebbe a klónba.
4. **A `tests/test_session_store/` modulok futtatásához `PYTHONPATH=.`
   szükséges** — ez a meglévő `tests/test_session_store/test_session_api.py`
   futtatásakor is ugyanígy hibázik import nélkül (`ModuleNotFoundError: No
   module named 'session_store'`), tehát ez egy preegzisztáló, jobtól
   független környezeti tény, nem ezen job hibája — ezzel a beállítással
   mind a 9 meglévő `test_session_api.py` teszt, mind a 6 új
   `test_vector_search.py` teszt, mind a teljes `tests/test_session_store/`
   suite (36 teszt) lefutott és zöld.
5. **EXPLAIN a 2-soros fixture-nél `Seq Scan`-t választott, NEM HNSW Index
   Scan-t** (lásd "Claim-Evidence Matrix" pontos psql kimenet) — ez a
   pgvector/Postgres planner dokumentált, elvárt viselkedése ilyen kis
   sorszámnál (a HNSW index csak akkor nyer a sequential scan felett, ha a
   traverzálás költsége alacsonyabb, mint a teljes (itt: 2 soros) tábla
   beolvasása). A `idx_session_idx_chunk_embeddings_hnsw` index létezik és
   szintaktikailag kompatibilis (`vector_cosine_ops` pontosan a `<=>`
   operátorhoz illik), de ennek a job-nak a fixture-mérete mellett nem
   bizonyítható, hogy ténylegesen használatba kerül — ez explicit, nem
   hallgatott limitáció.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Az additív migráció (`output/session-vector-search-api-migration.sql`) hibátlanul alkalmazható a teljes lánc (schema + chunk-indexer-migráció + retrieval-quality-migráció) után | proven | `psql` kimenet: `CREATE FUNCTION` / `COMMENT` mindkét korábbi migráció után, hibátlanul | `docker exec -i ... psql -v ON_ERROR_STOP=1 < output/session-vector-search-api-migration.sql` → `CREATE FUNCTION`, `COMMENT` | low |
| `session_api.search_context_vector(p_session_id, p_query_embedding VECTOR(384), p_limit)` létezik, `session_core.chunks` sorokat ad vissza (`chunk_id, turn_id, text, similarity`), `<=>` operátorral rendezve, `p_session_id`-ra szűrve | proven | a SQL definíció `output/session-vector-search-api-migration.sql:45-62`; teszt-hívás sikeresen visszaad sorokat (lásd lentebb a szemantikai relevancia sorokat) | tényleges `SELECT ... FROM session_api.search_context_vector(...)` hívás, valódi Postgres ellen, `tests/test_session_store/test_vector_search.py::TestSemanticRelevance` | low |
| `embed_query()` helper létezik, a MEGLÉVŐ `chunk_indexer.embed_texts()`-et hívja (nem új modell-betöltő kód) | proven | `session_store/vector_search.py:39-58` — `embed_query()` testje: `[vector] = embed_texts([text])`, ahol `embed_texts` importálva `session_store.chunk_indexer`-ből (`session_store/vector_search.py:30`) | kódolvasás + futtatás: `tests/test_session_store/test_vector_search.py::TestEmbedQueryDimension` PASSED | low |
| `embed_query()` TÉNYLEGES kimeneti dimenziója megegyezik 384-gyel | proven | pytest kimenet: `test_embed_query_output_dimension_matches_chunk_embeddings_column PASSED`; a teszt mind a Python-oldali `len(vector)`-t, mind a Postgres-oldali `vector_dims(embedding)`-et lekérdezi egy valódi beillesztett soron, és mindkettő `384` | `p_venv/bin/pytest tests/test_session_store/test_vector_search.py::TestEmbedQueryDimension -v` → `1 passed` | low |
| Két, szemantikailag jól elkülönülő témájú teszt-fixture létezik, a VALÓDI láncon keresztül (insert_envelope → turn_projector → chunk_indexer) | proven | `tests/test_session_store/test_vector_search.py::TestTwoTopicFixtureRealChain::test_fixture_builds_through_real_chain_and_produces_expected_rows PASSED` — Topic A (Postgres-migráció szöveg) és Topic B (CSS-styling szöveg), mindkettő 1 chunk-ra bomlik, mindkettőnek van `chunk_embeddings` sora | valódi `insert_envelope()` + `run_projection_batch()` + `run_indexing_batch()` hívás, majd `session_core.chunks`/`session_idx.chunk_embeddings` lekérdezés | low |
| Topic A query (`"database schema migration and index rebuild"`) a Topic A chunk-ot rangsorolja ELSŐKÉNT | proven | `test_topic_a_query_ranks_topic_a_chunk_first PASSED`; assertion: `chunk_ids_in_order[0] == topic_a_chunk_id` ÉS `similarity[topic_a] > similarity[topic_b]` | tényleges `search_context_vector()` hívás, 2 sort ad vissza, sorrend asszertálva | low |
| Topic B query (`"CSS flexbox styling and button hover color"`) a Topic B chunk-ot rangsorolja ELSŐKÉNT | proven | `test_topic_b_query_ranks_topic_b_chunk_first PASSED`; assertion: `chunk_ids_in_order[0] == topic_b_chunk_id` ÉS `similarity[topic_b] > similarity[topic_a]` | tényleges `search_context_vector()` hívás, 2 sort ad vissza, sorrend asszertálva | low |
| `p_limit` paraméter korlátozza a visszaadott sorok számát | proven | `test_limit_parameter_caps_result_count PASSED` — `limit=1` → 1 sor | tényleges hívás `limit=1`-gyel | low |
| `EXPLAIN` kimenet dokumentálva, index-használat (vagy annak hiánya) explicit kimondva | proven | pytest `-s` kimenet (lásd lent, "EXPLAIN plan (2-row fixture)") — `Seq Scan on chunk_embeddings e (cost=0.00..17.50 rows=750 width=40)`, NEM `Index Scan` | tényleges `EXPLAIN SELECT ...` futtatás a valódi 2-soros fixture-ön | medium (a sequential-scan eredmény NEM bizonyítja, hogy a HNSW index nagyobb adatmennyiségnél is valóban használatba kerül — ez explicit out-of-scope, lásd "Risks") |
| `idx_session_idx_chunk_embeddings_hnsw` index szintaktikailag kompatibilis a `search_context_vector()` ORDER BY kifejezésével | proven | a `<=>` operátor pontosan az `vector_cosine_ops` opclass-hoz tartozik (pgvector dokumentáció + `output/session-postgres-schema.sql:255-256` index definíció); a planner FELISMERHETNÉ az indexet, de a 2-soros fixture-nél nem választja | kódolvasás (`CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`) + EXPLAIN-futtatás (lásd fenti sor) | medium |
| Reachability: a `embed_query()` helper-nek 0 KÜLSŐ (nem-teszt) Python hívója van | proven | `grep -rn "embed_query" --include="*.py" . \| grep -v "test_" \| grep -v "/tests/"` → csak `session_store/vector_search.py:39` (definíció) és `:86` (`__all__`), nincs külső caller | tényleges `grep` futtatás, kimenet idézve "Reachability" szekcióban | low (dokumentált `deadcode`/`scaffold`, szándékos a job "Nem cél" szerint) |
| Reachability: a `session_api.search_context_vector()` SQL függvénynek 0 production Python/MCP hívója van, csak a teszt hívja | proven | `grep -rn "search_context_vector" --include="*.py" --include="*.sql" .` → `output/session-vector-search-api-migration.sql:45` (definíció) + `tests/test_session_store/test_vector_search.py` (teszthívások); nincs `mcp-server/` vagy egyéb production caller | tényleges `grep` futtatás, kimenet idézve "Reachability" szekcióban | low (a `session_api.search_context()` mintával konzisztens — az is csak teszt-hívott ebben a rétegben) |
| A teljes `tests/test_session_store/` suite (régi + új) hibátlanul fut a migrációk alkalmazása után | proven | `36 passed in 21.62s` | `PYTHONPATH=. ... p_venv/bin/pytest tests/test_session_store/ -v` | low |

## EXPLAIN (idézett kimenet)

```
Limit  (cost=32.25..32.26 rows=4 width=60)
  ->  Sort  (cost=32.25..32.26 rows=4 width=60)
        Sort Key: ((e.embedding <=> '[...384 float...]'::vector))
        ->  Hash Join  (cost=12.69..32.21 rows=4 width=60)
              Hash Cond: (e.chunk_id = c.chunk_id)
              ->  Seq Scan on chunk_embeddings e  (cost=0.00..17.50 rows=750 width=40)
              ->  Hash  (cost=12.64..12.64 rows=4 width=48)
                    ->  Bitmap Heap Scan on chunks c  (cost=4.18..12.64 rows=4 width=48)
                          Recheck Cond: (session_id = '...'::uuid)
                          ->  Bitmap Index Scan on idx_session_core_chunks_session_id ...
```

`uses_seq_scan=True uses_hnsw_index_scan=False` — a Postgres planner a 2-soros
`chunk_embeddings` táblánál (a `rows=750` egy globális statisztika-becslés,
nem a tényleges sorszám — a tábla a teszt idején valójában 2 sort
tartalmazott a `_clean_tables` fixture truncate-je után) sequential scant
választott a HNSW index helyett. Ez **dokumentált, elvárt jelenség** kis
sorszámnál — a pgvector HNSW indexe csak akkor versenyképes a planner
számára, ha a táblaméret elég nagy ahhoz, hogy az index-traverzálás
költsége alacsonyabb legyen a teljes tábla beolvasásánál. Ez NEM bizonyítja,
hogy a `idx_session_idx_chunk_embeddings_hnsw` index hibás vagy
inkompatibilis — a `<=>` operátor és a `vector_cosine_ops` opclass pontosan
egyezik az index definíciójával (`output/session-postgres-schema.sql:255-256`),
csak ennél a job-nál nincs elég sor ahhoz, hogy a planner valóban
választaná. Nagy-méretű index-használati teszt explicit ki van zárva
("Nem cél").

## Reachability

```
$ grep -rn "embed_query" --include="*.py" . | grep -v "test_" | grep -v "/tests/"
session_store/vector_search.py:39:def embed_query(text: str) -> list[float]:
session_store/vector_search.py:86:__all__ = ["embed_query", "to_pgvector_literal", "EXPECTED_EMBEDDING_DIM"]
```

0 KÜLSŐ (nem-definíciós, nem-teszt) caller — `deadcode`/`scaffold` státusz,
szándékos: az "Nem cél" szekció explicit kizárja az MCP szerver átírását,
hogy ezt a függvényt hívja.

```
$ grep -rn "search_context_vector" --include="*.py" --include="*.sql" .
output/session-vector-search-api-migration.sql:45:CREATE OR REPLACE FUNCTION session_api.search_context_vector(
output/session-vector-search-api-migration.sql:65:COMMENT ON FUNCTION session_api.search_context_vector(UUID, VECTOR(384), INTEGER) IS
output/session-vector-search-api-migration.sql:91:-- DROP FUNCTION IF EXISTS session_api.search_context_vector(UUID, VECTOR(384), INTEGER);
session_store/vector_search.py:2,9,25: (docstring references only, no call)
tests/test_session_store/test_vector_search.py:217: "FROM session_api.search_context_vector(%s, %s::vector, %s)"
```

Egyetlen tényleges SQL-hívás van: `tests/test_session_store/test_vector_search.py:217`,
a `_call_search_context_vector()` helperben. Nincs `mcp-server/`-beli vagy
egyéb production hívó — ez konzisztens a `session_api.search_context()`
mintájával, amelynek production hívója (MCP szerver wiring) ebben a rétegben
összesen még nincs (lásd `output/session-postgres-schema.sql` és
`output/session-retrieval-quality-report.md` korábbi job-ok azonos
státusza).

## Decisions Proposed

1. **pgvector text-literal formázás psycopg-n keresztül, NEM a `pgvector`
   PyPI adapter.** `session_store/vector_search.py:to_pgvector_literal()`
   egy `'[0.1,0.2,...]'` formátumú stringet ad vissza, amit a SQL hívásban
   `%s::vector`-ként castolunk. Indoklás: a `pgvector` package egy új
   runtime dependency-t jelentene egyetlen hívási pontért (ez a helper +
   tesztje), miközben a pgvector natívan elfogadja a text-literal inputot
   bármely `vector`-típusú paraméterre. Ha egy jövőbeli job sok helyen
   hívná ezt ismétlődően (pl. nagy batch-elt vektor-beillesztésnél), a
   `pgvector.psycopg.register_vector()` adapter ergonomikusabb lenne — ez
   akkor mérlegelendő újra.
2. **`similarity = 1 - cosine_distance`, de az ORDER BY a raw `<=>`
   kifejezésen marad, nem a derived `similarity` oszlopon.** Ez biztosítja,
   hogy a planner az ismert `<=>`/`vector_cosine_ops` operátor-mintát lássa
   az ORDER BY-ban (ez az, amit egy index-scan-matching planner felismerne
   nagyobb táblánál), míg a kimenő oszlop a hívó számára "magasabb = jobb"
   szemantikát ad, konzisztensen a `search_context()` `rank` oszlopával.
3. **A query-szöveg → embedding konverziót Python végzi, a SQL függvény
   KÉSZ vektort fogad.** Ez nem volt választható alternatíva — SQL/plpgsql-ből
   nincs mód lokális embedding-modell hívásra — de explicit dokumentálva van
   a migrációs fájl fejrészében és a helper docstring-jében, hogy ez egy
   architekturális kényszer, nem stilisztikai döntés.

## Rejected / Out Of Scope

- Hibrid (FTS + vektor) ranking-stratégia — külön, jövőbeli job (a job
  explicit "Nem cél"-ja).
- `session_core.source_refs` / `session_idx.ranking_features` feltöltése —
  nem ennek a jobnak a hatóköre.
- Nagy mennyiségű, valós session-adaton végzett teljesítmény-/
  index-skálázódási teszt — ezért a `status_after_merge: experimental`
  (nem `candidate`), lásd input.md "status indoklás".
- Az MCP szerver (`mcp-server/server.py`) átírása, hogy a `search_context_vector()`-t
  hívja — explicit ki van zárva, a függvény jelenleg csak a saját
  pytest-jén keresztül elérhető (lásd "Reachability").
- `pgvector` PyPI package hozzáadása a `requirements.in`-hez — a
  text-literal formázás (lásd "Decisions Proposed" 1.) megkerüli ezt a
  szükségletet ennél a hívásszámnál.

## Risks

1. **A HNSW index ténylegesen-használt státusza bizonyítatlan ennél a
   fixture-méretnél.** Az `EXPLAIN` futtatás csak azt bizonyítja, hogy a
   szintaxis kompatibilis (a `<=>` operátor egyezik a `vector_cosine_ops`-szal),
   de NEM azt, hogy a planner egy realisztikus, sok-soros táblánál is
   tényleg a HNSW index-scan-t választaná, vagy hogy az index-scan
   ténylegesen helyesen rangsorol nagy adatmennyiségnél (HNSW egy
   approximate-NN algoritmus — nem garantál egzakt top-k sorrendet nagy N
   esetén, ellentétben a brute-force sequential scan-nel, amit itt
   láttunk). Ez a job explicit nem vállalta ezt a bizonyítást ("Nem cél":
   "nagy mennyiségű, valós session-adaton végzett teljesítmény-/
   index-skálázódási teszt") — innen a `experimental` (nem `candidate`)
   státusz.
2. **A `to_pgvector_literal()` string-formázás `repr(float(x))`-et használ**,
   ami Python lebegőpontos repr-formátumot ad (pl. `0.1` helyett akár
   `0.10000000149011612` típusú hosszabb stringeket nagy pontosságú
   float32 → repr konverziónál) — ez működik (a tesztek bizonyítják), de
   hosszabb SQL-paramétert eredményez, mint egy kerekített formázás. Nem
   bizonyított teljesítmény-probléma ennél a hívásszámnál, de nagy
   batch-elt hívásnál érdemes lenne kerekítésre váltani (pl. 6 tizedesjegy).
3. **Két topic disztinkt elválasztása csak EGY query-pár ellen bizonyított**
   (Topic A/B query → Topic A/B chunk elsősége). Nem bizonyított, hogy a
   modell minden lehetséges query-megfogalmazásra ugyanilyen tisztán
   elválasztja a két témát — ez egy minimum-viable szemantikai bizonyíték,
   nem egy átfogó retrieval-quality kiértékelés (az explicit ki van zárva,
   lásd "Nem cél").

## Definition Of Done Check

- [x] additív migráció (`output/session-vector-search-api-migration.sql`)
      alkalmazva, kimenet idézve — `CREATE FUNCTION`, `COMMENT`
- [x] `embed_query()` helper létezik, a meglévő `chunk_indexer.embed_texts()`-et
      hívja (NEM új modell-betöltő kód), fájl:sor hivatkozással —
      `session_store/vector_search.py:39-58`, hívja
      `session_store/chunk_indexer.py:194` `embed_texts()`-et
- [x] `embed_query()` TÉNYLEGES kimeneti dimenziója lekérdezve, megegyezik
      384-gyel — `test_embed_query_output_dimension_matches_chunk_embeddings_column PASSED`
- [x] két-témájú, valódi-láncon-átmenő fixture létrehozva —
      `TestTwoTopicFixtureRealChain PASSED`
- [x] mindkét témára lefuttatott szemantikai-relevancia teszt, kimenet
      idézve, a várt chunk elsősége asszertálva — `TestSemanticRelevance`
      (2 teszt) PASSED
- [x] `EXPLAIN` kimenet idézve, az index-használat (vagy annak hiánya kis
      méretnél) explicit kimondva — lásd "EXPLAIN (idézett kimenet)" szekció
- [x] reachability `grep -rn` eredmény idézve, `file:line` hivatkozással —
      lásd "Reachability" szekció
- [x] claim-evidence tábla kitöltve, nem üres

## Next Jobs

1. Hibrid (FTS + vektor) ranking-stratégia kidolgozása és kiértékelése —
   `session_api.search_context()` (FTS) és `search_context_vector()`
   (vektor) eredményeinek kombinálása egyetlen rangsorolt listává.
2. Nagy-méretű (sok session, sok chunk) index-használati és teljesítmény-teszt
   — annak bizonyítása, hogy a HNSW index valóban index-scan-t kap nagy N
   esetén, és hogy az approximate-NN pontossága elfogadható a vártakhoz
   képest. Ez szükséges a `candidate` státuszhoz.
3. `to_pgvector_literal()` kerekített (pl. 6 tizedesjegy) formázásra váltás,
   ha nagy batch-elt hívásszám válik relevánssá.
4. Az MCP szerver wiring — `search_context_vector()` és `embed_query()`
   production hívóvá tétele egy jövőbeli, explicit job-ban.
