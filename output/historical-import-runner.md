# historical-import-runner-001 Output

## Scope

Ez egy TÉNYLEGES, futtatható batch-runner implementáció, amely végigjárja a
ChatGPT export-bundle sharded `conversations-NNN.json` fájljait, minden
conversation `mapping`-fáját egy KONKRÉTAN eldöntött, determinisztikus
sorrendben bejárja, és minden node-ra meghívja a MEGLÉVŐ
`chatgpt_message_to_envelope()`/`insert_envelope()` függvényeket. A job
ZÁRJA a `historical-chatgpt-export-importer-001` riport által explicit
NYITOTT KÉRDÉSKÉNT hagyott `mapping` fa-bejárási sorrend kérdést (lásd
`historical-chatgpt-importer-design.md` 329. sor).

A runner kód NEM módosítja `chatgpt_message_to_envelope()`-ot vagy
`insert_envelope()`-ot, és NEM reimplementálja a konverziós/idempotency
logikájukat — kizárólag hívja őket a fa-bejárás során meghatározott
sorrendben.

**KRITIKUS BIZTONSÁGI HATÁR — explicit kimondva**: minden ebben a jobban
felhasznált teszt-fixture (3+ shard-fájl) KIZÁRÓLAG fabrikált, szintetikus
tartalom — fiktív conversation-id-k (`synthetic-conv-NNN-NNN`), fiktív
node-id-k, fiktív szöveg ("synthetic user message body...",
"synthetic assistant reply body..."). **Valós, személyes export-bundle
elleni futtatás ennek a jobnak EXPLICIT NEM CÉLJA, és külön, dedikált
biztonsági megbeszélést igényel, MIELŐTT megfontolásra kerülne — ez a
megbeszélés ITT NEM TÖRTÉNT MEG.**

## Inputs Read

- `jobs/index.yaml` (`cic-mcp-factory` klón) — prerequisite-ellenőrzéshez,
  `- id: "..."` kulcsok alapján (lásd "Prerequisite Check").
- `jobs/historical-chatgpt-export-importer-001/output/historical-chatgpt-importer-design.md`
  (`cic-mcp-factory` klón) — TELJES egészében elolvasva, különös
  figyelemmel az "Export Bundle Structure" szekcióra (a `mapping`-node
  `{id, message, parent, children}` alakja, 117. sor), a 329. sorra
  ("A `mapping` fa-bejárási sorrendje NYITOTT KÉRDÉS marad"), és a
  "conversations-*.json To SessionIngressEnvelope Mapping" táblára.
- `session_store/chatgpt_import.py` (`cic-mcp-session` klón, target repo) —
  TELJES fájl elolvasva. `chatgpt_message_to_envelope()` szignatúrája a
  144. sorban (`def chatgpt_message_to_envelope(conversation, node,
  provider_session_id=None) -> dict`), nem módosítva.
- `session_store/envelope_writer.py` (`cic-mcp-session` klón) — TELJES
  fájl elolvasva. `insert_envelope()` szignatúrája a 165. sorban
  (`def insert_envelope(envelope, config=None) -> int | None`), nem
  módosítva.
- `tests/test_session_store/test_chatgpt_import.py` (`cic-mcp-session`
  klón) — TELJES fájl elolvasva, a `_synthetic_conversation()`/
  `_synthetic_user_message_node()`/`_synthetic_assistant_message_node()`
  fixture-stílus és a modul-docstring biztonsági-határ minta követve.
- `tests/test_session_store/test_envelope_writer.py` (`cic-mcp-session`
  klón) — TELJES fájl elolvasva, a `pg_config`/`_pg_config`/
  `_clean_envelopes_table`/`_count_rows` fixture-ök újrahasznosítva
  (import útján, NEM újraírva).
- `session_store/__init__.py` — a package scope-jának megerősítéséhez.
- A futó Postgres container (`historical-runner-test`,
  `localhost:55437/testdb`) — élő kapcsolat-ellenőrzéssel megerősítve
  (`psycopg.connect(...)` sikeres volt a fejlesztés elején).

## Prerequisite Check

```
$ grep -n '\- id: "historical-dedupe-idempotency-001"' -A 3 jobs/index.yaml
90:  - id: "historical-dedupe-idempotency-001"
91-    level: "capability"
92-    status: "done"
93-    parent: "historical-chatgpt-export-importer-001"

$ grep -n '\- id: "historical-chatgpt-export-importer-001"' -A 3 jobs/index.yaml
82:  - id: "historical-chatgpt-export-importer-001"
83-    level: "capability"
84-    status: "done"
85-    target_repo: "cic-mcp-session"
```

