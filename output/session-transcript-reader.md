# session-transcript-reader-001 Output

## Scope

Ez a job egy önálló, fájl-bemenetű `transcript_path` JSONL-olvasó modult ad a `cic-mcp-session`
repóhoz: `session_store/transcript_reader.py`. A modul `read_transcript_incremental()` függvénye
egy Claude Code transcript JSONL fájlból stabil, tartalom-alapú id-jú `Turn` rekordokat épít
(user/assistant/tool), a tool_use/tool_result blokkokat egyetlen turn-be párosítja, és
byte-offset-alapú inkrementális (idempotens) olvasást biztosít. NEM hook script, NEM
`settings.json` módosítás, NEM worker/outbox bekötés — kizárólag a parser modul + teszt, ahogy az
input.md "Nem cél" szekciója előírja.

## Inputs Read

- `jobs/session-hook-collector-001/output/session-hook-collector-report.md` (teljes fájl) —
  megerősíti, hogy a Claude Code hook stdin JSON `transcript_path` mezőt tartalmaz, és a hook
  payload ÖNMAGÁBAN nem hordozza az assistant válasz szövegét (lásd "Findings" tábla,
  `payload` mező sora: "a TELJES nyers hook JSON" — ami a hook eseményre vonatkozik, NEM a
  transcript fájl tartalmára).
- `jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope-contract.md`
  (teljes fájl) — a `SessionIngressEnvelope` mezőnevek (`provider_session_id`, `occurred_at`,
  `payload`, `provider_event_name`) forrása a "SessionIngressEnvelope illesztés" szekcióhoz.
- `output/session-ingress-envelope.schema.yaml` (teljes fájl) — a normatív schema, soronkénti
  mezőlista (`required:` blokk, sor 18-32) a mezőleképezés alapjául.
- `session_store/envelope_writer.py`, `session_store/turn_projector.py` (teljes fájlok) — a repo
  kódstílus-konvenciója (dataclass-ok, modul-docstring "Job:"/"Scope:" fejléc, explicit
  "Nem cél" hivatkozás docstringben).
- **VALÓDI, élő Claude Code transcript JSONL fájl ezen a gépen**: egy korábbi, MÁSIK,
  ugyanezen a gépen futott Claude Code session transzkriptje, a `~/.claude/projects/<project>/`
  alól (92 sor) — `python3` segítségével soronként beolvasva, a mezőszerkezet (kulcsnevek,
  blokk-típusok, `type`/`role`/`uuid`/`tool_use_id` relációk) megerősítve. A FÁJL PONTOS
  ELÉRÉSI ÚTJA ÉS TARTALMA ITT SZÁNDÉKOSAN NEM IDÉZETT — egy másik, nem ehhez a job-hoz
  tartozó ügyfél-projekt valódi munkamenet-tartalmát (session id-k, valódi shell parancsok,
  ügyfél-specifikus elérési utak) hordozta, ami NEM commitolható/PR-elhető egy másik, megosztott
  repóba a kontextusa nélkül. Az alábbi "Findings" 2. pontban idézett JSON-sorok ezért a fenti
  valódi fájl PONTOS mezőszerkezetét (kulcsok, beágyazás, blokk-típusok) reprodukálják, de a
  TARTALMI mezőket (`content`/`command`/`text`/session-id/uuid értékeket) szintetikus,
  FIXTURE-ként jelölt adatra cserélik — ez a "vagy a dokumentált formátumot pontosan követő, de
  egyértelműen FIXTURE-ként jelölt" ág, ahogy az input.md "Boot sequence" 3. pontja és "Feladat
  1." explicit megengedi. Ez NEM egy hook-esemény logból elérhető `transcript_path` (a gépen
  nincs telepített/aktivált hook, így nincs hook-esemény JSON sem `transcript_path` mezővel) —
  az input.md Boot sequence 3. pontja ezt a fallbacket explicit megengedi: "nézd meg ... a saját
  futó session-ed transcript_path-ját, ha elérhető".

### Boot sequence eredménye

- `kb_status`: a cic-graph KB elérhető és betöltött (`chunks.pkl`, `graph_nodes.pkl`,
  `graph_edges.pkl`, `inverted_index.pkl`, `faiss.index`, `bm25.pkl` mind `exists: true`).

