# session-ingress-envelope-contract-001 Output

## Scope

Ez a job a `SessionIngressEnvelope` schema formális definícióját adja a `cic-mcp-session`
repo számára — a kötelező formátumot, amibe MINDEN jövőbeli hook/importer/manual session-payload-ot
csomagolni kell, MIELŐTT bármi session-store-ba (`session_raw.*`) kerülne.

Ez egy DESIGN/CONTRACT job, nem kód-implementáció: nincs futtatható kód, nincs pytest, nincs
Postgres DDL (azt a `session-postgres-storage-design-001` job adja). A "Definition of Done"
schema-tartalom-alapú: minden `proven` állítás a `session-ingress-envelope.schema.yaml`
fájl konkrét, idézett részletére hivatkozik.

Target repo: `cic-mcp-session`. Output path: `output/session-ingress-envelope-contract.md`
(ez a fájl) és `output/session-ingress-envelope.schema.yaml` — az input.md "Required Output
Files" szekciója ezt a két explicit fájlnevet írja elő; az input.md "Target" szekciójának
`docs/contracts/` javaslata opcionális volt ("vagy hasonló, az agent válassza meg") — az
explicit "Required Output Files" lista erősebb kötés, ezért `output/` alá kerül mindkét
target-repo-beli artifact is, konzisztensen a job-slices.yaml `output_files:` mintájával.

## Inputs Read

- `${WORKDIR}/.cic-context/factory-docs/job-slices.yaml` — `session-ingress-envelope-contract-001`
  bejegyzés (phase 1A, acceptance_gates, required_evidence, forbidden_shortcuts) — NORMATÍV forrás.
- `${WORKDIR}/.cic-context/factory-docs/architecture.md` — "Postgres-first elv", "Trust modell",
  "cic-mcp-session" Igen/Nem határ-lista.
- `${WORKDIR}/.cic-context/corpus/normalized/thead-review-2026-06-20.yaml` — `dec-thead-0001`
  (session nem canonical knowledge graph), `dec-thead-0002` (hook nem interpretálhat
  szemantikusan), `rag_implications` (chunk metadata mezők).
- `${WORKDIR}/.cic-context/corpus/normalized/factory-systems-review-2026-06-20.yaml` —
  `fac-0005` (jelenlegi hook-logolás lightweight, summary JSONL), `risk-fac-0004`
  (events.jsonl NEM elég gazdag forrás a jövőbeli envelope-collectorhoz).
- `/home/sinkog/sync/claude_factory/CIC/workdir/tools/hooks/log-event.py` — kalibrációs
  referencia, csak olvasva, NEM módosítva.
- `${WORKDIR}/.cic-context/factory-docs/acceptance-contract.md` — "Universal Output Contract",
  "Artifact Contract", "Session-Specific Contract" szekciók.
- `cic-mcp-session/CLAUDE.md` (target repo klón) — repo szerkezet, jelenlegi `experimental`
  állapot, "Fő határok" és "Trust modell" szekció (ezek szó szerint megegyeznek az
  `architecture.md` "cic-mcp-session" résszel).

### Boot sequence eredménye

- `kb_status`: a cic-graph KB elérhető és betöltött (`chunks.pkl`, `graph_nodes.pkl`,
  `graph_edges.pkl`, `inverted_index.pkl`, `faiss.index`, `bm25.pkl` mind `exists: true`).
- `search_nodes("SessionIngressEnvelope")` → **üres eredmény** (`{"result": []}`).
- `search_nodes("session-ingress")` → **üres eredmény**.
- `search_nodes("trust-domain")` → **üres eredmény**.

Explicit jelzés: a KB-ban jelenleg NINCS node ezekre a fogalmakra — ez a job az ELSŐ formális
artifact, ami ezt a koncepciót létrehozza. Ez megfelel az input.md várt forgatókönyvének
("ha nincs (várható, hogy nincs), ezt explicit jelezd").

## Findings

1. A `cic-mcp-session` repo jelenleg `experimental` állapotban van, nincs még session-specifikus
   implementáció — a `make_source.py`/`mcp-server/` egy generikus FastMCP `base-repo` template
   öröksége, `source/` mappa üres (`.gitkeep` van benne). Nincs `output/` vagy `docs/contracts/`
   mappa a klónban — ezt a job hozza létre.
2. A repo `CLAUDE.md`-je már most szó szerint idézi az `architecture.md` "Fő határok" és
   "Trust modell" szekcióit — ez konzisztens a jelen jobnak átadott forrásokkal, nincs eltérés.
