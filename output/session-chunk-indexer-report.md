# session-chunk-indexer-001 Output

## Scope

Ez a job megírja a MÁSODIK outbox-worker-t a `cic-mcp-session` session rétegében:
`session_core.turns` sorokat darabol `session_core.chunks`-ra, FTS-indexeli
(`session_idx.chunk_fts.tsv`), és lokális embedding-modellel feltölti a
`session_idx.chunk_embeddings`-t. Ehhez egy ÚJ outbox `job_type`-ot
(`index_turn`) és egy ÚJ, additív DDL-migrációt vezet be
(`output/session-chunk-indexer-migration.sql`), a meglévő
`session-postgres-schema.sql` MELLETT, nem helyette.

Nem cél (lásd input.md "Nem cél"): `session_core.source_refs` feltöltése,
`session_idx.ranking_features` feltöltése, `session_api.search_context()`
retrieval-minőség kiértékelése, multi-worker lock-olás, permanens futtatási
infrastruktúra, külső LLM/HTTP embedding API.

## Inputs Read

- `output/session-postgres-schema.sql` (teljes fájl) — `session_core.chunks`,
  `session_idx.chunk_fts`, `session_idx.chunk_embeddings`, `session_jobs.outbox`
  DDL, és a `trg_session_raw_envelopes_enqueue` trigger mintája
  (`session_raw.enqueue_projection_job()`, sorok 301-314)
- `session_store/turn_projector.py` (teljes fájl) — a per-row-tranzakciós
  outbox-feldolgozási minta (`_project_one_job`, `run_projection_batch`,
  `_fetch_pending_jobs` FOR UPDATE SKIP LOCKED, `_mark_failed_or_dead_letter`)
- `session_store/envelope_writer.py` (teljes fájl) — `SessionStoreConfig`
  (env-alapú DB connection), amit a chunk-indexer worker is újrahasznál
- `make_source.py` sorok 1-40, 260-320 — `EMBEDDING_MODEL` env var minta
  (`make_source.py:17`), `create_embeddings()` (`make_source.py:290`)
  `SentenceTransformer.encode(..., normalize_embeddings=True)` mintája
- `tests/test_session_store/test_turn_projector.py` (teljes fájl) — a
  meglévő e2e teszt szerkezete (fixture-ök, `_valid_envelope`, truncate
  pattern), amit a `test_chunk_indexer.py` követ
- `.cic-context/factory-docs/job-slices.yaml` — `session-chunk-indexer-001`
  bejegyzés (phase 3, acceptance_gates, required_evidence, forbidden_shortcuts)
- `.cic-context/factory-docs/architecture.md` "## Schema szeparacio" szekció

## Findings

1. A `session-postgres-schema.sql` `session_idx.chunk_embeddings.embedding`
   oszlopa `VECTOR(1536)` placeholder volt, a fájl saját kommentje (sorok
   215-218) EXPLICIT ezt a jobot nevezte meg a dimenzió-döntés felelőjének.
2. A repo meglévő konvenció-modellje (`make_source.py:17`,
   `paraphrase-multilingual-MiniLM-L12-v2`) TÉNYLEGES kimeneti dimenziója
   `model.encode(...)` hívással lekérdezve **384**, NEM 1536 — lásd
   "Claim-Evidence Matrix" a tényleges parancs+kimenet idézetért.
3. A `session_core.chunks` tábla DDL-je már tartalmazta a `chunk_seq`,
   `token_count`, `UNIQUE (turn_id, chunk_seq)` mezőket/constraint-et —
   ezekhez nem kellett migráció, csak a worker oldali kitöltés.
4. A `turn_projector.py` mintája (`_fetch_pending_jobs` FOR UPDATE SKIP
   LOCKED + `_project_one_job` per-row tranzakció + `_mark_failed_or_dead_letter`)
   1:1 átvehető volt a chunk-indexer-hez, csak a forrás-tábla (`session_core.turns`
   helyett `session_jobs.outbox`-ból olvasva) és a célsorok (chunks/fts/embeddings
   helyett sessions/turns) különböznek.
