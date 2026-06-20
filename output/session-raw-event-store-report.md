# session-raw-event-store-001 Output

## Scope

Ez az ELSŐ valódi kód-implementációs job a `cic-mcp-session` session rétegében: egy
write-path Python modul (`session_store/envelope_writer.py`), ami egy
`SessionIngressEnvelope`-alakú dict-et validál és beír egy valódi Postgres
`session_raw.envelopes` táblájába. A modul:

- alkalmazás-szintű validációval ellenőrzi a kötelező mezőket INSERT előtt
- `ON CONFLICT (idempotency_key) DO NOTHING` mintával idempotens
- `canonical: true` / `interpreted: true` envelope-ot explicit elutasít, mielőtt
  bármilyen SQL-t felépítene
- a DB-kapcsolat paramétereit env var-okból olvassa (`SESSION_STORE_PG_*`, fallback
  `PG*` standard env var-okra), NINCS hardcode-olt connection string

Nem cél (lásd input.md "Nem cél"): outbox-worker, embedding-generálás,
`mcp-server/server.py` átírása, éles Postgres üzemeltetés. Ezeket ez a job nem
érinti.

## Inputs Read

- `output/session-postgres-schema.sql` (a `cic-mcp-session` klónban, `main`-en) —
  TELJESEN elolvasva a kódírás előtt
- `output/session-ingress-envelope.schema.yaml` (a `cic-mcp-session` klónban,
  `main`-en) — TELJESEN elolvasva a kódírás előtt
- `.cic-context/factory-docs/job-slices.yaml` — `session-raw-event-store-001`
  bejegyzés (phase 3, acceptance_gates, required_evidence, forbidden_shortcuts)
- `.cic-context/factory-docs/architecture.md` — "Postgres-first elv" szekció
- `CLAUDE.md` (cic-mcp-session klón) — trust modell, fő határok

## Findings

- A `session-postgres-schema.sql` DDL valóban 1:1 leképezi a
  `SessionIngressEnvelope` schema-t — minden kötelező envelope-mező típusos
  oszlop vagy JSONB sub-objektum a `session_raw.envelopes` táblában.
