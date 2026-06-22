# CLAUDE.md — cic-mcp-session

## Mi ez a repo?

Ez a `cic-mcp-*` család **session rétege**: egyetlen beszélgetés/session-scope esemény-, idővonal-,
chunk- és provenance-tár, amit a CIC agent-ek MCP-n keresztül érnek el.

A repo a `base-repo` `mcp/main` specializációs branch-éből lett bootstrapelve (`base-repo` remote
tartósan bekötve, ld. `git remote -v` — jövőbeli `mcp/main` frissítés újra mergelhető). Az MCP
szerver infrastruktúra, a build tooling és a release folyamat innen öröklődik — de a tartalom
(amit a `source/` alá töltünk, és amit a KB épít) session-specifikus lesz, nem generikus template.

## Fő határok (a `cic-mcp-factory/factory-docs/architecture.md` szerint)

**Igen:**
- `SessionIngressEnvelope` ingest
- raw event store
- turn/timeline projection
- chunk store
- source/provenance refs
- metadata index, full-text search, vector search
- session-scope context pack
- stabil SQL/API/MCP read tools

**Nem:**
- canonical tudás
- shared memory
- cross-session graph
- végleges döntésbányászat
- human review nélküli promotion

## Trust modell

```yaml
canonical: false
promotion_allowed: false
interpreted: false   # ingress/raw szinten
default_scope: session_id
cross_session: false
```

A session réteg sosem állít elő canonical tényt. A hook csak formalizál
(`SessionIngressEnvelope`-ba), szemantikai értelmezést (decision/claim extraction) nem végez —
az queue/DB/worker oldali feldolgozás, nem ingress-szintű feladat.

## Jelenlegi állapot

`experimental` — DE a session-specifikus implementáció ~17 capability-jobon keresztül már
megépült és valódi Postgres ellen bizonyítva van (lásd `output/session-*-report.md` minden
egyes állításhoz):

- **envelope ingest + raw event store**: `session_store/envelope_writer.py:165`
  (`insert_envelope`), `:105` (`validate_envelope`) → `session_raw.envelopes`
  (`output/session-postgres-schema.sql`) — `output/session-raw-event-store-report.md`
- **valódi producer**: `hooks/log-event.py` — Claude Code hook stdin JSON-ből épít
  `SessionIngressEnvelope`-ot és hívja `insert_envelope()`-et (`hooks/log-event.py:303-304`)
  — `output/session-hook-collector-report.md`
- **worker loop** (projection + chunk-indexelés, ütemezve): `session_store/turn_projector.py:300`
  (`run_projection_batch`), `session_store/chunk_indexer.py:378` (`run_indexing_batch`),
  összefűzve `session_store/worker_loop.py:65` (`run_one_iteration`)/`:93` (`run_loop`) —
  `output/session-turn-projector-report.md`, `output/session-chunk-indexer-report.md`,
  `output/session-worker-scheduler-report.md`
- **chunk indexer**: `session_core.chunks`/`session_idx.chunk_fts`/`chunk_embeddings`
  feltöltése, `paraphrase-multilingual-MiniLM-L12-v2` embedding modell, tényleges dimenzió
  384 (lemérve, nem feltételezve) — `output/session-chunk-indexer-report.md`
- **vector/hybrid/FTS search**: `session_api.search_context()` (FTS, `'simple'`-konfiguráció),
  `search_context_vector()` (cosine, HNSW index), `search_context_hybrid()` (RRF-fúzió) —
  `output/session-retrieval-quality-report.md`, `output/session-vector-search-api-report.md`,
  `output/session-hybrid-search-api-report.md`
- **session_api réteg**: `get_timeline()`, `get_context_pack()`, `session_status()`,
  `get_source_refs()` (`output/session-postgres-schema.sql` +
  `output/session-source-refs-api-migration.sql`) — `output/session-source-refs-api-report.md`
- **7 tool-os MCP szerver**: `mcp-server/session_server.py` — `search_session_context` +
  6 további tool (`search_session_context_fts`, `search_session_context_vector`,
  `get_session_timeline`, `get_session_context_pack`, `get_session_status`,
  `get_session_source_refs`) — `output/session-mcp-tools-report.md`,
  `output/session-mcp-tools-remaining-report.md`
- **host-natív `.venv-host` indítás**: `.mcp.json.tpl` mindkét bejegyzése
  (`cic-graph`/`cic-session`) `{{REPO_ROOT}}/.venv-host/bin/python`-ot használ, NEM a Docker
  builder lapos `p_venv`-jét — `output/session-mcp-venv-fix-report.md`

