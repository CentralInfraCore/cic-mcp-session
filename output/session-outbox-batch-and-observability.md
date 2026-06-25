# session-outbox-batch-and-observability-001 Output

## Scope

A `turn_projector.py` és `chunk_indexer.py` `FOR UPDATE SKIP LOCKED` select-jei eddig NEM
voltak `LIMIT`-elve (egy worker egy tranzakcióban zárolta az ÖSSZES `pending`/`failed` sort
az adott `job_type`-ra), és a `session_jobs.outbox` MÁR LÉTEZŐ `locked_by`/`locked_at`
oszlopait sosem töltötte ki sem a `turn_projector.py`, sem a `chunk_indexer.py`. Ez a job:

1. konfigurálható `batch_size` `LIMIT`-et ad a két worker claim-select-jéhez
2. `statement_timeout` biztonsági hálót állít be a claim-tranzakcióra
3. ténylegesen kitölti a `locked_by`/`locked_at` oszlopokat claim-nél, és törli befejezéskor
4. egy `get_outbox_metrics()` lekérdezést ad (`pending_count`, `oldest_pending_age_seconds`,
   `dead_letter_count`, `attempts_histogram`)

A megosztott logikát (`claim_outbox_rows`, `set_claim_statement_timeout`, `worker_identity`,
`clear_lock`, `get_outbox_metrics`) egy ÚJ, közös modul (`session_store/outbox_observability.py`)
adja, mert mindkét worker UGYANAZT a `session_jobs.outbox` táblát pollozza, azonos
claim/lock/megfigyelhetőségi szemantikával — duplikálás helyett.

**Nem érintett** (és nem is történt): az `attempts`/`max_attempts`/`dead_letter`
retry-eldöntési logika (`_mark_failed_or_dead_letter()` döntési ága változatlan, csak a
`locked_by`/`locked_at` null-ozása került hozzá UGYANAHHOZ az UPDATE-hez), egy külön
hosszú-életű monitoring-daemon (a `get_outbox_metrics()` egyszeri hívásra működik), és a
`session-schema-migration-tooling-001` hatóköre (ez a job KÖZVETLENÜL módosítja a Python
kódot, nem a migrációs-keretrendszert).

## Inputs Read

- `jobs/session-outbox-batch-and-observability-001/input.md` — job spec
- `session_store/turn_projector.py` (teljes fájl, módosítás előtt és után)
- `session_store/chunk_indexer.py` (teljes fájl, módosítás előtt és után)
- `output/session-postgres-schema.sql` — `session_jobs.outbox` teljes schema (274-292. sor),
  KÜLÖNÖSEN a `locked_by`/`locked_at` oszlopok és az `idx_session_jobs_outbox_status_created`
  index (`(status, created_at) WHERE status IN ('pending', 'failed')`)
- `tests/test_session_store/test_turn_projector.py`, `tests/test_session_store/test_chunk_indexer.py`
  — meglévő teszt-konvenció (valós Postgres, env-var konfiguráció, `TRUNCATE` autouse fixture)
- `session_store/envelope_writer.py` — `SessionStoreConfig` osztály, újrahasznosítva

## Findings

### 1. Pre-change állapot megerősítve

A módosítás ELŐTTI, git-committed (`HEAD`) verzió ellen futtatva:

```
$ git show HEAD:session_store/turn_projector.py | grep -n "LIMIT\|locked_by\|locked_at"
113:    mechanism input.md explicitly scopes out (no locked_by/locked_at
       bookkeeping is written here).

$ git show HEAD:session_store/chunk_indexer.py | grep -n "LIMIT\|locked_by\|locked_at"
(nincs kimenet)
```

Megerősítve: a `turn_projector.py` egyetlen `locked_by`/`locked_at` találata egy DOKUMENTÁCIÓS
mondat volt ("no locked_by/locked_at bookkeeping is written here") — a kódban SEHOL nem volt
tényleges írás. `LIMIT` egyik fájlban sem szerepelt. A `chunk_indexer.py`-ban egyik kifejezés
sem szerepelt egyáltalán.