3. `log-event.py` egy lightweight, NEM blokkoló hook-logger: `PostToolUse`/`PostToolUseFailure`/
   `Stop` eseményeket fogad stdin-en, és egy `summarize()` függvénnyel **lecsökkenti** az
   eseményt egy pár kulcsos summary dict-té — pl. Bash esetén csak `cmd` (truncated 100 karakterre),
   Write/Edit/Read esetén csak `file_path`, és egy `ok`/`error` boolean. A teljes `tool_input`,
   `tool_response` objektum NEM kerül megőrzésre. Ez pontosan az a hiányosság, amit
   `risk-fac-0004` és `fac-0005` jelez: "Current hook logging is intentionally lightweight and
   non-blocking... discards most raw payload detail" — ezért a `SessionIngressEnvelope.payload`
   mezőt szándékosan RAW, NEM summary-szintű payload-ra terveztem (lásd "log-event.py gap"
   szekció lent).
4. A schema-nak a trust-mezőket NEM szabad szabad string-ként kezelnie — az
   `acceptance-contract.md` "Forbidden Shortcuts" listája explicit kizárja ezt
   ("a mező neve `trust` és van benne valami szöveg" ≠ kikényszerített trust modell). A
   `session-ingress-envelope.schema.yaml`-ban ezért `trust` egy JSON Schema `enum`
   (`["session_local", "session_derived"]`), `canonical` és `interpreted` pedig `const: false`
   — nem dokumentáció, hanem schema-szintű kikényszerítés.

## log-event.py gap (explicit összevetés)

A jelenlegi `log-event.py` (sorok 23-46, `summarize()` függvény):

```python
def summarize(data: dict, event: str) -> dict:
    tool = data.get("tool_name", "")
    record: dict = {"ts": ts(), "event": event, "tool": tool}

    if event == "PostToolUse":
        inp = data.get("tool_input", {})
        if tool == "Bash":
            record["cmd"] = (inp.get("command", "") or "")[:100]
        elif tool in ("Write", "Edit", "Read"):
            record["file"] = inp.get("file_path", "")
        resp = data.get("tool_response", {})
        record["ok"] = not resp.get("is_error", False)
    ...
```

Mi hiányzik a `SessionIngressEnvelope`-hoz képest:

- **nincs raw payload preservation**: a teljes `tool_input`/`tool_response` JSON elveszik,
  csak egy 100 karakteres `cmd` substring vagy egy `file_path` marad — a
  `SessionIngressEnvelope.payload` mező ezzel szemben a teljes raw payloadot követeli meg,
  `raw_payload_hash` integritás-bizonyítékkal.
- **nincs idempotency key**: a `log-event.py` minden hívásnál csak appendel egy JSONL sort,
  nincs dedup-mechanizmus — a schema `idempotency_key` mezője ezt explicit pótolja.
- **nincs provider/trust/canonical/interpreted mező**: a JSONL sor csak `ts`/`event`/`tool`
  mezőket tartalmaz, nincs trust-domain jelölés, nincs explicit `interpreted: false` garancia
  (bár a jelenlegi summarize() implicit módon sosem interpretál szemantikusan, ezt a schema
  most explicit kikényszeríti, nem csak véletlenül igaz tulajdonságként hagyja).
- **mi marad jó mintaként**: a `log-event.py` "always exits 0, never blocks the agent"
  elve (docstring 9. sor) és a no-op silent-fail viselkedés (env var hiány esetén) — ezt a
  tulajdonságot a jövőbeli envelope-collector job-nak (`session-hook-collector-001`) meg kell
  őriznie, csak gazdagabb payload-dal, ahogy `fac-0005` impact mezője is írja: "A future
  SessionIngressEnvelope collector should preserve this non-blocking property but capture
  richer raw envelopes."

