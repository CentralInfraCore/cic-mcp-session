# session-turn-projector-001 Output

## Scope

Ez a job megírja az ELSŐ outbox-worker-t a `cic-mcp-session` session rétegében: egy Python
modul (`session_store/turn_projector.py`), ami a `session_jobs.outbox` `pending`/`failed`
(`job_type='project_envelope'`) sorait konzumálja, a hozzájuk tartozó `session_raw.envelopes`
sort projektálja `session_core.sessions`/`session_core.turns`-ba, és lezárja az outbox-sort
(`done`/`failed`/`dead_letter`).

A job a `session-raw-event-store-001`-ben megírt write-path-ra épül: `insert_envelope()`-ot
(nem írt új insert-logikát az envelope-okhoz) és a `trg_session_raw_envelopes_enqueue`
trigger által automatikusan generált outbox-sorokat fogyasztja.

Nem cél (lásd input.md "Nem cél"): embedding-generálás / `session_idx.*`, `session_core.chunks`/
`source_refs`/`manifests` feltöltése, konkurens multi-worker lock-olás/claim-mechanizmus,
permanens futtatási infrastruktúra (cron/supervisor/systemd timer), `mcp-server/server.py`
átírása.

## Inputs Read

- `output/session-postgres-schema.sql` (a `cic-mcp-session` klónban, `main`-en) — TELJESEN
  elolvasva a kódírás előtt: `session_jobs.outbox`, `session_core.sessions`,
  `session_core.turns`, `session_raw.envelopes` tábla-definíciók, a
  `trg_session_raw_envelopes_enqueue` trigger.
- `session_store/envelope_writer.py` (a `cic-mcp-session` klónban, `main`-en) — TELJESEN
  elolvasva a kódírás előtt: `insert_envelope()`, `validate_envelope()`,
  `SessionStoreConfig`.
- `tests/test_session_store/test_envelope_writer.py` — a meglévő teszt-fixture mintája
  (valódi Postgres ellen, `_pg_config`, `_clean_*` truncate fixture).
- `output/session-raw-event-store-report.md` — az előző job claim-evidence táblája és
  reachability-besorolási mintája (`scaffold` vs `proven` szóhasználat).
- `.cic-context/factory-docs/job-slices.yaml` — `session-turn-projector-001` bejegyzés
  (phase 3, acceptance_gates, required_evidence, forbidden_shortcuts) — NORMATÍV.
- `.cic-context/factory-docs/architecture.md` — "## Schema szeparacio" szekció.
- `CLAUDE.md` (cic-mcp-session klón) — trust modell, fő határok.

## Findings

- A `session_core.turns.role` mező `TEXT NOT NULL`, de a `SessionIngressEnvelope` schema nem
  definiál `role`-t — csak `provider_event_name`-et (opcionális) és `source.kind`-ot
  (kötelező, enum: `hook`/`importer`/`manual`/`api`). A workernek tehát mindenképp döntenie
  kell egy leképezésről — ez a job "Decisions Proposed"-ben dokumentált, kódban rögzített
  `map_role()` függvénnyel oldja fel.
- A `session_core.sessions` UNIQUE constraint `(provider, provider_session_id)` — ez közvetlenül
  használható `ON CONFLICT` target-ként az upsert-hez, nincs szükség előzetes SELECT-re a
  session_id megszerzéséhez.
- A `session_core.turns` UNIQUE constraint `(session_id, turn_seq)` létezik a DDL-ben, ami egy
  második védelmi réteg a `turn_seq` ütközés ellen (ha a worker hibásan ugyanazt a `turn_seq`-et
  számolná ki kétszer, az INSERT elbukna a constraint-en, nem csendben duplikálna) — ezt a jelen
  implementáció nem provokálja ki tudatosan tesztben, mert a race-mentesség a tranzakción belüli
  `MAX(turn_seq)+1` számítással és az egyetlen-worker-feltétellel áll, de a constraint léte
  dokumentált védelmi háló.