### 2. Batch `LIMIT` — valós teszt, 250 soros backlog

`session_store/outbox_observability.py:claim_outbox_rows()` (100-148. sor) egy CTE-vel
kombinálja a `LIMIT %(batch_size)s`-szel ellátott `FOR UPDATE SKIP LOCKED` SELECT-et és a
claim-elt sorok `locked_by`/`locked_at` UPDATE-jét, EGY tranzakcióban.

```
$ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55450 ... pytest
  tests/test_session_store/test_outbox_observability.py::test_claim_with_250_row_backlog_only_claims_batch_size -v
test_claim_with_250_row_backlog_only_claims_batch_size PASSED
```

A teszt 250 `pending` sort szúr be, `claim_outbox_rows(cur, "project_envelope", 100, ...)`-t
hív, és ÚJRA-lekérdezi a táblát: `locked_by IS NOT NULL` → 100 sor, `locked_by IS NULL` → 150
sor (`tests/test_session_store/test_outbox_observability.py:128-148`).

### 3. `statement_timeout` — 30s, csak a claim-tranzakcióra

`set_claim_statement_timeout()` (`outbox_observability.py:69-82`) `SET LOCAL statement_timeout
= 30000`-et futtat. `SET LOCAL` tranzakció-scope-olt — COMMIT-nál automatikusan resetelődik,
így SOSEM szivárog át a per-row feldolgozó tranzakciókba (`_project_one_job`/`_index_one_job`),
amik legitim módon hosszabbak lehetnek (pl. embedding generálás).

Valós, futtatott bizonyíték a tranzakció-scope-ra (`test_statement_timeout_is_set_local_scoped_to_transaction`
PASSED): a `current_setting('statement_timeout')` a tranzakción BELÜL `30s`-ot ad (Postgres
normalizálja a `30000ms` bemenetet `30s`-ra a kimeneten), a tranzakció UTÁN (ugyanazon a
connection-ön) viszont MÁR NEM `30s` — a `SET LOCAL` nem maradt a session-en.

**Az időkorlát indoklása (30s)**: a claim-tranzakció kizárólag egy indexelt predikátum
(`idx_session_jobs_outbox_status_created`) feletti, `batch_size`-zal korlátozott SELECT +
UPDATE — ez a jelenlegi tábla-méreteken jóval 1 másodperc alatt lefut; 30s bőséges többszöröse
ennek (nem szoros korlát, nem fals-pozitív-veszélyes egészséges claim-re), de elég rövid ahhoz,
hogy egy TÉNYLEGESEN elakadt claim-tranzakció (pl. egy másik lock-ra blokkolva) tíz-másodperces
nagyságrendben felszabadítsa a sor-zárakat, ne a végtelenségig.

### 4. `locked_by`/`locked_at` — claim-nél írva, befejezésnél törölve

`worker_identity()` (`outbox_observability.py:85-97`) `"<hostname>:<pid>:<uuid4-rövid>"`
formátumú azonosítót ad. A `claim_outbox_rows()` UPDATE...FROM RETURNING-gel EGYBEN írja a
`locked_by`/`locked_at`-et a SAJÁT SELECT által zárolt sorokra (sosem szélesebb körre).

```
test_claim_writes_locked_by_and_locked_at PASSED
test_clear_lock_nulls_locked_by_and_locked_at PASSED
test_run_projection_batch_clears_lock_on_done PASSED
```

Az utolsó teszt a TELJES `run_projection_batch()` belépési ponton keresztül (nem csak az
izolált `claim_outbox_rows`/`clear_lock` helper-eken) bizonyítja, hogy egy dangling
`source_id`-ú sor `pending → failed` átmenete UTÁN a `locked_by`/`locked_at` ismét `NULL`
(`_mark_failed_or_dead_letter()` UGYANAZON UPDATE-jébe került a null-ozás, lásd
`turn_projector.py:253-271`, `chunk_indexer.py` analóg helye).

