# session-hook-collector-001 Output

## Scope

Ez a job megírta az ELSŐ valódi `SessionIngressEnvelope`-producer scriptet a `cic-mcp-session`
repóban: `hooks/log-event.py`. A script egy Claude Code hook stdin JSON-jából determinisztikusan
felépít egy `SessionIngressEnvelope` dict-et, és meghívja a MEGLÉVŐ
`session_store.envelope_writer.insert_envelope()`-et — nem ír újra write-path logikát.

A scope KIZÁRÓLAG ez: a hook script léte + bizonyítása konstruált minta-payload-okkal valódi
Postgres ellen, beleértve a DB-elérhetetlenségi szimulációt. NEM scope: valódi, élő Claude Code
session elleni tesztelés (lásd input.md "Nem cél" — ez technikailag nem is végezhető el ebből a
job-ból), a hook aktiválása/telepítése bármilyen éles `.claude/settings.json`-ba, vagy
`insert_envelope()`/`validate_envelope()` módosítása.

## Inputs Read

- `session_store/envelope_writer.py` (teljes fájl, 233 sor) — `insert_envelope()`,
  `validate_envelope()`, `REQUIRED_FIELDS`, `SessionStoreConfig.from_env()`, `EnvelopeValidationError`
- `session_store/turn_projector.py` (teljes fájl, 361 sor) — `map_role()`,
  `PROVIDER_EVENT_NAME_TO_ROLE` (a `provider_event_name` ismert értékei: `PostToolUse`,
  `PreToolUse`, `PostToolUseFailure`, `PreToolUseFailure`, `Stop`, `SubagentStop`,
  `UserPromptSubmit`, `Notification`, `SessionStart`, `SessionEnd`)
- `session_store/chunk_indexer.py` (teljes fájl, 630 sor) — `extract_source_refs()`,
  `TOOL_NAME_KEY = "tool_name"`, `FILE_PATH_KEYS = ("file_path", "path", "notebook_path")`,
  `NESTED_TOOL_INPUT_KEY = "tool_input"`
- `tests/test_session_store/test_envelope_writer.py` — `_valid_envelope()` (sor 84–106), a
  reprodukciós recept docstringje (sor 1–25): valódi Postgres `pgvector/pgvector:pg16` image,
  `docker run -p 55432:5432`, `pg_isready` várakozás, majd `output/session-postgres-schema.sql`
  alkalmazása `psql -v ON_ERROR_STOP=1`-lel
- `output/session-ingress-envelope.schema.yaml` — a teljes `SessionIngressEnvelope` JSON Schema
  contract, KÜLÖNÖSEN az `idempotency_key` mező NORMATÍV, már dokumentált levezetési formulája
  (sor 214–247) — ez kiderült, hogy MÁR LÉTEZIK, nem ezen jobnak kellett kitalálnia, lásd
  "Decisions Proposed"
- `output/session-postgres-schema.sql` + a további 5 migrációs SQL fájl (`session-chunk-indexer-migration.sql`,
  `session-retrieval-quality-migration.sql`, `session-vector-search-api-migration.sql`,
  `session-hybrid-search-api-migration.sql`, `session-source-refs-api-migration.sql`) — minden
  fájl fejkommentje explicit megadja az alkalmazási sorrendet (1. schema, 2. chunk-indexer,
  3. retrieval-quality, 4. vector-search-api, 5. hybrid-search-api, 6. source-refs-api)
- `output/session-ingress-envelope-contract.md` — a `log-event.py` gap-elemzés (a jelenlegi,
  MÁS repóban élő `tools/hooks/log-event.py` `summarize()`-ja miért NEM elég gazdag payload,
  miért nincs idempotency key benne), és a `log-event.py@v2-envelope` mintapéldák
  (sor 204–360)
- `/home/sinkog/sync/claude_factory/CIC/workdir/tools/hooks/log-event.py` (teljes fájl, 75 sor) —
  a MÁR DEPLOYOLT cic-factory hook-mintapélda: "Always exits 0 — never blocks the agent",
  argparse `--event` kapcsoló, `CIC_JOB_ID`/`CIC_WORKDIR` env-feltételes no-op, minden hibát
  elnyel és `sys.exit(0)`-dal tér vissza
