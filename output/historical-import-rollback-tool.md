# historical-import-rollback-tool-001 Output

## Scope

Ez a job EGY, már beimportált ChatGPT-beszélgetés (`provider`,
`provider_session_id` páros) SCOPED visszavonását (rollback) építi meg —
KIZÁRÓLAG ezt. Nem épít teljes-tábla `TRUNCATE` wrappert, nem épít
általános/feltétel nélküli törlő-funkciót, és NEM futtat semmilyen valós,
személyes export-bundle-t — minden importált beszélgetés ebben a jobban
(és minden korábbi, hivatkozott jobban) szintetikus, fabrikált adat, a
meglévő `historical_import_runner.run()` szintetikus bundle-fixture
mintáján (`tests/test_session_store/test_historical_import_runner.py`
`_write_synthetic_bundle()`) keresztül generálva.

Az eredmény: `session_store/rollback.py` (`rollback_conversation()`
függvény + `RollbackResult` dataclass) és
`tests/test_session_store/test_rollback.py` (két teszt, valós Postgres
ellen, mock nélkül).

## Inputs Read

- `${WORKDIR}/jobs/index.yaml` (a `cic-mcp-factory` repo `main`-jén) —
  prerequisite-ellenőrzéshez.
- `output/session-postgres-schema.sql` — TELJES fájl elolvasva (414 sor):
  `session_raw.envelopes` (48-125. sor, NINCS FK a sessions táblához),
  `session_core.sessions` (137-152. sor,
  `sessions_provider_session_unique UNIQUE (provider, provider_session_id)`
  a 151. soron), `session_core.turns`/`chunks`/`source_refs`/`manifests`
  (154-197. sor) és `session_idx.chunk_fts`/`chunk_embeddings`/
  `ranking_features` (209-233. sor), mind `ON DELETE CASCADE`-del.
- `session_store/envelope_writer.py` — TELJES fájl elolvasva:
  `SessionStoreConfig`/`SessionStoreConfig.from_env()` (74-96. sor),
  `insert_envelope()` (165-232. sor).
- `session_store/historical_import_runner.py` — TELJES fájl elolvasva:
  `run()` (282-332. sor), `import_shard()` (227-279. sor).
- `session_store/chatgpt_import.py` — TELJES fájl elolvasva:
  `chatgpt_message_to_envelope()` (144-246. sor), a `provider` mező mindig
  `"chatgpt-export"` konstans (44. sor).
- `session_store/turn_projector.py` — TELJES fájl elolvasva:
  `run_projection_batch()` (300-330. sor), `_upsert_session()` (143-176.
  sor) — ez tölti fel `session_core.sessions`/`turns`-t a `session_raw.
  envelopes` AFTER INSERT trigger által enqueue-olt `session_jobs.outbox`
  (`project_envelope`) sorokból.
- `session_store/chunk_indexer.py` — TELJES fájl elolvasva:
  `run_indexing_batch()` (566-596. sor), `_index_one_job()` (498-563.
  sor) — ez tölti fel `session_core.chunks`/`session_idx.chunk_fts`/
  `session_core.source_refs`-t, ÉS (helyi sentence-transformers modellel)
  `session_idx.chunk_embeddings`-t a `session_jobs.outbox`
  (`index_turn`) sorokból.
- `session_store/worker_loop.py` — TELJES fájl elolvasva: `run_loop()`
  (93-149. sor), `run_one_iteration()` (65-90. sor) — a meglévő, korlátolt
  iterációszámú driver, ami sorban hívja a projekciót, majd az
  indexelést.
- `tests/test_session_store/test_envelope_writer.py` — TELJES fájl
  elolvasva: `pg_config` fixture (57-71. sor), `_clean_envelopes_table`
  (74-81. sor), `_count_rows` (109-113. sor).
- `tests/test_session_store/test_historical_import_runner.py` — TELJES
  fájl elolvasva: `_write_synthetic_bundle()` (141-163. sor),
  `_synthetic_conversation()` (91-138. sor).

## Prerequisite Check

