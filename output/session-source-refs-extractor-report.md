# session-source-refs-extractor-001 Output

## Scope

Ez a job feltölti a `cic-mcp-session` repo `session_core` schema utolsó üres
táblájának (`session_core.source_refs`) sorait: determinisztikus,
kulcs-/regex-illesztésen alapuló provenance-referencia kinyerés (`tool_call`,
`file`, `url`) egy `session_core.turns` sor `(role, content)`-jéből és a
belőle generált chunk szövegéből. NEM új outbox `job_type`/trigger — a
kinyerés a MEGLÉVŐ `chunk_indexer.py` `index_turn` outbox-worker
`_index_one_job()` per-row tranzakcióján BELÜL történik, közvetlenül a
`_insert_chunk()` hívás UTÁN, ugyanabban a tranzakcióban, mert a
`source_refs.chunk_id` FK megköveteli, hogy a chunk már létezzen.

Nem cél (input.md "Nem cél"): `session_idx.ranking_features` feltöltése,
recall-/pontosság-mérés valós session-adaton, AI/LLM-alapú kinyerés, az MCP
szerver átírása, `session_api` réteg bővítése `source_refs` lekérdezésére.

## Inputs Read

- `output/session-postgres-schema.sql` (teljes fájl) — `session_core.source_refs`
  DDL (sor 180-187: `source_ref_id`, `chunk_id` FK `ON DELETE CASCADE`,
  `ref_kind`, `ref_value`, `content_hash`)
- `session_store/chunk_indexer.py` (teljes fájl, módosítás előtti állapot) —
  `_index_one_job()` (a per-row tranzakció, amibe a kinyerést beillesztettem,
  közvetlenül az `_insert_chunk()` hívás után), `_fetch_turn()` (eredetileg
  csak `turn_id, session_id, content`-et SELECT-elt)
- `session_store/turn_projector.py` (teljes fájl, csak referenciaként) —
  `map_role()` (sor 82-94) — a `role` lehetséges értékei: `tool`,
  `assistant`, `user`, `system`, `manual`, `event`
- `output/session-chunk-indexer-migration.sql`, `output/session-retrieval-quality-migration.sql`,
  `output/session-vector-search-api-migration.sql`, `output/session-hybrid-search-api-migration.sql`
  (teljes fájlok) — a teljes meglévő migrációs lánc, amit a teszteléshez
  egymás után alkalmaztam
- `tests/test_session_store/test_chunk_indexer.py` (teljes fájl, módosítás
  előtti állapot) — a meglévő e2e teszt-szerkezet (`_clean_tables`,
  `_valid_envelope`, `_make_turn`), amit az új teszt-esetek követnek

## Findings

1. A `session_core.source_refs` tábla DDL-je (`session-postgres-schema.sql`
   sor 180-187) már tartalmazta a szükséges oszlopokat (`ref_kind TEXT NOT
   NULL`, `ref_value TEXT NOT NULL`, `content_hash TEXT` — NULL-able, ezt a
   jobot várva) és a `chunk_id` FK-t `ON DELETE CASCADE`-del — nem kellett
   migráció, csak a worker oldali kitöltés.
2. A `_fetch_turn()` eredetileg NEM SELECT-elte a `role`-t, csak
   `turn_id, session_id, content`-et — ezt bővítenem kellett, mert az
   `extract_source_refs()` `tool_call` szabálya a `role == 'tool'` feltételt
   igényli, és a `role` már ki van számolva/elmentve a `turn_projector.
   map_role()` által (nem kellett újra elérnem a `provider_event_name`-et).
3. A meglévő `_index_one_job()` chunk-létrehozó ciklusa (`for chunk_seq,
   piece in enumerate(pieces, start=1)`) pontosan az a hely, ahol a
   `chunk_id` elérhetővé válik az `_insert_chunk()` visszatérési értékéből —
   ide illesztettem be az `extract_source_refs()` hívást és az
   `_insert_source_ref()` ciklust, ugyanazon `cur`/tranzakció alatt.