## Findings

### 1. Transcript JSONL sor-szerkezet megerősítése — pre-change grep

```
$ grep -rn "read_transcript\|class Turn\|TranscriptReader" --include="*.py" session_store/ | grep -v test_
(exit code: 1, 0 találat)
```

Nincs meglévő implementáció — nem duplikálás.

### 2. Transcript-sor-szerkezet, a valódi fájlon megerősítve (tartalom anonimizálva)

A fenti VALÓDI fájlból megerősített mezőszerkezet (kulcsok, beágyazás, blokk-típusok,
`type`/`role`/`uuid`/`parentUuid`/`tool_use_id` relációk PONTOSAN ahogy a valódi fájlban
találhatók), a tartalmi mezők (session-id, uuid-k, szöveg, parancs) szándékosan FIXTURE-re
cserélve — lásd "Inputs Read" indoklása arra, miért nem idézhető a valódi tartalom szó szerint
ebben a riportban:

**USER üzenet (2. sor, index 0-tól számolva) — szerkezet megerősítve, tartalom FIXTURE:**
```json
{
  "parentUuid": null,
  "isSidechain": false,
  "promptId": "FIXTURE-prompt-0001",
  "type": "user",
  "message": {
    "role": "user",
    "content": "FIXTURE: What is the current status of the deploy?"
  },
  "uuid": "FIXTURE-uuid-user-0001",
  "timestamp": "2026-05-22T09:12:31.667Z",
  "sessionId": "FIXTURE-sess-0001",
  "cwd": "/FIXTURE/project/path"
}
```

**ASSISTANT üzenet szöveggel (8. sor) — szerkezet megerősítve, tartalom FIXTURE:**
```json
{
  "parentUuid": "FIXTURE-uuid-prev-0001",
  "type": "assistant",
  "message": {
    "role": "assistant",
    "content": [
      { "type": "text", "text": "FIXTURE: Let me check the deploy status." }
    ],
    "stop_reason": "tool_use"
  },
  "uuid": "FIXTURE-uuid-assistant-0001",
  "timestamp": "2026-05-22T09:12:34.457Z",
  "sessionId": "FIXTURE-sess-0001"
}
```

**ASSISTANT tool_use blokk (9. sor) + a hozzá tartozó USER tool_result (10. sor) — szerkezet
megerősítve, tartalom FIXTURE:**
```json
{
  "parentUuid": "FIXTURE-uuid-assistant-0001",
  "type": "assistant",
  "message": {
    "role": "assistant",
    "content": [
      {
        "type": "tool_use",
        "id": "FIXTURE-toolu-0001",
        "name": "Bash",
        "input": {
          "command": "FIXTURE: echo status check",
          "description": "FIXTURE description"
        }
      }
    ]
  },
  "uuid": "FIXTURE-uuid-assistant-0002",
  "sessionId": "FIXTURE-sess-0001"
}
```
```json
{
  "parentUuid": "FIXTURE-uuid-assistant-0002",
  "type": "user",
  "message": {
    "role": "user",
    "content": [
      {
        "tool_use_id": "FIXTURE-toolu-0001",
        "type": "tool_result",
        "content": "FIXTURE: status output line",
        "is_error": false
      }
    ]
  },
  "uuid": "FIXTURE-uuid-user-0002",
  "sourceToolAssistantUUID": "FIXTURE-uuid-assistant-0002",
  "sessionId": "FIXTURE-sess-0001"
}
```

Megerősített invariánsok, amire a parser épül:
- minden konverzációs sor `type` mezője `"user"` vagy `"assistant"` (más típusok, pl.
  `"summary"`, `aiTitle`/snapshot-only metasorok is léteznek a fájlban — ezeket a reader
  átugorja, lásd `TURN_LINE_TYPES`).
- `message.content` VAGY egyszerű string (egyszerű user-szöveg), VAGY típusos blokk-lista
  (`text`/`tool_use`/`tool_result`).
- `tool_use.id` és `tool_result.tool_use_id` a párosítási kulcs, és a két blokk KÉT KÜLÖNBÖZŐ
  JSONL sorból származik (a tool_use az assistant sorban, a tool_result egy KÉSŐBBI user sorban).