Ez a job NEM módosítja a `log-event.py`-t (lásd "Nem cél") — ez csak a gap dokumentálása a
jövőbeli `session-hook-collector-001` job számára.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Schema tartalmazza `apiVersion` és `kind` mezőt | proven | `schema.yaml`: `apiVersion: { type: string, const: "cic.session/v1" }`, `kind: { type: string, const: "SessionIngressEnvelope" }` | Schema fájl direkt idézése | low |
| Schema tartalmazza event identity mezőt | proven | `schema.yaml`: `event_id: { type: string, format: uuid }` | Schema fájl direkt idézése | low |
| Schema tartalmazza provider identity mezőket | proven | `schema.yaml`: `provider` (required, minLength 1), `provider_session_id` (required, minLength 1) | Schema fájl direkt idézése | low |
| Schema tartalmazza `source` mezőt | proven | `schema.yaml`: `source: { type: object, required: [kind, collector], properties: { kind: { enum: [hook, importer, manual, api] } } }` | Schema fájl direkt idézése | low |
| Schema tartalmazza `payload` mezőt, raw (nem interpretált) | proven | `schema.yaml`: `payload: { type: object, description: "stored AS-IS... MUST NOT be discarded or replaced by a derived summary" }` | Schema fájl direkt idézése | medium — a "raw, AS-IS" garancia jelenleg csak schema-szintű leírás, nincs implementáció ami ezt validálná valódi adaton |
| Schema tartalmazza trust mezőt enum-kikényszerítéssel | proven | `schema.yaml`: `trust: { type: string, enum: ["session_local", "session_derived"] }` | Schema fájl direkt idézése (JSON Schema enum, nem szabad string) | low |
| Schema tartalmazza raw preservation mezőt | proven | `schema.yaml`: `raw_payload_hash: { type: string, pattern: "^sha256:[a-f0-9]{64}$" }` | Schema fájl direkt idézése | low |
| `interpreted: false` ingress szinten KIKÉNYSZERÍTVE, nem csak dokumentálva | proven | `schema.yaml`: `interpreted: { type: boolean, const: false }` — JSON Schema `const`, sémaszinten lehetetlen `true`-ra állítani | Schema fájl direkt idézése (const, nem default) | low |
| `canonical: true` + ingress envelope kombináció TILOS és kikényszerítve | proven | `schema.yaml`: `canonical: { type: boolean, const: false }` + `forbidden_combinations[0]` (`forbidden-canonical-true`, enforcement: "JSON Schema const:false") | Schema fájl direkt idézése (const + explicit forbidden_combinations blokk) | low |
| `interpreted: true` ingress szinten TILOS és kikényszerítve | proven | `schema.yaml`: `forbidden_combinations[1]` (`forbidden-interpreted-true`, rule: "interpreted == true", enforcement: "JSON Schema const:false") | Schema fájl direkt idézése | low |
| Idempotency key konkrét mezőlistával definiálva | proven | `schema.yaml` `idempotency_key.description`: `sha256(provider + "\x1f" + provider_session_id + "\x1f" + (provider_event_name or "") + "\x1f" + occurred_at + "\x1f" + raw_payload_hash)` | Schema fájl direkt idézése (konkrét field-order + separator + hash algoritmus) | medium — nincs implementáció ami ezt valódi adaton futtatná, csak a tervezett algoritmus |
| Legalább 2 valid + 2 invalid példa megadva | proven | Lásd "Példák" szekció lent — 2 valid, 3 invalid (eggyel több mint a minimum) | Vizuális ellenőrzés a reportban | low |
| log-event.py gap explicit jelölve | proven | "log-event.py gap" szekció: konkrét idézett kódsorok + 3 hiányzó tulajdonság felsorolva | Report-szekció direkt idézése | low |
| Schema implementáció ellen validálva valódi adaton | missing | Nincs ingest kód, nincs Postgres tábla, nincs validátor futtatás | N/A — ez a `status_after_merge: experimental` indoklása | high — ez a fő limitáció, lásd "Risks" |
| KB-ban létezik `SessionIngressEnvelope`/`trust-domain` node | missing | `search_nodes` mindhárom query-re üres `{"result": []}` | MCP `search_nodes` hívás kimenete | low — várt állapot, ez az ELSŐ ilyen artifact |

## Decisions Proposed

1. **`apiVersion: cic.session/v1` mint fix const.** Ez teszi lehetővé, hogy jövőbeli
   schema-verziók (`v2`) explicit migrációként legyenek kezelve, ne csendes mező-driftként.
2. **`canonical` és `interpreted` mint JSON Schema `const: false`, nem `default: false`.**
   A `default` csak hiányzó mező esetén tölt ki értéket, de megengedné a `true` explicit
   beállítását — a `const` sémaszinten kizárja. Ez direkt válasz az
   "acceptance-contract.md" Forbidden Shortcuts pontjára ("trust mező neve + szöveg" ≠
   kikényszerített modell).
