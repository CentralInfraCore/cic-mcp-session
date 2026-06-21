# session-worker-scheduler-001 Output

## Scope

Ez a job megírja az ELSŐ mechanizmust, ami ISMÉTELTEN, ütemezetten hívja meg
mindkét meglévő outbox-workert: `turn_projector.run_projection_batch()`
(`project_envelope` job-ok) és `chunk_indexer.run_indexing_batch()`
(`index_turn` job-ok). Mindkettő reachability-státusza eddig `scaffold` volt
— senki nem hívta meg őket a saját CLI-jükön/tesztjeiken kívül.

Konkrétan: egy bounded polling loop modul (`session_store/worker_loop.py`),
ami minden iterációban ELŐSZÖR projekciót, UTÁNA indexelést futtat, a
MEGLÉVŐ batch-függvényeket újrahasználva (nem újraírva), `--max-iterations`
és `--interval-seconds` paraméterekkel, plusz egy dokumentált (NEM
deploy-olt) systemd timer+service unit pár.

**Explicit, őszinte korlát (input.md-ből idézve)**: a `cic-mcp-session`
repónak **nincs élő production Postgres instance-e** — minden teszt egy
ideiglenes, a job végén leállított/törölt `pgvector/pgvector:pg16`
Docker-konténer ellen futott. Ez a job NEM deploy-ol semmit
production-be — egy dokumentált, valódi (de ideiglenes) Postgres ellen
bizonyítottan működő mechanizmust ad.

Nem cél (input.md "Nem cél"): tényleges production deployment, a
`--interval-seconds` valós production-értékének hangolása, multi-worker
konkurencia-kezelés, monitoring/alerting integráció, az MCP szerver átírása.

## Inputs Read

- `kb_status` (MCP `cic-graph`) — KB elérhető, betöltve (`graph_nodes.pkl`,
  `graph_edges.pkl`, `inverted_index.pkl`, `faiss.index`, `bm25.pkl`
  mind `exists: true`, cache `currsize: 1`). `search_nodes("session worker
  scheduler outbox")` 0 találatot adott — ez új capability, még nincs KB
  node-ja, nem hiba.
- `.cic-context/factory-docs/job-slices.yaml` — `session-worker-scheduler-001`
  bejegyzés (sor 335-360), `phase: "3"`, `target_repo: cic-mcp-session`,
  `acceptance_gates`/`required_evidence`/`forbidden_shortcuts` — az input.md
  szó szerint ezt tükrözi, nincs eltérés
- `session_store/turn_projector.py` (teljes fájl) — `run_projection_batch()`
  (sor 300-330), `_main()` CLI minta (sor 333-356); ÚJRAHASZNÁLVA a
  `worker_loop.run_one_iteration()`-ben, NEM újraírva
- `session_store/chunk_indexer.py` (teljes fájl) — `run_indexing_batch()`
  (sor 378-408), `_main()` CLI minta (sor 411-434); ugyanígy újrahasznosítva
- `session_store/envelope_writer.py` (teljes fájl) — `SessionStoreConfig`,
  `insert_envelope()` — a valódi backlog-teszt write-path-je
- `tests/test_session_store/test_turn_projector.py`,
  `test_chunk_indexer.py` (teljes fájlok) — a valódi-Postgres
  teszt-fixture minta (`pg_config` fixture, `_clean_tables` autouse
  truncate, docker run/psql reprodukciós docstring), amit
  `test_worker_loop.py` követ
- Az 5 SQL fájl (`output/` alatt): `session-postgres-schema.sql` (413 sor),
  `session-chunk-indexer-migration.sql` (93 sor),
  `session-retrieval-quality-migration.sql` (161 sor),
  `session-vector-search-api-migration.sql` (91 sor),
  `session-hybrid-search-api-migration.sql` (201 sor) — mind alkalmazva,
  lásd "Findings"

## Findings

1. **5 SQL fájl, hiba nélkül, sorrendben alkalmazva** egy friss
   `pgvector/pgvector:pg16` konténeren (`session-worker-scheduler-test`,
   port 55435). Mind az 5 `psql -v ON_ERROR_STOP=1` hívás hiba nélkül
   futott (lásd Claim-Evidence Matrix pontos kimenettel).