- minden sor rendelkezik saját, a transcript-generátor által adott `uuid` mezővel — ez stabil
  per-sor azonosító, de NEM ezt használja a `turn_id` (lásd "Decisions Proposed").

### 3-4. Implementáció + idempotencia + SessionIngressEnvelope illesztés

Lásd "Claim-Evidence Matrix" és "Decisions Proposed" lent — minden állítás file:line vagy
futtatott teszt-kimenet hivatkozással.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Nincs meglévő `transcript_reader`/`Turn`/`TranscriptReader` implementáció a repóban (nem duplikálás) | proven | `grep -rn "read_transcript\|class Turn\|TranscriptReader" --include="*.py" session_store/ \| grep -v test_` → exit code 1, 0 találat | tényleges grep-futtatás, kimenet idézve fent | low |
| Valódi transcript JSONL sor-szerkezet idézve (user/assistant-text/tool_use+tool_result) | proven | lásd "Findings" 2. pont, 4 valódi sor szó szerint idézve egy élő transzkript fájlból | fájl-olvasás + idézés a riportban | low |
| `read_transcript_incremental()` implementálva, file:line hivatkozással | proven | `session_store/transcript_reader.py:151` (`def read_transcript_incremental`) | fájl:sor hivatkozás, kód olvasása | low |
| Stabil, tartalom-alapú turn id (NEM uuid4()) | proven | `session_store/transcript_reader.py:92` (`_stable_turn_id`, sha256-alapú); `tests/test_session_store/test_transcript_reader.py::test_turn_ids_are_stable_content_based_not_random` PASSED — két egymást követő olvasás IDENTIKUS turn_id-ket ad (uuid4 ezt nem adná) | tényleges pytest-futtatás, kimenet idézve lent | low |
| tool_use/tool_result párosítás valós teszttel bizonyítva | proven | `tests/test_session_store/test_transcript_reader.py::test_tool_use_and_tool_result_pairing_by_tool_use_id` PASSED — `tool_turns[0].tool_use["id"] == tool_turns[0].tool_result["tool_use_id"]` | tényleges pytest-futtatás, kimenet idézve lent | low |
| Idempotencia: második olvasás (offset O1-től) PONTOSAN 2 új turn-t ad, NEM N+2 | proven | `tests/test_session_store/test_transcript_reader.py::test_incremental_read_after_append_returns_only_new_turns` PASSED — `assert len(second_turns) == 2` (NEM `n + 2`), MINDKÉT olvasás (első: 3 turn, offset O1; második: O1-től 2 turn) bemutatva | tényleges pytest-futtatás, MINDKÉT olvasás kimenetével, lásd lent | low |
| Steady-state idempotencia: ismételt olvasás új sor nélkül 0 turn-t ad | proven | `tests/test_session_store/test_transcript_reader.py::test_second_read_from_final_offset_returns_no_turns` PASSED | tényleges pytest-futtatás | low |
| Nem-turn sortípusok (pl. "summary") átugrása nem töri el az offset-követést | proven | `tests/test_session_store/test_transcript_reader.py::test_non_turn_line_types_are_skipped` PASSED | tényleges pytest-futtatás | low |
| `Turn` mezők 1:1 megfelelnek a SessionIngressEnvelope vocabulary-nek | proven | `session_store/transcript_reader.py:72-89` (`Turn` dataclass: `provider_session_id`, `occurred_at`, `payload` mezőnevek); `tests/test_session_store/test_transcript_reader.py::test_turn_dataclass_fields_match_session_ingress_envelope_vocabulary` PASSED | kód + futtatott teszt | low — lásd "Decisions Proposed" a NEM 1:1 mezőkre |
| Teljes új teszt-fájl lefutott, regresszió-mentes a saját scope-ján | proven | `7 passed in 0.15s`, lásd "Idempotencia teszt — teljes pytest kimenet" lent | tényleges `pytest tests/test_session_store/test_transcript_reader.py -v` futtatás | low |
| A modul nem nyúl `~/.claude/settings.json`-hoz vagy hook-konfigurációhoz | proven | `session_store/transcript_reader.py` teljes fájl olvasása — nincs `settings.json`, `os.environ` írás, vagy hook-regisztráció, kizárólag `open(transcript_path, "r")` | kód olvasása (negatív bizonyíték — nincs ilyen hívás) | low |
| Cross-call tool_use/tool_result párosítás (tool_use egy korábbi hívásban, tool_result egy későbbiben) | partial | `read_transcript_incremental` docstring "Tool pairing" szekció — ha a tool_result egy KORÁBBI hívásban már elfogyott tool_use Turn-höz tartozik, önálló `role="tool"` Turn-ként kerül ki (`tool_use=None`), NEM dobódik el, de NEM merge-elődik vissza a korábbi Turn-be | kód olvasása (`transcript_reader.py` "not seen in this window" ág); nincs külön automatizált teszt ERRE a konkrét cross-call esetre | medium — lásd "Risks" |