5. A migráció a meglévő, ÜRES `session_idx.chunk_embeddings` táblán
   `ALTER COLUMN ... TYPE VECTOR(384)`-et futtatott — ez sikeres volt, mivel
   a tábla még sosem kapott írást semelyik korábbi jobtól.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Az additív migráció (`output/session-chunk-indexer-migration.sql`) hiba nélkül alkalmazható egy valódi Postgres instance-en, a meglévő `session-postgres-schema.sql` UTÁN | proven | `docker exec -i session-chunk-indexer-test psql -U postgres -d testdb -v ON_ERROR_STOP=1 < output/session-chunk-indexer-migration.sql` kimenet: `ALTER TABLE` / `COMMENT` / `CREATE FUNCTION` / `CREATE TRIGGER` (4 statement, hiba nélkül, a meglévő schema sikeres alkalmazása UTÁN: `CREATE EXTENSION`×3, `CREATE SCHEMA`×5, `CREATE TYPE`×4, `CREATE TABLE`×11, stb.) | valódi Postgres (`pgvector/pgvector:pg16`, konténer `session-chunk-indexer-test`, port 55434) | alacsony — idempotencia nincs tesztelve (ismételt futtatás `CREATE TRIGGER` hibát adna; nincs migrációs framework, ugyanaz a limitáció mint az előző két jobban) |
| A választott embedding modell (`paraphrase-multilingual-MiniLM-L12-v2`) TÉNYLEGES kimeneti dimenziója 384, NEM 1536 | proven | `p_venv/bin/python -c "from sentence_transformers import SentenceTransformer; model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2'); emb = model.encode(['hello world test sentence'], normalize_embeddings=True); print('DIMENSION:', emb.shape)"` kimenet: `DIMENSION: (1, 384)` | valódi `model.encode()` hívás, nem dokumentáció-feltételezés | alacsony — egy modellverzió-frissítés (HuggingFace Hub-on) elméletileg változtathatná a dimenziót, de a sentence-transformers modell-azonosító verzió-pinning-elve szerint ez stabil |
| A migráció a `session_idx.chunk_embeddings.embedding` oszlopot `VECTOR(1536)`-ról `VECTOR(384)`-re módosítja | proven | `docker exec session-chunk-indexer-test psql -U postgres -d testdb -c "\d session_idx.chunk_embeddings"` kimenet: `embedding \| vector(384) \| ... \| not null` | valódi Postgres, schema introspekció migráció UTÁN | alacsony |
| Determinisztikus, dokumentált, NEM AI/LLM-alapú chunking-stratégia | proven | `session_store/chunk_indexer.py` `extract_text()` (sor 130-150), `split_into_chunks()` (sor 180-202) — pure function-ök, fix `CHUNK_SIZE_CHARS=1500`/`CHUNK_OVERLAP_CHARS=200` konstansok, nincs modell-hívás a chunk-határ döntésben; teszt: `test_split_into_chunks_is_deterministic_and_handles_short_text`, `test_split_into_chunks_produces_multiple_overlapping_windows` PASSED | unit teszt (nincs DB-igény ehhez a 2 teszthez) | alacsony — a chunk-méret/átfedés hangolatlan, lásd "Risks" |
| Worker függvény/modul létezik | proven | `session_store/chunk_indexer.py:378` `run_indexing_batch()`, `session_store/chunk_indexer.py:329` `_index_one_job()` | fájl:sor hivatkozás + `py_compile` siker | n/a |
| End-to-end teszt: turn → outbox(index_turn) → worker → chunks/chunk_fts/chunk_embeddings sorok → outbox done | proven | `pytest tests/test_session_store/test_chunk_indexer.py::test_full_chain_turn_to_chunks_fts_embeddings_and_outbox_done -v` kimenet: `PASSED` (lásd "Definition Of Done Check" a teljes pytest-kimenetért) | valódi Postgres ellen (nem mock) | alacsony |
| Hibakezelés teszt (nem létező turn_id → failed/dead_letter, nem kivétel) | proven | `pytest ...::test_dangling_turn_id_marks_outbox_failed_not_crash_not_stuck_pending -v` és `::test_dangling_turn_id_reaches_dead_letter_after_max_attempts -v` kimenet: mindkettő `PASSED` | valódi Postgres ellen | alacsony |
| Többszörös chunk-keletkezés helyes `chunk_seq` sorozattal | proven | `pytest ...::test_long_content_produces_multiple_chunks_with_correct_seq -v` kimenet: `PASSED`; a teszt explicit assertálja `seqs == list(range(1, len(chunks) + 1))` | valódi Postgres ellen, 1000 szavas (`"word " * 1000`) content-tel | alacsony |
| Embedding-dimenzió `vector_dims()`-szel ellenőrizve, megegyezik a deklarált oszlop-dimenzióval | proven | `pytest ...::test_embedding_dimension_matches_declared_column_dimension -v` kimenet: `PASSED`; a teszt explicit `SELECT vector_dims(embedding)` és `pg_attribute.atttypmod` lekérdezést futtat és `assert actual_dim == EXPECTED_EMBEDDING_DIM == declared_dim`-et hajt végre (384 == 384 == 384) | valódi Postgres ellen, `vector_dims()` SQL függvény | alacsony |
| Reachability: 0 külső (nem teszt, nem CLI) hívó | proven | `grep -rn "run_indexing_batch" --include="*.py" . \| grep -v "p_venv" \| grep -v "test_" \| grep -v "/tests/"` kimenet kizárólag `session_store/chunk_indexer.py:42` (docstring), `session_store/chunk_indexer.py:378` (definíció), `session_store/chunk_indexer.py:425` (`_main()` saját CLI hívása) — 0 külső hívó | mechanikus `grep -rn` | n/a — `scaffold`, lásd "Definition Of Done Check" |
| A meglévő `turn_projector` tesztek nem regresszáltak a migráció után | proven | `pytest tests/test_session_store/ -v` kimenet: `21 passed in 15.99s` (9 chunk_indexer + 6 envelope_writer + 6 turn_projector teszt, mindegyik `PASSED`) | valódi Postgres ellen, közös konténer, mindkét migráció alkalmazva | alacsony |