### 5. Metrika-lekérdezés — ismert fixture-rel bizonyítva

`get_outbox_metrics()` (`outbox_observability.py:201-253`) három egyszerű aggregáló
lekérdezést futtat (nem egy daemon, egyszeri hívás, input.md "Nem cél" szerint).

```
$ pytest tests/test_session_store/test_outbox_observability.py::test_get_outbox_metrics_on_known_fixture -v
PASSED
```

Fixture: 3 `pending` (attempts=0), 2 `failed` (attempts=1), 2 `dead_letter` (attempts=5) sor.
Eredmény: `pending_count=5` (pending+failed összesen, megegyezik a claim-select WHERE
feltételével), `dead_letter_count=2`, `attempts_histogram={0: 3, 1: 2, 5: 2}`,
`oldest_pending_age_seconds` egy nem-negatív float. A `test_get_outbox_metrics_no_pending_rows_age_is_none`
teszt külön bizonyítja, hogy `pending_count == 0` esetén az age `None` (nem `0`), és a
`test_get_outbox_metrics_scopes_by_job_type` teszt bizonyítja a `job_type` szűrést
(`project_envelope`: 2, `index_turn`: 4, `None` (összesen): 6).

### Regresszió-ellenőrzés

A megosztott `claim_outbox_rows()`/`set_claim_statement_timeout()` bevezetése MINDKÉT worker
meglévő, korábbi job-okból származó teszt-suite-ját nem törte el:

```
$ pytest tests/test_session_store/test_turn_projector.py -v   →  6 passed in 1.96s
$ pytest tests/test_session_store/test_chunk_indexer.py -v    → 17 passed in 14.68s
```