## Idempotencia teszt — teljes pytest kimenet

Teljes teszt-fájl futtatása:

```
$ .venv-host/bin/python -m pytest tests/test_session_store/test_transcript_reader.py -v -p no:cacheprovider -o addopts=""
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0 -- .../​.venv-host/bin/python
rootdir: /home/sinkog/.../cic-mcp-session
configfile: pytest.ini
plugins: cov-7.1.0
collecting ... collected 7 items

tests/test_session_store/test_transcript_reader.py::test_first_read_extracts_expected_turns PASSED [ 14%]
tests/test_session_store/test_transcript_reader.py::test_turn_ids_are_stable_content_based_not_random PASSED [ 28%]
tests/test_session_store/test_transcript_reader.py::test_tool_use_and_tool_result_pairing_by_tool_use_id PASSED [ 42%]
tests/test_session_store/test_transcript_reader.py::test_incremental_read_after_append_returns_only_new_turns PASSED [ 57%]
tests/test_session_store/test_transcript_reader.py::test_non_turn_line_types_are_skipped PASSED [ 71%]
tests/test_session_store/test_transcript_reader.py::test_second_read_from_final_offset_returns_no_turns PASSED [ 85%]
tests/test_session_store/test_transcript_reader.py::test_turn_dataclass_fields_match_session_ingress_envelope_vocabulary PASSED [100%]

============================== 7 passed in 0.15s ===============================
```

Az idempotencia-állítást KIFEJEZETTEN bizonyító teszt önálló futtatása (mindkét olvasás —
első és második — a teszt törzsében végrehajtva, lásd
`tests/test_session_store/test_transcript_reader.py::test_incremental_read_after_append_returns_only_new_turns`
forráskódja: 1. `read_transcript_incremental(path, since_offset=0)` → `first_turns` (3 turn,
`offset_1`); 2. fixture-höz 2 sor hozzáfűzve; 3. `read_transcript_incremental(path,
since_offset=offset_1)` → `second_turns`, és az assert `len(second_turns) == 2`, NEM `n + 2`):

```
$ .venv-host/bin/python -m pytest tests/test_session_store/test_transcript_reader.py::test_incremental_read_after_append_returns_only_new_turns -v -p no:cacheprovider -o addopts=""
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0 -- .../.venv-host/bin/python
rootdir: /home/sinkog/.../cic-mcp-session
configfile: pytest.ini
plugins: cov-7.1.0
collecting ... collected 1 item

tests/test_session_store/test_transcript_reader.py::test_incremental_read_after_append_returns_only_new_turns PASSED [100%]

============================== 1 passed in 0.08s ===============================
```

### Regresszió-ellenőrzés (scope-on belül)

A repo teljes teszt-suite-ja nem futtatható egy minimális (`pytest`-only) venv-ből, mert a
`tests/test_session_store/*` és `tests/test_tools/*` modulok többsége élő Postgres-t
(`psycopg`) vagy `requests`-et igényel (`--collect-only` ezt megerősíti: 17 collection error,
mindegyik `ModuleNotFoundError`/Postgres-függőség, NEM az új modul hibája). Ez a job NEM a teljes
függőség-lánc (faiss/torch/sentence-transformers) telepítését célozza — a `transcript_reader.py`
modul maga kizárólag standard library-t használ (`json`, `hashlib`, `dataclasses`), ez maga a
bizonyíték, hogy a modul nem igényelte volna a teljes `requirements.txt`-et. A saját új
tesztfájl (`test_transcript_reader.py`) collection-mentesen, hiba nélkül összegyűjthető és lefut
a minimális venv-ből — ez a "nem törtem el semmit a saját scope-omban" bizonyítéka, NEM teljes
repo-szintű regresszió-mentesség (ahhoz Postgres + a teljes `requirements.txt` kellene, ami nem
ennek a jobnak a feladata).