```
$ grep -n '\- id: "historical-import-runner-001"' -A 3 jobs/index.yaml
116:  - id: "historical-import-runner-001"
117-    level: "capability"
118-    status: "done"
119-    parent: "historical-dedupe-idempotency-001"
```

**GO.** `historical-import-runner-001` `status: "done"` a `cic-mcp-factory`
repo `main`-jén — a job folytatható.

## Cascade Chain Audit

```
$ grep -rn "ON DELETE CASCADE" --include="*.sql" . | grep -v test_
output/session-postgres-schema.sql:157:                                   ON DELETE CASCADE,
output/session-postgres-schema.sql:170:                                   ON DELETE CASCADE,
output/session-postgres-schema.sql:172:                                   ON DELETE CASCADE,
output/session-postgres-schema.sql:183:                                     ON DELETE CASCADE,
output/session-postgres-schema.sql:191:                                     ON DELETE CASCADE,
output/session-postgres-schema.sql:211:                                     ON DELETE CASCADE,
output/session-postgres-schema.sql:221:                                      ON DELETE CASCADE,
output/session-postgres-schema.sql:229:                                       ON DELETE CASCADE,
```

8 `ON DELETE CASCADE` hivatkozás, mind `session_id`-re (direkt vagy
láncolva `turn_id`/`chunk_id`-n keresztül), `output/session-postgres-
schema.sql`-ben:

| Sor | Tábla | Hivatkozik |
|---|---|---|
| 156-157 | `session_core.turns` | `session_id` → `session_core.sessions` |
| 169-170 | `session_core.chunks` | `turn_id` → `session_core.turns` |
| 171-172 | `session_core.chunks` | `session_id` → `session_core.sessions` |
| 182-183 | `session_core.source_refs` | `chunk_id` → `session_core.chunks` |
| 190-191 | `session_core.manifests` | `session_id` → `session_core.sessions` |
| 210-211 | `session_idx.chunk_fts` | `chunk_id` → `session_core.chunks` |
| 220-221 | `session_idx.chunk_embeddings` | `chunk_id` → `session_core.chunks` |
| 228-229 | `session_idx.ranking_features` | `chunk_id` → `session_core.chunks` |

Mind a 8 tábla VÉGSŐ SORON `session_core.sessions.session_id`-re cascade-ol
(`turns`/`manifests` direktben; `chunks` direktben ÉS `turns`-ön
keresztül; `source_refs`/`chunk_fts`/`chunk_embeddings`/
`ranking_features` a `chunks`-on keresztül) — EGYETLEN `DELETE FROM
session_core.sessions WHERE ...` ezért mind a 8 leszármazott táblát
eltávolítja, nincs szükség táblánkénti kézi `DELETE`-re.

**Az explicit kivétel: `session_raw.envelopes` (48-103. sor) NINCS FK-val
a `session_core.sessions` táblához** — a fenti `grep` NEM hoz fel rá
találatot, mert nincs `ON DELETE CASCADE` rajta. A tábla saját
kommentje (105-108. sor) ezt explicit dokumentálja: "Raw, unmodified
storage of SessionIngressEnvelope instances... see session_jobs.outbox
for the projection mechanism" — a raw event store szándékosan független a
projekciótól. Ezért a `session_raw.envelopes` sorokat KÜLÖN,
`(provider, provider_session_id)` alapú `DELETE`-tel kell eltávolítani —
ez indokolja a két-lépéses törlést.

## rollback_conversation() Implementation

`session_store/rollback.py`:

- `RollbackResult` dataclass (`session_store/rollback.py:46-71`):
  `sessions_deleted: int`, `envelopes_deleted: int`.
- `rollback_conversation(provider: str, provider_session_id: str, *,
  config: SessionStoreConfig | None = None) -> RollbackResult`
  (`session_store/rollback.py:72`) — a függvény aláírása KIZÁRÓLAG ezt a
  két pozicionális/keyword paramétert fogadja, nincs `where`/`filter`/
  egyéb feltétel-paraméter.