- A `session_jobs.outbox` táblán van `FOR UPDATE SKIP LOCKED`-kompatibilis index
  (`idx_session_jobs_outbox_status_created ... WHERE status IN ('pending','failed')`) — a
  worker `SELECT ... FOR UPDATE SKIP LOCKED`-et használ a pending/failed sorok beolvasásánál,
  ami egyetlen-worker esetén nem szükséges védelem, de ingyenes és jövőbiztos (lásd "Decisions
  Proposed").
- A `session_raw.envelopes` táblának nincs `DELETE`-je sehol ebben a kódbázisban — a "nem
  létező `source_id`" hibakezelési tesztet ezért NEM töréssel/törléssel szimuláljuk (mert nincs
  meglévő törlés-API), hanem direkt egy outbox sort szúrunk be egy soha nem létezett
  `source_id`-ra (`999999999`) — ez pontosan az input.md 4. pontjának megengedett mintája
  ("pl. törölt VAGY soha nem létezett envelope").
- A repo gépén nincs előre telepített `psycopg`/`pytest` (ugyanaz a helyzet, mint az előző
  jobban) — egy ideiglenes venv-et (`/tmp/session-turn-projector-venv`) építettünk
  `psycopg[binary]` + `pytest` + `pytest-cov`-val (a `pytest.ini` `addopts`-ja `--cov=tools`-t
  vár, ezért a `pytest-cov` plugin szükséges, különben a pytest elutasítja az ismeretlen
  `--cov` flag-et).

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| `session-postgres-schema.sql` hiba nélkül alkalmazható egy valódi Postgres konténeren | proven | `docker exec -i session-turn-projector-test psql -U postgres -d testdb -v ON_ERROR_STOP=1 < output/session-postgres-schema.sql` kimenete: 5× `CREATE SCHEMA`, 4× `CREATE TYPE`, 8× `CREATE TABLE`, 11× `CREATE INDEX`, 5× `CREATE FUNCTION`, 1× `CREATE TRIGGER`, 3× `CREATE EXTENSION`, 1× `COMMENT`, 0 hiba | tényleges `psql` futtatás `pgvector/pgvector:pg16` konténeren | alacsony |
| determinisztikus role-leképezés létezik, nem AI/LLM-alapú | proven | `session_store/turn_projector.py:82` (`map_role`), `:60-78` (`PROVIDER_EVENT_NAME_TO_ROLE` dict) — pure function, nincs hálózati hívás | kódolvasás + `test_map_role_is_deterministic_and_covers_documented_cases PASSED` (lásd lent) | alacsony |
| worker függvény létezik és projektál session_core-ba | proven | `session_store/turn_projector.py:300` (`run_projection_batch`), `:236` (`_project_one_job`) | fájl:sor + lent idézett e2e pytest futtatás | alacsony |
| end-to-end lánc: insert_envelope → trigger → outbox → worker → session_core sorok → outbox done | proven | `test_full_chain_envelope_to_session_core_and_outbox_done PASSED` — lásd "Definition Of Done Check" idézett kimenet | pytest, valódi Postgres ellen (`localhost:55433`, nem mock) | alacsony |
| hibakezelés: nem létező `source_id` → `failed`/`dead_letter`, nincs kezeletlen kivétel, nincs örökre `pending` | proven | `test_dangling_source_id_marks_outbox_failed_not_crash_not_stuck_pending PASSED`, `test_dangling_source_id_reaches_dead_letter_after_max_attempts PASSED` | pytest, valódi Postgres ellen, asszertálja `status != 'pending'` és `attempts` növekedést | alacsony |
| `turn_seq` helyesen 1, 2 (majd 3) ugyanazon session 2+/3 envelope-jára | proven | `test_turn_seq_increments_across_multiple_envelopes_same_session PASSED`, `test_turn_seq_increments_across_three_envelopes_in_one_batch PASSED` | pytest, valódi Postgres ellen, asszertálja `[1, 2]` és `[1, 2, 3]` sorozatot | alacsony |
| `session_core.sessions` upsert `last_seen_at`-tel működik | proven | `test_turn_seq_increments_across_multiple_envelopes_same_session` asszertálja `len(sessions) == 1` két különböző envelope után ugyanarra a `(provider, provider_session_id)`-ra | pytest, valódi Postgres ellen, `ON CONFLICT (provider, provider_session_id) DO UPDATE` | alacsony |
| CLI belépési pont (`python -m session_store.turn_projector`) futtatható és dokumentált | proven | tényleges futtatás: `job_id=21 outcome=done`, exit_code=0; üres outbox esetén `no pending/failed project_envelope outbox jobs found`, exit_code=0 | tényleges `python -m session_store.turn_projector` futtatás, valódi Postgres ellen | alacsony |
| worker nem hív külső LLM/HTTP-t | proven | `session_store/turn_projector.py` teljes fájl átolvasva: csak `psycopg`, `logging`, `dataclasses` import — nincs `requests`/`httpx`/LLM-kliens import | kódolvasás (negatív bizonyíték: nincs ilyen import a fájlban) | alacsony |
| production reachability: `run_projection_batch`/`map_role`-t semmilyen production hívó nem hívja a CLI-n kívül | scaffold | `grep -rn "run_projection_batch" --include="*.py" . \| grep -v "test_" \| grep -v "/tests/"` → csak `session_store/turn_projector.py:29` (docstring), `:300` (definíció), `:347` (a fájl saját CLI `_main()`-je hívja) — 0 KÜLSŐ (a saját CLI-n kívüli) production hívás | tényleges `grep` futtatás, lásd "Findings"/"Definition Of Done Check" alatt idézve | közepes — szándékos, lásd "Risks" |
| CLI belépési pont létezik és dokumentált, MINT ELKÜLÖNÍTETT állítás a "valaki tényleg rendszeresen futtatja production-ben" állítástól | proven (CLI létezés) / missing (rendszeres futtatás) | a CLI létezik és lefutott (lásd fenti sor) — de NINCS cron/supervisor/systemd-timer bekötve sehol a repóban (`grep -rn "turn_projector" --include="*.yml" --include="*.yaml" --include="Makefile" . ` → 0 találat cron/scheduler config-ban) | tényleges `grep` futtatás cron/scheduler fájlokra, lásd "Definition Of Done Check" | közepes — explicit Nem cél, lásd input.md |

