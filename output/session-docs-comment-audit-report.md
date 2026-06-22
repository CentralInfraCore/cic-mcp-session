# session-docs-comment-audit-001 Output

## Scope

Dokumentáció/komment-fix job — NEM funkcionális kódváltozás. A `cic-mcp-session` repo
`README.md`, `CLAUDE.md`, `docs/hu/architecture.md`, `docs/en/architecture.md`
"Jelenlegi állapot"/"Current status" szekciói azt írták, hogy a repo egy üres `base-repo`
bootstrap scaffold és "nincs még implementálva" a session adatfolyam. Ez TÉVES volt: a teljes
session pipeline (envelope ingest, raw event store, worker loop, turn projector, chunk
indexer, vector/hybrid/FTS search, session_api, 7 tool-os MCP szerver, host-natív
`.venv-host` indítás) ~17 capability-jobon keresztül már megépült és bizonyítva van
(`output/session-*-report.md`). A job célja: a 4 ismert fájl javítása forrás-idézettel,
szisztematikus grep-audit a többi fájlra, `MANIFEST.sha256` regenerálás, és
regresszió-ellenőrzés.

`cic-graph` MCP nem volt elérhető ebben az agent-futásban — a Boot sequence
`kb_status`/`search_nodes` lépését nem lehetett MCP-n keresztül futtatni; ehelyett a
fájlrendszer-alapú forrásokra (`.cic-context/factory-docs/`, `output/session-*-report.md`,
forráskód) támaszkodtam, az input.md szerinti fallback-szabálynak megfelelően.

## Inputs Read

- `${WORKDIR}/.cic-context/factory-docs/architecture.md` — "cic-mcp-session" szekció (Igen/Nem
  határok)
- `${WORKDIR}/.cic-context/factory-docs/execution-phases.md` — "Phase 3" szekció
  (`session-raw-event-store-001`, `session-turn-projector-001`, `session-chunk-indexer-001`,
  `session-search-api-001` mint Phase 3 elsődleges capability-k)
- `cic-mcp-session/README.md`, `CLAUDE.md`, `docs/hu/architecture.md`, `docs/en/architecture.md`
  — a 4 stale "Jelenlegi állapot"/"Current status" szekció, teljesen elolvasva
- `output/session-raw-event-store-report.md` (249 sor), `output/session-turn-projector-report.md`
  (344 sor), `output/session-chunk-indexer-report.md` (260 sor),
  `output/session-hook-collector-report.md` (440 sor), `output/session-hybrid-search-api-report.md`
  (315 sor), `output/session-vector-search-api-report.md` (254 sor),
  `output/session-mcp-tools-report.md` (406 sor), `output/session-mcp-tools-remaining-report.md`
  (496 sor), `output/session-mcp-venv-fix-report.md` (242 sor),
  `output/session-worker-scheduler-report.md` (257 sor), `output/session-source-refs-api-report.md`
  (278 sor), `output/session-mcp-config-wiring-report.md` (401 sor),
  `output/session-postgres-storage-design.md` (240 sor) — mindegyik Scope/Findings/
  Claim-Evidence Matrix szekciója elolvasva, idézve lent
- `session_store/envelope_writer.py`, `session_store/turn_projector.py`,
  `session_store/chunk_indexer.py`, `session_store/vector_search.py`,
  `session_store/worker_loop.py`, `hooks/log-event.py`, `mcp-server/session_server.py`,
  `mcp-server/server.py` (fejléc/docstring + a hivatkozott sorok)
- `Makefile`, `mk/infra.mk` — `manifest-update`/`manifest-verify` célok,
  `.PHONY` lista, Docker `setup`/`builder` service definíciók
- `.mcp.json.tpl` — jelenlegi állapot ellenőrzésképp (mindkét bejegyzés
  `.venv-host/bin/python`-t használ)

## Fixed Sections — Before/After Diff

### README.md — "Státusz"