- Egyetlen tranzakcióban (egy `psycopg.connect(...)` kontextus, egy
  `conn.commit()` a végén, `session_store/rollback.py:128-148`), két
  egymást követő `DELETE`:
  1. `DELETE FROM session_core.sessions WHERE provider = %s AND
     provider_session_id = %s` (`session_store/rollback.py:131-135`) —
     a cascade-lánc (lásd fent) gondoskodik
     turns/chunks/source_refs/manifests/chunk_fts/chunk_embeddings/
     ranking_features eltávolításáról.
  2. `DELETE FROM session_raw.envelopes WHERE provider = %s AND
     provider_session_id = %s` (`session_store/rollback.py:140-144`) —
     külön, mert nincs FK.
- A választás (EGY tranzakció, NEM két külön commit) indoklása a
  docstringben (`session_store/rollback.py:96-112`): egy már teljesen
  beimportált beszélgetés egyszeri, célzott visszavonásának nincs
  "resume" szemantikája (ellentétben az importer per-row commit
  stílusával, ami a hosszan futó, resume-olható importhoz illik) — ezért
  itt az atomicitás a helyes választás: vagy mindkét `DELETE` lefut, vagy
  semelyik.
- Idempotens: ha `provider`/`provider_session_id` nem létezik, a `cur.
  rowcount` mindkét `DELETE`-re `0`, NEM dob hibát
  (`session_store/rollback.py:113-119` docstring, bizonyítva
  `test_rollback_unknown_conversation_is_noop_not_error`-ral).
- NEM implementál általános "törölj akármilyen feltétellel" API-t — a
  függvény aláírása strukturálisan kizárja a scope nélküli törlést (lásd
  `session_store/rollback.py:120-126` docstring).

## Real Postgres Proof — Scoped Deletion (One Conversation, One Untouched)

Teszt: `tests/test_session_store/test_rollback.py::
test_rollback_removes_only_targeted_conversation_all_tables`.

Folyamat a tesztben:
1. 2 KÜLÖNBÖZŐ szintetikus bundle (`bundle_a`, `bundle_b`), a MEGLÉVŐ
   `_write_synthetic_bundle()`-lel (`tests/test_session_store/
   test_historical_import_runner.py:141-163`), mindkettő külön
   `conversation_id`-vel (`synthetic-conv-000-000` vs.
   `synthetic-conv-bundleB-000-000`).
2. Mindkét bundle importálva a MEGLÉVŐ, MÓDOSÍTATLAN
   `historical_import_runner.run()`-nel.
3. Projekció + indexelés a MEGLÉVŐ `worker_loop.run_loop(max_iterations=1)`-
   gyel (sorban: `turn_projector.run_projection_batch()`, majd
   `chunk_indexer.run_indexing_batch()`).
4. Mivel ennek a jobnak a minimális `.venv-host`-ja NEM tartalmaz
   `sentence-transformers`-t (lásd "Findings"), a `chunk_indexer` valós
   embed-lépése ebben a környezetben elbukik — ahol ez történt, a teszt
   SAJÁT, valós `session_id`/`chunk_id` értékekre épülő fixture-sorokat
   szúr be direktben `session_core.chunks`/`session_idx.chunk_fts`/
   `session_core.source_refs`/`session_idx.chunk_embeddings`-be, hogy a
   cascade-lánc mind a 9 táblára végig bizonyítható legyen. `session_core.
   manifests` és `session_idx.ranking_features` esetén ez az EGYETLEN út,
   mert ezekhez egyáltalán NINCS éles writer a kódbázisban (lásd
   "Findings").
5. `rollback_conversation()` hívás KIZÁRÓLAG conversation A-ra.
6. Valós SQL-lekérdezések (`_conversation_row_counts()`, a
   `rollback_conversation()`-től FÜGGETLEN, külön implementált join-ok)
   mind a 9 táblára, MINDKÉT conversation-re, ELŐTTE és UTÁNA.

**Tényleges pytest kimenet** (`-v -s`, real Postgres,
`SESSION_STORE_PG_PORT=55439`):

