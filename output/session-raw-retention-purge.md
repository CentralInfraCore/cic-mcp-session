# session-raw-retention-purge-001 — capability riport

**Capability:** `cic_mcp.session.raw_retention_purge`
**Target repo:** `cic-mcp-session`
**Change type:** `new_capability`
**Státusz indításkor:** `experimental`

Idő-alapú retention purge a `session_raw.envelopes` táblára: a megőrzési
ablaknál (alapból 90 nap) régebbi nyers envelope-ok törlése **`occurred_at`
szerint** (sosem `ingested_at`), a törléssel **egy tranzakcióban** írt
`session_audit.raw_purges` audit-sorral.

---

## 1. Miért kellett ez a capability (melyik job/repo akadt el nélküle)

A `session-data-protection-001` job (lezárt, `done`) a megőrzési politikát
**csak dokumentálta** — `output/session-data-protection-retention-policy.md`,
"Next Jobs": *"Egy KÖVETŐ job, amely … a `session_audit.raw_purges`
audit-tábla + ütemezett purge-job … TÉNYLEGESEN implementálja"*. A politika
addig norma maradt runtime nélkül: nem volt hívható belépési pont, ami a
`session_raw.envelopes`-t a 90 napos ablak szerint ténylegesen csökkentené, és
nem volt audit-tábla a purge-eseményekhez. Ez a job ezt a hidat zárja be
(`concept → code → runtime → audit`).

A `rollback_conversation()` (`session_store/rollback.py:72`) már létezett, de az
**célzott** törlés `(provider, provider_session_id)` kulcs alapján (pl. GDPR
„töröld ezt a beszélgetést"). A **idő-alapú, automatikus** házieldolgozás (a
politika magja) hiányzott — ezt a kettőt a job szándékosan külön tartja, lásd 7.

## 2. Milyen tool/MCP contract jön létre

Nem MCP-tool, hanem **könyvtár-szintű belépési pont** (a purge operátor/ütemező
hívás, nem agent-lekérdezés):

```python
# session_store/retention_purge.py
purge_expired_raw_envelopes(
    *,
    purger: str,                          # KÖTELEZŐ — ki/mi futtatta (audit)
    retention_days: int | None = None,    # None → env → default(90)
    dry_run: bool = False,
    config: SessionStoreConfig | None = None,
) -> PurgeResult
```

- A `purger` **kötelező** (nincs default) — az audit-sornak rögzítenie kell, ki
  futtatta (szabad szöveg, pl. operátor-név vagy `"retention_cron"`), tükrözve a
  `session_audit.raw_reads.reader` konvencióját.
- Retention precedencia: **explicit arg > `SESSION_RAW_RETENTION_DAYS` env >
  `DEFAULT_RETENTION_DAYS=90`** (`resolve_retention_days()`).
- A DELETE prédikátuma: `occurred_at < now() - make_interval(days => N)` — a
  `now()` a tranzakció időbélyege (a tranzakción belül konstans), a `cutoff`
  ugyanabban a CTE-ben kerül kiolvasásra, mint amit a DELETE használ, így az
  audit-sor `cutoff`-ja bizonyíthatóan a ténylegesen alkalmazott határ.

## 3. Output schema

`PurgeResult` (frozen dataclass):

| mező | típus | jelentés |
|---|---|---|
| `rows_deleted` | int | ténylegesen törölt sorok (`DELETE … RETURNING` count); dry-run-on 0 |
| `would_delete` | int | dry-run: a prédikátum által épp matchelt sorok; valós run: = `rows_deleted` |
| `cutoff` | datetime | `now() - interval` határ; `occurred_at < cutoff` törlődött |
| `retention_days` | int | a ténylegesen használt ablak (override-ok feloldása után) |
| `dry_run` | bool | no-op előnézet volt-e |
| `purge_id` | int \| None | a `session_audit.raw_purges` audit-sor id-je; dry-run-on `None` |

Új tábla — `session_audit.raw_purges` (`migrations/0007_raw_retention_purge.sql`):