## Decisions Proposed

1. **Embedding modell**: `paraphrase-multilingual-MiniLM-L12-v2` (konzisztens
   `make_source.py:17`-tel). Indok: a session turn content vegyes
   magyar/angol (a repo `CLAUDE.md` kétnyelvű konvenciója szerint), a
   modell multilingual, és a repo már használja, NEM kellett új
   függőséget bevezetni (`sentence-transformers` már a
   `requirements.in`-ben). Nem találtam indokot eltérő modell
   választására.
2. **VECTOR(1536) → VECTOR(384)**: `ALTER TABLE ... ALTER COLUMN ... TYPE
   VECTOR(384)`, NEM táblát újra-létrehozás. Indok: a tábla a migráció
   pillanatában garantáltan üres (semelyik korábbi job nem írt bele —
   `session-turn-projector-001` explicit kizárta a `session_idx.*`
   feltöltést), ezért nincs adatvesztés/cast-kockázat, és a pgvector
   `ALTER COLUMN ... TYPE vector(n)` egy üres táblán metadata-only
   módosítás. A tábla újra-létrehozása (DROP+CREATE) extra munkát
   jelentett volna a HNSW index és az FK újra-létrehozásához, kockázat-
   csökkenés nélkül egy üres táblán.
3. **Chunking: fix méret + átfedés, NEM mondat/regex-határ**. Indok: a
   turn content gyakran félig-strukturált (tool output, JSON blob, kód),
   ahol naiv mondat-határ regex (". " stb.) rendszeresen rosszul vág
   (pl. "v1.2.3" vagy "e.g." közepén) — egy fix-méretű ablak
   determinisztikusabb és egyszerűbben tesztelhető. `CHUNK_SIZE_CHARS=1500`,
   `CHUNK_OVERLAP_CHARS=200` (az input.md "~1000-2000 karakteres" sávjában).
4. **Szöveg-kinyerés `content`-ből**: ismert kulcsok (`raw_text`, `text`,
   `content`, `message`) fix sorrendben, fallback a teljes objektum
   `json.dumps(sort_keys=True)` szerializációjára. Determinisztikus —
   ugyanaz a JSONB érték mindig ugyanazt a chunk-szöveget adja.
5. **FTS nyelv-konfiguráció: `to_tsvector('simple', ...)`**, NEM
   `'english'`/`'hungarian'`. Indok: a turn content vegyes nyelvű, egy
   nyelv-specifikus stemming-konfiguráció hallgatólagosan rosszul
   viselkedne a másik nyelven. A `simple` konfiguráció nem végez
   stemming/stopword-szűrést — biztonságos, nyelv-semleges
   alapbeállítás. A meglévő `session_api.search_context()` (változatlan,
   `plainto_tsquery('english', ...)`) és a `simple` tsvector
   interakciójának hangolása explicit jövőbeli, retrieval-minőség
   munka (lásd "Nem cél").
6. **Worker szerkezet**: a `turn_projector.py` mintáját 1:1 átvettem
   (`_fetch_pending_jobs` FOR UPDATE SKIP LOCKED, `_index_one_job`
   per-row tranzakció, `_mark_failed_or_dead_letter` attempts/max_attempts
   logika) — nem találtam ki újra a hibakezelést.

## Rejected / Out Of Scope