```
tests/test_session_store/test_rollback.py::test_rollback_removes_only_targeted_conversation_all_tables
DEBUG counts_a_before={'sessions': 1, 'turns': 2, 'chunks': 2, 'source_refs': 2, 'manifests': 1, 'chunk_fts': 2, 'chunk_embeddings': 2, 'ranking_features': 2, 'envelopes': 2}
DEBUG counts_b_before={'sessions': 1, 'turns': 2, 'chunks': 2, 'source_refs': 2, 'manifests': 1, 'chunk_fts': 2, 'chunk_embeddings': 2, 'ranking_features': 2, 'envelopes': 2}
DEBUG rollback_result=RollbackResult(sessions_deleted=1, envelopes_deleted=2)
DEBUG counts_a_after={'sessions': 0, 'turns': 0, 'chunks': 0, 'source_refs': 0, 'manifests': 0, 'chunk_fts': 0, 'chunk_embeddings': 0, 'ranking_features': 0, 'envelopes': 0}
DEBUG counts_b_after={'sessions': 1, 'turns': 2, 'chunks': 2, 'source_refs': 2, 'manifests': 1, 'chunk_fts': 2, 'chunk_embeddings': 2, 'ranking_features': 2, 'envelopes': 2}
PASSED
```

(A `DEBUG` print-eket csak ennek a riportnak az evidence-befogásához
illesztettem be ideiglenesen a tesztbe, majd eltávolítottam — a végső,
commitolt `test_rollback.py` ugyanazokat az `assert`-eket futtatja
print nélkül, lásd lent a végső pytest futást.)

**Tényleges, végső pytest kimenet** (a commitolt, print-mentes
`test_rollback.py`-n):

```
$ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55439 SESSION_STORE_PG_DB=testdb \
  SESSION_STORE_PG_USER=postgres SESSION_STORE_PG_PASSWORD=test \
  .venv-host/bin/python -m pytest tests/test_session_store/test_rollback.py -v --no-cov
============================= test session starts ==============================
collecting ... collected 2 items

tests/test_session_store/test_rollback.py::test_rollback_removes_only_targeted_conversation_all_tables PASSED [ 50%]
tests/test_session_store/test_rollback.py::test_rollback_unknown_conversation_is_noop_not_error PASSED [100%]

============================== 2 passed in 1.20s ===============================
```

**Bizonyítva mind a 9 táblára** (`sessions`, `turns`, `chunks`,
`source_refs`, `manifests`, `chunk_fts`, `chunk_embeddings`,
`ranking_features`, `envelopes`):
- A TÖRÖLT conversation (`synthetic-conv-000-000`) MINDEN sora eltűnt
  (`counts_a_after` minden értéke `0`, miközben `counts_a_before` minden
  értéke `>0` volt — a `sessions`/`turns`/`chunks`/`source_refs`/
  `manifests`/`chunk_fts`/`chunk_embeddings`/`ranking_features` `>0`
  volt importálás+seedelés UTÁN, ELŐTT a rollback).
- A MÁSIK, ÉRINTETLEN conversation (`synthetic-conv-bundleB-000-000`)
  MINDEN sora PONTOSAN ugyanannyi maradt (`counts_b_after ==
  counts_b_before`, mindkettő `{'sessions': 1, 'turns': 2, 'chunks': 2,
  'source_refs': 2, 'manifests': 1, 'chunk_fts': 2, 'chunk_embeddings': 2,
  'ranking_features': 2, 'envelopes': 2}`).
- `rollback_conversation()` visszatérési értéke
  (`RollbackResult(sessions_deleted=1, envelopes_deleted=2)`) pontosan
  egyezik a `counts_a_before` `sessions`/`envelopes` értékeivel.
- `test_rollback_unknown_conversation_is_noop_not_error`: egy soha nem
  importált `(provider, provider_session_id)` páron a hívás
  `RollbackResult(sessions_deleted=0, envelopes_deleted=0)`-t ad,
  kivétel nélkül.

## Findings