3. **`idempotency_key` ne tartalmazza `event_id`-t és `ingested_at`-ot.** Indoklás a
   schema description-ben: ezek szándékosan változhatnak retry/redelivery esetén, így ha
   bekerülnének a hash-be, két ismétlés különböző kulcsot kapna — ez aláásná a dedup célt.
4. **`source` objektum, nem egyetlen string mező.** Ez lehetővé teszi, hogy egy hook és egy
   manual bejegyzés megosztozzon egy `provider`-en, de strukturálisan megkülönböztethető
   legyen az ingress útja (`source.kind` enum) — ez direkt válasz a job-slices.yaml
   "source (honnan jött: hook/importer/manual stb.)" acceptance gate-re.
5. **`payload_encoding` enum hozzáadása** (`json`/`text`/`base64`) — nem volt explicit
   kötelező mező a job-slices.yaml-ban, de a "raw preservation" garancia (payload AS-IS)
   megköveteli, hogy a binary/unstructured providerek payload-ja is veszteség nélkül
   tárolható legyen; ez egy minimális, indokolt bővítés a kötelező mezőkön felül.

## Rejected / Out Of Scope

- **Postgres DDL** (`session_raw.*` tábla definíció) — külön job:
  `session-postgres-storage-design-001` (job-slices.yaml phase 3).
- **`log-event.py` átírása/kiterjesztése** — külön job: `session-hook-collector-001`
  (ha létrejön a job-slices.yaml-ban, vagy a `factory-systems-review` `recommended_next_jobs`
  alapján egy új job).
- **Importer implementáció** (ChatGPT `conversations.json` → envelope) — külön job:
  `historical-chatgpt-export-importer-001` (job-slices.yaml phase 5, prerequisitek között
  szerepel ez a job).
- **MCP tool/API definíció session olvasásra** — explicit "Nem cél" pont, ez egy következő
  réteg (`session_api.*` funkciók, `gateway-session-adapter-contract-001` job).
- **`event_id` mint idempotency forrás** — megfontolva, de elvetve, mert a hook minden
  retry-nál új `event_id`-t generálhat; a `provider`+`provider_session_id`+
  `provider_event_name`+`occurred_at`+`raw_payload_hash` kombináció determinisztikusabb.

## Risks

1. **Nincs implementáció, ami a schema-t valódi adaton validálná.** Ez a fő ok, amiért
   `status_after_merge: experimental`, nem `candidate` (lásd input.md "Target" szekció
   "status indoklás" — ehhez legalább egy működő importer/hook-collector job kellene).
2. **`raw_payload_hash` algoritmusa (SHA-256 + "sha256:" prefix) nincs még tesztelve
   tényleges canonicalizálási sorrenddel** (pl. JSON key-order normalizálás) — a schema
   leírja a formátumot, de a pontos canonicalizálási lépéseket (pl. `json.dumps(...,
   sort_keys=True)`) egy implementációs jobnak kell rögzítenie.
3. **A `payload` mező `type: object` túl megengedő lehet bináris/szöveges providerek
   esetén** — a `payload_encoding` enum csökkenti ezt a kockázatot, de a tényleges
   `raw_text`/`raw_base64` wrapper-struktúra még nincs JSON Schema `oneOf`-fal véglegesítve,
   ez egy implementációs jobnak finomítandó.
4. **A `workstream` mező opcionális és nincs format-validálva** — ha ez később
   `session_jobs.*` outbox kapcsoláshoz kell, lehet hogy szigorúbb formátum
   (pl. CIC job_id regex) kell rá egy következő iterációban.

## Definition Of Done Check

| DoD pont | Státusz | Megjegyzés |
|---|---|---|
| schema tartalmazza mind a kötelező mezőt (apiVersion, kind, event identity, provider identity, source, payload, trust, raw preservation), idézve a reportban | PASS | lásd Claim-Evidence Matrix, sorok 1-7 |
| schema validációs szabályai EXPLICIT kizárják `canonical: true` + ingress envelope kombinációt és `interpreted: true` ingress szinten, idézve a konkrét szabály-szöveget | PASS | `forbidden_combinations[0]` és `[1]`, idézve Claim-Evidence Matrix sorok 8-9 |
| idempotency key felépítése definiálva, konkrét mezőlistával | PASS | `idempotency_key.description`, lásd Claim-Evidence Matrix sor 10 |
| legalább 2 valid + 2 invalid envelope példa, invalid példáknál megnevezett konkrét sértett szabály | PASS | lásd "Példák" szekció: 2 valid + 3 invalid |
| claim-evidence tábla kitöltve, nem üres | PASS | 14 sor, lásd fent |
| explicit jelzett: `log-event.py` mintával összevetve mi hiányzik/bővül | PASS | lásd "log-event.py gap" szekció, konkrét kódsorok idézve |