4. A `url` szabály a CHUNK szövegére (a `split_into_chunks()` által
   előállított `piece`-re) illeszkedik, nem a teljes turn `content`-re —
   ez fontos egy többchunkos turn esetén, mert egy URL helyesen csak ahhoz a
   `chunk_id`-hoz kerül hozzárendelve, amelyiknek a szövege tényleg
   tartalmazza.
5. A meglévő `tests/test_session_store/` teszt-fixture-ök (`_valid_envelope`
   `provider_event_name="Stop"`-pal) `role='assistant'`-ra oldódnak fel
   (`turn_projector.PROVIDER_EVENT_NAME_TO_ROLE`), ezért a Case A
   (`tool_call`) teszteléséhez explicit `provider_event_name="PostToolUse"`
   override kellett, hogy a `role` valóban `'tool'`-ra oldódjon fel a valódi
   `insert_envelope()` → `run_projection_batch()` láncon keresztül.
6. A teljes meglévő SQL-lánc (5 fájl: `session-postgres-schema.sql`,
   `session-chunk-indexer-migration.sql`, `session-retrieval-quality-migration.sql`,
   `session-vector-search-api-migration.sql`, `session-hybrid-search-api-migration.sql`)
   hiba nélkül alkalmazható egymás után egy friss `pgvector/pgvector:pg16`
   instance-en — lásd "Claim-Evidence Matrix".
7. A `tests/test_session_store/test_vector_search.py::TestExplainIndexUsage::
   test_explain_documents_actual_plan_for_small_fixture` teszt ÖNÁLLÓAN, a
   forrás-ref módosításaim NÉLKÜL (git stash-elt állapotban, tiszta
   `main`-en) IS elhasal ugyanazzal a hibával ugyanezen a gépen/Postgres-
   instance-en — ez egy preexisting, planner-statisztika-függő flakiness
   (`Nested Loop` + `Index Scan ... chunk_embeddings_pkey` választás `Seq
   Scan` helyett), NEM az ebben a jobban írt kód okozta regresszió. Lásd
   "Risks" és "Claim-Evidence Matrix" a pontos bizonyítékért.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Mind az 5 meglévő SQL fájl (schema + 4 migráció) hiba nélkül alkalmazható egymás után egy valódi Postgres instance-en | proven | `docker exec -i session-source-refs-test psql -U postgres -d testdb -v ON_ERROR_STOP=1 < output/session-postgres-schema.sql` (34 statement: `CREATE EXTENSION`×3, `CREATE SCHEMA`×5, `CREATE TYPE`×4, `CREATE TABLE`×11, stb.) majd a 4 migráció (`session-chunk-indexer-migration.sql`: `ALTER TABLE`/`COMMENT`/`CREATE FUNCTION`/`CREATE TRIGGER`; `session-retrieval-quality-migration.sql`: 2×`CREATE FUNCTION`+`COMMENT`; `session-vector-search-api-migration.sql`: `CREATE FUNCTION`+`COMMENT`; `session-hybrid-search-api-migration.sql`: `CREATE FUNCTION`+`COMMENT`) — mind hiba nélkül, `ON_ERROR_STOP=1` mellett | valódi Postgres (`pgvector/pgvector:pg16`, konténer `session-source-refs-test`, port 55434) | alacsony — idempotencia nincs tesztelve (ismételt futtatás hibázna), ugyanaz a limitáció mint a korábbi jobokban |