- `.cic-context/factory-docs/job-slices.yaml` — `session-hook-collector-001` bejegyzés
  (sor 503–533) — `acceptance_gates`, `required_evidence`, `forbidden_shortcuts` listák

## Findings

### Hook JSON → SessionIngressEnvelope mezőleképezés

A Claude Code hook stdin JSON kontraktusát a saját tudásomból derítettem ki (nincs élő
dokumentáció-fetch ebben a jobban) — ez a forrás explicit megnevezve, nem találtam ki ad hoc
mezőneveket. Minden hook-eseményen jelen lévő közös mezők: `session_id`, `transcript_path`,
`cwd`, `hook_event_name`. Esemény-specifikus mezők: `tool_name`/`tool_input`
(PreToolUse/PostToolUse), `tool_response` (csak PostToolUse), `prompt` (UserPromptSubmit),
`stop_hook_active` (Stop/SubagentStop, loop-guard — ezt a meglévő, MÁR DEPLOYOLT
`tools/hooks/log-event.py` is explicit kezeli, sor 41–43: `if data.get("stop_hook_active"): return {}`).

| SessionIngressEnvelope mező | Forrás | Indoklás |
|---|---|---|
| `apiVersion` | konstans `"cic.session/v1"` | `envelope_writer.EXPECTED_API_VERSION` |
| `kind` | konstans `"SessionIngressEnvelope"` | `envelope_writer.EXPECTED_KIND` |
| `event_id` | `uuid.uuid4()`, a SCRIPT generálja | a hook JSON-nak NINCS saját per-event UUID-ja; a schema (`session-ingress-envelope.schema.yaml` sor 47–54) explicit megengedi, hogy `event_id` a collector-nál keletkezzen |
| `provider` | konstans `"claude-code"` | `_valid_envelope()` minta (sor 89) |
| `provider_session_id` | hook JSON `session_id` mező | saját tudás a hook-kontraktusról |
| `provider_event_name` | hook JSON `hook_event_name` mező | saját tudás a hook-kontraktusról; `turn_projector.PROVIDER_EVENT_NAME_TO_ROLE` ezt a mezőt várja pontosan ezekkel az értékekkel |
| `source.kind` | konstans `"hook"` | `validate_envelope()` enum: `hook/importer/manual/api` |
| `source.collector` | konstans `"log-event.py"` | MINDEN meglévő test fixture (`_valid_envelope()`, `output/session-ingress-envelope-contract.md` minta-payload-ok) ezt a nevet várja `source.collector`-ban |
| `occurred_at` | `now()` UTC, RFC3339, másodperc-pontosság | hook JSON nem ad provider-side timestamp-et; a schema dokumentált fallback-ja: "provider-side timestamp if available, else collector observation time" (`session-ingress-envelope.schema.yaml` sor 127–129) |
| `ingested_at` | `now()` UTC, RFC3339, másodperc-pontosság, az `occurred_at` UTÁN azonnal számolva | schema sor 131–138 |
| `payload` | a TELJES nyers hook JSON, módosítatlanul | schema sor 143–154: "structurally preserved, not semantically summarized" — ez EXPLICIT a `log-event.py` gap egyik pontja (lásd `session-ingress-envelope-contract.md` sor 79–103: a jelenlegi log-event.py `summarize()`-ja csonkolja a payloadot, ez a job-nak EZT kellett javítania) |
| `payload_encoding` | konstans `"json"` | a hook JSON mindig parse-olható JSON |
| `raw_payload_hash` | `"sha256:" + sha256(json.dumps(payload, sort_keys=True))` | determinisztikus serializáció a stabil hash-hez |
| `trust` | konstans `"session_local"` | CLAUDE.md trust modell: `interpreted: false` ingress/raw szinten, session réteg sosem canonical |
| `canonical` | konstans `False` | schema `const: false` |
| `interpreted` | konstans `False` | schema `const: false` |
| `idempotency_key` | lásd "Decisions Proposed" — KÉSZ, normatív formula | `session-ingress-envelope.schema.yaml` sor 214–247 |
| `workstream` | `os.environ.get("CIC_JOB_ID")` vagy `None` | a MÁR DEPLOYOLT `tools/hooks/log-event.py` ugyanezt a env var-t használja |
| `schema_notes` | `None` | nincs truncation/partial-capture eset, amit jelölni kellene |