2. **`session_store/worker_loop.py` ÚJ modul**, ami NEM ír újra
   projekciós/indexelési logikát:
   - `run_one_iteration()` (sor 65) közvetlenül hívja
     `turn_projector.run_projection_batch()`-et, majd
     `chunk_indexer.run_indexing_batch()`-et — ebben a sorrendben.
   - `run_loop(max_iterations, interval_seconds, config)` (sor 93) a
     bounded/unbounded ciklus, `IterationResult` (sor 48) listát ad vissza
     (`iteration`, `projection_count`, `indexing_count`).
   - `_main()` (sor 184) a CLI belépési pont (`python -m
     session_store.worker_loop --max-iterations N --interval-seconds S`),
     argparse-alapú, mert `--max-iterations`/`--interval-seconds` flag
     kell (a meglévő `turn_projector`/`chunk_indexer` `_main()`-ek nem
     használnak argparse-ot, mert nincs CLI argumentumuk — ez az ELSŐ
     paraméterezett CLI a `session_store` modulok között).

3. **Új teszt modul**: `tests/test_session_store/test_worker_loop.py`,
   ugyanazt a valódi-Postgres fixture-mintát követi mint
   `test_turn_projector.py`/`test_chunk_indexer.py` (`pg_config` fixture,
   `_clean_tables` autouse truncate, docker run/psql reprodukciós
   docstring).

4. **Teljes regresszió-teszt**: a meglévő 46 teszt (15 turn_projector +
   chunk_indexer, 6 envelope_writer, 7 hybrid_search, 8 session_api,
   7 vector_search, 3 ÚJ worker_loop) mind PASSED a migrált schemán —
   lásd Claim-Evidence Matrix.

5. **Reachability**: a `run_loop`/`run_one_iteration` függvényeknek a
   teszten és a modul saját CLI hívásán kívül NINCS hívója — lásd
   "Claim-Evidence Matrix" és a riport végén idézett `grep` kimenet.

