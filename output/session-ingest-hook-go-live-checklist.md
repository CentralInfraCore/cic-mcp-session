# Go-live checklist — session ingest hooks

- **Job:** `session-ingest-hook-sandboxed-001`
- **Capability:** `cic_mcp.session.ingest_hook_sandboxed` (`experimental`)

> **Ez a checklist NEM fut le automatikusan.** A `session-ingest-hook-sandboxed-001`
> job kizárólag a **sandbox** réteget szállítja. Az itt leírt lépések
> mindegyike **emberi/orchestrátor döntés** — egy futó Claude Code session
> minden tool-use-jának naplózása privacy/consent kérdés, nem build-lépés.
> A sandbox-szint addig is teljes értékűen tesztelhető a checklist BÁRMELY
> lépésének végrehajtása nélkül.

---

## 0. Előfeltétel-ellenőrzés (mielőtt bármit élesítesz)

- [ ] `session-raw-event-store-001` → `done` (a `insert_envelope` write-path él)
- [ ] `session-transcript-reader-001` → `done` (a `Stop` turn-kinyerés alapja)
- [ ] A production Postgres séma migrálva (`session_raw.envelopes` létezik)
- [ ] `session.env` valós DB-kredekkel kitöltve (soha nem committolva)
- [ ] A `session-data-protection` retention policy ismert és elfogadott
      (lásd `output/session-data-protection-retention-policy.md` —
      a betárolt session-adatra retention/purge vonatkozik)

## 1. Consent / privacy gate (NEM kihagyható)

- [ ] Explicit, dokumentált jóváhagyás, hogy a futó Claude Code session
      **minden** eseménye és transcript-turn-je betárolásra kerül
- [ ] Tisztázott, hogy mely projektek/contextusok vannak kizárva (ha van)
- [ ] A `payload` érzékeny tartalmának (tool-args, fájltartalom) kezelése
      eldöntve — redaction szükséges-e a drain előtt
- [ ] A retention/purge job (lásd lent) készen áll, mielőtt élő adat folyik be

## 2. Outbox-drain implementálása (még NEM létezik)

A sandbox a `CIC_SESSION_INGEST_OUTBOX` NDJSON fájlba ír. Az élesítéshez egy
**külön drain-komponens** kell, ami:

- [ ] olvassa az NDJSON outboxot (vagy a hookok közvetlenül a writer-t hívják)
- [ ] minden sort átad a `session_store.envelope_writer.insert_envelope`-nak
- [ ] az `idempotency_key`-re támaszkodva deduplikál (a turn-kulcs stabil)
- [ ] kezeli a back-pressure-t és az outbox-forgatást
- [ ] **megőrzi a failure-isolation-t**: a drain hibája sem blokkolhatja a hookot

> A drain contractja: `insert_envelope(envelope_dict)` — a sandbox envelope-ok
> mezőkészlete (lásd `session-ingest-hook-sandboxed.md` §3) már a writer
> elvárt alakja. A drain a **háttérben** fut, nem a user-turn hot-path-jén.

## 3. Settings.json wiring (SOHA nem a sandbox repo-ba)

- [ ] A `hooks/sandboxed-settings.example.json` `{{REPO_ROOT}}` / `{{SANDBOX_DIR}}`
      placeholderjei valós (de **továbbra is sandbox**) értékre cserélve a
      teszteléshez
- [ ] Csak miután a drain + consent megvan: a hook-bejegyzések átemelése a
      **valódi** `~/.claude/settings.json`-ba — ez az a pont, amit a
      `session-ingest-hook-sandboxed-001` job maga **SOHA nem tesz meg**
- [ ] A `CIC_SESSION_INGEST_OUTBOX` env beállítva (sandbox élesítésnél a
      drain-forrásra; teljes élesítésnél a writer közvetlen hívása)

## 4. Smoke-teszt élesítés előtt

- [ ] Egy izolált, eldobható session indítása a wiring-gel
- [ ] Ellenőrzés: a 6 hook envelope-jai megjelennek az outboxban / writer-ben
- [ ] Ellenőrzés: a `Stop` turn-envelope-jai inkrementálisan, duplikáció nélkül
- [ ] Ellenőrzés: szándékosan hibás input mellett a Claude Code turn **nem** akad el

## 5. Kill-switch / visszaállás

- [ ] A hook-bejegyzés eltávolítása a `settings.json`-ból azonnal leállítja a
      gyűjtést, a user-turn érintetlensége mellett
- [ ] `CIC_SESSION_INGEST_OUTBOX` egy eldobható útra állítása "soft kill"
- [ ] A sandbox outbox (`hooks/.sandbox-outbox/`) gitignore-olt és törölhető

---

### Mit szállít a job, és mit NEM

| Szállítja (sandbox) | NEM szállítja (külön, emberi gate) |
|---|---|
| 6 hook script + közös core | valódi `~/.claude/settings.json` írás |
| sandbox NDJSON outbox + offset | production Postgres rákötés |
| `Stop` → transcript turn kinyerés | outbox-drain implementáció |
| failure-isolation + 15 teszt | consent/privacy döntés |
| sandbox settings **példa** | retention/purge élő adatra |