- `session_core.source_refs` feltöltése — külön job (input.md "Nem cél")
- `session_idx.ranking_features` feltöltése — külön job
- `session_api.search_context()` retrieval-minőség kiértékelése — a
  `candidate` státuszhoz szükséges, ebben a jobban nincs mérve
- Konkurens, multi-worker-instance lock-olás/claim-mechanizmus — ugyanaz a
  single-worker feltétel, mint a `turn_projector`-nál (lásd "Risks")
- Permanens futtatási infrastruktúra (cron/supervisor/systemd timer)
- Külső LLM/HTTP embedding API — kizárólag lokális modell használva
- Migrációs framework/idempotens migráció — ugyanaz a limitáció, mint a
  `session-postgres-schema.sql`-nél (nincs migrációs eszköz bekötve,
  ismételt futtatás `CREATE TRIGGER`-en hibázna)
- `chunk_id` szintű `source_refs` vagy `ranking_features` populálás a
  worker-ből (csak `chunks`/`chunk_fts`/`chunk_embeddings`, az input.md
  scope-ja szerint)

## Risks

1. **Single-worker feltétel** — a `_fetch_pending_jobs` FOR UPDATE SKIP
   LOCKED megelőzi a duplikált feldolgozást egy worker-instance-en belül,
   de NEM implementál explicit claim/locking mechanizmust több
   egyidejű worker-instance-hez (ugyanaz a limitáció, mint
   `turn_projector`-nál, dokumentálva ott is).
2. **Chunk-méret/átfedés hangolatlan** — `CHUNK_SIZE_CHARS=1500`/
   `CHUNK_OVERLAP_CHARS=200` egy ésszerű, dokumentált alapérték, de a
   retrieval-minőségre gyakorolt hatása nincs mérve valós session-adatokon
   — ez a `status indoklás` szerint pontosan az, ami a `candidate`
   státuszhoz hiányzik.
3. **Embedding-hívás soronkénti (per-chunk), nem batch-elt** —
   `_index_one_job` minden chunk-hoz külön `embed_texts([piece])` hívást
   tesz, NEM gyűjti össze egy job batch összes chunk-ját egy
   `model.encode(texts)` hívásba. Ez egyszerűbb hibahatárolást ad
   (egy chunk embedding-hibája nem dobja el a többit), de performance
   szempontból szuboptimális nagy batch-eknél — elfogadható tradeoff
   `experimental` státusznál, dokumentált limitáció.
4. **Migráció nem idempotens** — `CREATE TRIGGER` (nem `CREATE OR REPLACE
   TRIGGER`, Postgres 16-ban nincs ilyen szintaxis triggerekhez) duplikált
   futtatásnál hibázna. Ugyanaz a limitáció, mint a meglévő
   `session-postgres-schema.sql`-nél — nincs migrációs framework.
5. **`atttypmod` formátum-függés** — a `test_embedding_dimension_matches_declared_column_dimension`
   teszt a `pg_attribute.atttypmod`-ot olvassa a deklarált dimenzió
   ellenőrzéséhez; ez pgvector belső reprezentációjára támaszkodik
   (jelenleg működik, de nem dokumentált stabil API a pgvector projekt
   részéről).

## Definition Of Done Check

- [x] migráció (`output/session-chunk-indexer-migration.sql`) alkalmazva
      egy valódi Postgres instance-en, a meglévő `session-postgres-schema.sql`
      UTÁN, hiba nélkül, kimenet idézve — lásd Claim-Evidence Matrix 1. sor
- [x] chunking-stratégia definiálva és indokolva (determinisztikus, NEM
      AI/LLM-alapú) — lásd "Decisions Proposed" 3-4. pont,
      `session_store/chunk_indexer.py:130-202`
- [x] worker függvény/modul létezik, fájl:sor hivatkozással —
      `session_store/chunk_indexer.py:378` (`run_indexing_batch`),
      `session_store/chunk_indexer.py:329` (`_index_one_job`)
- [x] a választott embedding modell neve + TÉNYLEGES dimenziója
      dokumentálva (384, NEM 1536), a migráció kezeli, indoklással —
      lásd Claim-Evidence Matrix 2-3. sor, "Decisions Proposed" 2. pont
- [x] end-to-end teszt lefuttatva, kimenet idézve:

  ```
  tests/test_session_store/test_chunk_indexer.py::test_full_chain_turn_to_chunks_fts_embeddings_and_outbox_done PASSED [ 55%]
  ```

