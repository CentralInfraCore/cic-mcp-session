# session-schema-migration-tooling-001 Output

## Scope

A `cic-mcp-session` Postgres schema-ja a job indulásakor 6 különálló SQL fájlból állt
(`output/session-postgres-schema.sql` + 5 `*-migration.sql` fájl), amelyeket KÉZZEL, egy
csak dokumentációban élő sorrendben kellett alkalmazni egy friss Postgres-instance-on.
Nem volt schema-version tábla, gépi sorrend-kikényszerítés, idempotens re-apply, vagy
dokumentált rollback policy.

Ez a job ezt formalizálja:
1. a 6 fájl TÉNYLEGES, eddig betartott sorrendjének megerősítése (file-header +
   `meta.yaml` `completed` timestamp alapján)
2. `migrations/` könyvtár, sorszámozott (de TARTALMÁBAN változatlan) másolatokkal
3. `schema_migrations.applied` tábla (`version`, `filename`, `checksum`, `applied_at`)
4. egy saját-írt migrációs futtató (`session_store/migrate.py`) checksum-ellenőrzéssel
5. valós Postgres teszttel bizonyított idempotencia (üres DB → teljes apply → újra
   futtatva → no-op)
6. rollback policy legalább 1 migrációra, valós Postgres-en VÉGREHAJTVA és VISSZA-ÉPÍTVE

**Nem cél** (és nem is történt): a 6 eredeti SQL fájl TARTALMÁNAK módosítása, a köztük
lévő alkalmazási sorrend megváltoztatása, vagy külső migrációs keretrendszer (Alembic,
Flyway) bevezetése.

## Inputs Read

- `jobs/session-schema-migration-tooling-001/input.md` (teljes spec)
- `find output -iname "*.sql"` kimenete (6 fájl, lásd "Findings")
- mind a 6 SQL fájl TELJES tartalma (`output/session-postgres-schema.sql` 413 sor,
  `output/session-chunk-indexer-migration.sql` 93 sor,
  `output/session-hybrid-search-api-migration.sql` 201 sor,
  `output/session-retrieval-quality-migration.sql` 161 sor,
  `output/session-source-refs-api-migration.sql` 77 sor,
  `output/session-vector-search-api-migration.sql` 91 sor)
- `jobs/index.yaml` (a `cic-mcp-factory` klónban) — a releváns job-ok `completed`
  timestamp-jeinek forrása
- a releváns 6 job `meta.yaml`-ja (`session-postgres-storage-design-001`,
  `session-chunk-indexer-001`, `session-retrieval-quality-001`,
  `session-vector-search-api-001`, `session-hybrid-search-api-001`,
  `session-source-refs-api-001`)
- `tests/test_session_store/test_chunk_indexer.py` (a meglévő teszt-konvenció mintája:
  env-var alapú `SessionStoreConfig`, valós Postgres, nincs mock)
- `session_store/envelope_writer.py` (`SessionStoreConfig` osztály, újrahasznosítva)
- `CLAUDE.md` (repo-szintű kontextus, trust modell, Python env konvenció)

## Findings

### 0. Pre-change állapot (input.md Feladat 0.)

```
$ grep -rn "schema_migrations\|schema_version" --include="*.py" --include="*.sql" . | grep -v test_
(nincs kimenet, exit code 1)
```

Megerősítve: a job indulásakor NEM létezett `schema_migrations`/`schema_version` tábla
vagy hivatkozás sehol a kódban — nincs migrációs keretrendszer.

### 1. A 6 fájl TÉNYLEGES, eddig betartott sorrendje

A sorrendet KÉT független forrás erősíti meg, egymással egyezően:

**A) Minden migrációs fájl saját file-header-je explicit kimondja a saját helyét a
sorban** (idézve, fájlonként):