### A `log-event.py` gap, amit ez a job zár

`output/session-ingress-envelope-contract.md` (sor 77–116) explicit dokumentálta, hogy a
JELENLEGI `tools/hooks/log-event.py` (MÁS repóban, a `cic-factory`-ban, NEM a `cic-mcp-session`-ben)
három ponton nem felel meg a `SessionIngressEnvelope` igényeinek:
1. `summarize()` csonkolja a payloadot (pl. `cmd[:100]`) — ez a job FULL raw payload-ot ír.
2. Nincs idempotency key — ez a job a schema normatív formuláját alkalmazza.
3. Nem `SessionIngressEnvelope`-ot ír, csak JSONL append-et — ez a job az `insert_envelope()`-et hívja.

Az ÚJ `hooks/log-event.py` (ebben a jobban, a `cic-mcp-session` repóban) ezt a hármat oldja fel,
de MEGTARTJA a régi script egyetlen, helyesnek bizonyult mintáját: "always exits 0, never blocks
the agent" (lásd `tools/hooks/log-event.py` docstring, sor 8: "Always exits 0 — never blocks the
agent").

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| A hook script létezik és `insert_envelope()`-et hívja, nem írja újra | proven | `hooks/log-event.py:303` (`from session_store.envelope_writer import insert_envelope`), `hooks/log-event.py:304` (`insert_envelope(envelope)`) | fájl:sor hivatkozás, kód olvasása | low |
| A hook JSON → envelope mezőleképezés táblázatban dokumentált, forrás-hivatkozással | proven | lásd "Findings" táblázat fent, minden sor forrás-hivatkozással | dokumentum-olvasás | low |
| Determinisztikus `idempotency_key` stratégia indokolva | proven | `output/session-ingress-envelope.schema.yaml` sor 214–247 (a MÁR LÉTEZŐ normatív formula), `hooks/log-event.py` `build_envelope()` ezt implementálja verbatim | kód + schema összevetés | low |
| 4 minta-payload (PreToolUse/PostToolUse/UserPromptSubmit/Stop) lefutott, SQL-eredmény idézve | proven | lásd alább, teljes `psql` output | tényleges script-futtatás valódi Postgres ellen + SQL `SELECT` | low |
| DB-elérhetetlenség szimuláció: exit code azonos mindkét állapotban | proven | lásd alább: `exit code (container running): 0` és `exit code (container stopped): 0` | `docker stop` + script futtatás + `echo $?` | low |
| Példa `.claude/settings.json` hook-konfiguráció, EXPLICIT "nincs aktiválva" | proven | lásd "Decisions Proposed" → "Példa hook-konfiguráció" szekció | dokumentum jelenléte a riportban | low |
| Teljes meglévő teszt-suite lefuttatva, regresszió-mentes | proven | `59 passed in 31.39s`, lásd alább teljes pytest output | tényleges `pytest` futtatás valódi Postgres ellen | low |
| A script SOHA nem ad blokkoló exit code-ot DB-hiba esetén | proven | ugyanaz az evidence mint a DB-elérhetetlenség sor; mindkét eset exit 0 | tényleges futtatás + exit code idézve | low |
| Valódi, élő Claude Code session ellen tesztelve | rejected | — | — | n/a — ezt a jobot EXPLICIT tiltja az input.md "Nem cél"; csak konstruált minta-payload-okkal tesztelt |

### 1. Postgres + teljes SQL-lánc alkalmazása

```
docker run -d --name session-hook-collector-test -e POSTGRES_PASSWORD=test \
    -e POSTGRES_DB=testdb -p 55433:5432 pgvector/pgvector:pg16
```

`pg_isready` pollozás után mind a 6 SQL fájl alkalmazva, ebben a sorrendben (minden fájl
saját fejkommentje szerint), mindegyik `ON_ERROR_STOP=1`-gyel, hiba nélkül:

```
=== applying output/session-postgres-schema.sql ===
CREATE EXTENSION / CREATE EXTENSION / CREATE EXTENSION / CREATE SCHEMA / CREATE TYPE (x3) /
CREATE TABLE / COMMENT / CREATE INDEX (x4) / CREATE SCHEMA / CREATE TABLE (x5) / CREATE SCHEMA /
CREATE TABLE (x3) / CREATE INDEX (x5) / CREATE SCHEMA / CREATE TYPE / CREATE TABLE / CREATE INDEX /
CREATE FUNCTION / CREATE TRIGGER / CREATE SCHEMA / CREATE FUNCTION (x4)
=== applying output/session-chunk-indexer-migration.sql ===
ALTER TABLE / COMMENT / CREATE FUNCTION / CREATE TRIGGER
=== applying output/session-retrieval-quality-migration.sql ===
CREATE FUNCTION / COMMENT / CREATE FUNCTION / COMMENT
=== applying output/session-vector-search-api-migration.sql ===
CREATE FUNCTION / COMMENT
=== applying output/session-hybrid-search-api-migration.sql ===
CREATE FUNCTION / COMMENT
=== applying output/session-source-refs-api-migration.sql ===
CREATE FUNCTION / COMMENT
```

### 4. Négy minta-payload — tényleges SQL-eredmény

A 4 minta-payload JSON-t (lásd "Decisions Proposed" → "4 minta-payload tartalma") sorban
`hooks/log-event.py` stdin-jére adva, mind a 4 hívás `exit code: 0`-val tért vissza. SQL
ellenőrzés:

```sql
SELECT provider, provider_session_id, provider_event_name, source_kind, source_collector,
       canonical, interpreted, trust, payload_encoding,
       raw_payload_hash, idempotency_key
FROM session_raw.envelopes
ORDER BY id;
```

```
  provider   |     provider_session_id     | provider_event_name | source_kind | source_collector | canonical | interpreted |     trust     | payload_encoding |                            raw_payload_hash                             |                             idempotency_key
-------------+-----------------------------+---------------------+-------------+------------------+-----------+-------------+---------------+------------------+-------------------------------------------------------------------------+-------------------------------------------------------------------------
 claude-code | sess-hook-test-pretool-001  | PreToolUse          | hook        | log-event.py     | f         | f           | session_local | json             | sha256:0fb3a94ed955f48141d370801eda678657908aefea66710ed26916a0005aae20 | sha256:525e288cba9f1fb534b961ec9f80b8a142218e7b8baae796f853295627cec440
 claude-code | sess-hook-test-posttool-002 | PostToolUse         | hook        | log-event.py     | f         | f           | session_local | json             | sha256:c6b7776017c9464952ad5f4b84e5be3326c71fecacbcf600ed25e08ceb05089b | sha256:d8ed39de65c07a48ac6d58a7916a0f34fa2f713ae80694789c260203b8a7119a
 claude-code | sess-hook-test-prompt-003   | UserPromptSubmit    | hook        | log-event.py     | f         | f           | session_local | json             | sha256:bbb3f2073dab9ee881d8304a8a95eee19b43f140faa10bdb53d7e279c9ba032b | sha256:21c0704aaa52eb85f6ac534a6b45529ab408fb0ec33b9d8cea5020d1b3264f0a
 claude-code | sess-hook-test-stop-004     | Stop                | hook        | log-event.py     | f         | f           | session_local | json             | sha256:2ce16bb95db12c58fc689d80e1acf915e57847d8da1b5e267cf9763fc7a2521c | sha256:21b2be8858907a7613f28556067c84b4281e5ffe1a3cc1f119f6dbd16b19bdfe
(4 rows)
```

Mind a 4 sor helyesen kitöltve: `provider="claude-code"`, a helyes `provider_event_name`,
`source_kind="hook"`/`source_collector="log-event.py"`, `canonical=f`/`interpreted=f`,
`trust="session_local"`, valid `sha256:`-prefixű hash-ek. A `PostToolUse` payload tartalom
ellenőrizve:

```sql
SELECT payload FROM session_raw.envelopes WHERE provider_session_id = 'sess-hook-test-posttool-002';
```
```
{"cwd": "/home/user/project", "tool_name": "Write", "session_id": "sess-hook-test-posttool-002",
 "tool_input": {"content": "hello world", "file_path": "/home/user/project/notes.md"},
 "tool_response": {"is_error": false}, "hook_event_name": "PostToolUse",
 "transcript_path": "/home/user/.claude/projects/test/transcript.jsonl"}
```

A `tool_input.file_path` mező JELEN VAN a tárolt payload-ban — ez a downstream
`chunk_indexer.extract_source_refs()` rule 2 (`FILE_PATH_KEYS`, `NESTED_TOOL_INPUT_KEY`) számára
közvetlenül feldolgozható, mert a FULL raw payload-ot tároljuk, nem egy csonkolt subset-et.

### 5. DB-elérhetetlenség szimuláció — tényleges futtatás

```
=== baseline run (container RUNNING) ===
exit code (container running): 0

=== stopping container ===
session-hook-collector-test

=== run with container STOPPED ===
[2026-06-22T06:15:58.857960+00:00] insert_envelope() failed (non-blocking, hook still exits 0):
OperationalError('connection failed: connection to server at "127.0.0.1", port 55433 failed:
Connection refused\n\tIs the server running on that host and accepting TCP/IP connections?')
exit code (container stopped): 0
```

**Mindkét állapotban `exit code = 0`** — a script viselkedése (nem-blokkoló) AZONOS, függetlenül
attól, hogy a Postgres fut vagy nem. A hiba stderr-re ÉS egy lokális fájlba (`hooks/log-event.errors.log`)
is logolva lett, de a hook hívóját (Claude Code-ot) ez NEM blokkolta volna.

### 7. Regresszió-ellenőrzés — teljes meglévő teszt-suite

```
============================= test session starts ==============================
collected 59 items
...
tests/test_session_store/test_envelope_writer.py::test_insert_valid_envelope_persists_row PASSED
tests/test_session_store/test_envelope_writer.py::test_duplicate_idempotency_key_is_noop_not_duplicate PASSED
tests/test_session_store/test_envelope_writer.py::test_canonical_true_is_rejected_before_db_write PASSED
tests/test_session_store/test_envelope_writer.py::test_interpreted_true_is_rejected_before_db_write PASSED
tests/test_session_store/test_envelope_writer.py::test_missing_required_field_is_rejected PASSED
tests/test_session_store/test_envelope_writer.py::test_invalid_source_kind_is_rejected PASSED
... (53 további teszt, mind PASSED — chunk_indexer, hybrid_search, session_api,
     session_source_refs_api, turn_projector, vector_search, worker_loop)
============================= 59 passed in 31.39s ==============================
```

Mind az 59 meglévő teszt PASSED, NULLA regresszió. (A `pytest-cov` "No data was collected"
figyelmeztetés a `tools/` modulokra vonatkozik — ez a `pytest.ini` örökölt `--cov=tools`
beállítása, nincs köze a `session_store`/hook munkámhoz, és NEM teszthiba.)

## Decisions Proposed

### A hook stdin JSON kontraktus forrása

A Claude Code hook stdin JSON pontos mezőit (`session_id`, `transcript_path`, `cwd`,
`hook_event_name`, és esemény-specifikus `tool_name`/`tool_input`/`tool_response`/`prompt`/
`stop_hook_active`) **a saját tudásomból** (Claude Code dokumentáció ismerete a tanítási
korpuszból) derítettem ki, NEM élő dokumentáció-fetchből ebben a jobban — ezt explicit jelzem,
nem találtam ki ad hoc mezőneveket. Ezt megerősíti a MÁR DEPLOYOLT
`/home/sinkog/sync/claude_factory/CIC/workdir/tools/hooks/log-event.py` is, amely ugyanezeket a
mezőneveket (`tool_name`, `tool_input`, `tool_response`, `session_id`, `stop_hook_active`)
használja egy MÁSIK, élesben futó Claude Code hook-implementációban — ez független megerősítés,
nem ennek a jobnak a kitalálása.

### Idempotency key — NEM kitalált, hanem MÁR LÉTEZŐ normatív formula