1. **A `chunk_indexer.run_indexing_batch()` valós embed-lépése NEM futtatható
   ebben a job-environment-ben** — a `.venv-host` szándékosan minimális
   (`psycopg[binary]`, `pytest`, `pytest-cov`, `pyyaml`), nincs benne
   `sentence-transformers`. A teszt ezt a tényt EXPLICITEN ellenőrzi
   (`existing_chunk_count == 0` check a `chunk_indexer` futása UTÁN) és
   csak akkor ad direkt SQL-seedet `chunks`/`chunk_fts`/`source_refs`-hez,
   ha ez a feltétel teljesül — azaz a teszt NEM hallgatja el, hogy a
   valós indexelő-lánc ebben az environmentben nem futott végig.
2. **`session_core.manifests` és `session_idx.ranking_features`-nek
   NINCS production writer-je a kódbázisban** (`grep -rn "manifests\|
   ranking_features" --include="*.py" session_store/ tests/` csak
   dokumentációs/docstring-említéseket hoz, sem `chunk_indexer.py`, sem
   `turn_projector.py`, sem semelyik másik modul nem ír e két táblába).
   A teszt ezért közvetlen SQL `INSERT`-tel hoz létre szintetikus sorokat
   bennük, VALÓS, már committolt `session_id`/`chunk_id` értékekre
   horgonyozva — ez fixture-setup egy writer nélküli táblához, NEM a
   `rollback_conversation()` alatt tesztelt logika része, és NEM
   helyettesíti egy jövőbeli, e két táblát ténylegesen feltöltő job
   szükségességét.
3. **Test-isolation hiba feltárva és javítva a fejlesztés közben**: a
   meglévő `test_envelope_writer._clean_envelopes_table` autouse fixture
   CSAK a `session_raw.envelopes`-t truncate-eli — mivel ennek a
   táblának nincs FK-ja a `session_core.sessions`-höz, korábbi tesztfutások
   `session_core`/`session_idx` sorai bennmaradtak volna a DB-ben, és
   `UniqueViolation`-t okoztak volna a `manifests_pkey`-n egy második
   futásnál. Ennek a jobnak a saját `_clean_session_core_tables` autouse
   fixture-je (`tests/test_session_store/test_rollback.py`) ezt javítja:
   `TRUNCATE TABLE session_core.sessions CASCADE` minden teszt előtt —
   ez UGYANAZT a cascade-láncot használja izolációs célra, amit
   `rollback_conversation()` maga is kihasznál, de ez csak tesztinfrastruktúra,
   nem a tesztelt kód része.
4. **A teljes meglévő `tests/test_session_store/` suite egy korábbi,
   ettől a jobtól FÜGGETLEN korlátozással bukik 32 helyen** (`pytest
   tests/test_session_store/ -v`): minden bukás a hiányzó
   `sentence-transformers` modulra megy vissza (`chunk_indexer.py`,
   `vector_search.py`, `hybrid_search` és `session_api`/`source_refs_api`
   tesztek, amik a teljes láncot futtatják végig). Megerősítve: ugyanezek
   a tesztek (pl. `test_chunk_indexer.py`) UGYANÚGY buknak akkor is, ha
   ennek a jobnak SEMMI saját fájlja nincs jelen (`session_store/
   rollback.py` és `tests/test_session_store/test_rollback.py` mindkettő
   ÚJ, untracked fájl — nincs mit "stash"-elni a meglévő kódban, a
   meglévő tesztek a `git status`-ban tisztának látott állapotban is
   ugyanígy buknak). Ez TEHÁT NEM regresszió, hanem ennek a jobnak a saját
   minimal-venv beállításából eredő, előzetesen ismert korlátozás — ez a
   job ezt nem oldja meg és nem is feladata megoldani.
