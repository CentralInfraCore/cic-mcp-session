# session-ingest-hook-sandboxed — capability promotion proposal

- **Job:** `session-ingest-hook-sandboxed-001`
- **Capability:** `cic_mcp.session.ingest_hook_sandboxed`
- **Target repo:** `cic-mcp-session`
- **Change type:** `new_capability`
- **Indulási státusz:** `experimental`
- **Branch:** `feature/session-ingest-hook-sandboxed-001`

---

## 0. Egymondatos összefoglaló

6 Claude Code hook script (`UserPromptSubmit`, `PostToolUse`, `PostToolUseFailure`,
`Stop`, `SessionStart`, `SessionEnd`), amelyek a session-eseményeket
`SessionIngressEnvelope` rekordokká alakítják és egy **sandboxolt, eldobható
NDJSON outbox**-ba írják — soha nem a valódi `~/.claude` configba és soha nem a
production Postgres-be —, teljes failure-isolation mellett (minden hook MINDIG
`exit 0`).

---

## 1. Miért kellett az új capability

A session trust-domain írási oldala (`session-raw-event-store-001`,
`session-turn-projector-001`, `session-chunk-indexer-001`) képes
`SessionIngressEnvelope`-ot tárolni és turn-ökké/chunk-okká vetíteni, a
`session-transcript-reader-001` pedig képes egy Claude Code transcript-fájlt
inkrementálisan `Turn`-ökre bontani — **de nem volt olyan komponens, ami a futó
Claude Code session eseményeit egyáltalán beemelte volna** ebbe a pipeline-ba.
A `hooks/log-event.py` egyetlen, általános naplózó hook volt; hiányzott a
per-event, envelope-termelő belépési réteg, ami a `transcript_reader`-t a `Stop`
eseményhez köti.

Ez a capability ezt a hiányt zárja be — **de szándékosan a sandbox-szintnél áll
meg**: nem köti rá a futó usert élesben. A valódi rákötés (production Postgres,
valódi `settings.json`) külön, emberi jóváhagyáshoz kötött lépés, lásd
`session-ingest-hook-go-live-checklist.md`.

---

## 2. Milyen tool / MCP contract jön létre

Nem MCP-tool, hanem **Claude Code hook contract** (stdin/stdout/exit-code):

| Elem | Contract |
|---|---|
| Bemenet | hook stdin JSON (`session_id`, `hook_event_name`, `transcript_path`, `cwd`, …) |
| Kimenet (sink) | append-only NDJSON sor a `CIC_SESSION_INGEST_OUTBOX` által megadott sandbox fájlba |
| Exit code | **MINDIG `0`** — semmilyen hiba nem propagálódik, a user turn-jét sosem blokkolja |
| Stop-specifikus | a `transcript_path`-ot inkrementálisan beolvassa `read_transcript_incremental`-lal, a byte-offsetet sandbox `.offsets/` állományban tárolja |
| Wiring | `hooks/sandboxed-settings.example.json` (placeholder `{{REPO_ROOT}}` / `{{SANDBOX_DIR}}`, soha nem valódi config) |

A közös logika a `hooks/_ingest_sandbox.py`-ban él; a 6 script vékony belépési
pont (kb. 30 sor mind), amely a saját identitásával (`collector_name`) hívja a
`run_hook()`-ot — a `Stop.py` `extract_turns=True`-val.

---

## 3. Output schema

Két envelope-alak kerül az outboxba, mindkettő `kind: SessionIngressEnvelope`,
`apiVersion: cic.session/v1`, a `hooks/log-event.py` mezőleképezését követve:

**Event envelope** (mind a 6 hook):
```
event_id, provider="claude-code", provider_session_id, provider_event_name,
source={kind:"hook", collector:"<Script>.py"}, occurred_at, ingested_at,
payload=<teljes hook stdin JSON>, raw_payload_hash="sha256:…",
idempotency_key="sha256:…"  (provider 0x1F session 0x1F event 0x1F occurred_at 0x1F hash),
trust="session_local", canonical=false, interpreted=false
```

**Turn envelope** (csak `Stop`, transcript-turn-önként):
```
provider_event_name="transcript.turn",
source={kind:"transcript", collector:"Stop.py"},
payload={turn_id, role, text, tool_use, tool_result, turn_payload},
idempotency_key = sha256(provider 0x1F session 0x1F "transcript.turn" 0x1F turn_id)
```
A turn idempotency-kulcsa a `Turn.turn_id` content-stabil hash-éből származik,
így ugyanannak a transcript-sornak az újraolvasása **azonos** kulcsot ad — a
későbbi drain deduplikálni tud.

---

## 4. Milyen teszt bizonyítja

`tests/test_hooks/test_ingest_sandbox.py` — **15 teszt, mind zöld**, a hookokat
**valódi subprocess-ként** futtatja (ahogy a Claude Code CLI tenné: külön
process, stdin JSON, megfigyelt exit code), mock nélkül:

| Teszt | Mit bizonyít |
|---|---|
| `test_hook_enqueues_event_envelope_and_exits_zero` (×6, paraméterezve) | mind a 6 hook envelope-ot ír az outboxba és `exit 0` |
| `test_stop_hook_extracts_transcript_turns` | a `Stop` valós `read_transcript_incremental`-lal 2 turn-t kinyer és enqueue-l |
| `test_stop_hook_incremental_no_duplicate_turns` | kétszeri `Stop` változatlan transcripten **nem** duplikál (offset működik) |
| `test_failure_isolation_broken_event_input` (×4) | hibás JSON / üres stdin / nem-objektum / hiányzó mezők → még mindig `exit 0` |
| `test_failure_isolation_stop_bad_transcript_path` | nemlétező `transcript_path` → `exit 0`, az event envelope így is bekerül |
| `test_default_sinks_are_sandbox_never_real_claude_config` | env nélkül a resolved outbox/log **sandbox** úton van, nem `~/.claude`-ban |
| `test_no_hook_performs_write_to_real_claude_config` | egyetlen hook-sor sem ír (`open`/`makedirs`/`.write`) valódi config-útra |

Futtatás:
```
python -m pytest tests/test_hooks/test_ingest_sandbox.py -v
# 15 passed
```

### Claim–evidence

| Állítás | Bizonyíték |
|---|---|
| Mind a 6 hook envelope-ot termel és `exit 0` | `test_hook_enqueues_event_envelope_and_exits_zero[*]` (6 PASSED) |
| `Stop` a `transcript_reader`-t hívja és turn-öket enqueue-l | `test_stop_hook_extracts_transcript_turns` PASSED |
| Inkrementális, nem-duplikáló transcript olvasás | `test_stop_hook_incremental_no_duplicate_turns` PASSED |
| Failure-isolation: semmilyen hiba nem blokkolja a usert | `test_failure_isolation_*` (5 PASSED) |
| A sink SOHA nem a valódi `~/.claude` / production PG | `test_default_sinks_*`, `test_no_hook_performs_write_*` PASSED |

---

## 5. Milyen státuszban indul

`experimental`. **Nem** állítható "go-live ready"-nek: a capability szándékosan
a sandboxnál áll meg. A privacy/consent szempontból nem triviális élesítést
(minden tool-use naplózása) külön, emberi döntés engedélyezi —
`session-ingest-hook-go-live-checklist.md`.

---

## 6. Registry / target-repo diff

Csak a `cic-mcp-session` cél-repó `feature/session-ingest-hook-sandboxed-001`
branch-én, kizárólag új fájlok + két meglévő bővítése:

```
hooks/_ingest_sandbox.py                       (új — közös ingest core)
hooks/UserPromptSubmit.py                      (új — vékony belépő)
hooks/PostToolUse.py                           (új)
hooks/PostToolUseFailure.py                    (új)
hooks/Stop.py                                  (új — + transcript-turn kinyerés)
hooks/SessionStart.py                          (új)
hooks/SessionEnd.py                            (új)
hooks/sandboxed-settings.example.json          (új — sandbox wiring példa)
tests/test_hooks/test_ingest_sandbox.py        (új — 15 teszt)
output/session-ingest-hook-sandboxed.md        (új — ez a report)
output/session-ingest-hook-go-live-checklist.md(új — élesítési checklist)
.gitignore                                     (+ /hooks/.sandbox-outbox/)
```

A meglévő `hooks/log-event.py` **érintetlen**. A `meta.yaml` `status` mezője
**nincs módosítva** (orchestrátor bookkeeping). Más cic-mcp-* repó nem változik.

---

## 7. Ismert limitációk

- **Csak sandbox sink.** A capability nem ír production PG-be és nem rak rá
  valódi `settings.json`-t — ez szándékos, nem hiányosság.
- **A `PostToolUseFailure` egy script-identitás.** A `provider_event_name` a
  stdin `hook_event_name`-jéből jön; ha a Claude Code natívan nem emittál
  `PostToolUseFailure` eseményt, a `PostToolUse` `tool_response` hibamezője a
  forrás — a wiring-részlet a go-live checklistben.
- **Az outbox-drain nincs implementálva.** A sandbox NDJSON → `insert_envelope`
  átemelés külön, még meg nem írt lépés (a go-live checklist írja le a
  contractját).
- **Az offset-store fájl-alapú és per-transcript.** Egyidejű több-process
  `Stop` ugyanarra a transcriptre nincs zárolva (egyetlen Claude Code session
  esetén nem fordul elő; konkurens drainnél figyelni kell).
- **Nincs méret-/visszanyomás-kezelés** a sandbox outboxon (eldobható, a
  drain feladata a forgatás).

---

## 8. Rollback / deprecate út

- **Rollback:** a teljes capability additív és sandboxolt. Visszavonás = a
  `hooks/<Event>.py` + `hooks/_ingest_sandbox.py` törlése (vagy a
  `sandboxed-settings.example.json` wiring nem-használata). Mivel semmi nem
  nyúl valódi confighoz vagy production PG-hez, a runtime-állapot **azonnal**
  visszaáll; a sandbox outbox (`hooks/.sandbox-outbox/`) gitignore-olt,
  törölhető.
- **Deprecate:** ha a go-live drain megvalósul és kiváltja a sandbox-réteget, a
  hookok a drain-cél env-átállításával (a `CIC_SESSION_INGEST_OUTBOX` helyett a
  production writer) deprecálhatók a script-logika módosítása nélkül.
- **Kill-switch:** a `settings.json` hook-bejegyzés eltávolítása vagy a
  `CIC_SESSION_INGEST_OUTBOX` egy `/dev/null`-szerű útra állítása azonnal
  leállítja a gyűjtést, a user-turn érintetlensége mellett.