Az input.md feladata ("3. Determinisztikus idempotency_key") úgy fogalmaz, mintha ezt a jobnak
kellene KIGONDOLNIA. A `output/session-ingress-envelope.schema.yaml` (sor 214–247) olvasása során
kiderült, hogy egy KORÁBBI job (`session-ingress-envelope-contract-001`) MÁR rögzítette a teljes,
normatív formulát:

```
idempotency_key = sha256(
  provider + "\x1f" +
  provider_session_id + "\x1f" +
  (provider_event_name or "") + "\x1f" +
  occurred_at + "\x1f" +
  raw_payload_hash
)
```

Ezt a formulát a script VERBATIM implementálja (`hooks/log-event.py` `build_envelope()`,
`unit_sep.join([...])` + `sha256_hex`), NEM egy új formulát talált ki. Trade-off (a schema saját
indoklása szerint, sor 235–243): `provider + provider_session_id` egy provider-session párra
skóp-olja a kulcsot (két provider újrahasználhatja ugyanazt a session-id stringet ütközés
nélkül); `provider_event_name` megkülönbözteti az azonos időbélyegű, de eltérő típusú
eseményeket; `occurred_at` megkülönbözteti a valóban megismételt akciókat; `raw_payload_hash` a
végső tie-breaker, hogy két eltérő payload AZONOS másodpercben se ütközzön — viszont AZONOS
provider-esemény ismételt küldése (retry, eltérő `event_id`/`ingested_at`-tal) MINDIG azonos
kulcsot ad, mert `event_id` és `ingested_at` SZÁNDÉKOSAN ki van zárva a hash-ből.

**Álpozitív/álnegatív kockázat**: ha a hook-runtime két KÜLÖNBÖZŐ valós eseményt ugyanabban a
másodpercben (occurred_at granularitás = másodperc) AZONOS `provider_event_name`-mel és AZONOS
payload-tartalommal (→ azonos `raw_payload_hash`) küld be, azok HAMISAN ugyanazt az
idempotency_key-t kapnák, és a második `ON CONFLICT DO NOTHING` miatt elveszne (álnegatív
duplikáció-szűrés, ami valójában két ESEMÉNYT egynek néz). Ez a kockázat ELFOGADOTT, mert a
formula NEM ennek a jobnak a döntése — egy korábbi, NORMATÍV job rögzítette, és ennek a jobnak a
feladata `insert_envelope()`-et hívni, NEM a schema-t újraírni (lásd "Nem cél").

### A script neve és helye: `hooks/log-event.py`

A `cic-mcp-session` repóban EZ AZ ELSŐ `hooks/log-event.py` fájl (a repóban korábban NEM
létezett `hooks/` mappa). A név NEM ütközik a `cic-factory`-beli, MÁS REPÓBAN élő
`tools/hooks/log-event.py`-vel — két különböző repo, két különböző célú script, ugyanaz a
fájlnév SZÁNDÉKOSAN, mert minden meglévő test fixture (`_valid_envelope()`,
`session-ingress-envelope-contract.md` minta-payload-ok) a `source.collector` mezőben pontosan
`"log-event.py"` stringet vár — ha más nevet választottam volna, az ELTÉRT volna a már rögzített
konvenciótól anélkül, hogy bármi haszna lenne.

### A `payload` mező tartalma: teljes nyers hook JSON, nem subset

Választás: a TELJES nyers hook JSON-t tárolom `payload`-ként, NEM egy kézzel kiválasztott
részhalmazt. Indoklás: a schema (`session-ingress-envelope.schema.yaml` sor 143–154) explicit
megköveteli, hogy a payload "structurally preserved, not semantically summarized or reduced"
legyen — ez PONTOSAN az a hiba, amit a régi `log-event.py` `summarize()`-ja elkövet (csonkolt
`cmd[:100]`, csak `ok`/`error`/`file` mező). A teljes payload tárolása azt is jelenti, hogy ez a
script NEM kell előre ismerje minden jövőbeli `hook_event_name` mezőszerkezetét — bármilyen új
hook-esemény-típus mezői automatikusan bekerülnek, anélkül hogy a scriptet módosítani kellene.

### Exception-kezelés szerkezete — három különálló try/except blokk