- A `canonical`/`interpreted` mezőkre a DDL-ben már van DB-szintű `CHECK (... =
  false)` constraint. A jelen jobban az alkalmazás-szintű elő-validációt
  választottuk a DB CHECK hibájának lekapásával szemben (lásd "Decisions
  Proposed" — indoklás ott).
- Az `idempotency_key` UNIQUE constraint már létezik a DDL-ben
  (`envelopes_idempotency_key_unique`), a write-path ezt `ON CONFLICT ...
  DO NOTHING` mintával használja ki.
- A repo `tools/` mappája kizárólag release/build tooling (compiler, vault
  signing, infra) — session-specifikus tartalomnak nem ide, hanem egy önálló
  `session_store/` package-be való, ezért ezt választottuk (az input.md
  mindkettőt megengedte: "pl. `session_store/` vagy `tools/session_store.py`").
- A gépen nincs előre telepített Postgres driver és nincs `p_venv/` — egy
  ideiglenes venv-et (`/tmp/session-raw-venv`) építettünk `psycopg[binary]` +
  `pytest` + `pip-tools`-szal a teszteléshez és a `requirements.txt`
  regenerálásához (`pip-compile`).

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| `session-postgres-schema.sql` hiba nélkül alkalmazható egy valódi Postgres konténeren | proven | `docker exec -i session-raw-event-store-test psql -U postgres -d testdb -v ON_ERROR_STOP=1 < output/session-postgres-schema.sql` kimenete: 5× `CREATE SCHEMA`, 4× `CREATE TYPE`, 8× `CREATE TABLE`, 11× `CREATE INDEX`, 5× `CREATE FUNCTION`, 1× `CREATE TRIGGER`, 3× `CREATE EXTENSION`, 0 hiba | tényleges `psql` futtatás `pgvector/pgvector:pg16` konténeren (PostgreSQL 16.14) | alacsony |
| write-path függvény létezik és validál+insertál | proven | `session_store/envelope_writer.py:165` (`insert_envelope`), `session_store/envelope_writer.py:105` (`validate_envelope`) | fájl:sor hivatkozás + lent idézett pytest futtatás | alacsony |
| sikeres insert valódi envelope-pal | proven | `test_insert_valid_envelope_persists_row PASSED` — lásd "Definition Of Done Check" idézett kimenet | pytest, valódi Postgres ellen (`localhost:55432`, nem mock) | alacsony |
| idempotencia: kétszer ugyanaz az `idempotency_key` → nincs duplikáció, nincs kivétel | proven | `test_duplicate_idempotency_key_is_noop_not_duplicate PASSED`, asszertál `second_id is None` és `_count_rows == 1` | pytest, valódi Postgres ellen, `ON CONFLICT DO NOTHING` + `RETURNING id` (None ha nincs új sor) | alacsony |
| `canonical: true` elutasítva | proven | `test_canonical_true_is_rejected_before_db_write PASSED`, `EnvelopeValidationError` dobva, `_count_rows == 0` | pytest, valódi Postgres ellen — bizonyítja hogy a DB-be SE jutott be sor | alacsony |
| `interpreted: true` elutasítva | proven | `test_interpreted_true_is_rejected_before_db_write PASSED`, `EnvelopeValidationError` dobva, `_count_rows == 0` | pytest, valódi Postgres ellen | alacsony |
| reprodukálható teszt-parancs dokumentált | proven | `tests/test_session_store/test_envelope_writer.py` modul docstring tartalmazza a teljes `docker run` → `psql` → `pytest` szekvenciát | a lenti "Definition Of Done Check" pontosan ezt a parancssort futtatta le | alacsony |
| production reachability: a write-path-ot semmilyen production hívó nem hívja ebben a jobban | scaffold | `grep -rn "insert_envelope" --include="*.py" . \| grep -v "test_" \| grep -v "/tests/"` → csak `session_store/envelope_writer.py:19` (docstring-utalás), `:109` (docstring-utalás), `:165` (definíció) — 0 production hívás | tényleges `grep` futtatás, lásd "Findings" alatt idézve még egyszer | közepes — szándékos, lásd "Risks" |
| `requirements.in`/`requirements.txt` frissítve `psycopg[binary]`-vel | proven | `requirements.in` diff: `psycopg[binary]` hozzáadva; `requirements.txt`: `psycopg[binary]==3.3.4`, `psycopg-binary==3.3.4` sorok, `pip-compile --output-file=requirements.txt requirements.in` futtatásából | `pip-compile` (pip-tools 7.5.3) tényleges futtatása egy erre épített venv-ben | alacsony |

## Decisions Proposed

- **canonical/interpreted elutasítás: alkalmazás-szintű előzetes validáció, NEM a
  DB CHECK hibájának lekapása.** Indoklás: a DDL-ben már van `CHECK (canonical =
  false)` / `CHECK (interpreted = false)` constraint, tehát a DB-szintű védelem
  eleve létezik függetlenül ettől a jobtól. Az alkalmazás-szintű
  `validate_envelope()` előzetes elutasítás (lásd `session_store/envelope_writer.py:105-163`)
  hozzáadott értéke: (1) explicit, ember-olvasható hibaüzenet ("rejected:
  canonical must be false..."), nem driver-specifikus `psycopg.errors.CheckViolation`
  parsing; (2) a hibás envelope SOHA nem ér el a DB-ig, így nincs félbehagyott
  tranzakció vagy connection-lifecycle kérdés egy elutasított írás körül. Mindkét
  megközelítés elfogadható volt az input.md szerint, ezt választottuk.
- **Modul helye: önálló `session_store/` package, nem `tools/session_store.py`.**
  Indoklás: a `tools/` mappa kizárólag release/build tooling-ot tartalmaz
  (compiler.py, vault signing, infra.py) — session-tartalmi kódnak (write-path,
  jövőbeli projector/read-path) saját névtér kell, hogy ne keveredjen a
  repo-szintű build-eszközökkel.

## Rejected / Out Of Scope

- outbox-worker (`session_jobs.outbox` konzumáló folyamat) — külön job:
  `session-turn-projector-001`
- embedding-generálás — külön job: `session-chunk-indexer-001`
- `mcp-server/server.py` átírása, hogy hívja ezt a write-path-ot — ez egy
  read-path komponens, explicit nem cél
- éles Postgres-instance üzemeltetés / migrációs framework bekötése — explicit
  nem cél
- DB CHECK constraint hibájának lekapása alternatív útként — elvi alternatíva
  volt, az alkalmazás-szintű validáció mellett döntöttünk (lásd "Decisions
  Proposed")

## Risks

- **Reachability gap (szándékos, közepes súlyosságú amíg fennáll):** a
  write-path-nak jelenleg NINCS production hívója — sem hook, sem importer,
  sem worker nem hívja `insert_envelope()`-t a jelen állapotban. A kód
  létezik és tesztelt, de egy SessionIngressEnvelope a valós rendszerben MA
  nem kerül be ezen a path-on a `session_raw.envelopes` táblába, amíg egy
  jövőbeli job (collector/hook integráció) ezt be nem köti. Ez explicit
  `scaffold` státusz a reachability claim-re, nem `proven`.
- **Connection lifecycle:** `insert_envelope()` minden hívásnál új
  `psycopg.connect()`-et nyit és zár (nincs connection pool). Production
  hívási volumennél ez újragondolandó (pl. `psycopg_pool`), de a jelen job
  scope-ja (write-path létezése + bizonyítása) ezt nem indokolja most.
  `psycopg[binary]` választás: a `psycopg[binary]`-csomagolt bináris build
  egyszerűbb telepítést ad CI/agent környezetben fordítóeszközök nélkül is
  (a `[binary]` extra C-fordítás nélküli wheel-t telepít).
- **`requirements.txt` regenerálás:** a gépen nem volt előre telepített
  `pip-compile`, ezért egy ideiglenes venv-et (`/tmp/session-raw-venv`)
  építettünk pip-tools 7.5.3-mal a regeneráláshoz. A `pip-compile` futtatás
  csak a `psycopg`-függő sorokat (`psycopg[binary]==3.3.4`,
  `psycopg-binary==3.3.4`, és egy `# via psycopg` megjegyzés-sor) érintette
  a `requirements.txt`-ben, a diff minimális (`git diff --stat`: 1 file
  changed, 6 insertions(+), 1 deletion(-)).
- **`p_venv/`:** a repo konvenció szerinti `p_venv/` (lásd CLAUDE.md "Python
  környezet") nem épült meg ebben a jobban (gitignored, nem job-output) — a
  fejlesztő/agent, aki ezt a kódot tovább viszi, futtassa a `make deps`-et
  vagy építsen saját venv-et a `requirements.txt` alapján.

## Definition Of Done Check

- [x] a `session-postgres-schema.sql` hiba nélkül alkalmazva egy valódi
      Postgres konténeren, idézve a kimenetet:

  ```
  $ docker run -d --name session-raw-event-store-test -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=testdb -p 55432:5432 pgvector/pgvector:pg16
  $ docker exec -i session-raw-event-store-test psql -U postgres -d testdb \
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
  ```

  (`\dn` ellenőrzés utána: `session_api`, `session_core`, `session_idx`,
  `session_jobs`, `session_raw` mind az 5 schema létrejött, 0 hiba.)

- [x] write-path függvény/modul létezik, fájl:sor hivatkozással a reportban:
      `session_store/envelope_writer.py:165` (`insert_envelope`),
      `session_store/envelope_writer.py:105` (`validate_envelope`).

- [x] sikeres insert teszt VALÓDI Postgres ellen, lefuttatva, kimenet idézve:

  ```
  $ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55432 \
    SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \
    SESSION_STORE_PG_PASSWORD=test \
    python -m pytest tests/test_session_store/ -v

  tests/test_session_store/test_envelope_writer.py::test_insert_valid_envelope_persists_row PASSED [ 16%]
  tests/test_session_store/test_envelope_writer.py::test_duplicate_idempotency_key_is_noop_not_duplicate PASSED [ 33%]
  tests/test_session_store/test_envelope_writer.py::test_canonical_true_is_rejected_before_db_write PASSED [ 50%]
  tests/test_session_store/test_envelope_writer.py::test_interpreted_true_is_rejected_before_db_write PASSED [ 66%]
  tests/test_session_store/test_envelope_writer.py::test_missing_required_field_is_rejected PASSED [ 83%]
  tests/test_session_store/test_envelope_writer.py::test_invalid_source_kind_is_rejected PASSED [100%]

  ============================== 6 passed in 0.78s ===============================
  ```

- [x] idempotencia teszt (kétszer ugyanaz az idempotency_key) lefuttatva,
      kimenet idézve, bizonyítva hogy nincs duplikáció és nincs kezeletlen
      kivétel: lásd fenti `test_duplicate_idempotency_key_is_noop_not_duplicate
      PASSED` — a teszt explicit asszertálja `second_id is None` (nincs új sor)
      és `_count_rows(pg_config) == 1` (nincs duplikáció) ugyanazon a
      kapcsolaton, kivétel dobása nélkül.

- [x] `canonical: true` elutasítás teszt lefuttatva, kimenet idézve: lásd fenti
      `test_canonical_true_is_rejected_before_db_write PASSED` —
      `EnvelopeValidationError` dobva, `_count_rows == 0` (nem jutott a DB-be).

- [x] `interpreted: true` elutasítás teszt lefuttatva, kimenet idézve: lásd
      fenti `test_interpreted_true_is_rejected_before_db_write PASSED`.

- [x] a teszt-futtatás reprodukálható egy dokumentált paranccsal
      (konténer-indítástól pytest-ig): lásd
      `tests/test_session_store/test_envelope_writer.py` modul docstring,
      pontos `docker run` → `pg_isready` várakozás → `psql` DDL-alkalmazás →
      env var-os `pytest` parancssor.

- [x] reachability `grep -rn` eredmény idézve, és a production hívási lánc
      állapota (van/nincs) explicit `proven`/`scaffold`-ként jelölve,
      file:line-nal ha van hívó:

  ```
  $ grep -rn "insert_envelope" --include="*.py" . | grep -v "test_" | grep -v "/tests/"
  session_store/envelope_writer.py:19:calls insert_envelope().
  session_store/envelope_writer.py:109:    database — pure in-memory validation, run BEFORE insert_envelope()
  session_store/envelope_writer.py:165:def insert_envelope(
  ```

  Production hívási lánc állapota: **NINCS** (a 3 találat: 2 docstring-utalás
  + 1 definíció, mind a saját fájlján belül). Státusz: **`scaffold`** — a
  write-path kód létezik és tesztelt, de nincs production hívási láncban
  ebben a jobban. (Ez szándékos, lásd input.md "Nem cél".)

- [x] claim-evidence tábla kitöltve, nem üres — lásd fenti.

## Next Jobs

- `session-turn-projector-001` — a `session_jobs.outbox` konzumáló worker, ami
  a `session_raw.envelopes`-ból `session_core.turns`/`session_core.sessions`-t
  projektál. Ez lenne a következő lépés `candidate`-hez (lásd job
  `status indoklás`).
- egy jövőbeli hook/importer integrációs job, ami tényleg hívja
  `insert_envelope()`-t egy valódi collector-ból (ez zárná be a reachability
  gap-et `scaffold`-ról `proven`-re).
- `session-chunk-indexer-001` — embedding-generálás (független ettől a jobtól).