| oszlop | típus | |
|---|---|---|
| `purge_id` | BIGSERIAL PK | |
| `purger` | TEXT NOT NULL | ki futtatta |
| `retention_days` | INTEGER NOT NULL | használt ablak |
| `cutoff` | TIMESTAMPTZ NOT NULL | `occurred_at` határ |
| `rows_deleted` | INTEGER NOT NULL | törölt sorok száma |
| `purged_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | mikor futott |

**Eltérés a spec javasolt sémájától:** nincs `dry_run` oszlop. A dry-run
szándékosan **nem ír sort** (előnézet, nem purge-esemény), így a táblában minden
sor egy valódi törlést jelent — ez tisztább invariáns, mint egy `dry_run=true`
sorokkal kevert tábla. Dokumentált, tudatos döntés.

## 4. Milyen teszt bizonyítja

`tests/test_session_store/test_retention_purge.py` — **valódi Postgres**
(`pgvector/pgvector:pg16`, `migrations/0001` + `0007` applikálva), nincs mock.
9/9 PASSED:

```
tests/test_session_store/test_retention_purge.py::test_purge_deletes_old_keeps_new PASSED
tests/test_session_store/test_retention_purge.py::test_purge_uses_occurred_at_not_ingested_at PASSED
tests/test_session_store/test_retention_purge.py::test_audit_row_written_and_atomic PASSED
tests/test_session_store/test_retention_purge.py::test_dry_run_deletes_nothing_and_writes_no_audit PASSED
tests/test_session_store/test_retention_purge.py::test_purge_does_not_touch_session_core PASSED
tests/test_session_store/test_retention_purge.py::test_retention_days_env_override PASSED
tests/test_session_store/test_retention_purge.py::test_resolve_retention_days_precedence PASSED
tests/test_session_store/test_retention_purge.py::test_resolve_retention_days_rejects_negative PASSED
tests/test_session_store/test_retention_purge.py::test_resolve_retention_days_rejects_non_integer_env PASSED
============================== 9 passed in 1.75s ===============================
```

Mit bizonyít claim → evidence:

| Állítás | Bizonyító teszt |
|---|---|
| Ablaknál régebbi sor törlődik, frissebb marad | `test_purge_deletes_old_keeps_new` |
| **A határ `occurred_at`, NEM `ingested_at`** (régi event-idő + friss ingest → törlés; friss event-idő + ősrégi ingest → marad) | `test_purge_uses_occurred_at_not_ingested_at` |
| Pontosan 1 audit-sor / valós purge, és `rows_deleted` == ténylegesen eltűnt sorok (= atomicitás, azonos tranzakció) | `test_audit_row_written_and_atomic` |
| Dry-run semmit nem töröl és nem ír audit-sort, de helyes előnézet-számot ad | `test_dry_run_deletes_nothing_and_writes_no_audit` |
| **A purge nem nyúl `session_core.*`-hoz** | `test_purge_does_not_touch_session_core` |
| Env override szűkíti az ablakot | `test_retention_days_env_override` |
| Precedencia + negatív/nem-integer validáció | `test_resolve_retention_days_*` |

### Reachability / korrektség-ellenőrzés (grep)

A spec előírta a forrás `occurred_at`-only voltának grep-igazolását:

```
$ grep -rn "ingested_at" session_store/retention_purge.py | grep -v "^.*#"
# (a DELETE/SELECT prédikátumban NINCS ingested_at — csak docstringben, mint tiltás)
$ grep -rn "occurred_at" session_store/retention_purge.py
# a prédikátum occurred_at-ot használ
```

A futtatott prédikátum `occurred_at < (SELECT ts FROM cutoff)`, és a
`test_purge_uses_occurred_at_not_ingested_at` teszt élesben is kizárja, hogy az
`ingested_at` befolyásolná a törlést.

## 5. Milyen státuszban indul

`experimental`. A capability + audit-tábla applikálva és valódi PG ellen
bizonyítva — de **ütemezett futtatás (cron/systemd) nincs** és nem is része a
jobnak (hosting/operátor döntés, lásd 7). Amíg nincs deployolt ütemezés és éles
review, nem `candidate`/`canonical`.

## 6. Milyen registry/target-repo diff készül

`cic-mcp-session` (target repo), `feature/session-raw-retention-purge-001`:

- `session_store/retention_purge.py` — ÚJ (belépési pont + `PurgeResult` + `resolve_retention_days`)
- `migrations/0007_raw_retention_purge.sql` — ÚJ additív migration (`session_audit.raw_purges`)
- `output/session-raw-retention-purge-migration.sql` — a migration tükre (doc-konvenció)
- `tests/test_session_store/test_retention_purge.py` — ÚJ (9 teszt, valódi PG)
- `output/session-raw-retention-purge.md` — ez a riport

A `session_store/rollback.py` és bármely más meglévő modul **változatlan**.

## 7. Ismert limitációk

- **Nincs ütemező.** A purge hívható függvény; a cron/systemd telepítés
  szándékosan kívül van (hosting-döntés). A hívó birtokolja az ütemezést és a
  kapcsolat-életciklust.
- **Scope = `session_raw.envelopes` only.** A `session_core.*` (projektált
  turn/chunk réteg) retentionje külön, későbbi döntés — ez a purge nem nyúl
  hozzá (`test_purge_does_not_touch_session_core` bizonyítja).
- **Pre-existing rés (felfedezés):** a `session_audit` sémát eredetileg a
  `session-data-protection-001` vezette be, de annak migrationje **csak
  `output/session-data-protection-migration.sql` alatt él, NINCS bedrótozva a
  számozott `migrations/` runner-be** (a `migrations/` 0001–0006 nem tartalmaz
  `session_audit`-ot). Ezért a `0007` migration `CREATE SCHEMA IF NOT EXISTS
  session_audit`-tal **self-contained** — nem függ a `raw_reads` létezésétől.
  A data-protection migration számozott runnerbe húzása külön (data-protection)
  feladat, nem ezé a jobé.

## 8. Rollback / deprecate út

- **Capability kikapcsolása:** egyszerűen ne hívd a `purge_expired_raw_envelopes()`-t
  (nincs daemon, nincs futó állapot, amit le kéne állítani).
- **Tábla eltávolítása:** `DROP TABLE session_audit.raw_purges;` — additív,
  semmilyen meglévő táblát/FK-t nem érint, így biztonságosan visszavonható. (A
  már törölt `session_raw.envelopes` sorok természetesen nem állíthatók vissza —
  a purge célja épp a törlés; cold-storage export nincs scope-ban.)
- **Deprecate:** ha a retention politika változik, a `DEFAULT_RETENTION_DAYS` /
  `SESSION_RAW_RETENTION_DAYS` állítható kód-/újra-deploy nélkül; a `retention_days`
  oszlop minden audit-sorban rögzíti, melyik ablak melyik törlést okozta.