## Példák

### VALID #1 — hook eredetű envelope (Claude Code PostToolUse, Bash tool)

```yaml
apiVersion: cic.session/v1
kind: SessionIngressEnvelope
event_id: "5e9f3c2a-7d41-4b8e-9a3f-1c2d3e4f5061"
provider: "claude-code"
provider_session_id: "sess-8f2a1b3c"
provider_event_name: "PostToolUse"
source:
  kind: "hook"
  collector: "log-event.py@v2-envelope"
occurred_at: "2026-06-20T20:31:05Z"
ingested_at: "2026-06-20T20:31:05Z"
payload:
  tool_name: "Bash"
  tool_input:
    command: "git status"
    description: "Show working tree status"
  tool_response:
    is_error: false
    stdout: "On branch main\nnothing to commit, working tree clean\n"
payload_encoding: "json"
raw_payload_hash: "sha256:3b1f9a2c4e6d8f0a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f506172"
trust: "session_local"
canonical: false
interpreted: false
idempotency_key: "sha256:7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5061728394a5b6c7d8e9f0a1b2c"
workstream: "session-ingress-envelope-contract-001"
```

Miért VALID: minden kötelező mező jelen van, `trust` az engedélyezett enum-ból, `canonical`
és `interpreted` mindkettő `false`, `source.kind` az engedélyezett enum-ból (`hook`),
`payload` a teljes raw tool_input/tool_response struktúrát megőrzi (nem csak egy truncated
`cmd` mezőt, szemben a jelenlegi `log-event.py`-jal).

### VALID #2 — manual entry (operator által rögzített session megjegyzés)

```yaml
apiVersion: cic.session/v1
kind: SessionIngressEnvelope
event_id: "a1b2c3d4-e5f6-4789-90ab-cdef01234567"
provider: "manual"
provider_session_id: "manual-2026-06-20-001"
source:
  kind: "manual"
  collector: "operator"
occurred_at: "2026-06-20T19:00:00Z"
ingested_at: "2026-06-20T19:05:00Z"
payload:
  raw_text: "Operator note: rerun session-postgres-storage-design-001 after this contract merges."
payload_encoding: "text"
raw_payload_hash: "sha256:9f8e7d6c5b4a39281706f5e4d3c2b1a0918273645f6e7d8c9b0a1f2e3d4c5b6"
trust: "session_local"
canonical: false
interpreted: false
idempotency_key: "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcd"
```

Miért VALID: `provider_event_name` opcionális, hiányozhat (nincs natív event név egy manuális
bejegyzésnél); `source.kind: manual` megengedett enum érték; `payload` itt egy `raw_text`
wrapper, mert a forrás nem strukturált JSON — ez a `payload_encoding: text` esettel
konzisztens.

### INVALID #1 — `canonical: true` ingress szinten

```yaml
apiVersion: cic.session/v1
kind: SessionIngressEnvelope
event_id: "b2c3d4e5-f607-4891-a2b3-c4d5e6f70819"
provider: "claude-code"
provider_session_id: "sess-1111"
source: { kind: "hook", collector: "log-event.py@v2-envelope" }
occurred_at: "2026-06-20T20:00:00Z"
ingested_at: "2026-06-20T20:00:00Z"
payload: { tool_name: "Read" }
payload_encoding: "json"
raw_payload_hash: "sha256:0000000000000000000000000000000000000000000000000000000000000a"
trust: "session_local"
canonical: true        # <-- SÉRTÉS
interpreted: false
idempotency_key: "sha256:0000000000000000000000000000000000000000000000000000000000000b"
```

