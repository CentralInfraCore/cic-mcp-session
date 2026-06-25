# session-data-protection-001 Output

## Scope

A `session_raw.envelopes` táblába a teljes, nyers hook-payload kerül be, JELENLEG semmilyen
redaction nélkül; nincs dokumentált retention policy; nincs audit-napló a raw envelope
OLVASÁSOKHOZ. Ez a job:

1. egy secret-redaction lépést épít be a raw envelope insert ELÉ (`session_store/redaction.py`,
   bekötve `envelope_writer.py:insert_envelope()`-be)
2. egy retention policy dokumentumot ír (`output/session-data-protection-retention-policy.md`)
   konkrét időtartammal (90 nap) ÉS explicit jelöli, hogy a TÉNYLEGES kikényszerítés egy
   KÖVETŐ job feladata
3. megerősíti, hogy `rollback_conversation()` (session_store/rollback.py:72) VÁLTOZATLAN
   marad mint törlési primitívum, ezt a jobot REUSE-olja
4. egy `session_audit.raw_reads` audit-táblát + egy `log_and_read_raw_envelopes()` belépési
   pontot ad (`session_store/raw_read_audit.py`), ami MINDEN raw envelope olvasásnál egy
   audit-sort ír, UGYANABBAN a tranzakcióban mint a SELECT

