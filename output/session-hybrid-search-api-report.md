# session-hybrid-search-api-001 Output

## Scope

Ez a job megírja a `cic-mcp-session` repo session rétegének ELSŐ hibrid (FTS+vektor)
`session_api` retrieval függvényét: `session_api.search_context_hybrid()`. A függvény
Reciprocal Rank Fusion-nel (RRF) kombinálja a már mergelt `session_api.search_context()`
(FTS, `'simple'`-konfiguráció, `ts_rank`) és `session_api.search_context_vector()`
(cosine-similarity) függvények rangsorát, ANÉLKÜL hogy újraírná a query-kifejezéseiket.

A scope-on belül:
- additív SQL migráció (`output/session-hybrid-search-api-migration.sql`)
- a fúziós módszer kiválasztása és a skála-eltérés probléma explicit kezelése
- háromchunkos fixture a valódi `insert_envelope -> turn_projector -> chunk_indexer`
  láncon keresztül, ahol a két alap-módszer TÉNYLEG eltérő eredményt ad
- mindhárom függvény (`search_context`, `search_context_vector`,
  `search_context_hybrid`) tényleges kimenetének bizonyítása ugyanarra a query-re
- reachability ellenőrzés

Nem cél (lásd input.md "Nem cél"): RRF `k` konstans hangolása valós adaton,
`ranking_features` feltöltése, MCP szerver bekötés, permanens infrastruktúra,
`source_refs` feltöltése.

## Inputs Read

- `output/session-retrieval-quality-migration.sql` — `session_api.search_context()`
  javított (`'simple'`-konfigurációs) definíciója, TELJESEN elolvasva, a hibrid
  függvény FTS-CTE-je ezt a kifejezést idézi szó szerint
- `output/session-vector-search-api-migration.sql` — `session_api.search_context_vector()`
  definíciója, TELJESEN elolvasva, a hibrid függvény vektor-CTE-je ezt idézi szó szerint
- `session_store/vector_search.py` — `embed_query()` (39. sor), `to_pgvector_literal()`
  (65. sor), TELJESEN elolvasva, újrahasználva a teszt query-embedding generálásához
  (a hibrid SQL függvény magán nem hív Pythont — a `p_query_embedding` paramétert a
  HÍVÓ adja át, pont úgy, mint `search_context_vector()`-nél)
- `tests/test_session_store/test_vector_search.py`,
  `tests/test_session_store/test_session_api.py` — a valódi-láncon-átmenő fixture
  minták, TELJESEN elolvasva, a `tests/test_session_store/test_hybrid_search.py`
  ugyanezt a `_valid_envelope` / `_run_chain_for_envelope` / `_clean_tables` mintát
  követi
- `output/session-postgres-schema.sql`, `output/session-chunk-indexer-migration.sql` —
  a teljes alap-séma és a chunk-indexelő migráció, alkalmazva a teszt-instance-en
  (elsőként, másodikként)
- `.cic-context/factory-docs/job-slices.yaml` `session-hybrid-search-api-001` bejegyzés —
  a goal/acceptance_gates/forbidden_shortcuts megegyezik az input.md-vel, nincs eltérés

## Findings

**1. Valódi Postgres lánc.** `pgvector/pgvector:pg16` konténer indítva (`docker run -d
--name session-hybrid-search-test -p 55437:5432 ...`), `pg_isready` 2 másodperc után
"accepting connections". Mind az 5 SQL fájl egymás után, hibamentesen alkalmazva
(`ON_ERROR_STOP=1`, mindegyik exit code 0):
`session-postgres-schema.sql` → `session-chunk-indexer-migration.sql` →
`session-retrieval-quality-migration.sql` → `session-vector-search-api-migration.sql` →
**`session-hybrid-search-api-migration.sql`** (ÚJ).

**2. A fixture pontos felépítése — egyetlen query, ahol a két alap-módszer ELTÉR.**

Query: `"database lookups"` (mind az FTS, mind a vektor oldalon ugyanez a query-string;
a vektor oldalra `embed_query("database lookups")` adja az embeddinget).