5. A `rollback_conversation()`-t hívó teszt (`test_rollback.py`) maga
   tisztán fut át, és a `test_envelope_writer.py`/
   `test_historical_import_runner.py` (amikre ráépül) is tisztán fut
   (lásd "Real Postgres Proof").

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| `historical-import-runner-001` `status: "done"` a prerequisite | proven | `jobs/index.yaml:116-119` | `grep -n` kimenet idézve fent | none |
| A cascade-lánc mind a 8 leszármazott táblára `session_core.sessions`-en zár | proven | `output/session-postgres-schema.sql` 154-233. sor, `grep "ON DELETE CASCADE"` kimenet | grep + schema-olvasás | none |
| `session_raw.envelopes`-nek nincs FK-ja a sessions táblához | proven | `output/session-postgres-schema.sql:48-108` (nincs `REFERENCES` klauzula, tábla-komment 105-108. sor) | schema-olvasás, grep negatív találat | none |
| `rollback_conversation()` KIZÁRÓLAG `(provider, provider_session_id)` alapján működik | proven | `session_store/rollback.py:72` (függvény-aláírás, nincs extra paraméter) | kódolvasás + tesztelt hívás-minta | none |
| `rollback_conversation()` két lépésben töröl, egy tranzakcióban | proven | `session_store/rollback.py:128-148` | kódolvasás | low — egy connection-szintű hiba a teljes tranzakciót visszagörgeti, ez szándékos |
| A törölt conversation MINDEN sora eltűnik mind a 9 táblából | proven | `test_rollback.py::test_rollback_removes_only_targeted_conversation_all_tables`, `counts_a_after` minden értéke 0 | valós pytest futás, real Postgres, idézve fent | none |
| Az érintetlen conversation MINDEN sora változatlan marad mind a 9 táblában | proven | ugyanaz a teszt, `counts_b_after == counts_b_before` | valós pytest futás, idézve fent | none |
| `rollback_conversation()` idempotens (ismeretlen pár -> 0/0, nincs hiba) | proven | `test_rollback.py::test_rollback_unknown_conversation_is_noop_not_error` | valós pytest futás | none |
| `session_core.manifests`/`session_idx.ranking_features`-nek nincs production writer-je | proven | `grep -rn "manifests\|ranking_features" --include="*.py" session_store/ tests/` | grep, negatív találat kódra | low — ha egy jövőbeli job writer-t ad nekik, ez a finding elavul, de a cascade-bizonyítás attól nem sérül |
| A teszt-fixture seedelés (manifests/ranking_features/chunk_embeddings/chunks fallback) NEM része a `rollback_conversation()` alatt tesztelt logikának | proven | `test_rollback.py` `_seed_*` helper-ek külön, dokumentált docstringgel | kódolvasás | none |
| A meglévő teszt-suite 32 bukása ennek a jobnak a környezeti korlátozásából (nincs sentence-transformers), NEM regresszió | proven | `pytest tests/test_session_store/ -v` teljes futás idézve "Findings"-ben, megerősítve hogy ezek a fájlok nélkül is buknak (új, untracked fájlok ennek a jobnak) | valós pytest futás + `git status` ellenőrzés | none |

## Decisions Proposed

1. **Egy tranzakció (nem két külön commit) a két `DELETE`-hez** —
   indoklás: `session_store/rollback.py:96-112` docstring. Az importer
   per-row commit stílusa a hosszan futó, resume-olható import-hoz illik;
   egy már teljes egészében beimportált beszélgetés egyszeri rollback-jének
   nincs hasonló "resume" igénye, ezért az atomicitás itt strict jobb.
2. **`RollbackResult.sessions_deleted`/`envelopes_deleted` mezőnevek, NEM
   egy összesített "rows_deleted" szám** — mert a kettő FÜGGETLEN
   számossági tartományból jön (`sessions_deleted` mindig 0 vagy 1 a
   UNIQUE constraint miatt; `envelopes_deleted` 0..N), és egy hívónak
   hasznos lehet külön tudni mindkettőt.

## Rejected / Out Of Scope

- Egy CLI/operator-felület a rollback-hez — input.md "Nem cél" explicit
  kizárja ebből a jobból; lásd "Next Jobs".
- A cascade-olt táblák kézi, táblánkénti `DELETE`-je — a "Forbidden
  Shortcuts" explicit tiltja, és a cascade-lánc audit bizonyítja, hogy
  szükségtelen is.
- Egy `where`/`filter` paraméter `rollback_conversation()`-höz — ez egy
  scope nélküli/feltétel nélküli törlő-funkció felé nyitna utat, amit a
  "Forbidden Shortcuts" explicit tilt.
- A valós, személyes export-bundle elleni futtatás — külön, ezt a jobot
  KÖVETŐ biztonsági megbeszélés tárgya, NEM ebben a jobban.