| `extract_source_refs()` determinisztikus, NEM AI/LLM-alapú | proven | `session_store/chunk_indexer.py:278` — pure function, fix kulcsnevek (`TOOL_NAME_KEY='tool_name'`, `FILE_PATH_KEYS=('file_path','path','notebook_path')`) és fix regex (`URL_PATTERN=re.compile(r'https?://\S+')`), nincs benne hívás semmilyen modellhez/API-hoz; unit teszt: `test_extract_source_refs_tool_call_rule`, `test_extract_source_refs_file_rule_top_level_and_nested`, `test_extract_source_refs_url_rule_matches_chunk_text_not_payload`, `test_extract_source_refs_returns_empty_list_for_nothing_extractable` mind PASSED | unit teszt, DB nélkül | alacsony |
| `_fetch_turn()` bővítve `role`-lal | proven | `session_store/chunk_indexer.py:382-399` — `SELECT turn_id, session_id, content, role FROM session_core.turns WHERE turn_id = %s` (eredetileg csak `turn_id, session_id, content`) | fájl:sor hivatkozás + a 4 e2e teszt-eset sikeres lefutása (a `role` érték nélkül a `tool_call` szabály soha nem találna semmit) | alacsony |
| Az integráció a MEGLÉVŐ `_index_one_job()` per-row tranzakcióján belül történik, NEM új outbox job_type | proven | `session_store/chunk_indexer.py:498-560` `_index_one_job()` — a `for ref_kind, ref_value in extract_source_refs(role, content, piece): _insert_source_ref(cur, chunk_id, ref_kind, ref_value)` ciklus a MEGLÉVŐ `with conn.transaction(): with conn.cursor() as cur:` blokkon belül, közvetlenül `_insert_chunk()`/`_insert_chunk_fts()`/`_insert_chunk_embedding()` UTÁN, UGYANAZON `cur`-on; nincs új `OUTBOX_JOB_TYPE` konstans, nincs új trigger, nincs új SQL migrációs fájl | kódidézet + `grep -n "OUTBOX_JOB_TYPE\|CREATE TRIGGER" session_store/chunk_indexer.py` → csak a meglévő `index_turn` job_type, 0 új trigger ebben a jobban | alacsony |
| Eset A (tool_call): valódi `insert_envelope()` lánc, `role='tool'`, `tool_name` kulcs → 1 `source_refs` sor | proven | `INSERT INTO session_raw.envelopes(...provider_event_name='PostToolUse'..., payload={"raw_text":"ran a tool","tool_name":"Read"})` → `run_projection_batch()` → `run_indexing_batch()` kimenet: `job_id=275 outcome=done chunk_count=1 source_ref_count=1`; SQL: `SELECT ref_kind, ref_value, content_hash FROM session_core.source_refs WHERE chunk_id=125` → `tool_call \| Read \| 9b9a8d05a7ec353bda84f9c1bb3178c299de3001b5e970508ddc889c487f92ca`; pytest: `test_case_a_tool_call_payload_produces_tool_call_source_ref` PASSED | valódi Postgres, valódi `insert_envelope()`→`run_projection_batch()`→`run_indexing_batch()` lánc | alacsony |
| Eset B (file): `tool_input.file_path` jelenléte → 1 `source_refs` sor `ref_kind='file'` | proven | `payload={"raw_text":"edited a file","tool_input":{"file_path":"/workspace/session_store/chunk_indexer.py"}}` → `job_id=276 outcome=done chunk_count=1 source_ref_count=1`; SQL: `chunk_id=126` → `file \| /workspace/session_store/chunk_indexer.py \| 4847d6115a5ce09177f76e5642a60f3ff715483eb95e444fb071ebe6c62b5929`; pytest: `test_case_b_file_path_payload_produces_file_source_ref` PASSED | valódi Postgres, valódi lánc | alacsony |
| Eset C (url): chunk szövegben URL → 1 `source_refs` sor `ref_kind='url'`, a CHUNK szövegéből (nem a raw payload struktúrájából) kinyerve | proven | `payload={"raw_text":"see the docs at https://example.com/docs for details"}` → `job_id=277 outcome=done chunk_count=1 source_ref_count=1`; SQL: `chunk_id=127` → `url \| https://example.com/docs \| de106e607d0e711199de3fb7eb98fe5d412ee49ac326eadd3db848ee272ad2cb`; pytest: `test_case_c_url_in_text_produces_url_source_ref` PASSED | valódi Postgres, valódi lánc | alacsony |
| Eset D (kontroll, semmi kinyerhető): 0 `source_refs` sor, NINCS hiba, outbox `done` | proven | `payload={"raw_text":"just a plain assistant reply, nothing to extract here"}` → `job_id=278 outcome=done chunk_count=1 source_ref_count=0`, `error=None`; SQL: `SELECT count(*) FROM session_core.source_refs WHERE chunk_id=128` → `0`; outbox: `job_id=278 \| index_turn \| done \| attempts=0 \| last_error=NULL` (lásd alább a teljes outbox-tábla idézet); pytest: `test_case_d_nothing_extractable_produces_zero_source_refs_no_error` PASSED | valódi Postgres, valódi lánc | alacsony |
| Mind a 4 eset outbox sora `done`, nem `failed`/`dead_letter` | proven | `SELECT job_id, job_type, status, attempts, last_error FROM session_jobs.outbox ORDER BY job_id` kimenet (8 sor: 4× `project_envelope` + 4× `index_turn`, mind `status=done`, `attempts=0`, `last_error` üres) — idézve teljes egészében alább | valódi Postgres, közvetlen SQL lekérdezés | alacsony |
| `content_hash` minden sorban kitöltve, sha256(ref_value) | proven | A 3 nem-üres eset (A/B/C) mindegyikénél a Postgres-ben tárolt `content_hash` BÁJTRA EGYEZIK a független Python `hashlib.sha256(ref_value.encode('utf-8')).hexdigest()` kiszámítással: `Read`→`9b9a8d05a7ec...`, `/workspace/session_store/chunk_indexer.py`→`4847d611...`, `https://example.com/docs`→`de106e60...` — mindhárom egyezik a Postgres-oszloppal | valódi Postgres + független Python `hashlib` újraszámítás, kézi keresztellenőrzés | alacsony — egy modellváltás (más hash-algoritmus) jövőbeli migrációt igényelne, de ez explicit dokumentált döntés, nem rejtett |
| Teljes meglévő teszt-suite lefuttatva, regresszió-mentesség bizonyítva | partial | `pytest tests/test_session_store/ -v --no-cov` kimenet: `53 passed, 1 failed in 26.75s`. Az 1 hiba (`test_vector_search.py::TestExplainIndexUsage::test_explain_documents_actual_plan_for_small_fixture`) `git stash`-elt állapotban (a forrás-ref módosítások NÉLKÜL, tiszta `main`-en) ÖNÁLLÓAN futtatva IS ugyanúgy elhasal — bizonyítottan preexisting, planner-statisztika-függő flakiness, nem az ebben a jobban írt kód okozta regresszió. Az ÚJ 8 teszt (4 unit + 4 e2e) mind PASSED, a chunk_indexer/turn_projector/worker_loop/session_api/hybrid_search teljes meglévő suite-ja (a flaky EXPLAIN-tesztet kivéve) mind PASSED | valódi Postgres ellen, kétszer is futtatva (egyszer a saját kóddal, egyszer `git stash`-elt tiszta `main`-en) | közepes — a meglévő `test_explain_documents_actual_plan_for_small_fixture` teszt környezet-/statisztika-függő, ez egy meglévő, ebben a jobban NEM javított hiányosság a tesztben, lásd "Risks" |
| `extract_source_refs()` és az integráció létezik, fájl:sor hivatkozással | proven | `session_store/chunk_indexer.py:278` `extract_source_refs()`, `session_store/chunk_indexer.py:382` `_fetch_turn()` (role-lal bővítve), `session_store/chunk_indexer.py:453` `_insert_source_ref()`, `session_store/chunk_indexer.py:498` `_index_one_job()` (a kinyerés-hívással bővítve) | fájl:sor hivatkozás + `py_compile` siker | n/a |