`main()` HÁROM külön try/except blokkra van bontva (stdin olvasás → JSON parse → envelope épités+insert),
NEM egy monolitikus blokkra, hogy minden hibatípus PONTOSAN megnevezhető legyen a log-üzenetben
(pl. "failed to parse hook stdin JSON" vs. "insert_envelope() failed") — ez segíti a jövőbeli
debuggolást anélkül, hogy a non-blocking garanciát gyengítené (MINDEN ág `return 0`-val zár).
Az `insert_envelope` import is a try blokkon BELÜL van (lazy import), hogy egy hiányzó `psycopg`
függőség se tudjon a try/except aktiválása ELŐTT elszállni.

### Példa hook-konfiguráció (NEM aktivált, NEM telepítve)

```jsonc
// .claude/settings.json — EXAMPLE ONLY, NOT INSTALLED, NOT ACTIVATED.
// This snippet is documentation, not a deployed configuration. Installing
// this is explicitly out of scope for session-hook-collector-001 (see
// input.md "Nem cél" / "Forbidden Shortcuts").
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "python3 /path/to/cic-mcp-session/hooks/log-event.py" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "python3 /path/to/cic-mcp-session/hooks/log-event.py" }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /path/to/cic-mcp-session/hooks/log-event.py" }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /path/to/cic-mcp-session/hooks/log-event.py" }
        ]
      }
    ]
  }
}
```

**EXPLICIT NYILATKOZAT: ez a konfiguráció SEHOL nincs aktiválva/telepítve.** Sem a
`cic-mcp-session`, sem a `cic-mcp-factory` repo élő `.claude/settings.json`-jában nem található
meg ez a bekötés — ez kizárólag dokumentációs minta a riportban, ahogy az input.md "6." pontja
megköveteli.

### 4 minta-payload tartalma (a hivatkozott teszteléshez)

```json
// PreToolUse
{"session_id": "sess-hook-test-pretool-001", "transcript_path": "...", "cwd": "/home/user/project",
 "hook_event_name": "PreToolUse", "tool_name": "Bash",
 "tool_input": {"command": "git status", "description": "Show working tree status"}}

// PostToolUse
{"session_id": "sess-hook-test-posttool-002", "transcript_path": "...", "cwd": "/home/user/project",
 "hook_event_name": "PostToolUse", "tool_name": "Write",
 "tool_input": {"file_path": "/home/user/project/notes.md", "content": "hello world"},
 "tool_response": {"is_error": false}}

// UserPromptSubmit
{"session_id": "sess-hook-test-prompt-003", "transcript_path": "...", "cwd": "/home/user/project",
 "hook_event_name": "UserPromptSubmit",
 "prompt": "Please run the test suite and report any failures."}

// Stop
{"session_id": "sess-hook-test-stop-004", "transcript_path": "...", "cwd": "/home/user/project",
 "hook_event_name": "Stop", "stop_hook_active": false}
```

## Rejected / Out Of Scope

- **Valódi, élő Claude Code session elleni tesztelés** — technikailag nem végezhető el ebből a
  jobból (lásd input.md "Nem cél"); minden bizonyíték konstruált minta-payload-okkal készült.
- **`insert_envelope()`/`validate_envelope()` módosítása** — a script KIZÁRÓLAG hívja ezeket,
  nem írja újra a write-path logikát.
- **Teljesítmény-optimalizálás, retry-logika, batch-elés** — input.md "Nem cél", ezt a script nem
  is implementálja (egyetlen envelope, egyetlen `insert_envelope()` hívás per script-futás).
- **Monitoring/alerting integráció** — input.md "Nem cél".
- **A hook TÉNYLEGES aktiválása/telepítése bármilyen éles `.claude/settings.json`-ba** — input.md
  "Nem cél" / "Forbidden Shortcuts"; a "Decisions Proposed" szekció hook-konfiguráció PUSZTÁN
  dokumentáció.
- **Új idempotency_key formula kitalálása** — kiderült, hogy ez MÁR LÉTEZIK normatívan (lásd
  "Decisions Proposed"), ezt a jobot nem terheli ez a feladat, csak az ALKALMAZÁSA.

## Risks