A `make_source.py`/`mcp-server/server.py` (a `cic-graph` KB szerver) valóban a `base-repo`
MCP-template öröksége és session-specifikus tartalmi szempontból generikus — ez a két modul
NEM session-specifikus implementáció, hanem a KB-réteg külön komponense, lásd "MCP szerver
tool-ok" lentebb a `mcp-server/server.py`-hoz, és a `mcp-server/session_server.py`-hoz a fenti
listát.

A fennmaradó, dokumentált rés: a fenti komponensek production reachability-je `scaffold`
szintű — nincs deployolt cron/systemd ütemezés a worker-loop-hoz
(`output/session-worker-scheduler-report.md` "Risks"), és a `cic-session` MCP szerver nincs
bekötve élesben semelyik orchestrátor/Claude Code session `.mcp.json`-jába
(`output/session-mcp-config-wiring-report.md`).

## MCP szerver

A repo egy FastMCP-alapú knowledge base szervert tartalmaz, ami a `source/` könyvtár tartalmából épít kereshető tudásgráfot.

```
source/          ← ide kerülnek a repo docs + a CIC ökoszisztéma repo-k
    ↓
make kb.build    ← make_source.py: TF-IDF + cosine similarity gráf
    ↓
kb_data/pkl/     ← generált pickle fájlok (gitignore-d)
    ↓
make mcp.run     ← mcp-server/server.py (FastMCP, stdio)
```

## Kulcs parancsok

```bash
make deps           # ./p_venv létrehozása Dockerrel — flat 'pip install --target', a Docker builder
                     # service PYTHONPATH-alapú workflow-jához, NEM egy host-natívan futtatható venv
make deps.local      # ./.venv-host létrehozása host-natívan (python3 -m venv) — EZT használja
                     # a .mcp.json.tpl (Claude Code host-natív MCP szerver indítás)
make mcp.config     # .mcp.json generálás
make kb.build       # Knowledge base generálás a source/ tartalmából
make mcp.run        # MCP szerver indítás (stdio, Claude Code-hoz)
make mcp.run.sse    # MCP szerver indítás (SSE/HTTP)

make up             # Docker dev környezet (release tooling-hoz)
make validate       # Schema validáció
make release-check VERSION=x.y.z
make release-prepare VERSION=x.y.z
make release-close VERSION=x.y.z
```

## Könyvtár struktúra

```
make_source.py        ← KB generátor
mcp-server/server.py  ← FastMCP szerver (12 tool)
source/               ← forrás docs (gitkeep, a fogadó repo tölti fel)
kb_data/              ← generált KB (pkl/, json/ — gitignore-d)
sqlite_data/          ← generált SQLite (gitignore-d)
schemas.json          ← adat struktúra sémák
sqlite_data/db_schema.json ← SQLite tábla definíciók
.mcp.json             ← MCP szerver konfiguráció Claude Code-hoz
p_venv/               ← flat package-dir Docker builder PYTHONPATH-hoz (gitignore-d, NEM venv)
.venv-host/           ← host-natív venv, .mcp.json.tpl ezt indítja (gitignore-d)

tools/                ← release tooling (compiler.py, infra.py, vault signing)
docs/                 ← architektúra és koncept dokumentáció (EN + HU)
features/             ← feature specifikációk
mk/infra.mk           ← Makefile implementáció
```

## Python környezet

Az MCP szerver és a KB generátor a host-natív `.venv-host/`-ot használja (nem Docker,
nem a `p_venv/`-et — az utóbbi egy lapos `pip install --target` könyvtár a Docker
`builder` service `PYTHONPATH=/app:/app/p_venv`-alapú workflow-jához, NEM egy
host-natívan futtatható venv `bin/python`-nal):
```bash
make deps.local                  # ./.venv-host build (első lépés, repo clone után)
.venv-host/bin/python make_source.py
.venv-host/bin/python mcp-server/server.py
```

A release tooling (validate, test, fmt) Docker containerben fut.

## MCP szerver tool-ok

| Tool | Leírás |
|------|--------|
| `search_query` | Multi-token keresés TF-IDF invertált indexen |
| `search_token` | Single token lookup |
| `search_code` | Substring keresés chunk tartalomban |
| `search_nodes` | Node keresés név/label/tag alapján |
| `resolve_path` | Chunk keresés fájlút alapján |
| `get_chunk` | Chunk lekérés ID alapján |
| `get_node` | Node lekérés ID alapján |
| `neighbors` | Graph szomszédok lekérése |
| `focus_pack` | Kontextus csomag (rule prioritizálással) |
| `explain_node` | Node mély elemzése |
| `kb_status` | KB állapot és fájl info |
| `reload_kb` | KB újratöltés lemezről |