6. **Deployment artifact**: `output/session-worker-scheduler-deployment/`
   alatt — `cic-session-worker-loop.service`, `cic-session-worker-loop.timer`,
   `worker-loop.env.example` — mindhárom fájl explicit, nagybetűs
   disclaimer-rel kezdődik: "DOCUMENTED DEPLOYMENT ARTIFACT — NOT DEPLOYED."

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Mind az 5 SQL fájl egymás után, hiba nélkül alkalmazható egy friss `pgvector/pgvector:pg16` konténeren | proven | `docker exec -i session-worker-scheduler-test psql -U postgres -d testdb -v ON_ERROR_STOP=1 < session-postgres-schema.sql` → `CREATE SCHEMA`/`CREATE TABLE`/`CREATE INDEX`/`CREATE TYPE`/`CREATE FUNCTION`/`CREATE TRIGGER` sorozat, hiba nélkül; majd ugyanígy a 4 migráció: chunk-indexer → `ALTER TABLE`/`COMMENT`/`CREATE FUNCTION`/`CREATE TRIGGER`; retrieval-quality → `CREATE FUNCTION`/`COMMENT`×2; vector-search-api → `CREATE FUNCTION`/`COMMENT`; hybrid-search-api → `CREATE FUNCTION`/`COMMENT` — mind hiba nélkül | valódi Postgres (`pgvector/pgvector:pg16`, konténer `session-worker-scheduler-test`, port 55435), `psql -v ON_ERROR_STOP=1` | alacsony — idempotencia nem tesztelt, ismert, korábbi jobokból örökölt limitáció |
| `worker_loop.run_one_iteration()` a MEGLÉVŐ `run_projection_batch()`/`run_indexing_batch()`-et hívja, projekció ELŐBB, indexelés UTÁNA, nem újraírva azok logikáját | proven | `session_store/worker_loop.py:65-79` (`run_one_iteration`) forrás: `from session_store.chunk_indexer import run_indexing_batch` / `from session_store.turn_projector import run_projection_batch` import, és a függvénytörzs `projection_results = run_projection_batch(config=cfg)` majd `indexing_results = run_indexing_batch(config=cfg)` — ebben a sorrendben | kódolvasás (statikus) + a teszt futási sorrendje is ezt bizonyítja (lásd alábbi sor) | alacsony |
| `--max-iterations` paraméter pontosan annyi iterációt futtat, amennyit kértek (nem többet, nem kevesebbet) | proven | `pytest tests/test_session_store/test_worker_loop.py::test_loop_respects_max_iterations_bound_exactly -v` kimenet: `PASSED`; a teszt explicit `assert len(results) == 1` (max_iterations=1) és `assert [r.iteration for r in results_5] == [1, 2, 3, 4, 5]` (max_iterations=5) | valódi Postgres ellen | alacsony |
| Valódi, 3-envelope backlog (a VALÓDI `insert_envelope()` lánccal létrehozva, manuális worker-hívás NÉLKÜL), a LOOP SAJÁT iterációi alatt, közbeavatkozás nélkül teljesen lecsapolva | proven | `pytest tests/test_session_store/test_worker_loop.py::test_loop_drains_real_multi_envelope_backlog_across_multiple_iterations -v -s --log-cli-level=INFO --no-cov` kimenet (idézve, NEM paraphrase-elve):<br>`INFO session_store.worker_loop:worker_loop.py:138 worker_loop iteration=1 projection_count=3 indexing_count=3`<br>`INFO session_store.worker_loop:worker_loop.py:138 worker_loop iteration=2 projection_count=0 indexing_count=0`<br>`INFO session_store.worker_loop:worker_loop.py:138 worker_loop iteration=3 projection_count=0 indexing_count=0`<br>`PASSED`. A teszt assertálja `_turns_count==3`, `_chunks_count==3`, `_chunk_embeddings_count==3`, `_pending_outbox_count(...)==0` mindkét job_type-ra, MINDEN insert_envelope/run_projection_batch/run_indexing_batch manuális hívás nélkül a teszt-kódban — csak `run_loop()` hívás | valódi Postgres ellen, 3 valódi `insert_envelope()` hívás, `run_loop(max_iterations=3, interval_seconds=0.1)`, iterációnkénti `--log-cli-level=INFO` log-idézet | alacsony — a backlog 1 iteráción belül lecsapolódott (3 envelope kis terhelés mellett), de ez 3 ITERÁCIÓN át lett bizonyítva üresre futva (2., 3. iteráció 0/0), ami pontosan azt mutatja, hogy a loop maga, ismételten, hiba nélkül fut a backlog lecsapolása UTÁN is — ld. "Risks" a nagyobb backlog esetére |
| Üres backlog esetén a loop TÖBB iteráción át sem dob hibát | proven | `pytest tests/test_session_store/test_worker_loop.py::test_loop_handles_empty_backlog_across_multiple_iterations_without_error -v -s --log-cli-level=INFO --no-cov` kimenet (idézve):<br>`INFO ... iteration=1 projection_count=0 indexing_count=0`<br>`INFO ... iteration=2 projection_count=0 indexing_count=0`<br>`INFO ... iteration=3 projection_count=0 indexing_count=0`<br>`INFO ... iteration=4 projection_count=0 indexing_count=0`<br>`PASSED` | valódi Postgres ellen, 0 pending job, `run_loop(max_iterations=4, interval_seconds=0.1)` | alacsony |
| A loop CLI belépési pontja (`python -m session_store.worker_loop`) önállóan futtatható | proven | `SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55435 ... p_venv/bin/python -m session_store.worker_loop --max-iterations 2 --interval-seconds 0.1` kimenet: `INFO:__main__:worker_loop iteration=1 projection_count=0 indexing_count=0` / `iteration=2 ...` / majd a `print()` összegző sorok `iteration=1 projection_count=0 indexing_count=0` / `iteration=2 ...` | közvetlen CLI hívás (nem pytest), valódi Postgres ellen | alacsony — a backlog ekkor már üres volt (a tesztek truncate-elték a táblákat), ez csak a CLI parse/futtatás bizonyítéka, nem a backlog-drain bizonyítéka (azt a fenti sor adja) |
| A teljes meglévő `tests/test_session_store/` suite (turn_projector, chunk_indexer, envelope_writer, hybrid_search, session_api, vector_search) NEM regresszált az 5 SQL fájl + az új modul után | proven | `pytest tests/test_session_store/ -v --no-cov` kimenet: `46 passed in 21.57s` (15 chunk_indexer/turn_projector + 6 envelope_writer + 7 hybrid_search + 8 session_api + 7 vector_search + 3 ÚJ worker_loop, mind PASSED) | valódi Postgres ellen, közös konténer, mind az 5 SQL fájl alkalmazva | alacsony |
| Reachability: `run_loop`/`run_one_iteration`-nek nincs külső (nem teszt, nem CLI) hívója | proven | `grep -rn "run_loop" --include="*.py" . \| grep -v "p_venv" \| grep -v "test_" \| grep -v "/tests/"` kimenet kizárólag `session_store/worker_loop.py:87` (komment, nem hívás), `session_store/worker_loop.py:93` (definíció), `session_store/worker_loop.py:199` (`_main()` saját hívása) — 0 külső hívó. `run_one_iteration` grep: `session_store/worker_loop.py:65` (definíció), `:118` (docstring komment), `:131` (`run_loop()` belső hívása) — 0 külső hívó | mechanikus `grep -rn` | n/a — `scaffold`/`experimental`, lásd "Definition Of Done Check" |
| A loop LÉTEZIK és TESZTELT (proven, kódszinten + valódi Postgres ellen) | proven | lásd fenti sorok | pytest + grep | n/a |
| A loop-ot/`worker_loop`-ot VALAKI/VALAMI TÉNYLEGESEN, ütemezetten futtatja PRODUCTION-ben | missing | nincs élő production Postgres instance a `cic-mcp-session` repóhoz, nincs telepített/engedélyezett systemd unit, nincs cron-bejegyzés sehol — a deployment artifact (lásd alábbi sor) DOKUMENTÁCIÓ, nem futó szolgáltatás | n/a (negatív állítás — nincs mit futtatni, nincs is hova) | n/a — ez a job explicit NEM ezt állítja, lásd "Nem cél" |
| Systemd timer+service unit pár dokumentálva, explicit "nincs production-ben futtatva" kijelentéssel | proven | `output/session-worker-scheduler-deployment/cic-session-worker-loop.service` és `.timer` fájlok mindegyike "DOCUMENTED DEPLOYMENT ARTIFACT — NOT DEPLOYED." sorral kezdődik, és a `.service` fájl explicit megjegyzi: "The cic-mcp-session repo currently has NO live production Postgres instance — there is nowhere for this unit to point at yet." | fájltartalom-idézet (lásd "Decisions Proposed") | alacsony |
| A `cic-mcp-session` klónban a target branch-re sikeresen pusholva | proven/pending | lásd a végső agent-válasz "git push" kimenet-idézete | tényleges `git push` parancs kimenete | n/a |