Teljes outbox-tábla idézet (mind a 4 eset, `done`, nincs hiba):

```
 job_id |     job_type     | status | attempts | last_error
--------+------------------+--------+----------+------------
    271 | project_envelope | done   |        0 |
    272 | project_envelope | done   |        0 |
    273 | project_envelope | done   |        0 |
    274 | project_envelope | done   |        0 |
    275 | index_turn       | done   |        0 |
    276 | index_turn       | done   |        0 |
    277 | index_turn       | done   |        0 |
    278 | index_turn       | done   |        0 |
```

Teljes `source_refs` idézet (per-session csoportosítva, a D eset kontrollként 0):

```
 provider_session_id | source_ref_count
----------------------+------------------
 sess-srcref-A       |                1
 sess-srcref-B       |                1
 sess-srcref-C       |                1
 sess-srcref-D       |                0
```

## Decisions Proposed

1. **Target path: a meglévő `session_store/chunk_indexer.py`-ba integrálva,
   NEM külön `session_store/source_refs.py` modulba.** Indok: az
   `extract_source_refs()` szorosan kapcsolódik a chunk-szöveghez (a
   `piece` paraméter a `split_into_chunks()` kimenete) és csak az
   `_index_one_job()` hívja — egy külön modul ugyanazt az import-kört
   igényelné, extra indirekciós réteget hozna létre érdemi elválasztási
   előny nélkül (ellentétben pl. `vector_search.py`-vel, ami egy NEM a
   chunk-indexeléshez kötött külön hívási útvonalat szolgál ki). A
   `embed_texts`/`extract_text`/`split_into_chunks` minta is mind ugyanebben
   a fájlban él, ugyanezen az elven.