mindkettő a SAJÁT, e job előtti job-ok output-jaiból eredő teszt-fájlja, MÓDOSÍTÁS NÉLKÜL.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Pre-change: nincs `LIMIT`, `locked_by`/`locked_at` sehol nincs írva | proven | `git show HEAD:session_store/turn_projector.py \| grep -n "LIMIT\|locked_by\|locked_at"` → 1 dokumentációs sor, nincs kód-szintű írás; `chunk_indexer.py`-ban 0 találat | tényleges grep a committed HEAD ellen | Nincs |
| Batch `LIMIT` implementálva, konfigurálható `batch_size` paraméterrel | proven | `outbox_observability.py:claim_outbox_rows()` (100-148. sor, `LIMIT %(batch_size)s`); `turn_projector.run_projection_batch(batch_size=...)`, `chunk_indexer.run_indexing_batch(batch_size=...)` | kód olvasás | Nincs |
| 250 soros backlog esetén egy hívás CSAK `batch_size` (100) sort zár | proven | `test_claim_with_250_row_backlog_only_claims_batch_size` PASSED — 100 `locked_by IS NOT NULL`, 150 `locked_by IS NULL` | valós Postgres pytest | Nincs |
| `statement_timeout` beállítva a claim-tranzakcióra, indokolt időkorláttal | proven | `outbox_observability.py:61` (`CLAIM_STATEMENT_TIMEOUT_MS = 30_000`), `:69-82` (`set_claim_statement_timeout`, `SET LOCAL`); `test_statement_timeout_is_set_local_scoped_to_transaction` PASSED | kód olvasás + valós Postgres pytest (`current_setting` belül/kívül) | Nincs |
| `statement_timeout` NEM szivárog a per-row feldolgozó tranzakciókba | proven | ugyanaz a teszt: a tranzakción KÍVÜL `current_setting('statement_timeout')` már nem `30s` | valós Postgres pytest | Nincs |
| `locked_by`/`locked_at` ténylegesen kitöltve claim pillanatában | proven | `test_claim_writes_locked_by_and_locked_at` PASSED — claim előtt NULL, claim után `worker_identity()` értékre állítva, `locked_at` nem NULL | valós Postgres pytest | Nincs |
| `locked_by`/`locked_at` törölve befejezéskor (done/failed/dead_letter) | proven | `test_clear_lock_nulls_locked_by_and_locked_at` PASSED (izolált helper) + `test_run_projection_batch_clears_lock_on_done` PASSED (teljes `run_projection_batch()` belépési ponton át) | valós Postgres pytest | Nincs |
| `attempts`/`max_attempts`/`dead_letter` döntési logika nem módosult | proven | `_mark_failed_or_dead_letter()` `new_attempts`/`new_status` számítása byte-azonos a módosítás előttivel (`git diff` csak a `locked_by = NULL, locked_at = NULL` SET-tagot adja hozzá UGYANAHHOZ az UPDATE-hez) | `git diff session_store/turn_projector.py session_store/chunk_indexer.py` | Nincs |
| `get_outbox_metrics()` pontos értékeket ad ismert fixture-ön | proven | `test_get_outbox_metrics_on_known_fixture` PASSED — `pending_count=5`, `dead_letter_count=2`, `attempts_histogram={0:3,1:2,5:2}` | valós Postgres pytest | Nincs |
| `oldest_pending_age_seconds` `None`, ha nincs pending sor (nem `0`) | proven | `test_get_outbox_metrics_no_pending_rows_age_is_none` PASSED | valós Postgres pytest | Nincs |
| `get_outbox_metrics()` `job_type` szerint szűrhető | proven | `test_get_outbox_metrics_scopes_by_job_type` PASSED — `project_envelope`:2, `index_turn`:4, `None`:6 | valós Postgres pytest | Nincs |
| Nincs külön, hosszú-életű monitoring-daemon bevezetve | proven | `get_outbox_metrics()` egyetlen, szinkron Python-függvény-hívás (3 SQL statement, 1 cursor), nincs loop/poll/scheduler a modulban | kód olvasás (negatív bizonyíték) | Nincs |
| A meglévő `turn_projector`/`chunk_indexer` teszt-suite-ok nem regresszáltak | proven | `pytest tests/test_session_store/test_turn_projector.py` → `6 passed`; `pytest tests/test_session_store/test_chunk_indexer.py` → `17 passed`, MINDKÉT fájl módosítás nélkül | tényleges pytest futtatás a módosított worker-kódra | Nincs |
| `meta.yaml` `status` mező nem módosítva | proven | a jelen munka csak a `cic-mcp-session` klónban dolgozott | git diff (cic-mcp-factory klón) | Nincs |

## Decisions Proposed

1. **Megosztott `outbox_observability.py` modul, nem két párhuzamos implementáció** —
   `turn_projector.py` és `chunk_indexer.py` UGYANAZT a `session_jobs.outbox` táblát pollozza,
   azonos claim/lock/statement_timeout/metrika-szemantikával; egy közös modul kizárja a
   szemantikai eltérést a két worker között.
2. **`locked_by`/`locked_at` MEGFIGYELHETŐSÉGI célú, NEM koordinációs primitívum** — a
   `FOR UPDATE SKIP LOCKED` (nem a `locked_by` oszlop) marad a TÉNYLEGES többszörös-worker
   védelem; a `locked_by`/`locked_at` "ki/mikor zárolta utoljára" kérdésre válaszol crash-
   forensics célból, dokumentálva mindkét modul docstring-jében.
3. **`claim_outbox_rows()` CTE-vel, nem két külön lépésben (SELECT, majd külön UPDATE)** — a
   `UPDATE ... FROM claimed` garantálja, hogy a `locked_by`/`locked_at` CSAK a SKIP LOCKED által
   tényleg zárolt sorokra íródik, sosem egy szélesebb, race-elhető halmazra.