Mindkét prerequisite `status: "done"`. **Döntés: GO.**

## Mapping Traversal Order Decision

Először megerősítve grep-pel a két meglévő függvény TÉNYLEGES, nem-teszt
szignatúrája (a `cic-mcp-session` klónban):

```
$ grep -rn "^def chatgpt_message_to_envelope\|^def insert_envelope" --include="*.py" . | grep -v test_
session_store/chatgpt_import.py:144:def chatgpt_message_to_envelope(
session_store/envelope_writer.py:165:def insert_envelope(
```

Ez megerősíti: `chatgpt_message_to_envelope()` a `session_store/chatgpt_import.py:144`
sorban él (szignatúra: `(conversation: Mapping[str, Any], node: Mapping[str, Any],
provider_session_id: str | None = None) -> dict`), `insert_envelope()` a
`session_store/envelope_writer.py:165` sorban (szignatúra:
`(envelope: Mapping[str, Any], config: SessionStoreConfig | None = None) -> int | None`).

**Döntés: DFS preorder a gyökér node(ok)tól, a `children` tömb on-disk
sorrendjét követve.**

Megvalósítás: `session_store/historical_import_runner.py:148-185`
(`iter_mapping_nodes_dfs_preorder()`). Algoritmus:

1. Gyökér node-nak minősül minden `node` ahol `node.get("parent") is None`
   — egy `mapping`-nek lehet TÖBB ilyen node-ja is (elágazó/leválasztott
   branch-ek esetén); mindegyiket bejárja, a `mapping` dict-beli (= on-disk
   JSON kulcs-) sorrendjében.
2. Minden gyökértől rekurzívan: a node-ot ELŐSZÖR adja vissza (preorder),
   majd a `children` tömb elemeit, PONTOSAN a tömb sorrendjében,
   rekurzívan.
3. Defenzív fallback: ha egy node SOHA nem érhető el semelyik gyökértől
   (deformált/részleges export-adat), a bejárás VÉGÜL még bejárja a
   `mapping` dict maradék, nem-látott node-jait is — NEM dobja el őket
   csendben.

**Indoklás (NYITOTT KÉRDÉS lezárása, `historical-chatgpt-importer-design.md`
329. sor)**:

1. **`current_node`-tól visszafelé `parent`-en SOSEM választott**: a design
   report saját "Risks" szekciója (357-361. sor) explicit figyelmeztet,
   hogy ez "elveszítve elágazó branch-eket" — csak az AKTÍV szálat hozná
   vissza egy regenerált/szerkesztett conversation-fában, a többi branch-et
   csendben elveszítve. Ez ELLENTMOND a job Definition Of Done
   "MINDEN node beírva" követelményének.
2. **DFS preorder a gyökértől `children`-en előre determinisztikus**: a
   `mapping[node].children` egy RENDEZETT tömb (nem egy dict-kulcs-halmaz),
   így a bejárás SOSEM függ Python dict-iterációs sorrendtől a tényleges
   gráf-struktúra szempontjából — csak a gyökerek azonosításánál (1. lépés)
   használja a `mapping` dict sorrendjét, ami maga is az export saját,
   on-disk JSON-kulcs-sorrendje, tehát stabil egy adott shard-fájlra.
3. **A Definition Of Done megköveteli, hogy "a sorszámoknak PONTOSAN
   egyezniük kell" több futás között** — DFS preorder, `children`-tömb-
   vezérelt rekurzióval, ezt garantálja: két futás a SAME bundle-ön mindig
   PONTOSAN ugyanazt a node-sorrendet produkálja (lásd "Real Postgres
   Kill-Mid-Run/Resume Proof" — a 2. futás (resume) pontosan ugyanazt a
   12 sort eredményezi, mint az 1. teljes futás).
4. **BFS-t sem választottuk**: a job-spec nem kér explicit időrendi
   sorbarendezést a node-ok KÖZÖTT (csak azt, hogy minden node beíródjon,
   és a bejárás determinisztikus legyen) — DFS preorder egyszerűbb,
   kevesebb állapotot tart (nincs explicit queue), és a fa-struktúra
   (szülő-gyermek lánc egy adott branch-en) természetes módon tükrözi az
   eredeti üzenet-szál sorrendjét egy nem-elágazó ágon, ami a leggyakoribb
   eset (a design report szerint átlagosan ~20.5 node/conversation, döntő
   többségük lineáris lánc egy regenerálás-mentes beszélgetésben).

## Runner Implementation