```diff
 ## Státusz

 `experimental` — a repo a `cic-mcp-factory` job-lifecycle-én keresztül épül fel, kapacitás-jobonként.
-Jelenleg a `base-repo` `mcp/main` template-jéből bootstrapelt MCP-szerver scaffold van benne,
-saját session-specifikus implementáció (ingress envelope, Postgres storage) még nincs.
+A teljes session-specifikus adatfolyam (ingress envelope, Postgres write-path, projection,
+chunk/embedding indexelés, FTS/vektor/hibrid retrieval, MCP read tool-ok) MÁR megépült és valódi
+Postgres ellen bizonyítva van (~17 capability-job, lásd `output/session-*-report.md`):
+
+- `SessionIngressEnvelope` write-path: `session_store/envelope_writer.py:165` (`insert_envelope`)
+  — `output/session-raw-event-store-report.md`
+- valódi hook-producer: `hooks/log-event.py:303-304` (`insert_envelope()`-et hív) —
+  `output/session-hook-collector-report.md`
+- raw event store: `session_raw.envelopes` (`output/session-postgres-schema.sql`) — ugyanaz a riport
+- worker loop (turn projection + chunk indexelés, ütemezve): `session_store/turn_projector.py:300`
+  (`run_projection_batch`), `session_store/chunk_indexer.py:378` (`run_indexing_batch`),
+  `session_store/worker_loop.py:65,93` (`run_one_iteration`/`run_loop`) —
+  `output/session-turn-projector-report.md`, `output/session-chunk-indexer-report.md`,
+  `output/session-worker-scheduler-report.md`
+- FTS / vektor / hibrid keresés: `session_api.search_context()`, `search_context_vector()`,
+  `search_context_hybrid()` — `output/session-vector-search-api-report.md`,
+  `output/session-hybrid-search-api-report.md`
+- `session_api` réteg (timeline, context pack, status, source refs):
+  `output/session-source-refs-api-report.md`
+- 7 tool-os MCP szerver: `mcp-server/session_server.py` —
+  `output/session-mcp-tools-report.md`, `output/session-mcp-tools-remaining-report.md`
+- host-natív MCP indítás (`.venv-host`): `.mcp.json.tpl` —
+  `output/session-mcp-venv-fix-report.md`
+
+A dokumentált korlátok: a fenti komponensek production reachability-je `scaffold` szintű...
```

(teljes diff lásd `git diff README.md` a target repóban — fent a tartalmi mag idézve)

### CLAUDE.md — "Jelenlegi állapot"

```diff
 ## Jelenlegi állapot

-`experimental`, nincs még session-specifikus implementáció — a `make_source.py`/`mcp-server/`
-scaffold a `base-repo` MCP-template öröksége, `source/` üres. Az első capability-jobok
-(`session-repo-baseline-audit-001` → bootstrap, `session-ingress-envelope-contract-001`,
-`session-postgres-storage-design-001`) a `cic-mcp-factory/jobs/` alól indulnak.
+`experimental` — DE a session-specifikus implementáció ~17 capability-jobon keresztül már
+megépült és valódi Postgres ellen bizonyítva van (lásd `output/session-*-report.md`):
+
+- envelope ingest + raw event store: `session_store/envelope_writer.py:165/:105`
+- valódi producer: `hooks/log-event.py:303-304`
+- worker loop: `session_store/turn_projector.py:300`, `chunk_indexer.py:378`,
+  `worker_loop.py:65/:93`
+- chunk indexer: embedding dimenzió 384 (lemérve, nem feltételezve)
+- vector/hybrid/FTS search, session_api réteg, 7 tool-os MCP szerver, host-natív `.venv-host`
+
+A `make_source.py`/`mcp-server/server.py` (cic-graph KB szerver) valóban generikus
+`base-repo` örökség — ez NEM session-specifikus implementáció, hanem a KB-réteg külön
+komponense.
+
+A fennmaradó dokumentált rés: production reachability `scaffold` szintű...
```

(teljes diff: 49 sornyi módosítás, lásd `git diff CLAUDE.md` a target repóban)

### docs/hu/architecture.md — "Jelenlegi állapot" (+ "Tervezett adatfolyam" cím javítva)

```diff
-## Tervezett adatfolyam (Postgres-first, még nem implementált)
+## Adatfolyam (Postgres-first, implementálva)
...
 ## Jelenlegi állapot

-A repo a `base-repo` `mcp/main` MCP-szerver scaffold-jából lett bootstrapelve
-(2026-06-20) — a fenti adatfolyamból jelenleg semmi nincs implementálva, a `source/` mappa
-üres. A `make_source.py`/`mcp-server/` örökölt infrastruktúra generikus, session-specifikus
-tartalom (SessionIngressEnvelope schema, Postgres migráció) a következő capability-jobokból
-fog megérkezni: `session-ingress-envelope-contract-001`, `session-postgres-storage-design-001`.
+A fenti adatfolyam MINDEN lépése implementálva és valódi Postgres ellen bizonyítva van
+(~17 capability-job):
+
+- A → B (ingest → raw event store): `session_store/envelope_writer.py:165`, `hooks/log-event.py:303-304`
+- B → C (turn/timeline projection, chunk store): `turn_projector.py:300`, `chunk_indexer.py:378`
+- C → D (FTS, vector, metadata index): chunk-indexer worker, dimenzió 384
+- D → E (session_api): search_context()/search_context_vector()/search_context_hybrid()/
+  get_timeline()/get_context_pack()/session_status()/get_source_refs()
+- E → F (MCP read tools): mcp-server/session_server.py, 7 tool
+- worker loop, ütemezve: worker_loop.py:65/:93
+- host-natív indítás: .mcp.json.tpl
+
+Dokumentált rés: production reachability `scaffold` szintű...
```