4. **`get_outbox_metrics()` három külön SELECT, nem egy kombinált lekérdezés** — a hisztogram
   `GROUP BY` alakja nem keverhető egy sorba a skalár aggregátumokkal; ez még egyetlen
   Python-szintű hívás/round-trip-csoport, nem napló-szerű ismételt poll.
5. **30s `statement_timeout`** — indoklás "Findings" 3. pont.

## Rejected / Out Of Scope

- Az `attempts`/`max_attempts`/`dead_letter` retry-logika módosítása — input.md "Nem cél",
  a döntési ág byte-azonos maradt.
- Külön, hosszú-életű monitoring-daemon/process — `get_outbox_metrics()` egyszeri hívásra
  működik, nincs scheduler/loop a modulban.
- A `session-schema-migration-tooling-001` hatóköre — ez a job a Python kódot módosítja
  közvetlenül, a migrációs-keretrendszer egy KÜLÖN job (és nem nyúlt ehhez a két fájlhoz).

## Risks

- **A `locked_by`/`locked_at` jelenleg NEM egy stale-lock-detektáló mechanizmus bemenete** —
  ha egy worker process meghal a claim UTÁN, de a feldolgozás KÖZBEN, a sor `locked_by`/
  `locked_at` értéke megmarad (mert a `clear_lock` csak a `_mark_done`/`_mark_failed_or_dead_letter`
  hívásokban fut, egy lezáratlan sor sosem éri el ezeket) — ez egy ISMERT, dokumentált rés:
  a `locked_by`/`locked_at` jelenleg csak "ki zárolta utoljára" megfigyelhetőség, NEM egy
  automatikus stale-lock-felszabadító mechanizmus. Mivel a `FOR UPDATE SKIP LOCKED` a TÉNYLEGES
  zárolást a Postgres-tranzakció szintjén tartja (ami a connection/process halálával
  automatikusan felszabadul), ez NEM jelent funkcionális hibát — csak azt, hogy a
  `locked_by`/`locked_at` oszlop egy halott worker után "stale" marad, amíg egy következő
  claim felül nem írja egy ÚJ claim-mel.
- **A `statement_timeout` 30s-os választása heurisztikus**, nem egy mért production
  terhelési adatból levezetett szám (a `cic-mcp-session` réteg jelenleg `experimental`,
  nincs production worker-ütemezés bekötve — `CLAUDE.md` "Jelenlegi állapot") — egy jövőbeli
  production-bekötés előtt érdemes lehet valós terhelés alapján finomítani.

## Definition Of Done Check

- [x] pre-change `LIMIT`-hiány és `locked_by`/`locked_at`-hiány grep-pel bizonyítva — "Findings" 1. pont
- [x] batch `LIMIT` valós teszttel bizonyítva (250 sor → csak batch_size zárolva/feldolgozva) — "Findings" 2. pont
- [x] `statement_timeout` beállítva, indokolva — "Findings" 3. pont
- [x] `locked_by`/`locked_at` ténylegesen kitöltve claim-nél, törölve befejezésnél, valós teszttel bizonyítva — "Findings" 4. pont
- [x] metrika-lekérdezés pontos értékeket ad ismert fixture-ön, valós teszttel bizonyítva — "Findings" 5. pont
- [x] claim-evidence tábla kitöltve, nem üres — fent, 13 sor

## Next Jobs

- Ha a `cic-mcp-session` réteg production-be kerül, érdemes egy külön jobban megfontolni egy
  stale-lock-detektáló lekérdezést (`locked_at` régebbi mint X, de a sor még `pending`/`failed`
  — ez egy halott worker nyoma), lásd "Risks".
- A `get_outbox_metrics()` jelenleg nincs bekötve semmilyen CLI/MCP tool-ba — egy jövőbeli job
  adhatna neki operátor-felületet (pl. egy `cic-session` MCP tool vagy CLI parancs).