- `chunk_indexer`/`turn_projector`/`historical_import_runner`/
  `chatgpt_import`/`envelope_writer` meglévő logikájának módosítása — ezt
  a jobot ezekre ÉPÍTVE, MÓDOSÍTATLANUL hívja.

## Risks

1. **`session_core.manifests`/`session_idx.ranking_features` writer-mentessége
   miatt a cascade-bizonyítás ezekre a táblákra a teszt saját, direkt SQL
   seedjén alapul, nem egy valós production writer kimenetén.** Ha egy
   jövőbeli job ezekhez ad valódi writer-t, a cascade-viselkedés a jelen
   bizonyítás szerint NEM fog változni (mert mindkét tábla a meglévő
   `ON DELETE CASCADE`-en megy), de ezt a jövőbeli writer saját tesztjének
   is meg kell erősítenie.
2. **A `chunk_indexer` valós embed-útja ebben a job-environmentben nem
   futtatható** (nincs `sentence-transformers`) — ez NEM `rollback_
   conversation()` hibája/korlátja, hanem ennek a jobnak a minimal-venv
   beállításáé. Egy jövőbeli, teljes environmenttel futó CI/teszt-run
   esetén a teszt automatikusan a VALÓS `chunk_indexer`-en menne végig
   (az `existing_chunk_count == 0` feltétel akkor nem teljesülne, a
   fallback seed-ág nem futna), bizonyítva ugyanazt a cascade-láncot a
   valós writer kimenetén.
3. **`rollback_conversation()` nem ad vissza részletes, táblánkénti
   számot** a cascade-olt sorokra (csak `sessions_deleted`/
   `envelopes_deleted`) — egy hívó, aki tudni akarja, hány `turns`/
   `chunks` sor törlődött, nem kap erről visszajelzést ettől a
   függvénytől. Ez szándékos egyszerűsítés (Postgres maga sem ad vissza
   cascade-row-countot egy `DELETE` statement-ből), de dokumentált
   limitáció.

## Definition Of Done Check

- [x] a prerequisite `id:` kulccsal megerősítve, GO döntés indokolva —
  lásd "Prerequisite Check".
- [x] a cascade-lánc file:line hivatkozással idézve, ÉS a
  `session_raw.envelopes` FK-nélküli kivétel explicit megnevezve —
  lásd "Cascade Chain Audit".
- [x] `rollback_conversation()` KIZÁRÓLAG `(provider, provider_session_id)`
  alapján működik, file:line hivatkozással — lásd
  "rollback_conversation() Implementation",
  `session_store/rollback.py:72`.
- [x] valós Postgres teszt: 2 conversation importálva, 1 rollback-elve, a
  TÖRÖLT conversation MINDEN táblájára ÉS az ÉRINTETLEN conversation
  MINDEN táblájára külön bizonyítva — lásd "Real Postgres Proof".
- [x] claim-evidence tábla kitöltve, nem üres — lásd fent.
- [x] a riport NEM állítja, hogy ez a job valós export-bundle-t importál
  vagy töröl — minden importált/törölt adat ebben a riportban
  EXPLICITEN szintetikus/fabrikált (lásd "Scope").

## Next Jobs

1. **CLI/operator-felület `rollback_conversation()`-höz** — egy egyszerű
   `python -m session_store.rollback <provider> <provider_session_id>`
   parancssoros wrapper, megerősítő prompttal — input.md ezt explicit
   nem kötelezővé tette, de hasznos lenne egy manuális operátor-workflow-hoz.
2. **`session_core.manifests`/`session_idx.ranking_features` valós
   writer-je** — jelenleg semelyik meglévő worker (sem `turn_projector`,
   sem `chunk_indexer`) nem tölti fel ezeket; egy jövőbeli job
   megépíthetné a hiányzó projekciós lépést.
3. **A valós, személyes export-bundle elleni futtatás biztonsági
   megbeszélése és jóváhagyása** — input.md "Nem cél" szerint ez
   explicit egy KÜLÖN, ezt a jobot KÖVETŐ döntés, nem ennek a jobnak a
   feladata.