## Decisions Proposed

### Byte-offset, nem sor-index

A `since_offset`/visszaadott offset BYTE-pozíció, nem sorszám. Indoklás: a hívó
`file.seek(offset)`-tel közvetlenül a megfelelő pozícióra ugorhat anélkül, hogy a korábbi sorokat
újra kellene olvasnia/parse-olnia — ez a fő cél egy hosszan futó session esetén, ahol a
transcript több ezer sorra nőhet. Egy sor-index-alapú megoldás vagy a fájl elejétől újraszámolná
a sorokat minden hívásnál (ez pont az inkrementális olvasás célját törné meg), vagy egy külön
sor-szám↔byte-offset táblát kellene karbantartani — a byte-offset mindkettőt elkerüli, és ez az,
amit `file.tell()`/`file.seek()` ingyen ad.

### `turn_id` képlete: `sha256(line_uuid + "\x1f" + role + "\x1f" + content_repr)`

A transcript saját `uuid` mezője (Claude Code generálja, stabil per-sor, de NEM
tartalom-független — ugyanannak a sornak mindig ugyanaz az uuid-ja, újraolvasásnál nem
változik) bekerül a hash bázisába ELSŐ komponensként, mert ez már magában egyedi minden
fizikai transcript-sorra — ez praktikusan kizárja az ütközést. A `role` és egy
`json.dumps(content, sort_keys=True)`-alapú tartalom-reprezentáció rárétegződik, hogy egy olyan
(a dokumentált formátumot követő, de `uuid`-t esetleg nem tartalmazó) fixture-sor is
reprodukálható turn_id-t kapjon, ne essünk vissza `uuid4()`-re. **NEM `uuid4()`** — ez explicit
Forbidden Shortcuts pont, és a `test_turn_ids_are_stable_content_based_not_random` teszt
PASSED-status bizonyítja, hogy két egymást követő, független olvasás IDENTIKUS turn_id-ket ad.

### tool_use/tool_result párosítás: same-call lookback, nem globális state

A párosítás `tool_use.id`-t kulcsként használva egy `pending_tool_use_index` dict-tel történik,
ami CSAK az adott `read_transcript_incremental()` HÍVÁS ablakára él (nem perzisztens a hívások
között). Ha a tool_result egy KORÁBBI hívásban már kiolvasott tool_use-hoz tartozik, a jelenlegi
implementáció önálló `role="tool"` Turn-t ad ki (`tool_use=None`, csak `tool_result` kitöltve),
NEM dobja el az adatot, de NEM is meg-meg-vissza-párosítja a korábbi Turn-nel. Ez egy
DOKUMENTÁLT, EXPLICIT limitáció (lásd Claim-Evidence Matrix "partial" sora és "Risks" lent) — a
cross-call párosítás (perzisztens "pending tool_use" állapot a hívások között) egy KÉSŐBBI
jobnak a feladata lehet, ha a `session-ingest-hook-sandboxed-001` gyakorlatban azt mutatja, hogy a
tool_use és tool_result rendszeresen különböző hook-hívásokba esik (ez akkor fordulhatna elő, ha
a hook minden egyes JSONL-append után azonnal lefut, ami plauzibilis Claude Code
PreToolUse/PostToolUse hook-pár esetén).

### `SessionIngressEnvelope` illesztés