2. **`ref_kind='tool_call'` kulcsa: `tool_name`.** Indok: input.md "2."
   explicit ezt a kulcsnevet adja meg példaként, és a repo semelyik
   schema/doc fájljában nem szerepel alternatív tool-azonosító kulcs (pl.
   `name`, `tool`) — nem volt nem-arbitrer alternatíva.
3. **`ref_kind='file'` kulcsai: `file_path`, `path`, `notebook_path`,
   ellenőrizve mind a `content` top-level-jén, mind a beágyazott
   `content['tool_input']`-on.** Indok: input.md "2." mindhárom kulcsot
   explicit megnevezi; a `tool_input` beágyazás a tool-hívás-alakú payload-ok
   konvencionális struktúráját tükrözi (a `PreToolUse`/`PostToolUse`
   eseménynevek `role='tool'`-ra oldódnak fel a `turn_projector`-ban, és a
   tool argumentumok jellemzően egy `tool_input` alobjektumban élnek). Mind
   a content-szintű, mind a tool_input-szintű találatot beillesztettem
   (`_extract_file_refs_from_dict()` helper, kétszer hívva), hogy egy
   szabálydefinícióval lefedjem mindkét elhelyezést.
4. **`ref_kind='url'` regex: `r'https?://\S+'`, a CHUNK szövegén
   (`piece`), NEM a raw `content` payload-on.** Indok: input.md "2." ezt
   explicit kéri ("a CHUNK SZÖVEGÉBEN (nem a raw payload-ban)"). A
   `re.findall()` minden találatot visszaad megjelenési sorrendben — egy
   turn, amit több chunkra darabolnak, minden chunk saját `piece`-ére kapja
   meg a saját URL-jeit, így egy adott URL helyesen a tartalmazó
   `chunk_id`-hoz kötődik, sosem egy másik chunkhoz ugyanazon turn-ön belül.
