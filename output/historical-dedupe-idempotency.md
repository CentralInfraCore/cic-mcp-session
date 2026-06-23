# historical-dedupe-idempotency-001 Output

## Scope

Ez egy IMPLEMENTÁCIÓS job, nem kontraktus-design. A `historical-chatgpt-export-
importer-001` (mergelve, `status: "done"`) kontraktus-szinten definiálta a
mezőleképezést és LOGIKAILAG levezette a dedupe-formula elégségességét, de a saját
Claim-Evidence Matrix-ában `partial`-ként jelölte ezt az állítást: "NINCS valós,
futtatott teszt két egymást átfedő export-futásra (csak logikai levezetés a schema
garanciáiból)".

Ez a job ezt a hiányt zárja: TÉNYLEGES converter-kódot ír
(`session_store/chatgpt_import.py`, `chatgpt_message_to_envelope()`), amely egy
ChatGPT export `mapping`-node-ot `SessionIngressEnvelope` dict-té alakít, és VALÓS
Postgres ellen futtatott pytest-tel bizonyítja, hogy egy változatlan szintetikus
üzenet újra-konvertálása/újra-beszúrása ugyanazt az `idempotency_key`-t adja, és a
sorok száma a táblában 1 marad. Az `insert_envelope()`/`ON CONFLICT (idempotency_key)
DO NOTHING` generikus mechanizmus (`session-raw-event-store-001`) MÁR létezik és MÁR
tesztelt — ez a job NEM ezt teszteli újra, hanem a converter-specifikus
helyességet: hogy a ChatGPT export-mezőkből SZÁMÍTOTT `idempotency_key` ugyanazt a
kulcsot adja-e egy változatlan üzenet újraimportálásakor.

**KRITIKUS BIZTONSÁGI HATÁR betartva a teljes job alatt**: a teszt-fixture-ben
szereplő `conversation_id` (`"test-conv-0001"`, `"test-conv-9999"`), `node`/
`message` id-k (`"test-node-0001"`, `"test-node-0002"`, `"test-node-0000-root"`) és
üzenet-tartalom (`"hello world test message"`, `"synthetic assistant reply for test
purposes"`) MIND nyilvánvalóan fabrikált, kézzel kitalált értékek. SEMMI nem
származik a `historical-chatgpt-export-importer-001` jobban vizsgált valódi,
személyes export-bundle-ből — abból a jobból KIZÁRÓLAG STRUKTÚRÁT (mező-nevek,
`{id, message, parent, children}` node-alak, `author.role`/`content.content_type`
mező-nevek) vettünk át, a tartalom 100%-ban kitalált.

## Inputs Read

- `jobs/index.yaml` (`cic-mcp-factory`) — prerequisite-ellenőrzéshez, lásd
  "Prerequisite Check".
- `jobs/historical-chatgpt-export-importer-001/output/historical-chatgpt-importer-
  design.md` — TELJES egészében elolvasva. A "conversations-*.json To
  SessionIngressEnvelope Mapping" tábla (139-148. sor) a converter pontos
  specifikációja, 1:1 követve. A "Dedupe/Idempotency Strategy" szekció (158-228.
  sor) a TÉNYLEGES, 5-komponensű `idempotency_key` formulát idézi (nem a job-spec
  rövidített parafrázisát) — ez a job ugyanezt az 5-komponensű formulát használja.
- `jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml`
  — TELJES egészében elolvasva, különös figyelemmel a `idempotency_key` (214-247.
  sor), `raw_payload_hash` (165-173. sor), `occurred_at` (124-129. sor), `payload`
  (143-154. sor), `provider`/`provider_session_id`/`provider_event_name` (66-91.
  sor) mezőkre.
- `cic-mcp-session/session_store/envelope_writer.py` — TELJES egészében elolvasva.
  `insert_envelope()` (165-232. sor), `ON CONFLICT (idempotency_key) DO NOTHING`
  (199. sor), `validate_envelope()` (105-162. sor).