Fájl: `session_store/historical_import_runner.py` (a meglévő `session_store/`
mappában, a `chatgpt_import.py`/`envelope_writer.py` melletti konvenciót
követve — NEM találtam ki új könyvtár-struktúrát).

Kulcs-elemek:

- `discover_shards(bundle_dir)` (`:106-117`) — `conversations-*.json`
  fájlok listázása, fájlnév szerint rendezve (a fix-szélességű zero-padded
  számjegy miatt ez ekvivalens a numerikus sorrenddel).
- `iter_mapping_nodes_dfs_preorder(mapping)` (`:120-160`) — a fent
  indokolt DFS preorder bejárás.
- `import_shard(shard_path, config=None, fail_after=None)` (`:227-267`) —
  EGY shard-fájl teljes feldolgozása: minden conversation-objektum
  `mapping`-jét bejárja a fenti sorrendben, minden node-ra, amelynek
  `message is not None`:
  - **`envelope = chatgpt_message_to_envelope(conversation, node)`**
    (`session_store/historical_import_runner.py:255`, hívja
    `session_store/chatgpt_import.py:144`-et, VÁLTOZATLANUL)
  - **`new_id = insert_envelope(envelope, config=config)`**
    (`session_store/historical_import_runner.py:256`, hívja
    `session_store/envelope_writer.py:165`-öt, VÁLTOZATLANUL)
  - A node-ok, amelyeknek `message is None` (pl. szintetikus
    root-container node-ok), KIHAGYVA — ez egy STRUKTURÁLIS szűrés
    (a node alakja, nem a tartalma alapján), nem szemantikai
    interpretáció.
- `run(bundle_dir, config=None, fail_after_shard=None, fail_after_node=None,
  use_progress_marker=True)` (`:270-309`) — a teljes bundle-ön végigmegy,
  shardonként hívja `import_shard()`-ot.

**Progress-jelölő mechanizmus** (`_shard_progress_path()`,
`_read_completed_shards()`, `_mark_shard_completed()`, `:163-201`): egy
egyszerű, sima szöveges fájl (`.historical_import_progress`) a bundle
mappában, soronként EGY teljesen feldolgozott shard FÁJLNÉV. Választás
indoklása:
- Nincs hozzá szükséges DB-migráció (a job "Nem cél" listája szerint a
  runner nem érint schema-t).
- Tisztán teljesítmény-optimalizáció: kihagyja a már KÉSZ shard-okat egy
  hideg resume-nál, mielőtt akár csak DB-kapcsolatot nyitna.
- **A KORREKTSÉGET SOSEM ez a fájl garantálja** — a tényleges dedupe az
  `insert_envelope()`-ban élő `ON CONFLICT (idempotency_key) DO NOTHING`
  (`envelope_writer.py:199`). Ha a progress-fájl HIÁNYOZNA, korrupt lenne,
  vagy egy shard-ot "készként" jelölne, ami valójában félig-feldolgozott
  maradt (pl. egy kill PONTOSAN a `_mark_shard_completed()` hívás előtt),
  a re-run a teljes shard-ot ÚJRA feldolgozná — ez NEM hiba, mert minden
  egyes node újra-konvertálása ugyanazt az `idempotency_key`-t generálja
  (determinisztikus `raw_payload_hash` + `occurred_at`), tehát a DB-szintű
  UNIQUE-constraint dedupe-ja akkor is helyesen no-op-ol, ha a
  progress-jelölés maga téves lenne.

## Synthetic Multi-Shard Test Fixture

Fájl: `tests/test_session_store/test_historical_import_runner.py`.

3 fabrikált shard, `_write_synthetic_bundle()` (`:139-161`) generálja
in-process, fájlra írva egy `tmp_path` pytest-fixture alá (SOHA git-tracked
helyre nem kerül valós tartalom, mert nincs is valós tartalom — minden
fabrikált). Minden shard 2 fabrikált conversation-t tartalmaz
(`_synthetic_conversation()`, `:108-136`), mindegyik egy 3-node-os fa:
`root` (`message: None`, mint egy valós export szintetikus root-container
node-ja) → `user` node → `assistant` node (DFS preorder lánc). Összesen:
3 shard × 2 conversation × 2 importálható node (a `root` kihagyva) = 12
node.

A fixture-stílus (`_synthetic_message_node()`, `:65-87`) közvetlenül a
`test_chatgpt_import.py` `_synthetic_user_message_node()`/
`_synthetic_assistant_message_node()` mintáját követi (`author.role`,
`content.content_type: "text"`, `create_time` epoch-float, stb.) — NEM
találtam fel új fixture-formátumot. A `pg_config`/`_count_rows`/
`_clean_envelopes_table` fixture-ök a `test_envelope_writer.py`-ból
re-exportálva (`tests/test_session_store/test_historical_import_runner.py:43-47`),
NEM újraírva.