**Nem érintett**: `rollback_conversation()` újraírása (REUSE, nem reimplement), a retention
policy TÉNYLEGES, automatikus kikényszerítése (purge-job — explicit KÖVETŐ job, lásd "Next
Jobs"), teljes ipari secret-scanning megoldás (egy kis, bővíthető regex-lista elegendő).

## Inputs Read

- `jobs/session-data-protection-001/input.md` — job spec
- `session_store/rollback.py` — `rollback_conversation()` (72-138. sor), teljes implementáció
- `session_store/envelope_writer.py` — a raw envelope insert-path, módosítás előtt és után
- `output/session-postgres-schema.sql` — `session_raw.envelopes` schema, `raw_payload_hash`
  CHECK constraint (`~ '^sha256:[a-f0-9]{64}$'`)
- `tests/test_session_store/test_envelope_writer.py`, `tests/test_session_store/test_rollback.py`
  — meglévő teszt-konvenció (valós Postgres, autouse TRUNCATE fixture)

## Findings

### 1. Pre-change insert-path — idézve

```
$ grep -rn "INSERT INTO session_raw.envelopes" --include="*.py" . | grep -v test_
./session_store/envelope_writer.py:200:        INSERT INTO session_raw.envelopes (
```

Ez a redaction-lépés beillesztési pontja: `envelope_writer.py:insert_envelope()`, az
`INSERT` SQL-statement felépítése ELŐTT.

### 2. Secret-redaction implementáció

`session_store/redaction.py:redact_secrets()` rekurzívan végigjárja a JSON-alakú payload-ot
(str/dict/list), és minden string-levélen végigfuttatja a `SECRET_PATTERNS` listát
(`openai_api_key`, `github_personal_access_token`, `aws_access_key_id`, `slack_token`,
`generic_bearer_token`, `private_key_block`), egyezésnél `[REDACTED]`-re cserélve.

**Bővíthetőség**: egy ÚJ minta hozzáadása KIZÁRÓLAG egy új `(name, compiled_regex)` páros
hozzáadása a `SECRET_PATTERNS` listához (`redaction.py:39-46`) — semmilyen más kódútvonal
nem igényel módosítást.

`envelope_writer.py:insert_envelope()` (181. sor körül) hívja `redact_secrets(envelope["payload"])`-ot,
és a REDAKTÁLT struktúrát adja át `psycopg.types.json.Json(...)`-nak — az EREDETI `envelope`
dict NEM módosul (a függvény új struktúrát ad vissza).

**`raw_payload_hash` szándékosan ÉRINTETLEN** — ezt a PRODUCER (hook script) számolja az
EREDETI bájtokon, MIELŐTT az envelope eljutna ide; a redaction a PERZISZTÁLT payload-ot
módosítja, nem a producer-oldali hash-t (lásd "Decisions Proposed").

### 3. Valós, futtatott bizonyíték — redaction, PERZISZTÁLT sor tartalmával

```
$ pytest tests/test_session_store/test_data_protection.py::test_insert_envelope_persists_redacted_payload_not_original -v
PASSED
```

A teszt egy szintetikus, nyilvánvalóan secret-alakú fixture-t ad be
(`"leaked credential: sk-FIXTURE9876543210ABCDEFGHIJKLMNOPQ end of message"`), majd EGY ÚJ
`SELECT payload FROM session_raw.envelopes WHERE id = ...` lekérdezéssel (NEM az in-process
visszatérési értékkel) ellenőrzi a PERZISZTÁLT tartalmat:

```
persisted_payload["raw_text"] == "leaked credential: [REDACTED] end of message"
```

A fixture secret string (`sk-FIXTURE9876...`) NEM szerepel a perzisztált sorban — csak a
`[REDACTED]` placeholder. `raw_payload_hash` a perzisztálás UTÁN is az EREDETI,
envelope-ban megadott érték (dokumentált, szándékos asszimetria, lásd fent).

### 4. Retention policy dokumentum

`output/session-data-protection-retention-policy.md` — **90 nap** alapértelmezett retention
a `session_raw.envelopes` rétegben, `occurred_at` alapján mérve. A kikényszerítési TERV
(ütemezett `DELETE ... WHERE occurred_at < now() - INTERVAL '90 days'` + egy jövőbeli
`session_audit.raw_purges` audit-tábla) EXPLICIT egy KÖVETŐ jobhoz van jelölve a dokumentum
"Next Jobs" szekciójában — ez a job MAGA NEM implementál semmilyen purge-mechanizmust, csak
a policy-dokumentumot írja meg, ahogy az input.md "status indoklás" is előírja
(`status_after_merge: experimental`, NEM `candidate`).

### 5. `rollback_conversation()` megerősítése + audit-log olvasásra

`rollback_conversation()` (`session_store/rollback.py:72-138`) — ez a job NEM módosította
(`git diff session_store/rollback.py` üres). Valós teszttel megerősítve, hogy TOVÁBBRA IS
törli a `session_raw.envelopes` sorokat:

```
$ pytest tests/test_session_store/test_data_protection.py::test_rollback_conversation_still_deletes_envelopes -v
PASSED
```

(1 envelope beszúrva → `count == 1` → `rollback_conversation()` hívás →
`result.envelopes_deleted == 1` → `count == 0`.)

`session_audit.raw_reads` tábla (`output/session-data-protection-migration.sql`) +
`session_store/raw_read_audit.py:log_and_read_raw_envelopes()` — minden hívás EGY SELECT-et
ÉS egy audit-INSERT-et futtat UGYANABBAN a tranzakcióban (`raw_read_audit.py:62-103`).

```
$ pytest tests/test_session_store/test_data_protection.py::test_log_and_read_raw_envelopes_writes_audit_row tests/test_session_store/test_data_protection.py::test_log_and_read_raw_envelopes_unscoped_read_is_also_audited -v
PASSED
PASSED
```

Mindkét teszt VALÓS olvasás UTÁN egy ÚJ `SELECT ... FROM session_audit.raw_reads WHERE
read_id = ...`-tal ellenőrzi a perzisztált audit-sort: `reader`/`read_kind`/`provider`/
`provider_session_id`/`rows_returned` mind a tényleges hívás paramétereit/eredményét adja
vissza. Az `unscoped` (provider/session_id NÉLKÜLI) olvasás IS auditálódik
(`provider`/`provider_session_id` `NULL` az audit-sorban, de a sor LÉTEZIK).

### Regresszió-ellenőrzés

```
$ pytest tests/test_session_store/test_envelope_writer.py tests/test_session_store/test_rollback.py -v
8 passed in 2.00s
```

Mindkét fájl egy KORÁBBI job output-ja, MÓDOSÍTÁS NÉLKÜL — a redaction-lépés bevezetése nem
törte el a meglévő idempotencia/validáció/rollback-viselkedést.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Pre-change insert-path file:line azonosítva | proven | `grep -rn "INSERT INTO session_raw.envelopes" --include="*.py" . \| grep -v test_` → `session_store/envelope_writer.py:200` | tényleges grep | Nincs |
| Secret-redaction implementálva, bővíthető minta-listával | proven | `session_store/redaction.py:SECRET_PATTERNS` (6 minta), `redact_secrets()` rekurzív; `test_redact_secrets_replaces_known_patterns` PASSED | kód olvasás + pytest | Nincs |
| A redaction a PERZISZTÁLT sorban jelenik meg, nem csak in-process | proven | `test_insert_envelope_persists_redacted_payload_not_original` PASSED — ÚJ SELECT a fixture secret HIÁNYát és a `[REDACTED]` jelenlétét bizonyítja | valós Postgres pytest, ÚJ follow-up SELECT | Nincs |
| `raw_payload_hash` szándékosan érintetlen a redaction által | proven | ugyanaz a teszt: `persisted_hash == envelope["raw_payload_hash"]` | valós Postgres pytest | Nincs |
| Retention policy dokumentum konkrét időtartammal | proven | `output/session-data-protection-retention-policy.md` — "90 nap", `occurred_at`-alapú | dokumentum olvasása | Nincs |
| A retention TÉNYLEGES kikényszerítése EXPLICIT NEM implementált, KÖVETŐ jobra jelölve | proven | retention policy dokumentum "Kikényszerítés — TERV, NEM implementálva" + "Next Jobs" szekció | dokumentum olvasása (negatív bizonyíték — nincs purge-kód a repóban) | Nincs |
| `rollback_conversation()` VÁLTOZATLAN, file:line-nal megerősítve | proven | `git diff session_store/rollback.py` üres; `rollback.py:72-138`; `test_rollback_conversation_still_deletes_envelopes` PASSED | `git diff` (negatív bizonyíték) + valós Postgres pytest | Nincs |
| `session_audit.raw_reads` tábla migrációként létezik | proven | `output/session-data-protection-migration.sql`; `psql -f` futtatás kimenete: `CREATE SCHEMA`/`CREATE TABLE`/3×`COMMENT`/`CREATE INDEX`, hibátlanul | tényleges psql futtatás egy valós Postgres ellen | Nincs |
| Egy raw envelope olvasás UTÁN TÉNYLEGESEN megjelenik az audit-sor | proven | `test_log_and_read_raw_envelopes_writes_audit_row` PASSED — ÚJ SELECT az audit-sor tartalmával (reader/read_kind/provider/session_id/rows_returned) | valós Postgres pytest, ÚJ follow-up SELECT | Nincs |
| Unscoped (provider nélküli) olvasás is auditálódik | proven | `test_log_and_read_raw_envelopes_unscoped_read_is_also_audited` PASSED | valós Postgres pytest | Nincs |
| A meglévő envelope_writer/rollback teszt-suite nem regresszált | proven | `pytest tests/test_session_store/test_envelope_writer.py tests/test_session_store/test_rollback.py` → `8 passed`, MÓDOSÍTÁS NÉLKÜL | tényleges pytest futtatás | Nincs |
| `meta.yaml` `status` mező nem módosítva | proven | a jelen munka csak a `cic-mcp-session` klónban dolgozott | git diff (cic-mcp-factory klón) | Nincs |

## Decisions Proposed

1. **`raw_payload_hash` érintetlen marad a redaction által** — a hash a PRODUCER-oldali,
   pre-redaction bájtokat dokumentálja (provenance/integritás a FORRÁS felé), nem a
   perzisztált, már redaktált tartalmat. Egy alternatíva (a hash-t a redaktált payload-ra
   újraszámolni) ELVETVE: az input.md nem kéri a hash módosítását, és egy ilyen
   újraszámolás elrejtené, hogy a producer EREDETILEG mit hashelt.
2. **Egy ÚJ `log_and_read_raw_envelopes()` belépési pont, nem a meglévő olvasási útvonalak
   (pl. a worker pipeline belső `_fetch_envelope()`-ja) auditálása** — a worker pipeline
   SAJÁT, belső konzumálása (projection/indexing) NEM "valaki kívülről nézi a nyers adatot"
   jellegű olvasás, hanem a dokumentált write-path folytatása; az input.md "Feladat" 5
   PÉLDÁi (admin lekérdezés, historical importer) explicit KÜLSŐ, out-of-band olvasásra
   utalnak — ezt egy ÚJ, dedikált belépési ponton vezetjük át, nem a worker belső kódján.
3. **`session_audit.raw_reads` saját, dedikált schema** (`session_audit`), nem a
   `session_raw`/`session_core` alá — az audit-log konceptuálisan elkülönül az adatrétegtől,
   amit auditál.
4. **Az audit-INSERT UGYANABBAN a tranzakcióban fut, mint a SELECT** — ha a SELECT
   sikeres, az audit-sor GARANTÁLTAN megíródik (vagy mindkettő commitol, vagy mindkettő
   rollback-elődik egy connection-hiba esetén) — nincs "a SELECT lefutott, de az audit-írás
   elveszett" ablak.

## Rejected / Out Of Scope

- `rollback_conversation()` újraírása vagy módosítása — REUSE, nem reimplement, lásd
  "Findings" 5. pont (`git diff` üres rá).
- A retention policy TÉNYLEGES, automatikus kikényszerítése (purge-job) — egy KÖVETŐ job,
  explicit jelölve "Next Jobs"-ban, NEM ennek a jobnak a hatóköre.
- Teljes, ipari secret-scanning megoldás (külső szolgáltatás integrálása) — egy kis,
  bővíthető, saját regex-lista elegendő, az input.md "Nem cél" ezt explicit megengedi.
- A worker pipeline belső raw envelope olvasásainak (pl. `_fetch_envelope()` a
  turn_projector-ban) átirányítása `log_and_read_raw_envelopes()`-en keresztül — lásd
  "Decisions Proposed" 2. pont, ez egy KÜLÖNBÖZŐ kategóriájú olvasás.

## Risks

- **A `SECRET_PATTERNS` lista egy KIS, kézzel írt minta-halmaz** — nem fedi le MINDEN
  lehetséges secret-formátumot (pl. egyedi belső API-key formátumok, jelszavak szabad
  szövegben). Ez DOKUMENTÁLT, SZÁNDÉKOS korlátozás (input.md "Nem cél": "teljes, ipari
  secret-scanning megoldás" nem cél) — a bővíthetőség (egy sor hozzáadása) a mitigáció.
- **A redaction CSAK az INSERT pillanatában fut** — ha egy payload már korábban (ezen a
  jobon ELŐTT) redaction nélkül lett perzisztálva, az a sor VÁLTOZATLAN marad (nincs
  retroaktív, már meglévő sorokra futó redaction-job ebben a hatókörben). Ez egy ISMERT,
  nem ennek a jobnak a feladata limitáció.
- **A retention policy MÉG NEM kikényszerített** — ez a job EXPLICIT `experimental` státusszal
  indul EZÉRT (nem `candidate`), lásd "status indoklás" az input.md-ben. Amíg a KÖVETŐ
  purge-job nem készül el, a `session_raw.envelopes` HATÁRTALAN ideig tárolja a (redaktált,
  de nem teljesen secret-mentes) adatot.
- **`log_and_read_raw_envelopes()` egy ÚJ belépési pont, nem KIKÉNYSZERÍTETT** — semmi nem
  akadályozza meg, hogy egy jövőbeli kód közvetlenül `psycopg.connect()`-tel olvassa a
  `session_raw.envelopes`-t, megkerülve az audit-logot. Ez egy KONVENCIÓ, nem egy DB-szintű
  kikényszerítés (pl. egy view + REVOKE a mögötte lévő táblára) — egy jövőbeli, szigorúbb
  job dönthet erről, ha gyakorlatban szükségessé válik.

## Definition Of Done Check

- [x] pre-change insert-path grep-pel azonosítva, file:line idézve — "Findings" 1. pont
- [x] secret-redaction valós teszttel bizonyítva (perzisztált sor tartalma idézve) — "Findings" 3. pont
- [x] retention policy dokumentum konkrét időtartammal és kikényszerítési tervvel — "Findings" 4. pont
- [x] `rollback_conversation()` file:line-nal megerősítve mint VÁLTOZATLAN törlési primitív — "Findings" 5. pont
- [x] `session_audit.raw_reads` audit-log valós teszttel bizonyítva — "Findings" 5. pont
- [x] claim-evidence tábla kitöltve, nem üres — fent, 12 sor

## Next Jobs

- **Egy ütemezett purge-job**, amely a retention policy "Kikényszerítés" tervét (lásd
  `output/session-data-protection-retention-policy.md`) TÉNYLEGESEN implementálja:
  `DELETE FROM session_raw.envelopes WHERE occurred_at < now() - INTERVAL '90 days'`,
  ütemezve, + egy `session_audit.raw_purges` audit-tábla a purge-eseményekhez. Ez a hiányzó
  láncszem, amiért ez a job `experimental`, nem `candidate`.
- Egy jövőbeli job megfontolhatja a `SECRET_PATTERNS` lista bővítését valós, production
  hook-payload-mintákon (jelenleg csak szintetikus fixture-ökön bizonyított).
- Ha gyakorlatban szükségessé válik, egy jövőbeli job szigoríthatja a raw envelope olvasást
  egy DB-szintű kikényszerítéssel (pl. view + REVOKE), nem csak konvencióval — lásd "Risks".