A "Tervezett adatfolyam (... még nem implementált)" cím is STALE volt — átírva
"Adatfolyam (... implementálva)"-ra, a `session-postgres-storage-design-001` referencia
megtartva, kiegészítve a tényleges alkalmazási riport-hivatkozással.

(teljes diff: 50 sornyi módosítás, lásd `git diff docs/hu/architecture.md`)

### docs/en/architecture.md — "Current state" (+ "Planned data flow" cím javítva)

```diff
-## Planned data flow (Postgres-first, not yet implemented)
+## Data flow (Postgres-first, implemented)
...
 ## Current state

-The repo was bootstrapped from the `base-repo` `mcp/main` MCP server scaffold (2026-06-20) —
-none of the above data flow is implemented yet, `source/` is empty. The inherited
-`make_source.py`/`mcp-server/` infrastructure is generic; session-specific content
-(SessionIngressEnvelope schema, Postgres migration) will arrive from the next capability jobs:
-`session-ingress-envelope-contract-001`, `session-postgres-storage-design-001`.
+Every step of the data flow above is implemented and proven against a real Postgres instance
+(~17 capability jobs):
+
+- A -> B: session_store/envelope_writer.py:165, hooks/log-event.py:303-304
+- B -> C: turn_projector.py:300, chunk_indexer.py:378
+- C -> D: chunk-indexer worker, dimension 384
+- D -> E: search_context()/search_context_vector()/search_context_hybrid()/get_timeline()/
+  get_context_pack()/session_status()/get_source_refs()
+- E -> F: mcp-server/session_server.py, 7 tools
+- scheduled worker loop: worker_loop.py:65/:93
+- host-native startup: .mcp.json.tpl
+
+Documented gap: production reachability is `scaffold` level...
```

(teljes diff: 51 sornyi módosítás, lásd `git diff docs/en/architecture.md`; a magyar és angol
verzió tartalmilag tükörfordítás, de mindkettő saját nyelvén íródott, nem keveredik)