## Decisions Proposed

- **Role-leképezés: determinisztikus dict-lookup, `provider_event_name` elsőbbséggel,
  `source.kind == 'manual'` másodlagos szabállyal, `'event'` fallback-kel.**
  (`session_store/turn_projector.py:60-90`, `map_role()`)
  - `PostToolUse`/`PreToolUse`/`PostToolUseFailure`/`PreToolUseFailure` → `tool`
  - `Stop`/`SubagentStop` → `assistant`
  - `UserPromptSubmit` → `user`
  - `Notification`/`SessionStart`/`SessionEnd` → `system`
  - `source.kind == "manual"` (ha a `provider_event_name` nem talál egyezést) → `manual`
  - minden más / hiányzó `provider_event_name` → `event` fallback
  Indoklás: ez egy egyszerű, tisztán tesztelhető if/dict-lookup, NINCS benne hálózati hívás
  vagy LLM-feldolgozás — összhangban van azzal, hogy ez "projektált, feldolgozott állapot"
  (`session_core` réteg), nem ingress-szintű szemantikus interpretáció. A `provider_event_name`
  elsőbbsége azért indokolt, mert ez a konkrétabb szignál (a Claude Code hook-eseménytípus), a
  `source.kind` csak fallback-ágon számít (amikor nincs `provider_event_name`, pl. egy `manual`
  bejegyzésnél, ahol nincs hook-esemény). A `event` fallback biztosítja, hogy a `role TEXT NOT
  NULL` constraint SOHA nem ütközhet egy ismeretlen/jövőbeli `provider_event_name` miatt — a
  worker nem dob kivételt egy új, még nem dokumentált esemény-típusra, hanem egy explicit,
  kereshető placeholder role-t ír.