- `cic-mcp-session/tests/test_session_store/test_envelope_writer.py` — TELJES
  egészében elolvasva. `pg_config`/`_clean_envelopes_table`/`_count_rows`
  fixture-ök és helper-ek (47-114. sor) közvetlenül újrahasznosítva (importálva, NEM
  lemásolva) az új teszt-fájlban. `test_duplicate_idempotency_key_is_noop_not_duplicate`
  (148-163. sor) mint minta a generikus mechanizmusra — ezt EZ a job NEM ismétli meg,
  hanem a converter-specifikus helyességet teszteli rá épülve.
- `cic-mcp-session/hooks/log-event.py` — `_sha256_hex()` (249-250. sor),
  `build_envelope()` (253-322. sor), különösen a `raw_payload_hash`/`idempotency_key`
  számítás (275-301. sor) — ez a meglévő, ELLENŐRZÖTT formula-implementáció a
  live-hook collector-ban; nincs importálható segédfüggvény belőle (lásd "Converter
  Implementation" — grep-pel megerősítve), így a formula a converterben
  re-implementálva van, VERBATIM követve a logikát.

## Prerequisite Check

```
$ grep -n '\- id: "historical-chatgpt-export-importer-001"' -A 3 jobs/index.yaml
73:  - id: "historical-chatgpt-export-importer-001"
74-    level: "capability"
75-    status: "done"
76-    target_repo: "cic-mcp-session"

$ grep -n '\- id: "session-raw-event-store-001"' -A 3 jobs/index.yaml
197:  - id: "session-raw-event-store-001"
198-    level: "capability"
199-    status: "done"
200-    parent: "session-postgres-storage-design-001"
```

Mindkét prerequisite `status: "done"`. **Döntés: GO.** A converter a
`historical-chatgpt-export-importer-001` mezőleképezésére, és a `session-raw-event-
store-001`-ben implementált `insert_envelope()`/dedupe-mechanizmusra épülhet.

## Converter Implementation

Először grep-pel megerősítve a `insert_envelope`/`ON CONFLICT` call-chain-jét
(teszt-fájlok kizárva), mint bizonyíték, hogy a dedupe-mechanizmus a converter ALATT
már létezik és nem kell újraírni:

```
$ grep -rn "def insert_envelope\|ON CONFLICT" session_store/envelope_writer.py | grep -v test_
session_store/envelope_writer.py:165:def insert_envelope(
session_store/envelope_writer.py:172:    insert was a no-op due to an idempotency_key collision (ON CONFLICT DO
session_store/envelope_writer.py:199:        ON CONFLICT (idempotency_key) DO NOTHING
```

Megerősítve: `session_store/envelope_writer.py:165` (`insert_envelope`) és `:199`
(`ON CONFLICT (idempotency_key) DO NOTHING`) — a generikus dedupe SQL-szint MÁR
implementálva van.

Ellenőriztük, van-e MÁR importálható segédfüggvény a hash-számításra
`session_store/`-ban (input.md 2. feladat: "HA van már segédfüggvény a repóban a
hash-számításra... HA NINCS, írd meg ezt is, és idézd a `file:line`-t"):

```
$ grep -rln "idempotency_key\s*=" session_store/ --include='*.py' | grep -v test_
(nincs találat)
$ grep -rln "raw_payload_hash\s*=" session_store/ --include='*.py' | grep -v test_
(nincs találat)
```

NINCS importálható segédfüggvény `session_store/`-ban. A formula MÁR létezik
`hooks/log-event.py:249-301`-ben (`_sha256_hex`, `build_envelope`), de az egy
hyphenated-filename CLI-script, nem importálható modulként `session_store/`-ból.
Ezért a formulát megírtam `session_store/chatgpt_import.py`-ban (`_sha256_hex`,
`compute_raw_payload_hash`, `compute_idempotency_key`), VERBATIM követve a
`hooks/log-event.py`-ban már bizonyított logikát (mező-sorrend, ASCII unit
separator `0x1F`, `sort_keys=True` JSON-kanonikalizálás).

A converter implementáció: `session_store/chatgpt_import.py`,
`chatgpt_message_to_envelope(conversation, node, provider_session_id=None) -> dict`
(176-243. sor a fájlban). Mezőleképezés `historical-chatgpt-importer-design.md`
"conversations-*.json To SessionIngressEnvelope Mapping" táblája szerint, 1:1:

| Export mező | `SessionIngressEnvelope` mező | Converter helye |
|---|---|---|
| `conversation["conversation_id"]` / `["id"]` | `provider_session_id` | `chatgpt_import.py:217-220` |
| `node["message"]["create_time"]` (epoch float) | `occurred_at` (RFC3339 UTC, mp-pontosság) | `chatgpt_import.py:222`, `normalize_occurred_at()` (`:141-153`) |
| `node["message"]["author"]["role"]` | `provider_event_name` | `chatgpt_import.py:214-216` |
| TELJES `node["message"]` objektum | `payload` | `chatgpt_import.py:226` (`dict(message)`, NEM csak `content.parts`) |
| (konstans) | `provider` = `"chatgpt-export"` | `chatgpt_import.py:43` (`PROVIDER_CHATGPT_EXPORT`) |
| sha256(kanonikus JSON `payload`) | `raw_payload_hash` | `chatgpt_import.py:228`, `compute_raw_payload_hash()` (`:69-86`) |
| 5-komponensű schema-formula | `idempotency_key` | `chatgpt_import.py:230-236`, `compute_idempotency_key()` (`:89-127`) |

A converter NEM hívja `insert_envelope()`-ot — pure function, dict-et ad vissza
(`chatgpt_import.py:176` szignatúra, docstring 188-191. sor explicit kimondja). A
hívó oldal (a teszt) hívja `insert_envelope()`-ot a `session_store.envelope_writer`-
ből, importálva, NEM duplikálva az insert-logikát.

Egyéb kötelező mezők (schema `required` lista, 18-32. sor) indokolt
konstans/levezetett értékkel:
- `event_id` = fresh `uuid.uuid4()` minden híváskor (schema 53-62. sor: az
  `event_id` NEM az idempotencia alapja, így egy újra-konvertált, ugyanazon
  logikai üzenetre is helyes egy új `event_id`-t generálni).
- `source` = `{"kind": "importer", "collector": "chatgpt-import-converter-v1"}`
  (schema 96-119. sor, `source.kind` enum `["hook", "importer", "manual", "api"]` —
  `"importer"` a helyes érték egy historikus export-konverterre).
- `ingested_at` = a hívás pillanatának UTC ideje (schema 131-138. sor: "When the
  envelope was constructed/wrapped by the collector" — ez a wrap-time, NEM az
  `occurred_at`).
- `trust` = `"session_local"`, `canonical` = `False`, `interpreted` = `False` —
  schema-konstansok, ugyanaz mint a `_valid_envelope()` teszt-mintában és a
  `hooks/log-event.py` producerben.
- `payload_encoding` = `"json"` (schema default).
- `workstream`, `schema_notes` = `None` (opcionális mezők, nincs historikus
  kontextusban értelmes érték).

## Synthetic Test Fixture (No Real Export Content)

`tests/test_session_store/test_chatgpt_import.py` négy fabrikált helper-t definiál:

- `_synthetic_conversation(conversation_id="test-conv-0001")` — csak `conversation_id`/
  `id`/`title` mezőkkel, mind nyilvánvalóan fabrikált placeholder.
- `_synthetic_user_message_node(node_id="test-node-0001", create_time=1700000000.0,
  text="hello world test message")` — `user` role, szintetikus szöveg.
- `_synthetic_assistant_message_node(node_id="test-node-0002", create_time=1700000005.0,
  text="synthetic assistant reply for test purposes")` — `assistant` role, eltérő
  `create_time` és tartalom, a non-collision bizonyításhoz.
- A `{id, message, parent, children}` node-alak és a `message` objektum kulcsai
  (`author`, `content`, `create_time`, `metadata`, stb.) a design report
  STRUKTÚRA-leírását követik (`historical-chatgpt-importer-design.md` "Export
  Bundle Structure" szekció, 100-133. sor) — de minden ÉRTÉK a fenti helper-ekben
  kitalált, kézzel írt teszt-adat.

SEMMI a valódi, személyes ChatGPT export-bundle-ből nem került bemásolásra:
nincs valós `conversation_id`/UUID, nincs valós üzenet-szöveg, nincs valós
`create_time` epoch-érték (a `1700000000.0`/`1700000005.0` kerek, nyilvánvalóan
teszt-célú konstansok).

## Real Postgres Test Run — Dedupe Proof

A meglévő, MÁR FUTÓ `session-raw-event-store-test` container ellen (séma már
felvíve), a `pg_config`/`_clean_envelopes_table`/`_count_rows` meglévő
fixture-mintával (importálva `test_envelope_writer.py`-ból, NEM lemásolva):

```
$ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55432 \
  SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \
  SESSION_STORE_PG_PASSWORD=test \
  .venv-host/bin/python -m pytest tests/test_session_store/test_chatgpt_import.py -v

============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.4.2, pluggy-1.6.0
collected 4 items

tests/test_session_store/test_chatgpt_import.py::test_converter_output_is_insertable PASSED [ 25%]
tests/test_session_store/test_chatgpt_import.py::test_reimporting_unchanged_synthetic_message_does_not_duplicate PASSED [ 50%]
tests/test_session_store/test_chatgpt_import.py::test_different_synthetic_message_gets_separate_row PASSED [ 75%]
tests/test_session_store/test_chatgpt_import.py::test_same_message_content_different_conversation_does_not_collide PASSED [100%]

============================== 4 passed in 0.64s ===============================
```

Regresszió-ellenőrzés (a meglévő `test_envelope_writer.py` is zöld marad mellette):

```
$ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55432 \
  SESSION_STORE_PG_DB=testdb SESSION_STORE_PG_USER=postgres \
  SESSION_STORE_PG_PASSWORD=test \
  .venv-host/bin/python -m pytest tests/test_session_store/test_envelope_writer.py tests/test_session_store/test_chatgpt_import.py -v

collected 10 items
tests/test_session_store/test_envelope_writer.py::test_insert_valid_envelope_persists_row PASSED [ 10%]
tests/test_session_store/test_envelope_writer.py::test_duplicate_idempotency_key_is_noop_not_duplicate PASSED [ 20%]
tests/test_session_store/test_envelope_writer.py::test_canonical_true_is_rejected_before_db_write PASSED [ 30%]
tests/test_session_store/test_envelope_writer.py::test_interpreted_true_is_rejected_before_db_write PASSED [ 40%]
tests/test_session_store/test_envelope_writer.py::test_missing_required_field_is_rejected PASSED [ 50%]
tests/test_session_store/test_envelope_writer.py::test_invalid_source_kind_is_rejected PASSED [ 60%]
tests/test_session_store/test_chatgpt_import.py::test_converter_output_is_insertable PASSED [ 70%]
tests/test_session_store/test_chatgpt_import.py::test_reimporting_unchanged_synthetic_message_does_not_duplicate PASSED [ 80%]
tests/test_session_store/test_chatgpt_import.py::test_different_synthetic_message_gets_separate_row PASSED [ 90%]
tests/test_session_store/test_chatgpt_import.py::test_same_message_content_different_conversation_does_not_collide PASSED [100%]

============================== 10 passed in 1.22s ===============================
```

A dedupe-bizonyíték konkrétan a `test_reimporting_unchanged_synthetic_message_does_not_duplicate`
tesztben: ugyanazt a szintetikus node-ot (új, független dict-példányként, mint egy
második export-futás szimulációja) konvertálva és beszúrva, a sorok száma a
beszúrás ELŐTT és UTÁN is pontosan **1** (`_count_rows(pg_config) == 1` mindkét
assert-nél), és a két konvertált envelope `idempotency_key`-je BIZONYÍTOTTAN
megegyezik (`second_envelope["idempotency_key"] == first_envelope["idempotency_key"]`),
miközben az `event_id` különbözik — pontosan azt a forgatókönyvet bizonyítva, amit
a megelőző job csak logikailag vezetett le.

A determinisztikus hash-értékek (referenciaként, a fixture determinizmusának
illusztrálására — ezek SHA-256 hash-ek, nem tartalom, biztonságosan idézhetők):
egy `_synthetic_conversation()` + `_synthetic_user_message_node()` páros
konvertálva mindig `raw_payload_hash = sha256:6579dbe40f9c7cc0035ec7e3e589b2556f628ad5810bd52e8d3c318af93d1187`
és `idempotency_key = sha256:95e8f97dcd18c5f0ffba3c7bbb7e8f98acb09f111126df413baaae6581139538`
értéket ad.

## Findings

- A `hooks/log-event.py`-ban MÁR létező `idempotency_key`/`raw_payload_hash`
  formula-implementáció (`_sha256_hex` 249-250. sor, `build_envelope` 275-301. sor)
  nem importálható segédfüggvényként `session_store/`-ból (hyphenated filename CLI
  script) — ez a job ezért egy VERBATIM-azonos formula-implementációt írt
  `session_store/chatgpt_import.py`-ban, nem talált fel új logikát.
- A megelőző job (`historical-chatgpt-export-importer-001`) Claim-Evidence
  Matrix-ának "partial" sora ("Dedupe-formula elégséges historikus importnál
  kiegészítés nélkül") most LEZÁRHATÓ `proven`-re — ez a job ad neki tényleges,
  futtatott Postgres-bizonyítékot, lásd a Claim-Evidence Matrix alábbi sorát.
- A `mapping`-node-on belüli `role`/`content_type` mezők variabilitása (a design
  report 7 `content_type` enum-értéket dokumentált) nem befolyásolja a
  dedupe-bizonyítékot — a `payload` egésze (nem csak `content.parts`) megy a
  hash-be, így bármely tartalom-típus ugyanúgy determinisztikus hash-t ad.
- A `mapping` fa-bejárási sorrend (DFS/BFS) explicit NEM ennek a jobnak a
  hatóköre (lásd "Nem cél") — a converter EGY node-ot alakít át, függetlenül a
  bejárási sorrendtől.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Mindkét prerequisite (`historical-chatgpt-export-importer-001`, `session-raw-event-store-001`) `status: "done"` | proven | `jobs/index.yaml:73-76` és `:197-200`, idézve "Prerequisite Check"-ben | grep `-n '- id: "..."' -A 3 jobs/index.yaml`, kimenet idézve | alacsony |
| `insert_envelope()`/`ON CONFLICT (idempotency_key) DO NOTHING` MÁR létezik, nem kell újraírni | proven | `session_store/envelope_writer.py:165` (`insert_envelope`), `:199` (`ON CONFLICT`) | `grep -rn "def insert_envelope\|ON CONFLICT" session_store/envelope_writer.py \| grep -v test_`, kimenet idézve | alacsony |
| Nincs MÁR importálható hash/idempotency_key segédfüggvény `session_store/`-ban | proven | `grep -rln "idempotency_key\s*=\|raw_payload_hash\s*=" session_store/ --include='*.py' \| grep -v test_` → nincs találat | grep, üres kimenet idézve | alacsony |
| Converter a design report mezőleképezését 1:1 követi | proven | `session_store/chatgpt_import.py:176-243` (`chatgpt_message_to_envelope`), `file:line` hivatkozással minden mezőre a "Converter Implementation" táblában | manuális kód-felülvizsgálat, sor-hivatkozással | alacsony |
| Converter NEM hívja `insert_envelope()`-ot direktben | proven | `chatgpt_import.py:176-243` teljes függelékteste, nincs `insert_envelope`/`psycopg`/DB-import a fájlban | `grep -n "insert_envelope\|psycopg" session_store/chatgpt_import.py` → nincs találat | alacsony |
| Szintetikus teszt-fixture, SEMMILYEN valós export-tartalom nélkül | proven | `tests/test_session_store/test_chatgpt_import.py` 4 helper-függvénye, mind fabrikált `"test-conv-..."`/`"test-node-..."` id-kkel és kitalált szöveggel | manuális, kétszeri átolvasás a commitolás előtt (lásd "Definition Of Done Check") | alacsony — emberi/AI hiba kockázata mindig megmarad |
| Változatlan szintetikus üzenet újra-konvertálása ugyanazt az `idempotency_key`-t adja | proven | `test_reimporting_unchanged_synthetic_message_does_not_duplicate`: `second_envelope["idempotency_key"] == first_envelope["idempotency_key"]` assert, miközben `event_id` különbözik | TÉNYLEGES `pytest -v` futás, "Real Postgres Test Run" szekcióban idézve, PASSED | alacsony |
| Re-import után a sorok száma változatlan (dedupe bizonyítva valós Postgres-en) | proven | `_count_rows(pg_config) == 1` MIND a első, MIND a második beszúrás után, ugyanazon tesztben | TÉNYLEGES `pytest -v` futás kimenete, `1 passed`/`4 passed`/`10 passed` (regresszió-ellenőrzéssel) idézve | alacsony |
| Eltérő szintetikus üzenet (más role/create_time/content) NEM ütközik, külön sort kap | proven | `test_different_synthetic_message_gets_separate_row`: `_count_rows(pg_config) == 2`, `first_id != second_id` | TÉNYLEGES `pytest -v` futás, PASSED | alacsony |
| Eltérő `conversation_id`, azonos message-tartalom NEM ütközik | proven | `test_same_message_content_different_conversation_does_not_collide`: `idempotency_key`-ek különböznek, 2 sor | TÉNYLEGES `pytest -v` futás, PASSED | alacsony |
| A generikus `test_duplicate_idempotency_key_is_noop_not_duplicate` teszt NEM lett megismételve új assertion nélkül | proven | `test_chatgpt_import.py` importálja a fixture-öket `test_envelope_writer.py`-ból, de a saját 4 tesztje converter-specifikus assert-eket tartalmaz (`idempotency_key` egyezés/eltérés a converter outputján, nem csak a generikus insert-mechanizmuson) | manuális kód-felülvizsgálat | alacsony |
| SEMMILYEN tényleges beszélgetés-tartalom a VALÓDI export-bundle-ből nem jelenik meg | proven | a teljes kód/teszt/riport kétszeri átolvasása a commitolás előtt (lásd "Definition Of Done Check") | manuális review | alacsony — emberi/AI hiba kockázata mindig megmarad, ezért kötelező volt a kétszeri átolvasás |

## Decisions Proposed

1. **A hash/idempotency_key formula re-implementálása (nem importálása) a
   converterben**, mert `hooks/log-event.py` nem importálható modul `session_store/`-
   ból (hyphenated filename, CLI-script szerkezet). Ha egy jövőbeli job egy közös
   `session_store/idempotency.py` segédmodult akar (DRY a `hooks/log-event.py` és a
   `chatgpt_import.py` formula-duplikációja között), az egy KÜLÖN refaktor-job
   hatóköre — ez a job NEM végzi el azt a refaktort, csak dokumentálja a
   duplikáció tényét.
2. **`source.kind` = `"importer"`** a converter által generált envelope-okon (nem
   `"hook"`, mint a `log-event.py` producernél) — a schema enum-ja (96-107. sor)
   explicit megkülönbözíti a négy ingress-utat, és egy historikus export-konverter
   pontosan az `"importer"` kategóriába esik.
3. **`event_id` mindig friss `uuid4()`** minden converter-híváson, akkor is, ha a
   logikai üzenet ugyanaz — összhangban a schema 53-62. sorának explicit
   indoklásával ("idempotency itself is governed by idempotency_key... because
   event_id is allowed to differ across re-deliveries").

## Rejected / Out Of Scope

- A teljes export-bundle (`conversations-NNN.json` shard-ok) bejárásának/batch-
  feldolgozásának implementálása — explicit "Nem cél" az input.md-ben, későbbi,
  performance-fókuszú jobra hagyva.
- A `mapping` fa-bejárási algoritmus (DFS/BFS sorrend) eldöntése — a converter EGY
  node-ot alakít át, a hívó oldal felelőssége a bejárás (lásd
  `chatgpt_message_to_envelope` docstring `node` paraméter leírása).
- A `SessionIngressEnvelope` schema módosítása — a séma változatlan maradt, csak
  meglévő mezőket/konstansokat (pl. `provider` `"chatgpt-export"` examples-listából)
  használtunk.
- VALÓDI export-bundle bármilyen tartalmának felhasználása teszt-fixture-ként —
  KIZÁRÓLAG fabrikált adat került a tesztbe (lásd "Synthetic Test Fixture").
- A `hooks/log-event.py`/`chatgpt_import.py` formula-duplikáció DRY-refaktorja egy
  közös `session_store/idempotency.py`-ba — dokumentálva mint jövőbeli lehetőség
  (lásd "Decisions Proposed" 1.), de nem ennek a jobnak a hatóköre.

## Risks

- **Formula-duplikáció**: a `idempotency_key`/`raw_payload_hash` számítás most KÉT
  helyen él szó szerint ugyanazzal a logikával (`hooks/log-event.py:249-301` és
  `session_store/chatgpt_import.py:69-127`) — ha a schema formula valaha módosul,
  mindkét helyet frissíteni kell, és nincs egyetlen, egységes forrás-hely. Ez egy
  ismert, dokumentált kockázat (lásd "Decisions Proposed" 1.), nem ennek a jobnak
  a hatóköre megszüntetni.
- **`mapping` fa-bejárás nyitott kérdés marad**: ahogy a megelőző job is jelezte,
  egy jövőbeli batch-importernek el kell döntenie a bejárási sorrendet — ez a job
  csak az EGY-node konverziót bizonyítja, nem a teljes fa kezelését.
- **Epoch-timestamp pontosság**: a `normalize_occurred_at()` `datetime.fromtimestamp`
  hívása feltételezi, hogy a ChatGPT export `create_time` Unix epoch float
  másodpercben van (nem milliszekundumban) — ez összhangban van a megelőző job
  struktúra-vizsgálatával, de NINCS valós export ellen tesztelve ebben a jobban
  (a szintetikus fixture is másodperc-pontosságú epoch-értékeket használ).
- **`payload_encoding` mindig `"json"`**: ha egy jövőbeli ChatGPT export-formátum
  bináris melléklet-tartalmat (pl. `file-*` attachment binárisokat) közvetlenül a
  `message`-objektumba ágyazna (jelenleg nem így van — a mellékletek külön
  top-level fájlok, a megelőző job struktúra-vizsgálata szerint), a `payload_encoding`
  mező felülvizsgálatra szorulhatna.

## Definition Of Done Check

- [x] mindkét prerequisite `id:` kulccsal megerősítve, GO döntés indokolva — lásd
      "Prerequisite Check".
- [x] converter implementáció a `historical-chatgpt-importer-design.md`
      mezőleképezését 1:1 követi, `file:line` hivatkozással — lásd "Converter
      Implementation" tábla.
- [x] szintetikus teszt-fixture, SEMMILYEN valós export-tartalom nélkül — lásd
      "Synthetic Test Fixture (No Real Export Content)".
- [x] valós Postgres teszt PARANCS + KIMENET idézve, mutatva hogy a második
      beszúrás után a sorok száma változatlan (dedupe bizonyítva) — lásd "Real
      Postgres Test Run — Dedupe Proof", `4 passed`/`10 passed` kimenet idézve.
- [x] claim-evidence tábla kitöltve, nem üres (11 sor) — lásd fent.
- [x] SEMMILYEN tényleges beszélgetés-tartalom a VALÓDI export-bundle-ből nem
      jelenik meg sehol — a teljes report, kód, teszt-fixture commitolás előtt
      MÉG EGYSZER átolvasva pontosan ennek ellenőrzésére; egyetlen valós
      `conversation_id`/üzenet-szöveg/UUID sem szerepel, csak nyilvánvalóan
      fabrikált `"test-conv-..."`/`"test-node-..."` placeholder-ek és SHA-256
      hash-ek (amelyek maguk is determinisztikus függvényei a fabrikált
      tartalomnak, nem tartalom-idézetek).

## Next Jobs

- Egy DRY-refaktor job, amely a `hooks/log-event.py` és a
  `session_store/chatgpt_import.py` közötti `idempotency_key`/`raw_payload_hash`
  formula-duplikációt egy közös `session_store/idempotency.py` segédmodulba
  vonja össze — jelenleg dokumentált kockázat, nem implementálva (lásd "Risks").
- Egy jövőbeli `historical-chatgpt-export-importer-implementation-001`-szerű job
  (a `historical-chatgpt-export-importer-001` "Next Jobs"-ában is jelzett), amely
  a `mapping` fa-bejárási sorrendet konkretizálja, a teljes
  `conversations-NNN.json` shard-okat beolvassa, és ezt a converter-t
  (`chatgpt_message_to_envelope`) hívja minden node-ra, batch-insert logikával.
- A `chat.html` (humán-olvasható export) mint backup-corpus feldolgozása — a
  megelőző job explicit jelezte mint KÉSŐBBI, nem ennek a Phase-nek a hatóköre.