## Decisions Proposed

1. **`run_one_iteration()` külön függvény `run_loop()`-tól.** A
   szétválasztás azért indokolt, hogy egyetlen iteráció logikája (projekció
   → indexelés sorrend) önállóan tesztelhető/olvasható legyen a
   ciklus-vezérléstől (max_iterations/interval) függetlenül — ez nem
   módosítja a meglévő batch-függvények viselkedését, csak a hívási
   sorrendet kapszulázza.

2. **`IterationResult.projection_count`/`indexing_count` a batch-eredmény
   LISTA HOSSZA, nem siker-számláló.** Egy `failed`/`dead_letter` sor is
   "feldolgozottnak" számít ebben a kontextusban (mert az outbox sor
   lezárva lett, csak nem `done`-ra) — a per-sor kimenet részletei a
   meglévő `ProjectionResult`/`IndexingResult` dataclass-okban élnek, ezt
   a riport nem duplikálja.

3. **Argparse a CLI-hez, eltérve a meglévő `turn_projector`/
   `chunk_indexer` `_main()` minta (nincs CLI argumentumuk) gyakorlatától.**
   Indokolt, mert ennek a modulnak VAN paraméterezhető viselkedése
   (`--max-iterations`, `--interval-seconds`), amit a meglévő két modul
   nem igényelt — nem a meglévő minta hibás újraírása, hanem egy új,
   indokolt különbség.