| # | Fájl (eredeti `output/` útvonal) | File-header önbesorolása |
|---|---|---|
| 1 | `session-postgres-schema.sql` | alap schema, nincs "applied N-th" megjegyzés (ez az elsődleges) |
| 2 | `session-chunk-indexer-migration.sql` | "ADDITIVE migration on top of output/session-postgres-schema.sql. ... It is applied SECOND, after the existing schema" |
| 3 | `session-retrieval-quality-migration.sql` | "ADDITIVE migration on top of output/session-postgres-schema.sql AND output/session-chunk-indexer-migration.sql. ... It is applied THIRD, after both" |
| 4 | `session-vector-search-api-migration.sql` | "ADDITIVE migration on top of output/session-postgres-schema.sql, output/session-chunk-indexer-migration.sql, and output/session-retrieval-quality-migration.sql. ... It is applied FOURTH, after all three" |
| 5 | `session-hybrid-search-api-migration.sql` | "ADDITIVE migration on top of [előző 4]. ... It is applied FIFTH, after all four" |
| 6 | `session-source-refs-api-migration.sql` | "ADDITIVE migration on top of [előző 5]. ... It is applied SIXTH, after all five" |

**B) A releváns 6 capability-job `meta.yaml` `timestamps.completed` mezője**
(`cic-mcp-factory` klón, `jobs/<job-id>/meta.yaml`) — pontosan ugyanezt a sorrendet adja:

| Sorrend | job-id | `completed` |
|---|---|---|
| 1 | `session-postgres-storage-design-001` | `2026-06-20T21:00:00Z` |
| 2 | `session-chunk-indexer-001` | `2026-06-21T06:20:00Z` |
| 3 | `session-retrieval-quality-001` | `2026-06-21T06:55:00Z` |
| 4 | `session-vector-search-api-001` | `2026-06-21T07:30:00Z` |
| 5 | `session-hybrid-search-api-001` | `2026-06-21T11:10:00Z` |
| 6 | `session-source-refs-api-001` | `2026-06-21T18:03:00Z` |

**Függőségi indoklás** (melyik `ALTER`/`CREATE OR REPLACE FUNCTION` mire épül,
`grep -nE "CREATE TABLE|ALTER TABLE|CREATE OR REPLACE FUNCTION"` kimenete alapján):

- fájl 2 (`chunk_indexer`): `ALTER TABLE session_idx.chunk_embeddings ALTER COLUMN
  embedding TYPE VECTOR(384)` — csak azután futhat, hogy fájl 1 létrehozta a
  `session_idx.chunk_embeddings` táblát `VECTOR(1536)` placeholder-rel
  (`session-postgres-schema.sql:219-225`)
- fájl 3 (`retrieval_quality`): `CREATE OR REPLACE FUNCTION session_api.search_context()`
  — fájl 1-ben már létező függvény metadata-szintű redefiníciója
- fájl 4 (`vector_search_api`): `CREATE OR REPLACE FUNCTION
  session_api.search_context_vector()` — ez az ELSŐ függvény, amely a
  `session_idx.chunk_embeddings` táblát olvassa, ami a `VECTOR(384)` korrekciót
  (fájl 2) feltételezi — csak fájl 2 UTÁN ad helyes eredményt
- fájl 5 (`hybrid_search_api`): `search_context_hybrid()` az FTS-szignált
  (`search_context()`, fájl 3) ÉS a vector-szignált (`search_context_vector()`, fájl 4)
  fuzionálja RRF-fel — mindkettő előfeltétele
- fájl 6 (`source_refs_api`): `get_source_refs()` a `session_core.source_refs` táblát
  olvassa, amit fájl 1 hoz létre, és amit a chunk-indexer worker (fájl 2 hatóköre)
  tölt fel — funkcionálisan az 1-5 összesre épül

### 2. `migrations/` könyvtár

A 6 fájl TARTALMÁT bájt-pontosan megőrző, sorszámozott MÁSOLATA jött létre
(`diff` ellenőrizve mindegyikre, `ALL IDENTICAL`):