- **Chunk A** (csak lexikailag releváns): *"Grandma's old recipe database is just a
  shoebox of index cards, and she always complains that her grandkids' phone lookups for
  restaurant reviews take forever compared to flipping through her cards."* — tartalmazza
  a `database` és `lookups` szavakat (külön, nem egymás mellett), de a kontextus
  családi anekdota/receptgyűjtés, NEM adatbázis-teljesítmény.
- **Chunk B** (csak szemantikailag releváns): *"Adding a secondary structure on the email
  column let the query planner skip straight to matching rows instead of scanning the
  entire customers table, making retrieval far faster."* — adatbázis-index/teljesítmény
  témáról szól, de a `database` és `lookups` szavak EGYIKE sem szerepel benne szó
  szerint.
- **Chunk C** (irreleváns kontroll): *"My cat enjoys sleeping on the windowsill all
  afternoon while birds chirp outside."*

A fixture-tervezés során (a hibrid migráció megírása ELŐTT) explicit ellenőrizve lett
`to_tsvector('simple', ...)` és tényleges `pgvector` `<=>` hívással:

```
plainto_tsquery('simple', 'database lookups') = 'database' & 'lookups'

to_tsvector('simple', <Chunk A>) @@ plainto_tsquery('simple','database lookups') = t
to_tsvector('simple', <Chunk B>) @@ plainto_tsquery('simple','database lookups') = f
to_tsvector('simple', <Chunk C>) @@ plainto_tsquery('simple','database lookups') = f

cos(query, A) via pgvector <=> : 0.3054926097393036
cos(query, B) via pgvector <=> : 0.4759539053412628
cos(query, C) via pgvector <=> : 0.0031044973979590385
```

Ez bizonyítja a Forbidden Shortcuts feltételt ELŐRE, a tényleges fixture megépítése
előtt: az FTS oldal CSAK Chunk A-t találja meg (B és C kiesik az AND-matchből), a
vektor oldal pedig B > A > C sorrendet ad — a két módszer TÉNYLEG eltérő eredményt
produkál ugyanarra a query-re.

**3. A három függvény tényleges, egymás mellé idézett kimenete (valódi Postgres ellen,
a háromchunkos fixture-rel, a valódi `insert_envelope -> run_projection_batch ->
run_indexing_batch` láncon keresztül felépítve):**

```
=== search_context() (FTS only) — query: 'database lookups' ===
(chunk_id=A, turn_id, "Grandma's old recipe database is just a shoebox of index
 cards, and she always complains that her grandkids' phone lookups for restaurant
 reviews take forever compared to flipping through her cards.", rank=0.0058589773)

=== search_context_vector() (vector only) — query: 'database lookups' ===
(chunk_id=B, turn_id, "Adding a secondary structure on the email column let the
 query planner skip straight to matching rows instead of scanning the entire
 customers table, making retrieval far faster.", similarity=0.4759539)
(chunk_id=A, turn_id, "Grandma's old recipe database is just a shoebox of index
 cards, ...", similarity=0.3054926)
(chunk_id=C, turn_id, "My cat enjoys sleeping on the windowsill all afternoon
 while birds chirp outside.", similarity=0.0031044974)

=== search_context_hybrid() (RRF fusion) — query: 'database lookups' ===
(chunk_id=A, turn_id, "Grandma's old recipe database is just a shoebox of index
 cards, ...", fused_score=0.03252247488101533)
(chunk_id=B, turn_id, "Adding a secondary structure on the email column let the
 query planner skip straight to matching rows instead of scanning the entire
 customers table, making retrieval far faster.", fused_score=0.01639344262295082)
(chunk_id=C, turn_id, "My cat enjoys sleeping on the windowsill all afternoon
 while birds chirp outside.", fused_score=0.015873015873015872)
```

