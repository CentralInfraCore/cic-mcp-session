# session-audit-migration-wiring-001 — capability riport

**Capability:** `cic_mcp.session.audit_migration_wiring`
**Target repo:** `cic-mcp-session`
**Change type:** `fix`
**Státusz indításkor:** `experimental`

A `session_audit` réteg bekötése a számozott, checksum-kényszerített migration-runnerbe
(`session_store/migrate.py`), két audittal igazolt provisioning-rés bezárásával.

---

## 1. Miért kellett (melyik lánc szakadt el nélküle)

A `cic-mcp-session` provisioning-bizalma a `migrate.py` számozott runneren áll: a
`migrations/000N_*.sql` az egyetlen, append-only, checksum-kényszerített forrás
(`session-schema-migration-tooling-001`). Két törés volt ezen a láncon:

1. **`session-data-protection-001` `raw_reads` migrationje sosem került a runnerbe.** A
   `session_audit.raw_reads` DDL csak `output/session-data-protection-migration.sql` alatt élt;
   a `migrations/` egyetlen számozott fájlja sem hozta létre. Egy tisztán `run_migrations()`-szel
   provisionált DB-ben így **nem létezett `session_audit.raw_reads`**, és a production hívási hely
   — `session_store/raw_read_audit.py:101` (`INSERT INTO session_audit.raw_reads`) — `relation …
   does not exist` hibára futott. A `test_data_protection.py` ezt elfedte (a sémát runneren kívül
   applikálta).

2. **A `0007_raw_retention_purge` (session-raw-retention-purge-001) stale-re tette a
   `test_migrate.py`-t.** A 0007 bevezette a `session_audit` sémát + `raw_purges`-t, de a from-zero
   teszt invariánsai `0001..0006`-ig voltak hard-kódolva. `run_migrations()` ma `0001..0007`-et ad
   vissza → az assertek buktak volna; csak azért nem volt piros a CI, mert a DB-tesztek ott nem
   futnak (nincs élő Postgres).

Ez a `concept → code → runtime` híd-szakadás klasszikus esete volt: a kód (`raw_read_audit.py`) és
a doc (`output/…-migration.sql`) megvolt, de a **runtime provisioning-lánc** megszakadt a
data-protection rétegnél.

## 2. Milyen contract / diff jön létre

Nincs új tool vagy függvény — meglévő DDL bekötése a runnerbe + a from-zero teszt-invariáns
helyreállítása:

- `migrations/0008_data_protection_raw_reads.sql` — ÚJ számozott migration (séma + `raw_reads` +
  COMMENT-ek + index), `… IF NOT EXISTS` (idempotens).
- `tests/test_session_store/test_migrate.py` — a from-zero invariáns kiterjesztve `0007`+`0008`-ra;
  `session_audit` a teardown-halmazba; új assertek a `raw_reads`+`raw_purges` létezésére; új
  regressziós teszt a runner-only `raw_read_audit` útra.

**Numbering — append-only (0008, NEM a 0007 elé):** a migrationök applikálás után immutabilisak
és checksum-kényszerítettek (`ChecksumMismatchError` változott fájlra). A 0007 már mergelt +
applikált, átszámozása megtörné a `schema_migrations.applied` verziókulcsát a már migrált DB-ken.
A `raw_reads` és `raw_purges` független (mindkettő csak `CREATE SCHEMA IF NOT EXISTS session_audit`-ot
igényel), ezért a relatív sorrend funkcionálisan közömbös — az append-only az egyetlen biztonságos út.

## 3. Output schema

Nincs új adat-output-típus. A `session_audit.raw_reads` séma változatlan (a data-protection-001
által definiált): `read_id BIGSERIAL PK, reader TEXT, read_kind TEXT, provider TEXT,
provider_session_id TEXT, rows_returned INTEGER, read_at TIMESTAMPTZ DEFAULT now()` + index a
`read_at`-en. A 0008 ezt teszi a runner részévé, nem módosítja.

## 4. Milyen teszt bizonyítja

**Valódi Postgres** (`pgvector/pgvector:pg16`, :55443), a from-zero út `run_migrations()`-szel.
`test_migrate.py` — 5/5 PASSED:

```
tests/test_session_store/test_migrate.py::test_discover_migrations_finds_all_in_order PASSED
tests/test_session_store/test_migrate.py::test_full_apply_on_empty_database PASSED
tests/test_session_store/test_migrate.py::test_second_run_is_idempotent_noop PASSED
tests/test_session_store/test_migrate.py::test_checksum_mismatch_hard_stops PASSED
tests/test_session_store/test_migrate.py::test_runner_only_provisioning_makes_raw_read_audit_work PASSED
============================== 5 passed in 0.96s ===============================
```