4. **Systemd `Type=oneshot` + külön `.timer`, NEM egy belső
   `while True` + `sleep` a `.service`-ben.** Így minden futás saját
   systemd journal bejegyzést kap, az ütemezést a `.timer` adja (nem a
   Python-kód maga) — ez jobban illeszkedik a "dokumentált, NEM deploy-olt"
   keretbe, mert nem kell a folyamat élethosszát kézzel kezelni egy
   esetleges jövőbeli telepítésnél.

5. **`p_venv/` lokálisan, pip-pel, NEM a repo `make deps`
   (Docker compose-alapú) target-jével épült fel** ebben a jobban — a
   `make deps` a teljes `requirements.txt`-et (torch, cuda-toolkit, stb.)
   telepítené Docker-en át, ami ehhez a feladathoz (psycopg + pytest +
   sentence-transformers) szükségtelenül lassú lett volna. A venv
   gitignored (`.gitignore:3` — `/p_venv/`), nem kerül commitba, és a
   csomagverziók megegyeznek a `requirements.txt`-ben rögzítettekkel
   (`psycopg[binary]==3.3.4`, `pytest==8.4.2`, `pytest-cov==7.0.0`,
   `pytest-mock==3.15.1`, `sentence-transformers==5.6.0`).

## Rejected / Out Of Scope

- tényleges production deployment (nincs is hova — nincs élő instance) —
  input.md "Nem cél"
- `--interval-seconds` valós production-értékének hangolása — input.md
  "Nem cél"
- multi-worker konkurencia-kezelés — ugyanaz a single-instance feltétel,
  mint `turn_projector`/`chunk_indexer`-nél (lásd azok docstringjei)
- monitoring/alerting integráció — input.md "Nem cél"
- az MCP szerver átírása — input.md "Nem cél"
- a `turn_projector`/`chunk_indexer` batch-logikájának módosítása vagy
  újraírása — explicit tiltott (Forbidden Shortcuts), nem történt meg

## Risks

- **Nagyobb backlog viselkedése nem tesztelt ebben a jobban**: a
  bizonyított teszt 3 envelope-ot drainel 1 iteráción belül (mert a batch
  függvények egy hívásban minden pending sort feldolgoznak) — egy
  jelentősen nagyobb (pl. 10k+ soros) backlog viselkedése (memóriahasználat,
  egy iteráció hossza) nem volt vizsgálva; a loop-architektúra (projekció →
  indexelés minden iterációban) ettől függetlenül helyes, csak a skálázási
  tulajdonságok nem mértek.
- **Egyetlen worker-instance feltételezés öröklődik** a
  `turn_projector`/`chunk_indexer` SKIP LOCKED mintájából — a loop nem ad
  hozzá és nem vesz el ebből a limitációból, de ha valaha 2 `worker_loop`
  folyamat indulna egyszerre, ugyanaz a (dokumentált, alacsony kockázatú)
  korlátozás érvényes mint a két alatta lévő workerre.
- **A systemd unit fájlok sosem lettek systemd-vel tesztelve** (nincs hova
  telepíteni) — a `.service`/`.timer` szintaxisa kézzel lett megírva a
  systemd dokumentáció alapján, NEM `systemd-analyze verify`-jal
  ellenőrizve. Ha a syntax hibás, ez csak egy tényleges telepítésnél derül
  ki — ez pontosan az a határ, amit a "dokumentált, nem deploy-olt"
  megfogalmazás jelez.
- **`p_venv/` lokálisan, ad-hoc pip install-lal épült**, nem a repo saját
  `make deps` Docker-folyamatával — ha a `make deps` valamiért más
  csomagkészletet/verziót pinnel a jövőben, ez a job venv-je nem feltétlenül
  tükrözi azt (bár a pip-installed verziók megegyeznek a
  `requirements.txt`-ben rögzítettekkel).