- **Modul helye: `session_store/turn_projector.py`, konzisztensen az előző job
  `session_store/envelope_writer.py` elhelyezésével.** Indoklás: ugyanaz a package, mert a
  `session_store` névtér már a "session write/projection logika" konvenció (lásd
  `session_store/__init__.py` frissített docstring-je), nem a `tools/` release-tooling mappa.
- **`FOR UPDATE SKIP LOCKED` a pending/failed outbox-sorok lekérésénél, annak ellenére, hogy a
  job explicit kizárja a multi-worker konkurencia-kezelést.** Indoklás: ez egy ingyenes,
  egysoros védelem (nem igényel külön `locked_by`/`locked_at` bookkeeping-et, amit a job
  explicit nem ír), ami egyetlen worker esetén nem változtat semmin, de elejét veszi annak,
  hogy egy véletlenül elindított második worker-instance duplán dolgozzon fel egy sort. NEM
  helyettesíti a candidate-szintű claim-mechanizmust (lásd "Risks").
- **Minden outbox-sor saját tranzakcióban projektálva (`_project_one_job`), nem egy nagy batch
  tranzakcióban.** Indoklás: ha egy sor hibás (pl. dangling `source_id`), ez nem boríthatja fel
  a batch többi, már sikeresen feldolgozott sorát — minden sor önállóan commit-olódik vagy
  bukik el, így egy hibás envelope nem blokkolja a többi feldolgozását.