Regresszió-mentesség a fogyasztón — `test_data_protection.py` ugyanazon a **runner-provisionált**
DB-n (nem kézi apply), 6/6 PASSED:

```
tests/test_session_store/test_data_protection.py ......                  [100%]
============================== 6 passed in 1.07s ===============================
```

### Claim → evidence

| Állítás | Státusz | Bizonyíték | Verifikációs módszer |
|---|---|---|---|
| Friss DB `run_migrations()`-szel létrehozza a `session_audit.raw_reads`-et | implemented | `test_full_apply_on_empty_database`: `audit_tables == {raw_reads, raw_purges}` | pytest valódi PG |
| `raw_read_audit.py` runner-only provisioninggal működik | implemented | `test_runner_only_provisioning_makes_raw_read_audit_work`: audit-sor keletkezik a `:101` call site-on | pytest: valós INSERT+SELECT |
| 0007 + 0008 a runner része, sorrendben | implemented | `applied == ["0001"…"0008"]` + `discover_migrations` rendezett lista | pytest + `grep -rn` |
| 0007 változatlan (append-only) | implemented | `git diff` csak `test_migrate.py`-t + új `0008`-at érint, `migrations/0007_*`-ot NEM | `git status --porcelain` |
| Fogyasztó (data-protection) végpontig ép runner-only DB-n | implemented | `test_data_protection.py` 6/6 a migrált DB-n | pytest valódi PG |

### Reachability / korrektség (grep)

```
$ grep -rn "session_audit.raw_reads" migrations/
migrations/0008_data_protection_raw_reads.sql:32:CREATE TABLE IF NOT EXISTS session_audit.raw_reads ( …
$ grep -rln "session_audit" migrations/
migrations/0007_raw_retention_purge.sql
migrations/0008_data_protection_raw_reads.sql
$ grep -n "INSERT INTO session_audit.raw_reads" session_store/raw_read_audit.py
101:                INSERT INTO session_audit.raw_reads
```

**Production call site (`symbol létezik` ≠ `production hívja`):** a `raw_reads` releváns
fogyasztója a `session_store/raw_read_audit.py:101` `INSERT`, a `log_and_read_raw_envelopes()`-en
belül — a regressziós teszt ezt a hívási helyet futtatja, nem csak a tábla létezését nézi.

## 5. Milyen státuszban indul

`experimental`. A capability provisioning-szintű fix; a `cic-mcp-session` egésze továbbra is
`experimental` (a reachability scaffold-szintje — nincs deployolt ütemezés, MCP nincs élő
`.mcp.json`-ban — változatlan, nem ennek a jobnak a tárgya).

## 6. Registry / target-repo diff

`cic-mcp-session`, `feature/session-audit-migration-wiring-001`:
- `migrations/0008_data_protection_raw_reads.sql` — ÚJ
- `tests/test_session_store/test_migrate.py` — MÓDOSÍTVA (invariáns + regressziós teszt)
- `output/session-audit-migration-wiring.md` — ez a riport

`migrations/0007_*`, `session_store/raw_read_audit.py`, `output/session-data-protection-migration.sql`
(doc-tükör) — **változatlan**.

## 7. Ismert limitációk

- A 0008 tartalma a `output/session-data-protection-migration.sql` tükre. Ha a kettő a jövőben
  eltérne, a számozott migration (0008) az autoritatív a runner számára; az `output/` eredeti a
  doc-tükör. Érdemes egy későbbi konvencióval kikényszeríteni az egyezést (jelenleg emberi review).
- Meglévő, kézzel provisionált (data-protection-t kézzel applikált) DB-ken a `migrate.py` a 0008-at
  `IF NOT EXISTS` miatt no-op-ként futtatja, de **rögzíti** a `schema_migrations.applied`-be — ez
  szándékos és biztonságos (a runner innentől tudja, hogy a 0008 „megvan").

## 8. Rollback / deprecate út

- A 0008 tisztán additív (`CREATE … IF NOT EXISTS`), semmilyen meglévő táblát/FK-t nem érint.
  Visszavonás: `DROP TABLE session_audit.raw_reads;` (+ a `schema_migrations.applied` 0008 sorának
  törlése, ha a runner-állapotot is vissza akarjuk állítani) — de ez visszahozná az 1. pont rését,
  ezért nem ajánlott.
- A `test_migrate.py` invariáns-bővítés tisztán a valós migration-halmazt tükrözi; jövőbeli
  migrationöknél (0009+) ugyanígy bővítendő.