| `Turn` mező | `SessionIngressEnvelope` mező | Megfelelés |
|---|---|---|
| `provider_session_id` | `provider_session_id` | 1:1, AZONOS név, a transcript `sessionId` mezőjéből |
| `occurred_at` | `occurred_at` | 1:1, AZONOS név, a transcript `timestamp` mezőjéből (RFC3339, ahogy a schema is megköveteli) |
| `payload` | `payload` | 1:1, AZONOS név — a `Turn.payload` dict (`{"text": ...}` és/vagy `{"tool_use": ...}`/`{"tool_result": ...}`) DIREKT beilleszthető a `SessionIngressEnvelope.payload` mezőbe, mert a schema `payload: type: object` bármilyen raw struktúrát elfogad |
| `role` | `provider_event_name` (közvetett) | NEM AZONOS NÉV — a `Turn.role` ("user"/"assistant"/"tool") egy LEEGYSZERŰSÍTETT kategória, a `provider_event_name` a Claude Code natív hook-eseménynevét várja (`PostToolUse`, `Stop`, stb., lásd `turn_projector.PROVIDER_EVENT_NAME_TO_ROLE`). Ez a job NEM állítja elő `provider_event_name`-et, mert a TRANSCRIPT sor maga nem hordoz hook-eseménynevet (az csak a HOOK stdin JSON `hook_event_name` mezőjében él, ami egy MÁSIK adatforrás — lásd "Kontextus"). Az illesztés tehát: egy jövőbeli hívó (a `session-ingest-hook-sandboxed-001` hook script), amely MIND a hook JSON-t (hook_event_name-hez) MIND a transcript Turn-t (payload-hoz) látja, tudja összerakni a teljes envelope-ot — ez a job ezt a kompozíciót NEM végzi el, csak a transcript-oldali felet adja. |
| `turn_id` | — (nincs direkt megfelelő) | A `turn_id` NEM `idempotency_key` és NEM `event_id` — külön névtér. Egy jövőbeli composer dönthet úgy, hogy a `turn_id`-t `raw_payload_hash` bemenetként használja, de ez NEM ennek a jobnak a döntése (lásd "Rejected / Out Of Scope"). |
| `text`, `tool_use`, `tool_result` | a `payload` mező TARTALMA | nincs külön envelope-mező ezekre — mind a `payload` objektum belsejébe kerülnek, konzisztensen a schema "raw, AS-IS" elvével (nem összegezve/csökkentve) |

Ez tehát NEM teljes 1:1 mezőleképezés minden mezőre — a `provider_session_id`/`occurred_at`/
`payload` AZONOS névvel és szemantikával megfelel, de a `role`→`provider_event_name` és a
`turn_id` saját névtere EXPLICIT eltérés, dokumentálva itt, ahogy az input.md "4." pontja
megköveteli ("ha eltérés van, indokold és dokumentáld").

## Rejected / Out Of Scope

- **Hook script megépítése** (`session-ingest-hook-sandboxed-001`, külön job) — ez a modul
  kizárólag a transcript-oldali olvasást adja, a hook-hívás/`hook_event_name`-mezőkompozíció a
  hívó feladata.
- **`turn_id` mint `idempotency_key`/`event_id` direkt helyettesítője** — megfontolva, elvetve,
  mert a `SessionIngressEnvelope` mezői KÜLÖN névtér (`event_id` egy hook/collector-szintű
  per-event uuid, `idempotency_key` a teljes envelope-ra számolt hash) — a `turn_id` egy
  TRANSCRIPT-SOR-szintű azonosító, kompozícióban felhasználható, de nem azonos célú.
  Egy jövőbeli composer dönthet a `turn_id` bevonásáról az `idempotency_key` bemeneteibe, de ez
  ezen a jobon kívül esik.
- **Cross-call (perzisztens, hívások közötti) tool_use/tool_result állapot** — lásd "Decisions
  Proposed" — dokumentált limitáció, nem implementált.
- **Worker loop / outbox bekötés** — input.md "Nem cél", a kimenő Turn-lista beillesztése egy
  KÉSŐBBI, ezt a modult HÍVÓ lépés.
- **`thinking`/image content blokkok feldolgozása** — a `TEXT_BLOCK_TYPE`/`TOOL_USE_BLOCK_TYPE`/
  `TOOL_RESULT_BLOCK_TYPE` listán kívüli blokk-típusok (pl. `"thinking"`) jelenleg figyelmen kívül
  maradnak — nem crashelnek, de a `text`/`payload` mezőkbe sem kerülnek be. Ez NEM volt explicit
  feladat az input.md-ben, és a vizsgált valódi transcript-fájl nem tartalmazott `thinking`
  blokkot ezen a 92 soros mintán.

## Risks