- **`turn_seq` számítás: `SELECT COALESCE(MAX(turn_seq), 0) + 1 ... WHERE session_id = %s`,
  a sor-projektáló tranzakción belül, az `INSERT INTO session_core.turns` előtt.** Indoklás:
  ez pontosan az input.md 3. pont előírása ("tranzakción belül kiszámolva, hogy elkerüld a
  race-t — egyetlen worker-instance feltételezéssel ez elég, dokumentáld a limitációt"). A
  limitáció dokumentálva: lásd "Risks".

## Rejected / Out Of Scope

- konkurens, multi-worker-instance lock-olás/claim-mechanizmus (pl. `locked_by`/`locked_at`
  mezők kitöltése, advisory lock) — explicit Nem cél, lásd input.md és "Risks".
- permanens futtatási infrastruktúra (cron/supervisor/systemd timer) — explicit Nem cél; a CLI
  létezik és futtatható, de NINCS bekötve semmilyen ütemezőbe.
- `mcp-server/server.py` átírása, hogy bármit innen olvasson — explicit Nem cél.
- embedding-generálás / `session_idx.*` feltöltése — külön job: `session-chunk-indexer-001`.
- `session_core.chunks`/`source_refs`/`manifests` feltöltése — ez a job csak
  `sessions`+`turns`-ig megy, ahogy az input.md előírja.
- AI/LLM-alapú role-klasszifikáció alternatívaként — elvi alternatíva volt, de az input.md
  explicit megtiltja ("Ez NEM lehet LLM/AI-hívás"), és a determinisztikus dict-lookup
  egyébként is egyszerűbb és tesztelhetőbb.

## Risks

- **Single-worker-instance feltételezés (szándékos, dokumentált limitáció a `status
  indoklás`-ban is):** a `turn_seq` race-mentessége és az outbox-sorok feldolgozása jelenleg
  KIZÁRÓLAG akkor garantált, ha egyidejűleg legfeljebb egy worker-folyamat fut. A
  `FOR UPDATE SKIP LOCKED` megakadályozza, hogy két worker UGYANAZT az outbox-sort dolgozza fel
  kétszer, de NEM akadályozza meg, hogy két worker UGYANAHHOZ a session-höz tartozó KÉT
  KÜLÖNBÖZŐ outbox-sort egyszerre projektáljon, és mindkettő ugyanazt a `MAX(turn_seq)+1`
  értéket számolja ki egy versenyhelyzetben — ez `turns_session_seq_unique` constraint
  violation-t dobna (amit a worker jelenlegi hibakezelése `failed`-re tenne, nem csendes
  korrupcióként, de ez nem hatékony működés). `candidate`-hez kellene egy explicit
  `SELECT ... FOR UPDATE` a `session_core.sessions` soron VAGY egy Postgres advisory lock
  `session_id` alapján, plusz egy konkurencia-teszt, ami ezt bizonyítja — ez a jelen job
  explicit NEM ad (lásd input.md "Target" "status indoklás" és "Nem cél").
- **Nincs permanens futtatási mechanizmus:** a worker csak akkor fut, ha valaki manuálisan
  meghívja a `python -m session_store.turn_projector` parancsot vagy importálja
  `run_projection_batch()`-et. Amíg ez nincs cron/supervisor-ba kötve, a `session_jobs.outbox`
  sorok a valós rendszerben pending állapotban maradnak egy emberi/automatizált trigger
  hiányában — ez explicit Nem cél, nem hiba, de fontos limitáció a "candidate" státuszhoz.
- **`turn_seq` constraint-ütközés esetén `failed`-re kerül, nem `dead_letter`-re elsőre:** ha a
  fent leírt race bekövetkezne, a `psycopg.errors.UniqueViolation` a worker generikus
  except-ágában landol, és `attempts`-et növelve `failed`/`dead_letter`-re teszi az outbox sort
  — de újrapróbálkozásnál a `MAX(turn_seq)+1` újraszámítás ekkor már a frissebb állapotot látja,
  tehát egy retry valószínűleg sikeres lenne. Ez nem volt explicit tesztelve ebben a jobban
  (a race előidézése konkurens worker-indítást igényelne, ami Nem cél), csak kódolvasással
  indokolt feltételezés.
- **Connection lifecycle:** `run_projection_batch()` egy `psycopg.connect()`-et nyit a teljes
  batch-hez, de minden sort saját tranzakcióban commit-ol/rollback-el (`conn.transaction()`
  context manager soronként). Nagy batch-eknél ez egy hosszú életű connection-t jelent — ha ez
  gondot okoz production terhelésnél, egy jövőbeli job megfontolhatja a per-job connection
  nyitást, de a jelen job scope-ja (worker létezése + bizonyítása) ezt nem indokolja most.
- **A `payload` mező 1:1 bekerül a `session_core.turns.content`-be, feldolgozás nélkül.** A
  worker nem parsolja/strukturálja a payload tartalmát (pl. tool-hívás argumentumait külön
  oszlopokba) — ez jövőbeli `session_core.chunks` projekciónak lehet a feladata, itt explicit
  Nem cél.

## Definition Of Done Check

- [x] role-leképezés definiálva és indokolva (determinisztikus, NEM AI/LLM-alapú):
      `session_store/turn_projector.py:60-90` (`PROVIDER_EVENT_NAME_TO_ROLE` dict +
      `map_role()` pure function), indoklás lásd "Decisions Proposed". Pytest bizonyíték:

  ```
  tests/test_session_store/test_turn_projector.py::test_map_role_is_deterministic_and_covers_documented_cases PASSED
  ```

- [x] worker függvény/modul létezik, fájl:sor hivatkozással:
      `session_store/turn_projector.py:300` (`run_projection_batch`),
      `session_store/turn_projector.py:236` (`_project_one_job`),
      `session_store/turn_projector.py:82` (`map_role`).

- [x] end-to-end teszt (insert_envelope → outbox → worker → session_core sorok → outbox done)
      lefuttatva, kimenet idézve:

  ```
  $ docker run -d --name session-turn-projector-test -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=testdb -p 55433:5432 pgvector/pgvector:pg16
  $ docker exec -i session-turn-projector-test psql -U postgres -d testdb \
      -v ON_ERROR_STOP=1 < output/session-postgres-schema.sql
  CREATE EXTENSION
  CREATE EXTENSION
  CREATE EXTENSION
  CREATE SCHEMA
  CREATE TYPE
  CREATE TYPE
  CREATE TYPE
  CREATE TABLE
  COMMENT
  CREATE INDEX
  CREATE INDEX
  CREATE INDEX
  CREATE INDEX
  CREATE SCHEMA
  CREATE TABLE
  CREATE TABLE
  CREATE TABLE
  CREATE TABLE
  CREATE TABLE
  CREATE SCHEMA
  CREATE TABLE
  CREATE TABLE
  CREATE TABLE
  CREATE INDEX
  CREATE INDEX
  CREATE INDEX
  CREATE INDEX
  CREATE INDEX
  CREATE SCHEMA
  CREATE TYPE
  CREATE TABLE
  CREATE INDEX
  CREATE FUNCTION
  CREATE TRIGGER
  CREATE SCHEMA
  CREATE FUNCTION
  CREATE FUNCTION
  CREATE FUNCTION
  CREATE FUNCTION

  $ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55433 \
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \
    SESSION_STORE_PG_PASSWORD=test \
    python -m pytest tests/test_session_store/ -v --no-cov

  tests/test_session_store/test_envelope_writer.py::test_insert_valid_envelope_persists_row PASSED [  8%]
  tests/test_session_store/test_envelope_writer.py::test_duplicate_idempotency_key_is_noop_not_duplicate PASSED [ 16%]
  tests/test_session_store/test_envelope_writer.py::test_canonical_true_is_rejected_before_db_write PASSED [ 25%]
  tests/test_session_store/test_envelope_writer.py::test_interpreted_true_is_rejected_before_db_write PASSED [ 33%]
  tests/test_session_store/test_envelope_writer.py::test_missing_required_field_is_rejected PASSED [ 41%]
  tests/test_session_store/test_envelope_writer.py::test_invalid_source_kind_is_rejected PASSED [ 50%]
  tests/test_session_store/test_turn_projector.py::test_map_role_is_deterministic_and_covers_documented_cases PASSED [ 58%]
  tests/test_session_store/test_turn_projector.py::test_full_chain_envelope_to_session_core_and_outbox_done PASSED [ 66%]
  tests/test_session_store/test_turn_projector.py::test_dangling_source_id_marks_outbox_failed_not_crash_not_stuck_pending PASSED [ 75%]
  tests/test_session_store/test_turn_projector.py::test_dangling_source_id_reaches_dead_letter_after_max_attempts PASSED [ 83%]
  tests/test_session_store/test_turn_projector.py::test_turn_seq_increments_across_multiple_envelopes_same_session PASSED [ 91%]
  tests/test_session_store/test_turn_projector.py::test_turn_seq_increments_across_three_envelopes_in_one_batch PASSED [100%]

  ============================== 12 passed in 1.41s ==============================
  ```

  A `test_full_chain_envelope_to_session_core_and_outbox_done` konkrétan asszertálja: (1) az
  `insert_envelope()` hívás után pontosan 1 outbox sor jön létre `status='pending'`, (2)
  `run_projection_batch()` után az outbox sor `status='done'`, `last_error IS NULL`, (3)
  `session_core.sessions`-ben pontosan 1 sor jön létre a helyes `provider`/`provider_session_id`/
  `trust` értékekkel, (4) `session_core.turns`-ben pontosan 1 sor jön létre `turn_seq=1`,
  `role='assistant'` (mert `provider_event_name='Stop'`), és `source_envelope_id` az
  `insert_envelope()` által visszaadott id-vel egyezik.

- [x] hibakezelés teszt (nem létező source_id) lefuttatva, kimenet idézve, bizonyítva hogy
      nincs kezeletlen kivétel és nincs örökre pending sor: lásd fenti
      `test_dangling_source_id_marks_outbox_failed_not_crash_not_stuck_pending PASSED` és
      `test_dangling_source_id_reaches_dead_letter_after_max_attempts PASSED`. Az első teszt
      explicit asszertálja, hogy `run_projection_batch()` NEM dob kivételt (a hívás maga nem
      `pytest.raises` blokkban van), az eredmény `outcome in ("failed", "dead_letter")`, az
      outbox `status != 'pending'`, és `session_core.sessions`-ben NEM jött létre sor. A
      második teszt bizonyítja, hogy ismételt feldolgozás `failed` → `dead_letter`-re lép, ha
      `attempts >= max_attempts`, és hogy egy `dead_letter` sor a harmadik batch-ben már NEM
      kerül újra feldolgozásra (`results_3 == []`).

- [x] turn_seq helyes inkrementálás tesztelve 2+ envelope-ra ugyanazon session-höz: lásd
      `test_turn_seq_increments_across_multiple_envelopes_same_session PASSED` (2 envelope,
      `turn_seq` sorozat `[1, 2]`, asszertálva `role` és `source_envelope_id` mezőkkel együtt)
      és `test_turn_seq_increments_across_three_envelopes_in_one_batch PASSED` (3 envelope EGY
      batch-ben, `turn_seq` sorozat `[1, 2, 3]`).

- [x] reachability `grep -rn` eredmény idézve, production hívási lánc állapota explicit
      `proven`/`scaffold`-ként jelölve (a CLI-dokumentáltság és a "valaki tényleg futtatja"
      külön állításként, nem összemosva):

  ```
  $ grep -rn "run_projection_batch" --include="*.py" . | grep -v "test_" | grep -v "/tests/"
  session_store/turn_projector.py:29:point below (`python -m session_store.turn_projector`) invoke
  session_store/turn_projector.py:300:def run_projection_batch(config: SessionStoreConfig | None = None) -> list[ProjectionResult]:
  session_store/turn_projector.py:347:    results = run_projection_batch()
  ```

  Production hívási lánc állapota: **NINCS külső hívó** (a 3 találat: 1 docstring-utalás, 1
  definíció, 1 a saját fájl `_main()`-jéből, ami a CLI belépési pont — mind a saját fájlján
  belül). Státusz: **`scaffold`** a "valaki/valami rendszeresen futtatja production-ben"
  állításra.

  A CLI belépési pont LÉTEZÉSE és FUTTATHATÓSÁGA ettől ELKÜLÖNÍTVE, **`proven`**:

  ```
  $ docker exec session-turn-projector-test psql -U postgres -d testdb -c \
      "TRUNCATE TABLE session_jobs.outbox, session_core.turns, session_core.sessions, \
       session_raw.envelopes CASCADE;"
  TRUNCATE TABLE
  $ python -c "... insert_envelope(env, config=cfg) ..."
  inserted id= 19
  $ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55433 SESSION_STORE_PG_DB=testdb \
    SESSION_STORE_PG_USER=postgres SESSION_STORE_PG_PASSWORD=test \
    python -m session_store.turn_projector
  job_id=21 outcome=done
  exit_code=0
  ```

  Üres outbox esetén:

  ```
  $ python -m session_store.turn_projector
  no pending/failed project_envelope outbox jobs found
  exit_code=0
  ```

  A "valaki/valami tényleg rendszeresen futtatja production-ben" állítás ettől KÜLÖN, explicit
  **`missing`**: nincs cron/supervisor/systemd-timer/Makefile-target bekötve a repóban, ami
  ezt a CLI-t ütemezve hívná (`grep -rn "turn_projector" --include="*.yml" --include="*.yaml"
  --include="Makefile" .` → 0 találat scheduler-konfigurációban). Ez explicit Nem cél (lásd
  input.md "Nem cél": "permanens futtatási infrastruktúra (cron/supervisor/systemd timer)").

- [x] claim-evidence tábla kitöltve, nem üres — lásd fenti.

## Next Jobs

- Multi-worker konkurencia-job (`candidate`-hez): explicit `SELECT ... FOR UPDATE` vagy
  advisory lock a `session_core.sessions` soron a `turn_seq` race elkerülésére, plusz egy
  konkurens-worker-teszt, ami ezt bizonyítja (lásd "Risks").
- Permanens futtatási mechanizmus job: cron/supervisor/systemd-timer bekötés, ami rendszeresen
  meghívja `python -m session_store.turn_projector`-t — ez zárná be a "scaffold" reachability
  gap-et "proven"-re a "valaki tényleg futtatja" állításra.
- `session-chunk-indexer-001` — embedding-generálás / `session_idx.*` feltöltése (független
  ettől a jobtól, lásd input.md "Nem cél").
- Egy jövőbeli job, ami `session_core.chunks`/`source_refs`/`manifests` táblákat tölti fel a
  `session_core.turns` sorokból (chunking logika) — jelen job ezt explicit nem érinti.