Melyik szabályt sérti: pontosan a `forbidden-canonical-true` szabályt
(`schema.yaml forbidden_combinations[0]`, rule: `"canonical == true"`). A `canonical` mező
JSON Schema `const: false` — `canonical: true` sémaszinten elutasítandó, NEM azért hogy
"`canonical: true` + ingress envelope" kombináció külön külön szabály lenne, hanem mert
`canonical: true` ÖNMAGÁBAN tilos session ingress szinten (lásd input.md "Tiltott
kombinációk" pontos megfogalmazása). Ez a 159-160. sor megfogalmazását követi pontosan:
"`canonical: true` megengedett session ingress szinten — TILOS".

### INVALID #2 — `interpreted: true` ingress szinten

```yaml
apiVersion: cic.session/v1
kind: SessionIngressEnvelope
event_id: "c3d4e5f6-0719-4a02-b3c4-d5e6f7081930"
provider: "claude-code"
provider_session_id: "sess-2222"
source: { kind: "hook", collector: "log-event.py@v2-envelope" }
occurred_at: "2026-06-20T20:10:00Z"
ingested_at: "2026-06-20T20:10:00Z"
payload: { tool_name: "Edit", decision: "approved by reviewer" }
payload_encoding: "json"
raw_payload_hash: "sha256:1111111111111111111111111111111111111111111111111111111111111c"
trust: "session_local"
canonical: false
interpreted: true       # <-- SÉRTÉS
idempotency_key: "sha256:2222222222222222222222222222222222222222222222222222222222222d"
```

Melyik szabályt sérti: a `forbidden-interpreted-true` szabályt (`schema.yaml
forbidden_combinations[1]`, rule: `"interpreted == true"`). A `interpreted` mező
`const: false` — ez direkt megsérti `dec-thead-0002`-t ("the hook should not semantically
interpret"). Megjegyzés: a `payload.decision` mező tartalma maga is gyanús (egy
"approved by reviewer" szemantikus állítás a raw payload-ba csomagolva) — ez NEM a schema
hibája (a payload object szabadon tartalmazhat bármilyen raw provider adatot), hanem az
`interpreted: true` flag állítása az, ami formálisan tiltott; a payload tartalmi
"gyanússága" külön, nem ezen a mezőn keresztül kezelt kérdés (downstream worker felelőssége
eldönteni, mit csinál egy ilyen payload-dal — ingress szinten csak a flag-konzisztencia
ellenőrizhető).

### INVALID #3 — `trust` mező nem az engedélyezett enum-ból

```yaml
apiVersion: cic.session/v1
kind: SessionIngressEnvelope
event_id: "d4e5f607-1930-4b13-c4d5-e6f708193041"
provider: "claude-code"
provider_session_id: "sess-3333"
source: { kind: "hook", collector: "log-event.py@v2-envelope" }
occurred_at: "2026-06-20T20:15:00Z"
ingested_at: "2026-06-20T20:15:00Z"
payload: { tool_name: "Write" }
payload_encoding: "json"
raw_payload_hash: "sha256:3333333333333333333333333333333333333333333333333333333333333e"
trust: "reviewed"       # <-- SÉRTÉS
canonical: false
interpreted: false
idempotency_key: "sha256:4444444444444444444444444444444444444444444444444444444444444f"
```

Melyik szabályt sérti: a `trust` mező JSON Schema `enum: ["session_local",
"session_derived"]`-jét — `"reviewed"` a knowledge-layer trust vocabulary-jából származik
(architecture.md "Trust modell": `knowledge.trust: reviewed/canonical`), nem a session
layerből. Ez direkt az `acceptance-contract.md` Forbidden Shortcuts pontját demonstrálja:
egy szabad string mező megengedné ezt, az enum-kikényszerítés ("Forbidden Shortcuts" 4.
pont) viszont elutasítja.

## Next Jobs

1. `session-postgres-storage-design-001` (phase 3) — `session_raw.*` DDL, ami a
   `SessionIngressEnvelope`-ot tárolja, `idempotency_key` UNIQUE constraint-tel.
2. `session-hook-collector-001` (javasolt, ha bekerül a job-slices.yaml-ba) — a `log-event.py`
   mintáját kiterjesztő, non-blocking envelope-collector, ami valódi `SessionIngressEnvelope`
   objektumokat ír (nem summary JSONL-t).
3. `historical-chatgpt-export-importer-001` (phase 5, prerequisite: ez a job + a postgres
   storage job) — `conversations.json` → `SessionIngressEnvelope` mapping.
4. Egy jövőbeli job, ami a `cic-graph` KB-ba node-okat hoz be ehhez a schemahoz (jelenleg
   `search_nodes` üres eredményt ad `SessionIngressEnvelope`/`trust-domain`-re — ez a job
   maga az első forrás, amiből egy ilyen node generálható lenne, de a KB frissítés nem
   ennek a jobnak a feladata).