5. **`content_hash = sha256(ref_value.encode('utf-8')).hexdigest()`** —
   NEM a chunk saját content_hash-e, NEM a teljes sor hash-e. Indok: input.md
   "3." explicit megengedi ("sha256 az ref_value-ból, vagy indokold más
   választásodat"); a `ref_value` önmagában az a tartalom, amire egy
   jövőbeli dedup/lookup réteg illeszkedne (egy fájlútvonal, egy URL, egy
   tool-név) — ha a hash-elés `(ref_kind, ref_value)` páron menne, két
   különböző `ref_kind` alatt megjelenő, de szövegként azonos érték (elvi
   eset, jelen szabályokkal nem fordul elő, de jövőbiztosság kedvéért
   releváns) más hash-t kapna, holott a tényleges dedup-érdek a
   `ref_value` string-azonosság, nem a `(kind, value)` pár-azonosság.
6. **`IndexingResult` bővítve `source_ref_count: int = 0` mezővel.**
   Indok: a 4 teszt-eset bizonyításához (és a CLI `_main()` kimenetéhez)
   szükség volt egy explicit, programozott módon ellenőrizhető jelzésre,
   hogy hány `source_refs` sor készült egy adott outbox-job futása alatt —
   nem csak a tényleges SQL-lekérdezés, hanem a függvény visszatérési
   értéke is bizonyítja a számot.

## Rejected / Out Of Scope

- `session_idx.ranking_features` feltöltése — input.md "Nem cél"
- recall-/pontosság-mérés valós session-adaton (mennyi referenciát hagy ki a
  kinyerés) — input.md "Nem cél"; ez a `candidate` státuszhoz szükséges
  jövőbeli munka (lásd "Target" "státusz indoklás")
- AI/LLM-alapú entitás-felismerés vagy kinyerés — input.md "Forbidden
  Shortcuts", explicit TILOS
- az MCP szerver átírása, hogy ezt a táblát olvassa — input.md "Nem cél"
- `session_api` réteg bővítése `source_refs` lekérdezésére — input.md
  "Nem cél", külön jövőbeli job
- új outbox `job_type`/trigger bevezetése — input.md "Fontos architekturális
  döntés" / "Forbidden Shortcuts", explicit TILOS; a meglévő `index_turn`
  per-row tranzakciót használtam
- A `test_explain_documents_actual_plan_for_small_fixture` flaky teszt
  javítása — ez egy preexisting, ettől a jobtól független tesztkód
  (`test_vector_search.py`, `session-vector-search-api-001` jobból), a
  javítása nem ennek a jobnak a scope-ja (input.md nem említi a
  `session_idx.chunk_embeddings`/`vector_search` réteget egyáltalán)

## Risks

1. **Kulcs-/regex-illesztés korlátozott recall** — a `tool_call`/`file`
   szabályok csak a dokumentált, fix kulcsneveket ismerik fel
   (`tool_name`/`file_path`/`path`/`notebook_path`); egy más kulcsnevű
   provider/event-formátum (pl. egy jövőbeli hook, ami `filename` vagy
   `tool` kulcsot használna) néma módon 0 referenciát eredményezne, NEM
   hibát. Ez szándékos (input.md "státusz indoklás": "ez a job ezt nem
   méri" — a recall-mérés explicit jövőbeli, `candidate`-hez szükséges
   munka).
2. **`url` regex egyszerű, nem RFC-pontos** — `r'https?://\S+'` mohó
   `\S+` mintát használ, ami egy mondatvégi írásjelet (pl. egy záró pont
   vagy zárójel) is az URL részének tekinthet egyes szövegelrendezéseknél;
   nem implementáltam írásjel-levágást, mert input.md "2." csak egy "fix
   regex mintát" kér, nem RFC 3986-konform URL-parsing-ot, és egy
   túlbonyolított minta saját maga válna a nem-determinisztikus
   hibaforrássá.
3. **`test_explain_documents_actual_plan_for_small_fixture` flaky teszt** —
   ez a `session-vector-search-api-001` jobból származó, ettől a munkától
   FÜGGETLEN teszt a Postgres planner aktuális statisztika-állapotától
   függően Seq Scan helyett egy `Nested Loop` + `Index Scan ...
   chunk_embeddings_pkey` tervet választhat, amit a teszt assert-je nem fed
   le. Bizonyítottan (lásd "Findings" 7. pont) NEM ez a job okozta — `git
   stash`-elt, tiszta `main`-en önállóan futtatva is ugyanígy elhasal. Ez
   egy meglévő, ebben a jobban NEM javított hiányosság — javítása (pl. a
   teszt assert kibővítése a `Nested Loop`/`Index Scan` eset elfogadására,
   vagy a fixture méretének/`ANALYZE`-jának determinisztikusabbá tétele)
   külön jövőbeli munka.
4. **Egy turn, ahol ugyanaz a fájlútvonal/URL többször szerepel, többször
   beszúrásra kerül** — nincs `DISTINCT`/dedup logika a beszúrás előtt;
   minden `re.findall()`/kulcs-találat saját sort kap, még ha az érték
   azonos is egy korábbi sorral. Ez konzisztens az input.md "egy sor minden
   talált útvonalra"/"egy sor minden talált URL-re" megfogalmazásával
   (nem "egyedi" útvonalra/URL-re), de egy jövőbeli `candidate`-felé vezető
   munkánál érdemes lehet újragondolni.
5. **Nincs migrációs framework / idempotencia** — ugyanaz a limitáció,
   mint a korábbi jobokban (`session-postgres-schema.sql`,
   `session-chunk-indexer-migration.sql` stb.) — ez a job NEM adott hozzá
   új DDL-t (a `source_refs` tábla már létezett), így ez a kockázat
   ehhez a joghoz közvetlenül nem társul, de a teljes lánc öröksége.

## Definition Of Done Check

- [x] kinyerési szabályok definiálva, indokolva, NEM AI/LLM-alapúak —
  `session_store/chunk_indexer.py:1-99` (modul docstring "Source-ref
  extraction" szekció) + "Decisions Proposed" 2-4. pont
- [x] `extract_source_refs()` létezik, fájl:sor hivatkozással —
  `session_store/chunk_indexer.py:278`
- [x] `_fetch_turn()` bővítve `role`-lal, fájl:sor hivatkozással —
  `session_store/chunk_indexer.py:382-399`
- [x] az integráció a MEGLÉVŐ `_index_one_job()` per-row tranzakcióján
  belül történik (NEM új outbox job_type), kódidézettel bizonyítva —
  `session_store/chunk_indexer.py:498-560`, lásd Claim-Evidence Matrix
  3. sor
- [x] mind a 4 teszt-eset (tool_call, file, url, semmi) lefuttatva,
  tényleges SQL-eredmény idézve mindegyikre — lásd Claim-Evidence Matrix
  5-8. sor + a teljes outbox/source_refs idézetek
- [x] `content_hash` minden sorban kitöltve, a hash-függvény dokumentálva —
  `session_store/chunk_indexer.py:453-466` `_insert_source_ref()` +
  Claim-Evidence Matrix 9. sor (független Python-újraszámítás egyezik)
- [x] teljes meglévő teszt-suite lefuttatva, regresszió-mentesség
  bizonyítva — `53 passed, 1 failed`; az 1 hiba bizonyítottan preexisting
  (lásd Claim-Evidence Matrix 10. sor, "Findings" 7. pont, "Risks" 3. pont)
- [x] claim-evidence tábla kitöltve, nem üres — fent, 11 sor

## Next Jobs

1. **Recall-/pontosság-mérés valós session-adaton** (`candidate`
   státuszhoz) — input.md "státusz indoklás" explicit megnevezi ezt
   szükséges következő lépésként: mennyi tényleges fájl/URL/tool-hívás
   referenciát hagy ki a jelenlegi kulcs-/regex-készlet valós,
   begyűjtött session-adaton mérve.
2. **`session_api` réteg bővítése `source_refs` lekérdezésére** — input.md
   "Nem cél" explicit jövőbeli jobként nevezi meg (pl. egy
   `session_api.get_source_refs(p_chunk_id)` vagy hasonló stabil
   SQL-függvény, amit az MCP szerver hívhatna).
3. **`test_explain_documents_actual_plan_for_small_fixture` stabilizálása**
   — a "Risks" 3. pontban dokumentált preexisting flakiness javítása
   (a `session-vector-search-api-001` jobhoz tartozó teszt, nem ehhez a
   jobhoz).
4. **`session_idx.ranking_features` feltöltése** — a `session_core` család
   utolsó, még üres adattáblája a `source_refs` után.