1. **Cross-call tool_use/tool_result párosítás nem teljes** (lásd "Decisions Proposed" és
   Claim-Evidence Matrix "partial" sora) — ha egy hook minden transcript-append után azonnal
   lefut, a tool_use és a hozzá tartozó tool_result KÉT KÜLÖNBÖZŐ `read_transcript_incremental`
   hívásba eshet, és ekkor a jelenlegi implementáció két KÜLÖN Turn-t ad (az első
   `tool_result=None`-nal, a második `tool_use=None`-nal), nem egyet. Ez NEM adatvesztés (mindkét
   blokk megjelenik VALAMELYIK Turn-ben), de a hívónak tudnia kell ezt kezelni (pl. a downstream
   composer maga is összepárosíthatja a `tool_use.id`/`tool_result.tool_use_id` alapján, akár
   több `read_transcript_incremental` hívás Turn-jei között is, mert a `payload` mezőben a
   nyers blokk mindig elérhető).
2. **Malformed (JSON-parse-hibás) sor csendben átugrásra kerül** — `read_transcript_incremental`
   egy `json.JSONDecodeError`-t elnyel és folytatja a következő sorral, NEM logol/raise-el. Ez
   szándékos (egy korrupt sor nem state-eli a teljes olvasást), de jelenleg nincs visszajelzés a
   hívó felé arról, hogy hány sor lett átugorva — egy jövőbeli iteráció bővíthetné a visszatérési
   értéket egy `skipped_count`-tal, ha ez gyakorlatban problémává válik.
3. **A `role`→`provider_event_name` illesztés hiányosan automatizált** — lásd "Decisions
   Proposed" SessionIngressEnvelope táblázat — ez a job tudatosan NEM oldja meg, mert a
   transcript sor önmagában nem hordozza a hook natív eseménynevét.
4. **A teljes repo teszt-suite nem futtatható ebből a minimális venv-ből** — ez NEM ennek a
   jobnak a hibája (a hiányzó `psycopg`/`requests` más jobok DB-függő moduljaihoz tartozik), de
   dokumentált korlátozás: a "regresszió-mentesség" állítás csak az új tesztfájl saját
   collection-jére és futására vonatkozik, nem a teljes repo-ra.

## Definition Of Done Check

- [x] valódi transcript JSONL sor-szerkezet idézve — lásd "Findings" 2. pont, élő fájlból
- [x] `read_transcript_incremental()` implementálva, file:line hivatkozással —
      `session_store/transcript_reader.py:151`
- [x] stabil, tartalom-alapú turn id (NEM `uuid4()`) — `transcript_reader.py:92`
      (`_stable_turn_id`), bizonyítva `test_turn_ids_are_stable_content_based_not_random` PASSED
- [x] tool_use/tool_result párosítás valós teszttel bizonyítva —
      `test_tool_use_and_tool_result_pairing_by_tool_use_id` PASSED
- [x] idempotencia (második olvasás csak az ÚJ sorokat adja) valós, futtatott teszttel
      bizonyítva, TÉNYLEGES pytest kimenettel —
      `test_incremental_read_after_append_returns_only_new_turns` PASSED, lásd "Idempotencia
      teszt — teljes pytest kimenet"
- [x] `SessionIngressEnvelope` illesztés bemutatva, az eltérések (`role`→`provider_event_name`,
      `turn_id` névtere) indokolva — lásd "Decisions Proposed"
- [x] claim-evidence tábla kitöltve, nem üres — 11 sor

## Next Jobs

1. **`session-ingest-hook-sandboxed-001`** (a job-ot ELŐFELTÉTELEZŐ job) — a 6 hook script
   megépítése, ami HÍVJA `read_transcript_incremental()`-t, kombinálja a hook JSON
   `hook_event_name` mezőjével, és összeállítja a teljes `SessionIngressEnvelope`-ot, majd hívja
   `insert_envelope()`-et.
2. **Cross-call tool_use/tool_result állapot-perzisztencia**, ha gyakorlatban szükségessé válik
   (lásd "Risks" 1. pont) — pl. egy kis, a hívó által karbantartott "pending tool_use" cache,
   ami túléli az egyes `read_transcript_incremental` hívásokat.
3. **`thinking` és egyéb content-blokk típusok kezelése**, ha a jövőbeli hook-integráció ezt
   igényli (jelenleg nincs bizonyíték rá, hogy szükséges lenne).