## AI Reasoning Protocol

### Boot fázis (kötelező session-indításkor)

Mielőtt szakmai kérdésre válaszolsz, futtasd le ezt a szekvenciát:

1. `kb_status` — KB artifact állapot ellenőrzése
2. Keress rá az `axioms` és `symbols` node-okra (`search_nodes`)
3. Keress rá a `limits` és `contract` fogalmakra
4. Határozd meg a runtime státuszt: **production / scaffold / concept**

A boot végén internalizáld:
- Mit tekintesz invariánsnak
- Melyek a kulcsfogalmak
- Mi runtime, mi scaffold, mi csak koncepció

**Amíg ez nem teljesült, ne válaszolj szakmai kérdésre.**

### Graph-first reasoning (ne search→snippet→answer)

Query helyett subgraph-építés:

1. Azonosítsd a fogalmat
2. Keresd meg az induló node-okat (`search_nodes`, `find_nodes`)
3. Járd be az 1–2 hop szomszédokat (`neighbors`)
4. Szűrj kapcsolattípus szerint
5. Építs lokális subgraphot
6. Csak ebből válaszolj

A chunk nem elsődleges tudáselem — csak bizonyíték. Elsődleges: **node, edge, status, provenance**.

### Háromrétegű státusz-kényszer

Minden állítás előtt kötelező meghatározni:

- `implemented` — van kód, van runtime belépési pont
- `scaffold` — van kód, de nincs éles runtime híd
- `concept` — csak dokumentáció vagy graph node, kód nincs

Ha node- vagy file-szinten nem tudod alátámasztani, **nem mondhatod ki tényként**.

### Kötelező ellenőrző lánc

Minden fontos állításhoz belső validáció:

```
fogalom → definíciós doc → companion/meta → graph kapcsolat → kód/scaffold hely → runtime belépési pont
```

Ha a lánc megszakad:
> "a modell itt létezik, de az implementációs lánc itt megszakad: [hely]"

Nem azt mondod, hogy "nincs" — hanem megnevezed, hol törik el.

### Bridge detector

Minden válasz előtt ellenőrizd a hidakat:

- `concept → code` bridge: van-e implementáció?
- `code → runtime` bridge: van-e belépési pont?
- `runtime → audit` bridge: van-e trace/log/proof?

Ha hiányzik: státusz = scaffold vagy concept, nem implemented.

### Immersion mód

Ha a feladat fogalmi megértés (nem implementáció, nem audit):

**Tilos:**
- Javaslatot tenni
- Kritikát mondani
- Hiányt feltételezni

**Csak:**
- Axiómákat felvenni
- Fogalmi szerkezetet és relációkat térképezni
- Rendszerlogikát internalizálni

### Válasz formátum (strukturált állításokhoz)

| Mező | Tartalom |
|------|----------|
| **fogalom** | mi ez |
| **mit jelent a rendszerben** | szerepe, funkciója |
| **hol él** | node ID, fájlút |
| **státusz** | implemented / scaffold / concept |
| **mihez kapcsolódik** | szomszédos node-ok, edge-típusok |
| **bizonyíték** | chunk ID vagy doc referencia |
| **nyitott híd** | hol törik el az implementációs lánc |

---

## Repo konvenciók

- Branch naming: `{component}/releases/v{VERSION}` (release), `mcp/devel` (MCP fejlesztés)
- Tag format: `{component}@v{VERSION}`
- Commit signing: Vault Transit engine (git hook)
- `project.yaml`: minden release metaadatot és kriptográfiai aláírást tartalmaz
- `md.meta.schema.yaml`: dokumentáció metadata sémája (tags, categories, used_in)

## Kapcsolódó rendszerek

- **cic-mcp-factory**: a komponens capability-jobjainak gyártó/karbantartó factory-ja
- **cic-mcp-knowledge**: canonical réteg — ide csak emberi review után, soha automatikusan nem promote-olunk
- **cic-mcp-gateway**: ez a réteg fogja a session-source-ot adapterként fogyasztani (`session.context_pack` → `GatewayContextEnvelope`)
- **CIC-Relay**: Go-alapú control plane (Nexus orchestrator, WASM)
- **CIC-Schemas**: Schema compiler és Vault signing
- **CIC-Registry**: 3-rétegű registry (schemas/mods/agents)
- **HashiCorp Vault**: Transit signing, KV v2 cert storage