- [x] hibakezelés teszt (nem létező turn_id) lefuttatva, kimenet idézve:

  ```
  tests/test_session_store/test_chunk_indexer.py::test_dangling_turn_id_marks_outbox_failed_not_crash_not_stuck_pending PASSED [ 66%]
  tests/test_session_store/test_chunk_indexer.py::test_dangling_turn_id_reaches_dead_letter_after_max_attempts PASSED [ 77%]
  ```

- [x] többszörös chunk-keletkezés tesztelve, kimenet idézve:

  ```
  tests/test_session_store/test_chunk_indexer.py::test_long_content_produces_multiple_chunks_with_correct_seq PASSED [ 88%]
  ```

- [x] embedding-dimenzió teszt (`vector_dims(embedding)`) lefuttatva,
      kimenet idézve:

  ```
  tests/test_session_store/test_chunk_indexer.py::test_embedding_dimension_matches_declared_column_dimension PASSED [100%]
  ```

  Teljes futás (9 teszt, mind PASSED):

  ```
  ============================= test session starts ==============================
  collected 9 items

  tests/test_session_store/test_chunk_indexer.py::test_extract_text_uses_known_keys_in_documented_order PASSED [ 11%]
  tests/test_session_store/test_chunk_indexer.py::test_extract_text_falls_back_to_sorted_json_for_unknown_shape PASSED [ 22%]
  tests/test_session_store/test_chunk_indexer.py::test_split_into_chunks_is_deterministic_and_handles_short_text PASSED [ 33%]
  tests/test_session_store/test_chunk_indexer.py::test_split_into_chunks_produces_multiple_overlapping_windows PASSED [ 44%]
  tests/test_session_store/test_chunk_indexer.py::test_full_chain_turn_to_chunks_fts_embeddings_and_outbox_done PASSED [ 55%]
  tests/test_session_store/test_chunk_indexer.py::test_dangling_turn_id_marks_outbox_failed_not_crash_not_stuck_pending PASSED [ 66%]
  tests/test_session_store/test_chunk_indexer.py::test_dangling_turn_id_reaches_dead_letter_after_max_attempts PASSED [ 77%]
  tests/test_session_store/test_chunk_indexer.py::test_long_content_produces_multiple_chunks_with_correct_seq PASSED [ 88%]
  tests/test_session_store/test_chunk_indexer.py::test_embedding_dimension_matches_declared_column_dimension PASSED [100%]
  ============================== 9 passed in 14.18s ==============================
  ```

  Teljes `tests/test_session_store/` suite (regresszió-ellenőrzés a
  `turn_projector`-ra is, közös konténeren, mindkét migráció alkalmazva):

  ```
  21 passed in 15.99s
  ```

- [x] reachability `grep -rn` eredmény idézve, `scaffold`/`proven`
      megkülönböztetve:

  ```
  $ grep -rn "run_indexing_batch" --include="*.py" . | grep -v "p_venv" | grep -v "test_" | grep -v "/tests/"
  session_store/chunk_indexer.py:42:run_indexing_batch().
  session_store/chunk_indexer.py:378:def run_indexing_batch(config: SessionStoreConfig | None = None) -> list[IndexingResult]:
  session_store/chunk_indexer.py:425:    results = run_indexing_batch()
  ```

  Mindhárom találat a saját modulon belül van: sor 42 docstring-utalás,
  sor 378 a függvény definíciója, sor 425 a `_main()` CLI entry point
  saját hívása (`python -m session_store.chunk_indexer`). **0 KÜLSŐ
  hívó** — ez `scaffold`: a worker futtatható (CLI-n és teszten
  keresztül `proven`), de nincs production hívási lánc (nincs
  cron/supervisor/MCP-szerver wiring, ami rendszeresen futtatná) —
  ugyanaz a megkülönböztetés, mint `turn_projector.run_projection_batch`
  esetén az előző jobban.

- [x] claim-evidence tábla kitöltve, nem üres — lásd fent, 11 sor

## Next Jobs

- Retrieval-minőség kiértékelés valós session-adatokon
  (`session_api.search_context()` ellen) — szükséges a `candidate`
  státuszhoz, ahogy a "status indoklás" jelzi.
- `session_core.source_refs` feltöltő worker (külön job, lásd "Nem cél").
- `session_idx.ranking_features` feltöltő worker (külön job).
- Production wiring (cron/supervisor/MCP-szerver hívási lánc) a
  `chunk_indexer.run_indexing_batch()`-hez és a `turn_projector.run_projection_batch()`-hez
  egyaránt — jelenleg mindkettő `scaffold`.
- Chunk-méret/átfedés hangolása valós retrieval-mérés alapján.