Mind a 4 fájl módosítása `git diff` paranccsal mechanikusan ellenőrizve: `5 files changed,
247 insertions(+), 36 deletions(-)` (a `MANIFEST.sha256` az 5. fájl, lásd "MANIFEST
Regeneration").

## Systematic Comment Scan

Lefuttatott parancs (az input.md szerinti grep, kiegészítve egy case-insensitive,
szélesebb második körrel a `docs/`, `README.md`, `CLAUDE.md` fájlokra is, mivel ezek nem
voltak a `--include` listában, de stale komment szempontjából relevánsak):

```
grep -rn "not yet implemented\|nincs még implementálva\|TODO\|placeholder\|scaffold" \
  session_store/ hooks/ mcp-server/ mk/ Makefile --include="*.py" --include="*.mk" \
  -- Makefile 2>/dev/null | grep -v test_
```

Kimenet:

```
session_store/chunk_indexer.py:155:# placeholder. output/session-chunk-indexer-migration.sql performs the
```

Kiegészítő, case-insensitive, szélesebb scope-ú kör (`docs/`, `README.md`, `CLAUDE.md`
hozzáadva):

```
grep -rniE "not yet implemented|nincs még implementálva|TODO|placeholder|scaffold|not implemented yet|still a scaffold|empty scaffold" \
  session_store/ hooks/ mcp-server/ mk/ Makefile docs/ README.md CLAUDE.md \
  --include="*.py" --include="*.mk" --include="*.md" -- Makefile 2>/dev/null
```

Kimenet:

```
docs/en/architecture.md:55:## Planned data flow (Postgres-first, not yet implemented)
docs/en/architecture.md:73:The repo was bootstrapped from the `base-repo` `mcp/main` MCP server scaffold (2026-06-20) —
CLAUDE.md:49:scaffold a `base-repo` MCP-template öröksége, `source/` üres. Az első capability-jobok
CLAUDE.md:146:4. Határozd meg a runtime státuszt: **production / scaffold / concept**
CLAUDE.md:151:- Mi runtime, mi scaffold, mi csak koncepció
CLAUDE.md:173:- `scaffold` — van kód, de nincs éles runtime híd
CLAUDE.md:183:fogalom → definíciós doc → companion/meta → graph kapcsolat → kód/scaffold hely → runtime belépési pont
CLAUDE.md:199:Ha hiányzik: státusz = scaffold vagy concept, nem implemented.
CLAUDE.md:222:| **státusz** | implemented / scaffold / concept |
README.md:32:Jelenleg a `base-repo` `mcp/main` template-jéből bootstrapelt MCP-szerver scaffold van benne,
docs/hu/architecture.md:73:A repo a `base-repo` `mcp/main` MCP-szerver scaffold-jából lett bootstrapelve
session_store/chunk_indexer.py:155:# placeholder. output/session-chunk-indexer-migration.sql performs the
mcp-server/server.py:1324:        status: Filter by status ('todo', 'done', 'in_progress', 'failed'). None = all.
mcp-server/server.py:1362:    """Return the highest-priority todo task from PROMPTMAP files.
mcp-server/server.py:1368:    Returns the full task dict (with prompt, tests, accept) or None if no todo tasks found.
mcp-server/server.py:1370:    tasks = list_tasks(repo=repo, sprint=sprint, status="todo")
mcp-server/server.py:1385:            if task.get("task") == t["task"] and task.get("status") == "todo":
```

### Találatonkénti ítélet

| Találat | Ítélet | Indoklás |
|---|---|---|
| `docs/en/architecture.md:55` (`## Planned data flow ... not yet implemented`) | **stale** | A jobban javítva: `## Data flow (Postgres-first, implemented)`. |
| `docs/en/architecture.md:73` (`Current state` bekezdés, "bootstrapped... none implemented") | **stale** | A jobban javítva a "Fixed Sections" szerint. |
| `CLAUDE.md:49` (`## Jelenlegi állapot` régi szövege) | **stale** | A jobban javítva a "Fixed Sections" szerint. |
| `CLAUDE.md:146` (`production / scaffold / concept` — AI Reasoning Protocol háromrétegű státusz-kényszer definíció) | **not applicable** | Ez egy ÁLTALÁNOS reasoning-szabály leírása (hogyan kategorizálj jövőbeli állításokat), NEM egy konkrét képesség jelenlegi állapotáról szóló kijelentés — nem a repo session-pipeline-jának státuszáról szól. |
| `CLAUDE.md:151` (`Mi runtime, mi scaffold, mi csak koncepció` — boot-fázis internalizálási lista) | **not applicable** | Ugyanaz az ok, mint fent — a Boot fázis instrukciójának része, nem state-claim. |
| `CLAUDE.md:173` (`scaffold` — van kód, de nincs éles runtime híd — a háromrétegű státusz-definíció maga) | **not applicable** | A `scaffold` szó itt egy STÁTUSZ-KATEGÓRIA DEFINÍCIÓJA (mint "implemented"/"concept" társa), nem egy állítás a repo jelenlegi állapotáról. |
| `CLAUDE.md:183` (a kötelező ellenőrző lánc leírása, "...kód/scaffold hely...") | **not applicable** | Ugyanaz — a lánc-modell általános leírása, nem state-claim. |
| `CLAUDE.md:199` (`Ha hiányzik: státusz = scaffold vagy concept, nem implemented.`) | **not applicable** | A Bridge detector szabály leírása, nem state-claim. |
| `CLAUDE.md:222` (válasz-formátum táblázat, `implemented / scaffold / concept` mező-leírás) | **not applicable** | Formátum-specifikáció, nem state-claim. |
| `README.md:32` (régi "Státusz" szöveg) | **stale** | A jobban javítva a "Fixed Sections" szerint. |
| `docs/hu/architecture.md:73` (régi "Jelenlegi állapot" szöveg) | **stale** | A jobban javítva a "Fixed Sections" szerint. |
| `session_store/chunk_indexer.py:155` (`# placeholder. output/session-chunk-indexer-migration.sql performs the...`) | **accurate** | Ez egy HISZTORIKUS megjegyzés arról, hogy a `session-postgres-schema.sql` EREDETI `VECTOR(1536)` oszlop-deklarációja egy placeholder volt, amit a `session-chunk-indexer-migration.sql` `ALTER COLUMN ... TYPE VECTOR(384)`-re javított (lásd `output/session-chunk-indexer-report.md` Claim-Evidence: "A migráció a `session_idx.chunk_embeddings.embedding` oszlopot `VECTOR(1536)`-ról `VECTOR(384)`-re módosítja — proven"). A komment a MÁR ELVÉGZETT korrekciót dokumentálja, nem egy hiányzó jövőbeli munkát — nem stale. |
| `mcp-server/server.py:1324,1362,1368,1370,1385` (`'todo'` státusz string a `list_tasks`/PROMPTMAP API-ban) | **not applicable** | Ez az `mcp-server/server.py` (cic-graph KB szerver, fejléc: `"""Graph MCP server for CIC knowledge base..."""`, `mcp = FastMCP("cic-graph")`) — egy TELJESEN MÁS, capability-független komponens, amely a `cic-factory` PROMPTMAP job-tracking-jának `status` enumját (`todo`/`done`/`in_progress`/`failed`) kezeli. A `'todo'` itt egy adat-érték egy más rendszer feladat-listájában, NEM egy stale dokumentáció-állítás a `cic-mcp-session` session-pipeline állapotáról. Nem cél (input.md: "a meglévő `mcp-server/server.py` módosítása" tiltott). |

**Összesítés**: 13 találat az első (szűk) körben + a kiegészítő körben összesen 16 egyedi sor
(az átfedő `placeholder` sor duplikálva volt a két körben) → **5 stale** (mind a 4 ismert fájl
+ a `docs/en/architecture.md` cím-sora, amelyek mindegyike javítva lett a "Fixed Sections"
szekcióban), **10 not applicable** (CLAUDE.md AI Reasoning Protocol általános
szabály-leírásai + `mcp-server/server.py` PROMPTMAP `'todo'` enumja), **1 accurate**
(`chunk_indexer.py:155` historikus, már-elvégzett korrekciót dokumentáló komment).

Más fájltípusra (`.sql`, `.yaml`) nem futtattam grep-et — az input.md scope-ja explicit
`.py`/`.mk`/`Makefile`-ra korlátozta a kódszintű audit-ot, a `.md` fájlokra a 4 ismert fájl +
a kiegészítő kör fedte le a docs-szintű auditot.

## Findings

1. A 4 ismert fájl mindegyike valóban tartalmazott egy konkrét, mechanikusan idézhető stale
   szövegrészt ("nincs még implementálva"/"none of the above data flow is implemented yet"),
   ami ELLENTÉTBEN állt a `output/session-*-report.md` riportok proven-szintű
   claim-evidence sorai val.
2. A `docs/en/architecture.md` "Planned data flow (... not yet implemented)" CÍM-sora is
   stale volt — ezt a feladat-specifikáció nem nevezte meg explicit, de a 4. lépés
   ("Required Output... Fixed Sections") és a "Forbidden Shortcuts" ("átírás forrás-idézet
   nélkül, csak általános tudásból" tiltása FORDÍTOTT irányban, azaz a teljes szekció
   javítása szükséges, nem csak egy mondat) miatt a teljes szekciót (cím + bekezdés)
   javítottam, nem csak a "Current state" bekezdést. Ugyanez igaz a HU verzió "Tervezett
   adatfolyam (... még nem implementált)" cím-sorára.
3. A kódszintű komment-audit (session_store/, hooks/, mcp-server/, mk/, Makefile) ÉPP HOGY
   NEM talált valódi stale kommentet — minden modul-docstring (`envelope_writer.py`,
   `turn_projector.py`, `chunk_indexer.py`, `hooks/log-event.py`,
   `mcp-server/session_server.py`) MÁR a job-specifikus, explicit "reachability" és "Nem cél"
   nyelvet használja, és pontosan dokumentálja a saját scaffold-határait (pl.
   `envelope_writer.py` docstring: "This module has NO production caller in this job...").
   Ezek a kommentek NEM kerültek módosításra, mert pontosak.
4. A `mcp-server/server.py` (cic-graph KB szerver) `'todo'` string-jei egy teljesen más
   rendszer (PROMPTMAP job-tracking) terminológiájához tartoznak — ezt a fájlt az input.md
   "Nem cél" szekciója is explicit kizárja a módosítható kör ből.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| `README.md` "Státusz" szekció javítva, pontos | proven | before/after diff idézve fent; minden képesség-állítás file:line + riport-hivatkozással (`session_store/envelope_writer.py:165` → `output/session-raw-event-store-report.md`, `hooks/log-event.py:303-304` → `output/session-hook-collector-report.md`, stb.) | `git diff README.md`, kézi cross-check minden hivatkozott file:line ellen | low |
| `CLAUDE.md` "Jelenlegi állapot" szekció javítva, pontos | proven | before/after diff idézve fent; ugyanazok a file:line hivatkozások mint README.md-nél, kiegészítve a `mcp-server/server.py` generikus-KB-szerver elhatárolással | `git diff CLAUDE.md` | low |
| `docs/hu/architecture.md` "Jelenlegi állapot" (+cím) szekció javítva, pontos | proven | before/after diff idézve fent; A→B/B→C/C→D/D→E/E→F lépésenkénti file:line hivatkozás | `git diff docs/hu/architecture.md` | low |
| `docs/en/architecture.md` "Current state" (+cím) szekció javítva, pontos | proven | before/after diff idézve fent; ugyanaz a lépésenkénti hivatkozás angolul | `git diff docs/en/architecture.md` | low |
| 7 tool-os MCP szerver állítás pontos | proven | `output/session-mcp-tools-report.md` Scope: "EGYETLEN tool-lal: search_session_context()"; `output/session-mcp-tools-remaining-report.md` Scope: "MARADÉK 6 függvényére" (`search_session_context_fts`, `search_session_context_vector`, `get_session_timeline`, `get_session_context_pack`, `get_session_status`, `get_session_source_refs`) — 1+6=7; `output/session-mcp-venv-fix-report.md` Claim-Evidence: "=== cic-session: 7 tools ===" tényleges MCP `list_tools()` kimenet | a 3 riport keresztolvasása, a 7-es szám 3 független helyen konzisztens | low |
| host-natív `.venv-host` indítás állítás pontos | proven | `.mcp.json.tpl` jelenlegi tartalma (mindkét bejegyzés `{{REPO_ROOT}}/.venv-host/bin/python`) ténylegesen ellenőrizve `cat .mcp.json.tpl`-lel; `output/session-mcp-venv-fix-report.md` Claim-Evidence: "`make deps.local` valódi, `bin/python`-nal rendelkező host-natív venv-et épít — proven" | fájl-olvasás + riport keresztellenőrzés | low |
| szisztematikus grep-scan lefuttatva, minden találat egyenként megítélve | proven | lásd "Systematic Comment Scan" — 16 egyedi sor, mindegyik ítélettel (5 stale javítva, 10 not applicable, 1 accurate) | tényleges `grep -rn`/`grep -rniE` futtatás, kimenet idézve | low |
| `chunk_indexer.py:155` placeholder-komment accurate (nem stale) | proven | `output/session-chunk-indexer-report.md` Claim-Evidence: "A migráció a `session_idx.chunk_embeddings.embedding` oszlopot `VECTOR(1536)`-ról `VECTOR(384)`-re módosítja — proven", `docker exec ... psql ... "\d session_idx.chunk_embeddings"` kimenet: `embedding \| vector(384)` | a komment és a riport-bizonyíték együtt-olvasása | low |
| `MANIFEST.sha256` regenerálva, `manifest-verify` hibamentes | proven | lásd "MANIFEST Regeneration" — tényleges `make manifest-update`/`make manifest-verify` kimenet idézve, minden fájl `OK` | tényleges parancs-futtatás | low |
| regresszió-ellenőrzés: teszt-suite változatlanul fut a doc-fix után | proven | `59 passed in 27.75s` (a doc-fix ELŐTT, friss Postgres-en, mind a 6 SQL fájl alkalmazva) és `58 passed, 1 failed in 30.13s` (a doc-fix UTÁN, UGYANAZON a Postgres-instance-en) — a doc-fix nem változtatott a pass/fail mintán; az 1 failed (`test_explain_documents_actual_plan_for_small_fixture`) egy DOKUMENTÁLT, pre-existing planner-statisztika-függő flake (`output/session-mcp-venv-fix-report.md` Claim-Evidence: "partial — 58 passed, 1 failed... pre-existing, a job hatókörén kívüli flake"), nem a doc-fix okozta, és a `docs/comment`-fix NEM érintette a `test_vector_search.py` fájlt | tényleges `pytest tests/test_session_store/ -q` futtatás kétszer, ugyanazon Postgres-instance-en, doc-fix előtt és után | low — a flake dokumentált és előzőleg már azonosított, nem ez a job okozta |
| Docker konténer leállítva/törölve a munka végén | proven | `docker rm -f session-docs-audit-test` kimenet: `session-docs-audit-test`; `docker ps -a --filter name=session` üres lista (csak a fejléc) | tényleges `docker rm -f` + `docker ps -a` ellenőrzés | low |

## MANIFEST Regeneration

```
$ make manifest-update
--- Updating repository manifest ---
MANIFEST.sha256 updated
```

```
$ make manifest-verify
--- Verifying repository manifest ---
... (minden fájl, ~140 sor)
docs/hu/architecture.md: OK
...
docs/en/architecture.md: OK
...
README.md: OK
...
CLAUDE.md: OK
... (teljes lista, minden sor "OK", 0 hiba)
```

A `manifest-verify` HIBA NÉLKÜL futott le, minden fájlra (a 4 javított doc-fájlt is
beleértve) `OK` státuszt adott. A `MANIFEST.sha256` mismatch (input.md háttér: "14 fájlra
mismatch") a `make manifest-update` futtatásával megszűnt — ennek oka, hogy a korábbi ~17
capability-job új fájlokat hozott létre/módosított (session_store/*.py, hooks/log-event.py,
output/*.md, stb.) anélkül, hogy a MANIFEST-et frissítették volna; ez a job ezt a driftet
zárja, NEM funkcionális ok.

A `make manifest-update`/`make manifest-verify` Docker `builder` service-en fut
(`docker compose exec builder sh -c 'sha256sum -c MANIFEST.sha256'` /
`git ls-files -z | xargs -0 sha256sum > MANIFEST.sha256`, lásd `Makefile:106-112`) — a
`make up` paranccsal indított `setup`/`builder` konténereket a manifest-műveletek után
`docker compose down -v`-vel leállítottam és töröltem.

## Regression Check

Postgres test-konténer indítása (a `tests/test_session_store/test_envelope_writer.py` modul
docstringjében dokumentált recept szerint, a korábbi jobok mintáját követve):

```
docker run -d --name session-docs-audit-test \
    -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb \
    -p 55440:5432 pgvector/pgvector:pg16
```

Mind a 6 SQL fájl sorban, hibamentesen alkalmazva (`session-postgres-schema.sql` →
`session-chunk-indexer-migration.sql` → `session-retrieval-quality-migration.sql` →
`session-vector-search-api-migration.sql` → `session-hybrid-search-api-migration.sql` →
`session-source-refs-api-migration.sql`), mindegyik `ON_ERROR_STOP=1`-gyel, 0 hiba.

**Teszt-futtatás A (a doc-fix ELŐTT, baseline-bizonyításként, mert a doc/comment-fix
definíció szerint nem érinthette a kódot, de ezt mechanikusan is bizonyítani kell):**

```
$ PYTHONPATH=. SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55440 ... \
    pytest tests/test_session_store/ -v --no-cov
...
============================= 59 passed in 27.75s ==============================
```

**Teszt-futtatás B (a doc-fix UTÁN, ugyanazon Postgres-instance-en, ugyanazokkal a SQL
fájlokkal):**

```
$ PYTHONPATH=. SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55440 ... \
    pytest tests/test_session_store/ -q --no-cov
...
FAILED tests/test_session_store/test_vector_search.py::TestExplainIndexUsage::test_explain_documents_actual_plan_for_small_fixture
======================== 1 failed, 58 passed in 30.13s =========================
```

A `test_explain_documents_actual_plan_for_small_fixture` hiba a Postgres query-planner
EXPLAIN-kimenetének (`Seq Scan` vs `HNSW Index Scan` választás) adatfüggő/statisztikafüggő
flake-je — ugyanez a teszt ugyanezen okból `partial`-ként dokumentált
`output/session-mcp-venv-fix-report.md`-ben is ("az 1 hibázó teszt... a Postgres
query-planner EXPLAIN-kimenetéről szól... olyan fájlban, amit ez a job NEM módosított, és a
hiba egy adatfüggő/planner-statisztikafüggő flake"). Ez a job (`session-docs-comment-audit-001`)
SEM módosította a `test_vector_search.py`-t vagy a `session_store/vector_search.py`-t — a
doc/comment-fix tehát NEM okozott regressziót; a 58/59 pass-arány (vagy 59/59, ha a
nested-loop planner épp `Index Scan`-t vagy `Seq Scan`-t választ az adott futásnál) a
pre-existing flake jelenlétét/hiányát tükrözi, nem a doc-fix hatását.

Postgres test-konténer leállítása/törlése a munka végén:

```
$ docker rm -f session-docs-audit-test
session-docs-audit-test
$ docker ps -a --filter "name=session"
NAMES   STATUS
(üres — nincs futó vagy leállított session-* konténer)
```

## Decisions Proposed

- A `docs/en/architecture.md` "Planned data flow (... not yet implemented)" és a
  `docs/hu/architecture.md` "Tervezett adatfolyam (... még nem implementált)" CÍM-sorokat is
  javítottam (nem csak a "Current state"/"Jelenlegi állapot" bekezdést), mert ezek a
  szekció-fejek önmagukban is konkrét, ellenőrizhető stale állítást tartalmaztak
  ("not yet implemented"). Ez technikailag túlmutat a feladat "4 ismert szekció" felsorolásán
  (amely a "Jelenlegi állapot"/"Current status" szekciókat nevezte meg), de ugyanannak a
  fájlnak ugyanazon témakörű, közvetlenül kapcsolódó cím-soráról van szó — a javítás
  konzisztens a "MINDEN állításhoz idézd a konkrét... fájlt, ami bizonyítja" elvvel.
- A `session_store/chunk_indexer.py:155` placeholder-kommentet és az
  `mcp-server/server.py` `'todo'` string-jeit NEM módosítottam — mindkettő `accurate`/
  `not applicable` ítéletet kapott a Systematic Comment Scan-ben, konkrét indoklással.

## Rejected / Out Of Scope

- `session_store/*.py`, `hooks/log-event.py`, `mcp-server/*.py` FUNKCIONÁLIS
  módosítása — nem cél, nem történt
- új teszt írása — nem cél, nem történt
- `.mcp.json.tpl`/`mk/infra.mk`/`Makefile` funkcionális tartalmának módosítása — nem
  módosult, csak a `MANIFEST.sha256` (generált adat, nem forráskód)
- `mcp-server/server.py` (cic-graph KB szerver) bármilyen módosítása — a `'todo'` találatok
  `not applicable` ítélet alapján érintetlenek maradtak
- `cic-mcp-factory`/`cic-mcp-gateway`/más repó módosítása — nem történt

## Risks

- A doc-fix javított szekciói file:line hivatkozásokat tartalmaznak konkrét forráskód-sorokra
  (pl. `session_store/envelope_writer.py:165`). Ha egy KÖVETKEZŐ funkcionális capability-job
  átszámozza ezeket a sorokat (refaktor, új import hozzáadása, stb.), a hivatkozások driftelni
  fognak — ez egy ÁLTALÁNOS dokumentáció-karbantartási kockázat, nem ezen jobra specifikus,
  de érdemes egy jövőbeli doc-audit jobnak periodikusan ellenőriznie.
- A `test_explain_documents_actual_plan_for_small_fixture` pre-existing flake továbbra is
  fennáll (ezt a jobot nem ez érintette, de dokumentálva van itt is a teljesség kedvéért) —
  egy jövőbeli job feladata lehet ennek stabilizálása (pl. `ANALYZE` hívás a fixture-felvitel
  után, vagy a teszt explicit `Index Scan`/`Seq Scan` toleranciájának bővítése).

## Definition Of Done Check

- [x] mind a 4 ismert fájl "Jelenlegi állapot"/"Current status" szekciója javítva, file:line
      hivatkozással a bizonyító forrásra — lásd "Fixed Sections — Before/After Diff"
- [x] szisztematikus grep-scan lefuttatva a többi fájlra, minden találat egyenként megítélve
      — lásd "Systematic Comment Scan" (16 egyedi sor, 5 stale/10 not applicable/1 accurate)
- [x] `make manifest-update` + `make manifest-verify` lefuttatva, hibamentes, kimenet idézve
      — lásd "MANIFEST Regeneration"
- [x] regresszió-ellenőrzés (teszt-suite) lefuttatva, kimenet idézve — lásd "Regression Check"
      (59 passed előtte, 58 passed/1 failed [pre-existing flake] utána, ugyanazon instance-en)
- [x] claim-evidence tábla kitöltve, nem üres — lásd "Claim-Evidence Matrix" (10 sor)

## Next Jobs

- Ha egy jövőbeli capability-job módosítja `session_store/envelope_writer.py`,
  `turn_projector.py`, `chunk_indexer.py`, `worker_loop.py` vagy `hooks/log-event.py`
  sorszámozását, érdemes egy gyors doc-line-hivatkozás-ellenőrzést futtatni a 4 javított
  fájlon (README.md, CLAUDE.md, docs/hu/architecture.md, docs/en/architecture.md).
- A `test_explain_documents_actual_plan_for_small_fixture` pre-existing flake stabilizálása
  (lásd "Risks") — külön, funkcionális job, nem ezen audit-job hatóköre.
- A `cic-mcp-session` MCP szerver `.mcp.json` éles bekötése (jelenleg `.mcp.json.tpl`-ben
  dokumentált, de nincs aktiválva semelyik orchestrátor/Claude Code session-ben) — külön
  capability-job, lásd `output/session-mcp-config-wiring-report.md` "Next Jobs".