1. **`occurred_at` másodperc-granularitás + retry-window**: ha a Claude Code hook-runtime KÉT
   KÜLÖNBÖZŐ valós eseményt AZONOS másodpercben, AZONOS `provider_event_name`-mel és AZONOS
   payload-tartalommal küld be (ritka, de elképzelhető pl. nagyon gyors, automatizált tool-hívás
   sorozatnál), azok hamisan AZONOS `idempotency_key`-t kapnának, és a második `ON CONFLICT DO
   NOTHING` miatt elveszne. Ez a séma örökölt korlátja (lásd "Decisions Proposed"), nem ennek a
   jobnak az újítása.
2. **`status_after_merge: experimental`** indokolt, mert a script logikája KONSTRUÁLT
   minta-payload-okkal bizonyított, de SOSEM futott valódi, élő Claude Code session ellen.
   `candidate`-hez egy tényleges, élő session általi meghívás szükséges (lásd input.md "Target" →
   "status indoklás").
3. **A `hooks/log-event.py` saját hibalog-fájlja (`log-event.errors.log`) korlátlanul nőhet** —
   ez a job NEM implementál log-rotációt vagy méretkorlátot (input.md "Nem cél":
   teljesítmény-optimalizálás/monitoring). Egy hosszú ideig hibás DB-kapcsolat mellett futó
   session sok hibasort írhat ebbe a fájlba. Jövőbeli job foglalkozhat ezzel, ha szükségessé válik.
4. **A `workstream` mező (`CIC_JOB_ID` env var) csak akkor töltődik ki, ha a script egy
   CIC-job-kontextusban fut** — egy önálló, nem-CIC Claude Code sessionben ez mindig `None` lesz,
   ami VÁRT viselkedés, nem hiba.
5. **A `.job_venv/` lokális venv-et ez a job hozta létre a teszteléshez** — NEM része a
   commitnak (untracked, a `.gitignore` csak `/p_venv/`-et zárja ki explicit, de a
   `.job_venv/` mappa SOSEM lesz `git add`-elve ebben a jobban).

## Definition Of Done Check

- [x] a hook script létrejött, `insert_envelope()`-et hívja, fájl:sor hivatkozással —
      `hooks/log-event.py:303-304`
- [x] a hook JSON → envelope mezőleképezés táblázatban dokumentálva, forrás-hivatkozással —
      lásd "Findings" táblázat
- [x] determinisztikus `idempotency_key` stratégia indokolva — lásd "Decisions Proposed"
      (a MÁR LÉTEZŐ schema-formula alkalmazva, nem újrakitalálva)
- [x] mind a 4 minta-payload (PreToolUse/PostToolUse/UserPromptSubmit/Stop) lefuttatva, tényleges
      SQL-eredmény idézve mindegyikre — lásd Claim-Evidence Matrix "4." sor + Findings
- [x] DB-elérhetetlenség szimuláció lefuttatva, az exit code/viselkedés azonossága bizonyítva
      mindkét állapotban — `exit code (container running): 0` / `exit code (container stopped): 0`
- [x] példa `.claude/settings.json` hook-konfiguráció a riportban, EXPLICIT "nincs aktiválva"
      kijelentéssel — lásd "Decisions Proposed" → "Példa hook-konfiguráció"
- [x] teljes meglévő teszt-suite lefuttatva, regresszió-mentesség bizonyítva — `59 passed in 31.39s`
- [x] claim-evidence tábla kitöltve, nem üres — 9 sor

## Next Jobs

- **Élő session-validáció** (`candidate` státuszhoz): egy tényleges, élő Claude Code session
  bekötése a hook-kal egy ELLENŐRZÖTT, nem-éles tesztkörnyezetben — ez bizonyítaná, hogy a hook
  stdin JSON kontraktus a SAJÁT TUDÁSBÓL levezetett mezőnevekkel valóban egyezik a futásidejű
  valósággal.
- **Log-rotáció/méretkorlát a `log-event.errors.log`-hoz**, ha a Risk #3 valós problémává válik
  gyakorlatban.
- **A régi, `cic-factory`-beli `tools/hooks/log-event.py` migrálása/deprecate-elése** erre az új,
  envelope-alapú mintára — ez egy ÖNÁLLÓ, `cic-factory` target_repo-jú capability-job lenne, nem
  része ennek a jobnak.