Minden conversation-id, node-id és szöveg explicit "synthetic"/"test"
előtaggal jelölt (pl. `synthetic-conv-000-001`, "synthetic user message
body for synthetic-conv-000-001") — ELLENŐRZÖTTEN nincs valós export-tartalom
sehol ebben a fájlban.

## Real Postgres Kill-Mid-Run/Resume Proof

Két formában futtatva: (a) pytest suite a `cic-mcp-session` `.venv-host`
alatt, a megadott `SESSION_STORE_PG_*` env-varokkal a `historical-runner-test`
container ellen; (b) egy manuális, lépésenkénti forgatókönyv VALÓS `psql`
kimenettel.

### (a) pytest

```
$ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55437 \
  SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \
  SESSION_STORE_PG_PASSWORD=test PYTHONPATH=. \
  .venv-host/bin/pytest tests/test_session_store/test_historical_import_runner.py -v --no-cov

collected 5 items

tests/test_session_store/test_historical_import_runner.py::test_dfs_preorder_visits_root_then_children_in_order PASSED
tests/test_session_store/test_historical_import_runner.py::test_dfs_preorder_visits_detached_subtree_without_dropping_it PASSED
tests/test_session_store/test_historical_import_runner.py::test_discover_shards_returns_sorted_filenames PASSED
tests/test_session_store/test_historical_import_runner.py::test_full_run_inserts_every_node_once PASSED
tests/test_session_store/test_historical_import_runner.py::test_kill_mid_run_then_resume_matches_full_run_row_count PASSED

5 passed in 1.02s
```

A `test_kill_mid_run_then_resume_matches_full_run_row_count` teszt
ténylegesen: (1) teljes futás, 12 sor; (2) truncate; (3) megszakítja a
futást a `conversations-001.json` shard 1. node-ja UTÁN
(`ImportInterrupted` kivétel, `fail_after_shard`/`fail_after_node` hook),
5 sor a DB-ben (4 a kész shard 000-ból + 1 a megszakadt shard 001-ből);
(4) resume — ÚJRA futtatva `run()`-t, progress-marker miatt shard 000
kihagyva, shard 001 + 002 feldolgozva — 12 sor a végén, `rows_inserted == 7`,
`rows_deduped == 1` (a shard 001 1. node-ja, amit a kill ELŐTT már beírt,
most dedupe-ként no-op).

Az `tests/test_session_store/` TELJES suite-ja is futtatva regresszió-
ellenőrzésként: a `test_chatgpt_import.py`/`test_envelope_writer.py`/
`test_historical_import_runner.py` (a jelen jobhoz közvetlenül kapcsolódó
modulok) mindegyik tesztje ZÖLD (15/15). A suite TÖBBI fájlja
(`test_chunk_indexer.py`, `test_hybrid_search.py`, `test_vector_search.py`,
`test_session_api.py`, `test_worker_loop.py`, `test_session_source_refs_api.py`)
32 teszt ELBUKOTT — ez ELŐRE VÁRT, NEM regresszió: a job-spec explicit
leírja, hogy a `.venv-host` SZÁNDÉKOSAN minimális (`psycopg`, `pytest`,
`pytest-cov`, `pyyaml`), és NEM tartalmazza a `sentence_transformers`
embedding-modellt, amit ezek a modulok igényelnek (a hibaüzenetek
explicit `ModuleNotFoundError: No module named 'sentence_transformers'`-t
idéznek, illetve ennek továbbgyűrűző következményeit). Ez a jelen job
hatókörén kívül esik (nem módosítottam, nem futtattam ezeket relevánsan).

### (b) Manuális psql-bizonyíték

```
$ # STEP 1: teljes futás a szintetikus 3-shard bundle-ön
$ python3 -m session_store.historical_import_runner /tmp/manual_proof/run1/synthetic_export_bundle
shards processed: 3, nodes visited: 12, rows inserted: 12, rows deduped: 0

$ psql -h localhost -p 55437 -U postgres -d testdb \
    -c "SELECT count(*) AS row_count_after_full_run FROM session_raw.envelopes;"
 row_count_after_full_run
---------------------------
                        12

$ # STEP 2: truncate, majd SZÁNDÉKOS megszakítás a 2. shard (conversations-001.json)
$ #         1. node-ja UTÁN (ImportInterrupted kivétel)
INTERRUPTED AS EXPECTED: simulated interruption after 1 node(s) in conversations-001.json (test-only fail_after hook)

$ psql -h localhost -p 55437 -U postgres -d testdb \
    -c "SELECT count(*) AS row_count_after_kill FROM session_raw.envelopes;"
 row_count_after_kill
-----------------------
                     5

$ cat /tmp/manual_proof/run2/.historical_import_progress
conversations-000.json

$ # STEP 3: resume -- run() ÚJRA, ugyanazon a bundle mappán, fail_after hook nélkül
$ python3 -m session_store.historical_import_runner /tmp/manual_proof/run2
shards processed: 2, nodes visited: 8, rows inserted: 7, rows deduped: 1

$ # STEP 4: VALÓS psql-lekérdezés -- a sorszám PONTOSAN egyezik a STEP 1 eredményével
$ psql -h localhost -p 55437 -U postgres -d testdb \
    -c "SELECT count(*) AS row_count_after_resume FROM session_raw.envelopes;"
 row_count_after_resume
-------------------------
                      12

$ psql -h localhost -p 55437 -U postgres -d testdb \
    -c "SELECT count(*) AS distinct_idempotency_keys FROM (SELECT DISTINCT idempotency_key FROM session_raw.envelopes) x;"
 distinct_idempotency_keys
----------------------------
                         12

$ psql -h localhost -p 55437 -U postgres -d testdb \
    -c "SELECT count(*) AS duplicate_idempotency_keys FROM (SELECT idempotency_key, count(*) c FROM session_raw.envelopes GROUP BY idempotency_key HAVING count(*) > 1) y;"
 duplicate_idempotency_keys
-----------------------------
                           0

$ psql -h localhost -p 55437 -U postgres -d testdb \
    -c "SELECT provider_session_id, count(*) FROM session_raw.envelopes GROUP BY provider_session_id ORDER BY provider_session_id;"
  provider_session_id   | count
-------------------------+-------
 synthetic-conv-000-000 |     2
 synthetic-conv-000-001 |     2
 synthetic-conv-001-000 |     2
 synthetic-conv-001-001 |     2
 synthetic-conv-002-000 |     2
 synthetic-conv-002-001 |     2
(6 rows)
```

**Eredmény**: STEP 1 (teljes futás) = 12 sor. STEP 2 (kill mid-shard-1,
1 node után) = 5 sor. STEP 3 (resume) = 7 új sor + 1 dedupe-no-op. STEP 4
(végső állapot) = 12 sor, 12 distinct `idempotency_key`, 0 duplikáció,
mind a 6 szintetikus conversation pontosan 2-2 sorral — PONTOSAN egyezik
az 1. lépés eredményével.

A manuális futtatás után a `/tmp/manual_proof/` ideiglenes fájlokat
töröltem, és a `session_raw.envelopes` táblát truncate-eltem — a klón
végállapota tiszta, nincs benne a manuális proof artefaktja.

## Findings

- A `mapping` fa-bejárási sorrend NYITOTT KÉRDÉSE (`historical-chatgpt-importer-design.md`
  329. sor) ezzel a jobbal LEZÁRVA: DFS preorder, gyökértől, `children`
  tömb-sorrendet követve (lásd "Mapping Traversal Order Decision").
- A runner SOSEM reimplementálja a konverziós/idempotency logikát — minden
  node-ra pontosan EGYSZER hívja `chatgpt_message_to_envelope()`-ot és
  `insert_envelope()`-ot, file:line hivatkozással igazolva.
- A progress-jelölő fájl (`.historical_import_progress`) PUSZTÁN
  teljesítmény-optimalizáció — a kill-mid-run/resume teszt explicit
  bizonyítja, hogy a tényleges korrektséget az `idempotency_key` UNIQUE
  constraint + `ON CONFLICT DO NOTHING` garantálja, FÜGGETLENÜL attól,
  hogy a progress-jelölés pontos-e (a shard 001 RÉSZLEGESEN feldolgozott
  állapotban maradt a kill után, a resume MÉGIS helyesen, duplikáció
  nélkül zárta le).
- **Explicit kimondva (Definition Of Done kötelező pont)**: ez a job
  KIZÁRÓLAG fabrikált, szintetikus shard-fájlokkal futott. **Valós,
  személyes ChatGPT export-bundle elleni futtatás ennek a jobnak
  EXPLICIT NEM CÉLJA, és külön, dedikált biztonsági megbeszélést igényel,
  MIELŐTT megfontolásra kerülne — ez a megbeszélés ITT NEM TÖRTÉNT MEG.**
  Egy valós bundle ELTÉRŐ kockázatokat hordozhat (méret — a design report
  szerint 1959 conversation/20 shard egy valós exportban; melléklet-fájlok
  kezelése; PII a `payload`-ban tárolt teljes `message`-objektumban), amik
  ezen a szintetikus, 3-shard/6-conversation/12-node bizonyítékon NEM
  lettek vizsgálva.
- A `.venv-host` szándékosan minimális (nincs `sentence_transformers`) —
  ez a job NEM igényelte (a runner kizárólag `psycopg`-et importáló
  modulokat hív), és a suite többi, nem-kapcsolódó modulja (chunk
  indexer, vector/hybrid search) ELŐRE VÁRTAN elbukik ebben a venv-ben —
  ez nem ennek a jobnak a hatóköre, nem regresszió.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Mindkét prerequisite (`historical-dedupe-idempotency-001`, `historical-chatgpt-export-importer-001`) `status: "done"` | proven | `jobs/index.yaml:90-93` és `:82-85`, idézve "Prerequisite Check"-ben | `grep -n '\- id: "..."' -A 3 jobs/index.yaml`, kimenet idézve | alacsony |
| `chatgpt_message_to_envelope()`/`insert_envelope()` tényleges, nem-teszt szignatúrája megerősítve | proven | `session_store/chatgpt_import.py:144`, `session_store/envelope_writer.py:165` | `grep -rn "^def chatgpt_message_to_envelope\|^def insert_envelope" --include="*.py" . \| grep -v test_`, kimenet idézve | alacsony |
| `mapping` fa-bejárási sorrend konkrétan eldöntve (DFS preorder, gyökértől, `children`-sorrendet követve) | proven | `session_store/historical_import_runner.py:120-160` (`iter_mapping_nodes_dfs_preorder`), indoklás "Mapping Traversal Order Decision"-ben | kód olvasás + 2 unit teszt (`test_dfs_preorder_visits_root_then_children_in_order`, `test_dfs_preorder_visits_detached_subtree_without_dropping_it`), mindkettő PASSED | alacsony |
| A runner a MEGLÉVŐ konverter/writer függvényeket hívja, NEM reimplementálja a logikájukat | proven | `session_store/historical_import_runner.py:255-256` (`chatgpt_message_to_envelope(...)`, `insert_envelope(...)` hívások), import `:38-39` | kód olvasás, file:line hivatkozás | alacsony |
| Legalább 3 szintetikus, fabrikált shard-fájl előállítva | proven | `tests/test_session_store/test_historical_import_runner.py:139-161` (`_write_synthetic_bundle`, 3 shard, 2 conversation/shard) | kód olvasás + `test_discover_shards_returns_sorted_filenames` PASSED, kimenet: 3 fájlnév | alacsony |
| Teljes futás VALÓS Postgres ellen, pontos sorszám | proven | STEP 1 manuális futás: "shards processed: 3, nodes visited: 12, rows inserted: 12, rows deduped: 0"; psql: `row_count_after_full_run = 12` | tényleges CLI-futás + `psql` lekérdezés, kimenet idézve | alacsony |
| Kill-mid-run szándékosan megszakítja a futást, részleges állapotot hagy | proven | STEP 2: `ImportInterrupted` kivétel idézve, psql: `row_count_after_kill = 5`, progress-marker tartalma: `conversations-000.json` | tényleges CLI-futás (kivétel elkapva) + psql + fájl-olvasás, kimenet idézve | alacsony |
| Resume a TELJES bundle-ön a teljes futáshoz PONTOSAN egyező sorszámot ad, duplikáció nélkül | proven | STEP 3+4: "rows inserted: 7, rows deduped: 1"; psql: `row_count_after_resume = 12`, `distinct_idempotency_keys = 12`, `duplicate_idempotency_keys = 0`, per-conversation: mind 2-2 sor | tényleges CLI-futás + psql, kimenet idézve, KÖZVETLENÜL összevetve a STEP 1 baseline-nal (12 == 12) | alacsony |
| Ugyanez pytest-tesztként is bizonyítva, NEM csak manuálisan | proven | `test_kill_mid_run_then_resume_matches_full_run_row_count` PASSED | `pytest ... -v --no-cov`, kimenet idézve, 5/5 PASSED | alacsony |
| Progress-marker fájl NEM korrektségi mechanizmus, csak optimalizáció — a dedupe a kill ELLENÉRE is helyes | proven | a kill UTÁN a shard 001 RÉSZLEGESEN feldolgozott (1/4 node), a resume MÉGIS helyesen zárja: 7 új + 1 dedupe, NEM 8 új (ami duplikációt jelentene) | a `rows_deduped == 1` érték közvetlen bizonyítéka, idézve | alacsony |
| Meglévő, kapcsolódó tesztek (`test_chatgpt_import.py`, `test_envelope_writer.py`) nem regresszáltak | proven | "tests/test_session_store/test_chatgpt_import.py tests/test_session_store/test_envelope_writer.py tests/test_session_store/test_historical_import_runner.py ... 15 passed in 2.07s" | `pytest` futtatás, kimenet idézve | alacsony |
| Valós, személyes export-bundle elleni futtatás NEM történt meg, külön biztonsági review szükséges | proven | minden fixture explicit "synthetic"/"test" előtaggal, "Findings" szekcióban explicit kimondva | manuális kód- és riport-átolvasás a commitolás előtt | alacsony — emberi/AI hiba kockázata mindig megmarad |

## Decisions Proposed

1. **DFS preorder a gyökértől, `children`-sorrendet követve** — lásd
   "Mapping Traversal Order Decision", a NYITOTT KÉRDÉS lezárása.
2. **Progress-jelölés sima szöveges fájllal (`.historical_import_progress`),
   shard-fájlnevenkénti egy sorral** — NEM DB-táblával, hogy ne kelljen
   schema-módosítás, és mert a tényleges korrektséget mindenképp a
   DB-szintű idempotency-key constraint adja, nem a progress-jelölés
   pontossága.
3. **`message is None` node-ok strukturális kihagyása** (nem szemantikai
   szűrés) — a `chatgpt_message_to_envelope()` `node["message"]`-et
   feltétlenül olvasná (lásd a függvény docstring-je,
   `chatgpt_import.py:159-162`: "system/tool nodes with a None message are
   the caller's concern to filter out"), így a runner pontosan EZT a
   szerződést teljesíti — a node ALAKJA (nincs `message`), nem a TARTALMA
   alapján dönt.
4. **`ImportInterrupted` kivétel + `fail_after_shard`/`fail_after_node`
   teszt-only hook** a kill-mid-run szimulálásához, valódi `kill -9`
   helyett — mert `insert_envelope()` soronként commitol (nincs
   többsoros tranzakció amit egy valódi process-kill visszagörgetne), így
   egy in-process kivétel pontosan ugyanazt a megfigyelhető, részlegesen-
   commitolt DB-állapotot produkálja, mint egy külső kill.

## Rejected / Out Of Scope

- `current_node`-tól visszafelé `parent`-en bejárás — ELVETVE, mert
  elveszítené az elágazó branch-eket (lásd "Mapping Traversal Order
  Decision" 1. indoklási pont).
- BFS bejárás — ELVETVE, nem ad extra garanciát a job követelményeihez
  képest, és bonyolultabb állapot-kezelést igényelne (explicit queue) a
  DFS-hez képest.
- `chatgpt_message_to_envelope()`/`insert_envelope()` módosítása — explicit
  "Nem cél" az input.md-ben, NEM módosítva.
- Valós, személyes export-bundle elleni futtatás — explicit "Nem cél",
  külön biztonsági megbeszélést igényel, ami ITT NEM történt meg.
- A `historical-chatgpt-export-importer-001` strukturális riportjának
  felülírása — csak a fa-bejárási sorrend nyitott kérdését zártam le, a
  riport többi tartalmát nem érintettem.
- shared/gateway bekötés — másik, már lezárt Phase 6 job tárgya.
- Melléklet-fájlok (`file-*`/`file_*`) kezelése — a design report explicit
  jelzi mint egy jövőbeli importer-job hatáskörét (NEM ennek a jobnak),
  ez a runner csak a `mapping`-fában lévő `message`-objektumokat
  konvertálja, melléklet-referenciákat NEM dereferál.

## Risks

- **Progress-fájl konkurrencia**: ha két `run()` hívás PÁRHUZAMOSAN futna
  ugyanazon a `bundle_dir`-en, a `.historical_import_progress` fájlra írás
  (`_mark_shard_completed()`, sima `open(..., "a")`) NEM atomikus/lock-olt
  — ez NEM teszteltve ebben a jobban (a kill-mid-run/resume teszt
  SZEKVENCIÁLIS, nem párhuzamos futásokat bizonyít). Egyetlen-folyamatos
  batch-import forgatókönyvben (ami a job-spec által leírt használati
  eset) ez nem releváns, de párhuzamos/elosztott futtatás esetén
  felülvizsgálatra szorulna.
- **Nagy, valós bundle teljesítménye nem mérve**: a szintetikus bizonyíték
  12 node-ra fut (3 shard × 2 conversation × 2 node) — egy valós export
  (design report szerint 1959 conversation, átlag ~20.5 node/conversation,
  kb. 40 000 node összesen) teljesítmény-jellemzői (memóriahasználat egy
  100-conversation-os shard egyidejű JSON-betöltésénél, soronkénti
  `insert_envelope()`-hívás overhead-je 40 000-szer) NEM lettek mérve —
  ez a job kizárólag a KORREKTSÉGET (resume helyessége) bizonyítja, nem a
  skálázhatóságot.
- **Valós export-bundle melléklet-fájlok, PII, méret**: lásd "Findings" —
  a valós, személyes bundle elleni futtatás külön biztonsági review-t
  igényel, ami itt nem történt meg; ez NEM a jelen szintetikus
  bizonyíték hibája, hanem egy explicit, szándékos hatókör-határ.
- **A `_shard_progress_path()` fájl git-tracked helyre kerülhetne, ha a
  bundle_dir egy git-tracked mappa lenne** — a jelen tesztek mindig
  `tmp_path` (pytest ideiglenes mappa) alatt futnak, ez SOHA nem kerül
  commitolásra; de egy jövőbeli production-wiring-nek ezt a kockázatot
  explicit kezelnie kell (pl. `.gitignore` bejegyzés a bundle-mappákra).

## Definition Of Done Check

- [x] mindkét prerequisite `id:` kulccsal megerősítve, GO döntés indokolva
      — lásd "Prerequisite Check".
- [x] a `mapping` fa-bejárási sorrend KONKRÉTAN eldöntve és megírva,
      file:line hivatkozással (`session_store/historical_import_runner.py:120-160`),
      a nyitott kérdés explicit lezárva — lásd "Mapping Traversal Order
      Decision".
- [x] a runner a MEGLÉVŐ `chatgpt_message_to_envelope()`/`insert_envelope()`-ot
      hívja, NEM reimplementálja a logikájukat, file:line hivatkozással
      (`session_store/historical_import_runner.py:255-256`) — lásd
      "Runner Implementation".
- [x] legalább 3 szintetikus, fabrikált shard-fájl, valós tartalom nélkül
      — lásd "Synthetic Multi-Shard Test Fixture" (3 shard, mind
      fabrikált).
- [x] valós Postgres teszt: teljes futás + kill-mid-run + resume, a
      sorszámok PONTOSAN egyeznek, nincs duplikáció — a TÉNYLEGES
      számértékek idézve (12 → 5 (kill) → 12 (resume), 0 duplikáció) —
      lásd "Real Postgres Kill-Mid-Run/Resume Proof".
- [x] a riport explicit kimondja, hogy valós, személyes export-bundle
      elleni futtatás külön biztonsági review-t igényel, és ez itt NEM
      történt meg — lásd "Scope" és "Findings".
- [x] claim-evidence tábla kitöltve, nem üres (12 sor) — lásd fent.

## Next Jobs

- Egy jövőbeli, dedikált biztonsági review-job, amely ELŐSZÖR eldönti, a
  valós, személyes ChatGPT export-bundle elleni futtatás milyen
  feltételekkel (hozzáférés-korlátozás, melléklet-fájl-kezelés,
  PII-szűrés a `payload`-ban) lenne megengedett — ez a job EXPLICIT NEM
  ez, és nem is helyettesíti.
- Egy jövőbeli teljesítmény-job, amely a runner-t egy NAGYOBB,
  SZINTÉN szintetikus (nem valós), de a valós bundle méretskáláját
  közelítő (pl. több ezer node) bundle-ön futtatja, és méri a
  memóriahasználatot/futási időt — a jelen job kizárólag a korrektséget
  (resume helyessége) bizonyítja, nem a skálázhatóságot.
- Egy jövőbeli melléklet-kezelő job, amely eldönti, a `file-*`/`file_*`
  melléklet-fájlokat hogyan kell a `SessionIngressEnvelope`-hoz kötni
  (külön blob-store vs. `payload`-on belüli hivatkozás) — a design report
  "Risks" szekciója (373-377. sor) ezt explicit nyitott kérdésként hagyta,
  és a jelen runner sem oldja meg (csak a `mapping`-fában lévő
  `message`-objektumokat konvertálja).
- Production wiring (cron/systemd vagy egy explicit operator-parancs) a
  `_main()` CLI entry point-ra építve — jelenleg ez egy manuális,
  nem-production belépési pont (`historical_import_runner.py:312-330`),
  hasonlóan a `chatgpt_import.py`/`envelope_writer.py` "NO production
  caller in this job" mintájához.