(A pontos `chunk_id`/`turn_id` értékek a táblák `TRUNCATE`-jétől és a futtatás
sorrendjétől függő szekvencia-számok — a `chunk_id_by_label` mapping és a szövegek
azonosítják egyértelműen melyik sor melyik chunk; a `tests/test_session_store/
test_hybrid_search.py` minden assertion-je a tényleges visszaadott `chunk_id`-kra
hivatkozik, nem hardcode-olt számokra.)

**Megfigyelés, amit a report explicit kimond (nem rejti el):** a hibrid rangsorban
Chunk A (0.0325) megelőzi Chunk B-t (0.0164) — ez NEM hiba, hanem a választott RRF
formula direkt következménye: A mindkét oldalon kap RRF-tagot (FTS rang #1 → `1/61`,
vektor rang #2 → `1/62`), míg B csak a vektor oldalon kap tagot (vektor rang #1 →
`1/61`), C pedig csak a vektor oldalon (vektor rang #3 → `1/63`). A számtani ellenőrzés:
`A = 1/61 + 1/62 = 0.032522...`, `B = 1/61 = 0.016393...`, `C = 1/63 = 0.015873...` —
EGYEZIK a tényleges `fused_score` kimenettel (lásd `tests/test_session_store/
test_hybrid_search.py::TestSearchContextHybridFusion::test_fused_score_matches_rrf_formula_for_each_chunk`,
ami ezt a formulát mindhárom chunkra automatikusan újraszámolja és összeveti). A
Definition Of Done pontosan azt kéri, hogy "MIND A, MIND B magasabban legyen, mint C"
— ez TELJESÜL (`0.0325 > 0.0159` és `0.0164 > 0.0159`); azt NEM kéri, hogy A és B
egymáshoz viszonyítva is egy adott sorrendben legyen, és A magasabb pontszáma
legitim, mert A-nak tényleges (bár gyenge) szignálja van mindkét oldalon, B-nek csak
egy oldalon — ez maga is bizonyítja, hogy a fúzió valódi munkát végez (nem csak az
egyik oldal eredményét másolja át változatlanul).

**4. Reprodukált tesztfutás (valódi Postgres ellen, `pytest`):**

```
tests/test_session_store/test_hybrid_search.py::TestThreeChunkFixtureRealChain::test_fixture_builds_through_real_chain_and_produces_expected_rows PASSED
tests/test_session_store/test_hybrid_search.py::TestSearchContextLexicalOnly::test_finds_chunk_a_but_not_chunk_b PASSED
tests/test_session_store/test_hybrid_search.py::TestSearchContextVectorSemanticOnly::test_ranks_chunk_b_above_chunk_a_above_chunk_c PASSED
tests/test_session_store/test_hybrid_search.py::TestSearchContextHybridFusion::test_ranks_both_chunk_a_and_chunk_b_above_chunk_c PASSED
tests/test_session_store/test_hybrid_search.py::TestSearchContextHybridFusion::test_chunk_b_is_present_in_hybrid_despite_zero_fts_overlap PASSED
tests/test_session_store/test_hybrid_search.py::TestSearchContextHybridFusion::test_fused_score_matches_rrf_formula_for_each_chunk PASSED
tests/test_session_store/test_hybrid_search.py::TestSearchContextHybridFusion::test_limit_parameter_caps_result_count PASSED
============================== 7 passed in 16.86s ==============================
```

Regresszió-ellenőrzés: a meglévő `test_session_api.py` (9 teszt) és `test_vector_search.py`
(6 teszt) suite is futtatva ugyanezen instance-en az ÚJ migráció alkalmazása UTÁN —
mind a 15 teszt PASSED, az új additív migráció nem bontott el semmit a meglévő
függvényekben.

**5. EXPLAIN — index-használat a hibrid függvényre.** A 3 sornyi fixture-méretnél a
planner Bitmap Index Scan-t használ a `chunk_fts`-en
(`idx_session_idx_chunk_fts_tsv`) és a `chunks.session_id`-n
(`idx_session_core_chunks_session_id`), de Seq Scan-t a `chunk_embeddings`-en (nem
HNSW Index Scan-t) — ugyanaz a dokumentált, elfogadott eredmény, mint
`search_context_vector()` saját EXPLAIN tesztjében (`test_vector_search.py::
TestExplainIndexUsage`): ilyen kis sorszámnál a Seq Scan olcsóbb, mint az index
bejárása, ez NEM hiba.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Mind az 5 SQL fájl (4 meglévő + 1 új) hibamentesen alkalmazható egymás után egy valódi `pgvector/pgvector:pg16` instance-en | proven | "Findings" 1. pont — mind az 5 `psql ... -v ON_ERROR_STOP=1` hívás exit code 0, `CREATE FUNCTION`/`COMMENT` kimenettel | tényleges `docker exec psql` futtatás | alacsony |
| Az RRF fúziós módszer EXPLICIT kezeli a `ts_rank`/cosine-similarity skála-eltérés problémát, és a naív súlyozott összeg alternatívát indokoltan elveti | proven | `output/session-hybrid-search-api-migration.sql` 22-79. sor (kommentblokk: "THE SCALE-MISMATCH PROBLEM", "REJECTED ALTERNATIVE — naive weighted sum", "CHOSEN METHOD — Reciprocal Rank Fusion") | a migrációs fájl tartalmának közvetlen idézése | alacsony |
| `search_context_hybrid()` a `search_context()` és `search_context_vector()` MEGLÉVŐ query-kifejezéseit hívja, nem írja újra azokat | proven | a migráció `fts_matches` CTE-je (113-119. sor) szóról szóra a `search_context()` `WHERE`/`ts_rank` kifejezését tartalmazza (forrás: `output/session-retrieval-quality-migration.sql:74-80`); a `vector_matches` CTE (129-133. sor) szóról szóra a `search_context_vector()` kifejezését tartalmazza (forrás: `output/session-vector-search-api-migration.sql:56-61`) | a két fájl közvetlen összevetése | alacsony |
| Háromchunkos fixture, ahol Chunk A csak lexikailag, Chunk B csak szemantikailag releváns, Chunk C irreleváns — és a két alap-módszer TÉNYLEG eltérő eredményt ad ugyanarra a query-re | proven | "Findings" 2. pont: `to_tsvector(...) @@ plainto_tsquery(...)` = t/f/f Chunk A/B/C-re; `cos(query,A)=0.305`, `cos(query,B)=0.476`, `cos(query,C)=0.003` (pgvector `<=>` operátorral mérve) | tényleges SQL `SELECT` Chunk A/B/C tsvector-jára és pgvector `<=>` operátorára | alacsony |
| `search_context()` (FTS only) a teszt-query-re Chunk A-t megtalálja, Chunk B-t NEM | proven | "Findings" 3. pont idézett kimenet: `search_context()` kizárólag Chunk A sorát adja vissza; `tests/.../test_hybrid_search.py::TestSearchContextLexicalOnly::test_finds_chunk_a_but_not_chunk_b` PASSED | tényleges `psql`/pytest futtatás valódi Postgres ellen | alacsony |
| `search_context_vector()` (vector only) a teszt-query-re Chunk B-t magasan rangsorolja, Chunk A-t alacsonyabban | proven | "Findings" 3. pont idézett kimenet: `similarity` sorrend B(0.476) > A(0.305) > C(0.003); `tests/.../test_hybrid_search.py::TestSearchContextVectorSemanticOnly` PASSED | tényleges `psql`/pytest futtatás valódi Postgres ellen | alacsony |
| `search_context_hybrid()` a teszt-query-re MIND Chunk A-t, MIND Chunk B-t magasabban rangsorolja, mint Chunk C-t | proven | "Findings" 3. pont idézett kimenet: `fused_score` A(0.0325) > C(0.0159) ÉS B(0.0164) > C(0.0159); `tests/.../test_hybrid_search.py::TestSearchContextHybridFusion::test_ranks_both_chunk_a_and_chunk_b_above_chunk_c` PASSED | tényleges `psql`/pytest futtatás valódi Postgres ellen | alacsony |
| Chunk B (ami az FTS oldalon NULLA sort kap) mégis megjelenik a hibrid eredményben, a `FULL OUTER JOIN` miatt | proven | `tests/.../test_hybrid_search.py::TestSearchContextHybridFusion::test_chunk_b_is_present_in_hybrid_despite_zero_fts_overlap` PASSED | pytest assertion a tényleges visszaadott `chunk_id`-kra | alacsony |
| A `fused_score` a dokumentált RRF formulával (`1/(k+rank)` per oldal, `k=60`, hiányzó oldal = 0) számítva érkezik, nem csak "valamilyen helyesen rendező szám" | proven | `tests/.../test_hybrid_search.py::TestSearchContextHybridFusion::test_fused_score_matches_rrf_formula_for_each_chunk` PASSED — a teszt mindhárom chunkra Python-oldalon újraszámolja a formulát a tényleges FTS/vektor rangokból és `pytest.approx`-szal összeveti a tényleges `fused_score`-ral | pytest assertion, exact formula re-derivation | alacsony |
| Az additív migráció nem regresszionál a meglévő `search_context()`/`search_context_vector()`/`session_status()`/`get_timeline()`/`get_context_pack()` függvényeken | proven | "Findings" 4. pont: `test_session_api.py` (9 teszt) + `test_vector_search.py` (6 teszt) mind PASSED ugyanazon instance-en, az új migráció alkalmazása UTÁN | tényleges pytest futtatás | alacsony |
| Nincs Python helper a fúzióhoz (a fúzió tisztán SQL-ben megoldható) — `embed_query`/`to_pgvector_literal` viszont továbbra is szükséges a hívó oldalán a query-embedding előállításához, és helyesen reachable | proven | `grep -rn "embed_query\|to_pgvector_literal" --include="*.py" . \| grep -v test` → `session_store/vector_search.py:39`, `session_store/vector_search.py:65` (definíciók); a hibrid SQL függvény maga `session_api.search_context_hybrid` `output/session-hybrid-search-api-migration.sql:98` — paraméterként KAPJA a `p_query_embedding`-et, nem hívja Pythont | tényleges `grep -rn` futtatás, idézve "Reachability" szekcióban | alacsony |
| `EXPLAIN` dokumentálja a hibrid függvény tényleges végrehajtási tervét ennél a fixture-méretnél | proven | "Findings" 5. pont — tényleges `EXPLAIN` kimenet: Bitmap Index Scan a `chunk_fts`-en és `chunks.session_id`-n, Seq Scan a `chunk_embeddings`-en | tényleges `EXPLAIN SELECT * FROM session_api.search_context_hybrid(...)` futtatás | alacsony |

## Decisions Proposed

1. **RRF (Reciprocal Rank Fusion), k=60, mindkét oldal SAJÁT rangsorára alkalmazva.**
   Indoklás: lásd `output/session-hybrid-search-api-migration.sql` 22-79. sora — a
   `ts_rank()` korpusz-/query-hossz-függő, nem korlátos skálájú érték, a cosine
   similarity pedig fix [-1,1] tartományú geometriai mennyiség; a kettő numerikusan
   NEM összemérhető. Az RRF a RANGPOZÍCIÓRA fuzionál, nem a nyers pontszámra, így a
   skála-eltérés kérdése el sem kerül (nem kell konverziós faktort/normalizálást
   választani). `k=60` az irodalomban (Cormack/Clarke/Buettcher TREC-munka) elfogadott
   alapérték, NEM ezen a jobon hangolva (lásd input.md "Nem cél").

2. **Elvetve: naív súlyozott összeg (`0.5*ts_rank + 0.5*similarity`).** Explicit
   elvetve a Forbidden Shortcuts miatt — a két pontszám skálája nem összemérhető, egy
   fix súlypár ma jól nézhet ki, de a korpusz/query alakja változásával hamis
   eredményt adhat anélkül, hogy ez a hibajelből látszana.

3. **Elvetve: per-query min-max normalizálás [0,1]-re.** A migrációs fájl kommentje
   (53-62. sor) dokumentálja: 1-2 eredménysornál (a gyakori eset egy kis session-ben) a
   min-max normalizálás degenerál (a legjobb mindig 1.0-ra, a másik mindig 0.0-ra
   skálázódik, függetlenül a tényleges score-réstől) — ez rosszabb approximáció, mint
   a nyers score-ok teljes mellőzése.

4. **Tiszta SQL implementáció, nincs új Python helper.** Az RRF "rangra redukálás +
   1/(k+rank) összegzés" lépése két CTE-vel (`ROW_NUMBER() OVER (ORDER BY ...)`) és egy
   `FULL OUTER JOIN`-nal kifejezhető standard SQL-ben. A meglévő
   `session_store/vector_search.py:embed_query()`/`to_pgvector_literal()` Python helper
   TOVÁBBRA IS szükséges a HÍVÓ oldalon (a query szöveg → embedding konverzióhoz), de
   ez nem új kód — ugyanaz, amit `search_context_vector()` hívói is használnak.

5. **`FULL OUTER JOIN` a két CTE között, `COALESCE(... , 0.0)` hiányzó oldalra.** Egy
   chunk, ami csak egyik oldalon szerepel, NEM kap szintetikus "legrosszabb rang"
   büntetést — egyszerűen 0-t ad a hiányzó oldal RRF-tagjához. Ez teszi lehetővé, hogy
   a hibrid függvény degradáljon egyetlen oldal rangsorára, ha a másik oldal nulla
   találatot ad (pl. tisztán szemantikus query, nulla lexikai egyezéssel).

## Rejected / Out Of Scope

- RRF `k` konstans hangolása valós session-adaton (input.md "Nem cél")
- `session_idx.ranking_features` feltöltése/használata (input.md "Nem cél")
- az MCP szerver átírása, hogy ezt a függvényt hívja (input.md "Nem cél")
- permanens futtatási infrastruktúra — a teszt-konténer a munka végén leállítva és
  törölve (`docker rm -f session-hybrid-search-test`)
- `session_core.source_refs` feltöltése (input.md "Nem cél")
- naív súlyozott összeg fúzió (lásd "Decisions Proposed" 2.)
- per-query min-max normalizálás (lásd "Decisions Proposed" 3.)

## Risks

- **`k=60` nincs validálva valós session-adaton.** Az irodalmi alapérték elfogadható
  kiindulópont `experimental` státuszhoz, de nem bizonyított, hogy ez az optimális
  konstans a `cic-mcp-session` tényleges lekérdezési mintáira — ezt egy jövőbeli,
  valós-méretű kiértékelő job-nak kell elvégeznie, mielőtt `candidate`-re lépne.
- **A fixture KONSTRUÁLT, nem valós-méretű.** 3 chunk egy session-ben nem reprezentálja
  azt, hogyan viselkedik az RRF tucatnyi/száznyi valódi, átfedő relevanciájú chunk
  között — a rang-pozíció alapú fúzió nagyobb N-nél másképp viselkedhet (pl. ha sok
  chunk azonos rangon végez holtversenyben).
- **A hibrid rangsorban Chunk A megelőzi Chunk B-t** (lásd "Findings" 3. pont
  "Megfigyelés" bekezdése) — ez a választott formula legitim következménye, nem hiba,
  de azt jelenti, hogy az RRF NEM garantálja, hogy a "tisztán szemantikus" találat
  mindig magasabban végez, mint egy "részben lexikailag is releváns, de szemantikailag
  off-topic" találat — csak azt garantálja, hogy mindkettő megelőzi az irrelevánsat.
  Ez dokumentálva van, nem rejtve.
- **A `chunk_embeddings`-en Seq Scan fut ennél a méretnél** (HNSW index nem aktiválódik)
  — ugyanaz a dokumentált, elfogadott korlátozás, mint `search_context_vector()`-nél;
  nagyobb session-eknél ezt újra kellene futtatni az EXPLAIN-nel.
- **Két `psycopg` kapcsolat / kettős query-kiértékelés** a hibrid SQL-ben (a CTE-k két
  külön index-elérésen futnak) — nagyobb session-eknél ez két teljes oldal-scan-t
  jelent egyetlen hívásban, ami a nyers `search_context()`/`search_context_vector()`
  hívásokhoz képest dupla munkát ró a táblákra; nincs jelenleg mérve, hogy ez milyen
  abszolút latency-többletet jelent valós méretnél.

## Definition Of Done Check

- [x] fúziós stratégia kiválasztva és indokolva, a skála-eltérés problémája EXPLICIT
      kezelve — lásd "Decisions Proposed" 1-3., migráció 22-79. sor
- [x] additív migráció alkalmazva, kimenet idézve — lásd "Findings" 1. pont
- [x] háromchunkos (lexikai/szemantikai/irreleváns) fixture létrehozva a VALÓDI láncon —
      lásd "Findings" 2. pont, `tests/test_session_store/test_hybrid_search.py`
- [x] `search_context()` kimenet idézve a teszt-query-re (Chunk A megtalálva, Chunk B
      nem) — lásd "Findings" 3. pont
- [x] `search_context_vector()` kimenet idézve ugyanarra a query-re (Chunk B magasan,
      Chunk A nem feltétlenül) — lásd "Findings" 3. pont
- [x] `search_context_hybrid()` kimenet idézve ugyanarra a query-re, bizonyítva hogy
      MIND A, MIND B magasabban van, mint C — lásd "Findings" 3. pont
- [x] reachability dokumentálva (nincs Python helper a fúzióhoz; `embed_query`/
      `to_pgvector_literal` reachability dokumentálva) — lásd "Reachability" alább
- [x] claim-evidence tábla kitöltve, nem üres — lásd fentebb

## Reachability

Nem készült új Python helper ehhez a jobhoz (a fúzió tisztán SQL-ben megoldható, lásd
"Decisions Proposed" 4.). A MEGLÉVŐ helper-ek, amiket a hívó oldal a teszt-fixture-ben
újrahasznosít a query-embedding előállításához:

```
$ grep -rn "embed_query\|to_pgvector_literal" --include="*.py" . | grep -v "test_" | grep -v "/tests/"
session_store/vector_search.py:39:def embed_query(text: str) -> list[float]:
session_store/vector_search.py:65:def to_pgvector_literal(vector: list[float]) -> str:
```

A SQL függvény oldala a `session_api.*` minta szerint:

```
output/session-hybrid-search-api-migration.sql:98:CREATE OR REPLACE FUNCTION session_api.search_context_hybrid(
output/session-hybrid-search-api-migration.sql:174:COMMENT ON FUNCTION session_api.search_context_hybrid(UUID, TEXT, VECTOR(384), INTEGER) IS
```

Hívók (jelenleg KIZÁRÓLAG a saját pytest suite-ja — nincs production caller, ugyanúgy,
ahogy `search_context_vector()`-nek sincs, lásd `session_store/vector_search.py:28-32`
docstring):

```
tests/test_session_store/test_hybrid_search.py:256:def _call_search_context_hybrid(pg_config, session_id, query_text, limit=20):
tests/test_session_store/test_hybrid_search.py:263:                "FROM session_api.search_context_hybrid(%s, %s, %s::vector, %s)",
```

## Next Jobs

- valós-méretű (nem konstruált) kiértékelés és RRF `k` paraméter hangolás
  `candidate` státuszhoz (lásd meta.yaml "status indoklás")
- MCP szerver bekötés, hogy a session réteg fogyasztói (`cic-mcp-gateway`) tényleg
  hívják a `search_context_hybrid()`-et
- `session_idx.ranking_features` feltöltés/használat kiértékelése, ha a sima RRF
  nem elég a valós minőségi célokra
- nagyobb fixture-rel EXPLAIN újrafuttatás, hogy a HNSW index mikortól aktiválódik
  a vektor oldali CTE-ben kombinált lekérdezés esetén is (jelenleg csak izolált
  `search_context_vector()`-re van ez dokumentálva)