## Definition Of Done Check

- [x] polling loop implementálva, a MEGLÉVŐ `run_projection_batch()`/
      `run_indexing_batch()`-et hívja, fájl:sor hivatkozással —
      `session_store/worker_loop.py:65-79` (`run_one_iteration`)
- [x] `--max-iterations` paraméter létezik és tesztelhető —
      `session_store/worker_loop.py:93-150` (`run_loop`), tesztelve
      `test_loop_respects_max_iterations_bound_exactly`
- [x] systemd unit/cron deployment artifact dokumentálva, EXPLICIT "nincs
      production-ben futtatva" kijelentéssel —
      `output/session-worker-scheduler-deployment/cic-session-worker-loop.service`
      + `.timer`
- [x] többiterációs, valódi backlog-lecsapolási teszt lefuttatva,
      iterációnkénti haladás idézve — lásd Claim-Evidence Matrix
      (`iteration=1 projection_count=3 indexing_count=3`, majd
      `iteration=2`/`3` → `0/0`)
- [x] üres backlog teszt lefuttatva, kimenet idézve — lásd Claim-Evidence
      Matrix (4 iteráció, mind `0/0`)
- [x] reachability `grep -rn` eredmény idézve, `file:line` hivatkozással, a
      "létezik/tesztelt" és a "production-ben fut" állítás KÜLÖN kezelve —
      lásd "Findings" pont 5, Claim-Evidence Matrix utolsó sorok, és a
      riport végén lévő idézett grep kimenet
- [x] claim-evidence tábla kitöltve, nem üres

## Next Jobs

- Egy jövőbeli job, ha/amikor a `cic-mcp-session` repóhoz tényleges
  production Postgres instance létesül: a `.service`/`.timer` fájlok
  tényleges telepítése + `systemd-analyze verify` ellenőrzése +
  `--interval-seconds` valós production-érték meghatározása (jelenleg ez
  explicit nem cél).
- Multi-worker konkurencia-kezelés (advisory lock vagy explicit
  `FOR UPDATE` `session_core.sessions`-on), ha valaha több
  `worker_loop`/`turn_projector`/`chunk_indexer` instance egyszerre futna.
- Nagyobb (pl. szintetikus 1k+ soros) backlog-teszt a loop egy-iterációs
  drain-kapacitásának/idejének felmérésére.

---

## Reachability grep — teljes idézett kimenet

```
$ grep -rn "run_loop" --include="*.py" . | grep -v "p_venv" | grep -v "test_" | grep -v "/tests/"
session_store/worker_loop.py:87:        iteration=0,  # caller (run_loop) fills in the real 1-based iteration number
session_store/worker_loop.py:93:def run_loop(
session_store/worker_loop.py:199:        results = run_loop(

$ grep -rn "run_one_iteration" --include="*.py" . | grep -v "p_venv" | grep -v "test_" | grep -v "/tests/"
session_store/worker_loop.py:65:def run_one_iteration(config: SessionStoreConfig | None = None) -> IterationResult:
session_store/worker_loop.py:118:    run_one_iteration docstring. Only a connection-level failure (e.g.
session_store/worker_loop.py:131:        result = run_one_iteration(config=cfg)
```

**Létezik/tesztelt (proven) vs. production-ben fut (missing) — explicit
szétválasztva**: a fenti `grep` kimenet kizárólag a modul saját definícióját,
egy belső kommentet, és a modul saját `_main()`-jének hívását mutatja — 0
külső (más modulból/scriptből jövő) hívó. A loop léte és teszteltsége
(`proven`, lásd Claim-Evidence Matrix) egy ÁLLÍTÁS; az, hogy valaki/valami
TÉNYLEGESEN, ütemezetten futtatja ezt production-ben, egy MÁSIK állítás,
amelynek státusza `missing` — nincs production Postgres instance, nincs
telepített systemd unit, nincs cron-bejegyzés. A két állítás nem keverhető
össze.