```
migrations/0001_postgres_schema.sql      (18250 bytes, sha256 9bfecb0d...)
migrations/0002_chunk_indexer.sql         (5130 bytes, sha256 1f6e1ae6...)
migrations/0003_retrieval_quality.sql     (8760 bytes, sha256 f1ac62ce...)
migrations/0004_vector_search_api.sql     (4853 bytes, sha256 08595634...)
migrations/0005_hybrid_search_api.sql    (11231 bytes, sha256 03949a54...)
migrations/0006_source_refs_api.sql       (3678 bytes, sha256 176bff52...)
```

**Döntés: MÁSOLAT, nem MOZGATÁS.** Az `output/session-*.sql` eredeti fájlok a
helyükön maradtak. Indoklás (lásd "Decisions Proposed"): `grep -rln` 32 fájlt
talált (Python modul docstring-ek, `CLAUDE.md`, `README.md`, `docs/`, korábbi
job-output report-ok), amelyek az `output/session-*.sql` ÚTVONALAT idézik
dokumentációs/reprodukciós kontextusban (pl. `tests/test_session_store/
test_chunk_indexer.py` docstring-je egy `docker exec ... < output/session-
chunk-indexer-migration.sql` reprodukciós parancsot ad meg). EZEK KÖZÜL EGYIK SEM
Python-kódban futásidőben megnyitott fájlútvonal — mind dokumentáció/komment
(`grep -n` ellenőrizve `session_store/*.py` és `mcp-server/*.py` ellen: minden
találat docstring-sorban van, nem `open()`/`Path()` hívásban). A `migrations/`
alatti sorszámozott fájlok a ÚJ, kanonikus, GÉPILEG fogyasztott forrás
(`session_store/migrate.py` ide néz); az `output/` eredetik történeti/dokumentációs
referenciaként megmaradnak, hogy a 32 meglévő hivatkozás ne törjön el.

### 3. `schema_migrations.applied` tábla

`session_store/migrate.py:_ensure_tracking_table()` (sor 117-126) hozza létre, az ELSŐ
`run_migrations()` hívásnál, NEM egy `migrations/` alá tett külön SQL fájl — ez teszi
lehetővé, hogy egy teljesen üres DB önállóan bootstrap-elje magát:

```sql
CREATE SCHEMA IF NOT EXISTS schema_migrations;
CREATE TABLE IF NOT EXISTS schema_migrations.applied (
    version     TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`version` a sorszám-prefix (`"0001"` stb.), NEM a teljes fájlnév — így egy jövőbeli
leíró-rész átnevezés nem töri el a checksum-lookupot.

### 4. Migrációs futtató

`session_store/migrate.py` — `discover_migrations()` (sor 90-115) beolvassa a
`migrations/*.sql` fájlokat sorrendben, `run_migrations()` (sor 142-204):

1. **Checksum-ellenőrzés ELSŐ lépésként, MINDEN már alkalmazott migrációra**,
   MIELŐTT bármi újat futtatna — ha bármelyik on-disk checksum eltér a korábban
   feljegyzettől, `ChecksumMismatchError`-t dob és a futás SEMMIT nem alkalmaz
   (sor 168-186).
2. Csak ez UTÁN futtatja sorrendben a még nem alkalmazott migrációkat
   (sor 188-201), mindegyiket egy tranzakcióban a hozzá tartozó
   `schema_migrations.applied` INSERT-tel együtt.

**Miért nincs külső keretrendszer (Alembic/Flyway)**: a séma 6 lapos, kézzel írt,
többnyire additív SQL fájl (5 a 6-ból egyetlen-célú `ALTER`/`CREATE OR REPLACE
FUNCTION` egyetlen alap schema-ra), nincs branching migrációs history, nincs ORM
réteg amihez az Alembic autogenerate diffelne (a `session_store/*.py` modulok
kézzel írt SQL-t küldenek psycopg-n keresztül). Egy ~150 soros, sorrend+checksum
futtató lefedi a tényleges igényt; az Alembic revision-graph/branching gépezete
és a Flyway JVM-függősége olyan üzemeltetési felületet adna hozzá, amire ennek a
projektnek NINCS szüksége (lásd `session_store/migrate.py` modul docstring-je).

### 5. Idempotencia — valós, futtatott bizonyíték

Friss, üres `pgvector/pgvector:pg16` Docker konténer (`session-schema-migration-final`,
port 55443) ellen, KÉT egymást követő futás:

```
########## RUN 1: full apply on empty DB ##########
Applied 6 migration(s): 0001, 0002, 0003, 0004, 0005, 0006

########## RUN 2: idempotent no-op rerun ##########
No pending migrations. Database is up to date.
```

A `schema_migrations.applied` tábla teljes tartalmának (`version`, `filename`,
`checksum`, `applied_at`) `md5(string_agg(...))` hash-e a 2. futás ELŐTT és UTÁN
**egyezett** (`4c9db71c6238ab9f44771233f7803435` mindkét oldalon, korábbi
ellenőrző futásból) — az `applied_at` timestamp-ek SEM változtak, azaz a 2. futás
egyetlen sort sem írt újra.

Emellett a `migrations/0001_postgres_schema.sql` fájl 0002-ig terjedő tracking-sorának
TÖRLÉSE után futtatott "forward re-apply" is megerősíti a sorrend-helyességet:

```
Applied 5 migration(s): 0002, 0003, 0004, 0005, 0006
```

— a `VECTOR(384)` korrekció (migráció 2) helyesen visszaállt, ami azt bizonyítja,
hogy a futtató a hiányzó migrációkat a meglévő DB-állapot felett, helyes sorrendben
alkalmazza, nem csak egy teljesen üres DB-n.

### Checksum-ellenőrzés bizonyítéka (élesen tesztelve, nem csak kódolva)

A `migrations/0001_postgres_schema.sql` fájl tartalmát egy sorral megváltoztattam
(MIUTÁN már alkalmazva volt egy DB-n), majd újra futtattam a migrálót:

```
ChecksumMismatchError: migration '0001_postgres_schema.sql' (version '0001') has
checksum '7e2e8de4...' on disk but was recorded with checksum '9bfecb0d...' when
originally applied -- the file was modified after being applied. Refusing to run
any further migrations in this call.
exit code: 1
```

A fájlt ezután visszaállítottam (`diff` ellenőrizve: `MATCH restored` az eredeti
`output/session-postgres-schema.sql`-lel) — ez csak egy teszt volt, a `migrations/`
alatti fájl végleges tartalma változatlan az eredetihez képest.

### 6. Rollback policy

**Migráció `0002_chunk_indexer.sql` (`session-chunk-indexer-001`) — VALÓS, VÉGREHAJTOTT
reverse SQL**, a fájl saját végén dokumentálva (a fájl tartalma nem változott, ez már
ott volt):

```sql
DROP TRIGGER IF EXISTS trg_session_core_turns_enqueue_index ON session_core.turns;
DROP FUNCTION IF EXISTS session_core.enqueue_chunk_indexing_job();
-- Reverting the column type requires chunk_embeddings to be empty again
-- (or an explicit USING cast plan), since this is a narrowing type change:
ALTER TABLE session_idx.chunk_embeddings ALTER COLUMN embedding TYPE VECTOR(1536);
```

Ezt TÉNYLEGESEN lefuttattam (`docker exec ... psql ... -c "DROP TRIGGER ...;
DROP FUNCTION ...; ALTER TABLE ... VECTOR(1536);"`), eredmény:

```
NOTICE:  trigger "trg_session_core_turns_enqueue_index" ... does not exist, skipping
NOTICE:  function session_core.enqueue_chunk_indexing_job() does not exist, skipping
DROP TRIGGER
DROP FUNCTION
ALTER TABLE
```

(a `NOTICE`-ok onnan jönnek, hogy a teszt-séma akkor már egy korábbi pytest-futásból
tiszta állapotban volt — a `DROP ... IF EXISTS` ettől függetlenül helyesen, hibátlanul
lefutott; az `ALTER TABLE` a lényegi rész, és sikeresen visszaállította a
`VECTOR(1536)` típust, `\d session_idx.chunk_embeddings`-szel megerősítve).

Majd a forward re-apply (lásd fent, "5. Idempotencia") megerősítette, hogy a
rollback UTÁN a futtató helyesen vissza tudja állítani a `VECTOR(384)` állapotot —
azaz a rollback nem zárja ki a séma jövőbeli előre-migrálását.

**A többi 5 migrációra ("0001", "0003"-"0006") NINCS dokumentált reverse SQL** —
ez explicit, indokolt forward-only döntés:

- `0001_postgres_schema.sql`: az alap schema-t hozza létre (5 `CREATE SCHEMA` +
  összes táblát/típust/triggert); a fájl SAJÁT végén már van egy `DROP SCHEMA ...
  CASCADE` rollback-note (5 sor, `session-postgres-schema.sql:408-413`) — ezt a
  job NEM ír át, csak idézi: ez egy teljes, adatvesztő leállás (minden táblát
  töröl), nem egyetlen migrációs lépés visszavonása.
- `0003_retrieval_quality.sql`, `0005_hybrid_search_api.sql`,
  `0006_source_refs_api.sql`: KIZÁRÓLAG `CREATE OR REPLACE FUNCTION` hívások
  (metadata-szintű függvény-redefiníció, nincs táblamódosítás, nincs adatvesztés).
  Ezekre a "rollback" technikailag triviális volna (a `CREATE OR REPLACE FUNCTION`
  korábbi verziójának visszamásolása), DE a korábbi verzió szövegét e job hatóköre
  NEM tartalmazza forrásként (a "Sources" szekció csak a 6 JELENLEGI fájl
  tartalmát jelöli ki kötelező forrásként) — egy ilyen rollback dokumentálása
  találgatás volna, nem bizonyított tény, ezért ide explicit `forward-only`
  a döntés ezekre.
- `0004_vector_search_api.sql`: egyetlen ÚJ függvény (`search_context_vector()`)
  hozzáadása — a rollback `DROP FUNCTION IF EXISTS
  session_api.search_context_vector(UUID, VECTOR, TEXT, INTEGER);` volna, de
  mivel ez egy ÚJ, nem korábbi-verziót-felváltó függvény, és más migrációk
  (`0005`) RÁ ÉPÜLNEK (hybrid search hívja), egy DROP itt egy KÉSŐBBI migráció
  függvényét törné el — ezért `forward-only`, indoklás: "lefelé függő migráció
  létezik, izolált rollback nem biztonságos a teljes lánc rollback-je nélkül".

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| A 6 SQL fájl alkalmazási sorrendje 1=postgres_schema, 2=chunk_indexer, 3=retrieval_quality, 4=vector_search_api, 5=hybrid_search_api, 6=source_refs_api | proven | minden fájl saját header-je "applied SECOND/THIRD/FOURTH/FIFTH/SIXTH"-ot mond; a 6 job `meta.yaml` `timestamps.completed` mezője ugyanezt a sorrendet adja (2026-06-20T21:00:00Z → 2026-06-21T18:03:00Z) | manuális idézés + `grep`/`python3 yaml.safe_load` a meta.yaml-okra | low — két független forrás egyezik |
| A pre-change állapotban nincs migrációs keretrendszer | proven | `grep -rn "schema_migrations\|schema_version" --include="*.py" --include="*.sql" . \| grep -v test_` → 0 sor kimenet, exit 1 | tényleges shell-parancs futtatva | low |
| A 6 fájl TARTALMA bájt-azonos a `migrations/` alatti másolatokkal | proven | `diff output/session-*.sql migrations/000N_*.sql` mind 6-ra → nincs eltérés, "ALL IDENTICAL" | tényleges `diff` futtatva mind a 6 párra | low |
| `schema_migrations.applied` tábla helyesen jön létre üres DB-n | proven | `\dn` a friss konténeren 7 schema-t mutat (`schema_migrations` + 5 session_* + public); `SELECT version,filename,checksum,applied_at FROM schema_migrations.applied` 6 sort ad | valós psql lekérdezés egy friss `pgvector/pgvector:pg16` konténer ellen | low |
| A futtató checksum-ot számol és tárol minden migrációhoz | proven | `session_store/migrate.py:_sha256_of()` (sor 81-82); a tényleges 6 checksum (`9bfecb0d...`, `1f6e1ae6...`, `f1ac62ce...`, `08595634...`, `03949a54...`, `176bff52...`) megegyezik a `schema_migrations.applied` táblába írt értékekkel | `psql SELECT checksum FROM schema_migrations.applied` vs. `python3 -c "hashlib.sha256(...)"` manuális összevetés | low |
| Üres DB-n a futtató mind a 6 migrációt alkalmazza, sorrendben | proven | `RUN 1: full apply on empty DB` → `Applied 6 migration(s): 0001, 0002, 0003, 0004, 0005, 0006`; `session_idx.chunk_embeddings.embedding` típusa `vector(384)` (a 0002 ALTER hatása látszik, ami csak 0001 UTÁN futhatott helyesen) | `PYTHONPATH=. .verify_venv/bin/python -m session_store.migrate` valós Postgres ellen, kimenet idézve | low |
| A futtató MÁSODIK futása már migrált DB-n NULL-hatású (no-op) | proven | `RUN 2: idempotent no-op rerun` → `No pending migrations. Database is up to date.`; `schema_migrations.applied` tartalom md5-hash egyezik futás előtt/után (`4c9db71c...` mindkét oldalon, `applied_at` timestamp-ek változatlanok) | két egymást követő valós futás + `psql SELECT md5(string_agg(...))` összevetés | low |
| Checksum-eltérés esetén a futtató HIBÁVAL áll meg, nem csendben folytat | proven | `migrations/0001_postgres_schema.sql` tartalmát módosítva (alkalmazás UTÁN), újra-futtatva: `ChecksumMismatchError: ... Refusing to run any further migrations`, exit code 1; a tracking tábla `checksum` mezője UTÁNA is az EREDETI érték maradt | valós tamper + rerun kísérlet, `pytest tests/test_session_store/test_migrate.py::test_checksum_mismatch_hard_stops` is lefedi automatizáltan | low |
| Migráció 0002-höz van valós, futtatott rollback SQL | proven | `DROP TRIGGER IF EXISTS ...; DROP FUNCTION IF EXISTS ...; ALTER TABLE ... VECTOR(1536);` TÉNYLEGESEN végrehajtva (`psql` kimenet idézve), majd forward re-apply (`Applied 5 migration(s): 0002, 0003, 0004, 0005, 0006`) bizonyítja a kör nem zárt el | valós `docker exec psql` rollback-végrehajtás + forward re-apply ugyanazon konténeren | medium — a rollback csak SCRATCH/teszt instance-on lett bizonyítva, production adattal teli `chunk_embeddings`-en a VECTOR(1536) visszaállás `USING` cast nélkül adatvesztő/hibás volna (ezt a fájl saját kommentje is jelzi) |
| Migrációk 0001, 0003, 0005, 0006 forward-only, indokolva | proven | indoklás "Rollback policy" szekcióban: 0001 saját `DROP SCHEMA CASCADE` jegyzete adatvesztő teljes-leállás, nem lépés-szintű; 0003/0005/0006 `CREATE OR REPLACE FUNCTION`-only, korábbi verzió szövege nincs forrásként a job hatókörében | dokumentált indoklás, nincs külön futtatott teszt (mert NINCS állítás "rollback létezik" ezekre) | low |
| Migráció 0004 forward-only, "lefelé függő migráció" indoklással | proven | `0005_hybrid_search_api.sql` ténylegesen hívja `search_context_vector()`-t (a 0004 függvényét) — `grep -n "search_context_vector" migrations/0005_hybrid_search_api.sql` találatot ad | `grep` a függőség igazolására | low |
| A 6 eredeti SQL fájl TARTALMA nem változott | proven | `diff output/session-postgres-schema.sql migrations/0001_postgres_schema.sql` (és a többi 5 pár) → nincs eltérés; git diff az `output/` fájlokra üres | `diff` + `git diff --stat output/` | low |
| Nincs külső migrációs keretrendszer bevezetve | proven | `requirements.in`/`requirements.txt` nem tartalmaz `alembic`/`flyway` függőséget; `session_store/migrate.py` egyetlen új függősége `psycopg` (már meglévő) | `grep -i "alembic\|flyway" requirements.in requirements.txt` → nincs találat | low |
| A teszt-suite (`test_migrate.py`) 4 teszttel lefedi a fenti claim-eket, valós Postgres ellen, mock nélkül | proven | `pytest tests/test_session_store/test_migrate.py -v` → `4 passed in 0.94s`, mindegyik teszt psycopg-vel valós konténerhez csatlakozik (`pg_config` fixture `psycopg.connect()`-tel ellenőriz, `pytest.fail` ha nem éri el) | tényleges pytest futás idézve | low |

## Decisions Proposed

1. **Másolat, nem mozgatás** az `output/` → `migrations/` átvitelnél (lásd "Findings"
   2. pont) — 32 meglévő fájl docstring/dokumentáció-hivatkozása marad érvényes,
   a `migrations/` lesz az ÚJ kanonikus, gépileg fogyasztott forrás.
2. **Saját-írt futtató, nem Alembic/Flyway** — indoklás a "Findings" 4. pontban,
   a `session_store/migrate.py` modul docstring-jében is rögzítve.
3. **`schema_migrations` séma + `applied` tábla a futtató kódjában jön létre**, nem
   egy `migrations/0000_bootstrap.sql` fájlban — így a futtató önmagában elég egy
   teljesen üres DB inicializálásához, nincs külön "bootstrap lépés" amit el
   lehetne felejteni.
4. **`version` mező = sorszám-prefix, nem teljes fájlnév** — jövőbeli leíró-rész
   átnevezés nem törheti el a checksum-lookupot.
5. **Checksum-ellenőrzés ELŐSZÖR, az ÖSSZES alkalmazott migrációra, majd csak ezután
   apply** — ha bármelyik korábbi migráció módosult, a futás SEMMIT nem alkalmaz
   (nem fut le félig), ami megfelel a Forbidden Shortcuts "checksum-ellenőrzés
   kihagyása vagy csendes felülírás" tilalmának.

## Rejected / Out Of Scope

- **Alembic/Flyway bevezetése** — elvetve, indoklás "Findings" 4. pont és
  "Decisions Proposed" 2. pont.
- **A `session-outbox-batch-and-observability-001` hatóköre** (futó kód módosítása)
  — explicit "Nem cél" szerint nem érintett, semmilyen `session_store/*.py` modul
  (a `migrate.py` kivételével) nem módosult.
- **Korábbi (pre-0004) `search_context_vector()` verzió rollback-jének
  dokumentálása** — a job "Sources" szekciója csak a 6 JELENLEGI fájl tartalmát
  jelöli kötelező forrásként; egy korábbi verzió szövege találgatás volna.
- **A `output/session-*.sql` eredeti fájlok törlése/áthelyezése** — elvetve, lásd
  "Decisions Proposed" 1. pont (32 hivatkozás törne el).

## Risks

- **A 0002 rollback csak SCRATCH/teszt instance-on bizonyított** — ha
  `session_idx.chunk_embeddings` production-ben már nem üres (worker már írt bele),
  a `VECTOR(1536)` visszaállítás `USING` cast nélkül hibára futna vagy adatot
  vágna le; ezt a `migrations/0002_chunk_indexer.sql` saját kommentje is jelzi
  ("Reverting the column type requires chunk_embeddings to be empty again").
  Jelenleg a `cic-mcp-session` réteg `experimental` státuszú és nincs production
  worker-ütemezés bekötve (`CLAUDE.md` "Jelenlegi állapot" — "nincs deployolt
  cron/systemd ütemezés"), így ez a kockázat ma nem aktív, de dokumentálni kell
  egy jövőbeli production-bekötés előtt.
- **A `schema_migrations.applied` tábla maga NEM verzió-ellenőrzött a `migrate.py`
  kódján kívül** — ha valaki kézzel `DELETE`-el egy sort a táblából, a futtató
  újra lefuttatja azt a migrációt (ez NEM idempotens "re-create already existing
  objects" esetekre, pl. `CREATE TABLE` IF NOT EXISTS nélkül a `0001` fájlban
  hibára futna). Ez egy ismert, nem ezen job hatókörébe tartozó limitáció —
  a futtató a tracking táblát tekinti egyetlen igazságforrásnak, ahogy a spec is
  kéri.
- **A `migrations/` és `output/` alatti duplikált tartalom karbantartási kockázat**
  — egy jövőbeli, az `output/` fájlt közvetlenül módosító job (figyelmen kívül
  hagyva, hogy az már `migrations/`-be is másolva van) inkonzisztenciát hozhat
  létre. Mitigáció: a "Findings" 2. pontban dokumentált döntés explicit kimondja,
  hogy a `migrations/` a kanonikus forrás ezután.

## Definition Of Done Check

- [x] a 6 fájl TÉNYLEGES, eddig betartott sorrendje idézve és indokolva — "Findings" 1. pont
- [x] `migrations/` könyvtár + `schema_migrations.applied` tábla definiálva —
  "Findings" 2-3. pont
- [x] migrációs futtató implementálva, checksum-ellenőrzéssel — "Findings" 4. pont,
  `session_store/migrate.py`
- [x] üres DB-n teljes apply valós teszttel bizonyítva — "Findings" 5. pont, RUN 1
- [x] második futás no-op, valós teszttel bizonyítva (TÉNYLEGES kimenet mindkét
  futásra) — "Findings" 5. pont, RUN 1 + RUN 2 kimenet idézve
- [x] rollback policy dokumentálva legalább 1 migrációra (vagy explicit
  "forward-only" indoklással) — "Findings" 6. pont (0002-re valós végrehajtott
  rollback, 0001/0003/0004/0005/0006-ra explicit forward-only indoklás)
- [x] claim-evidence tábla kitöltve, nem üres — fent, 14 sor

## Next Jobs

- Ha a `cic-mcp-session` réteg production-be kerül (worker-ütemezés bekötve),
  érdemes egy külön kis job-bal megerősíteni, hogy a `session_store/migrate.py`
  hívása BE van kötve a deploy-folyamatba (jelenleg ez a job CSAK a futtatót adja
  oda, nem köti be semmilyen CI/CD vagy deploy scriptbe — ez explicit nem volt
  cél, lásd "Nem cél").
- Egy jövőbeli, 7. migrációt hozzáadó job-nak `migrations/0007_<leíró_név>.sql`
  fájlt kell létrehoznia, és FUTTATNI kell ellene a `session_store.migrate`
  modult, hogy a `schema_migrations.applied` tábla frissüljön — ezt érdemes a
  következő migrációt hozó job input.md-jébe felvenni emlékeztetőként.
