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

`experimental`, nincs még session-specifikus implementáció — a `make_source.py`/`mcp-server/`
scaffold a `base-repo` MCP-template öröksége, `source/` üres. Az első capability-jobok
(`session-repo-baseline-audit-001` → bootstrap, `session-ingress-envelope-contract-001`,
`session-postgres-storage-design-001`) a `cic-mcp-factory/jobs/` alól indulnak.

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
make mcp.config     # .mcp.json generálás (első lépés, repo clone után!)
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
p_venv/               ← Python venv (gitignore-d)

tools/                ← release tooling (compiler.py, infra.py, vault signing)
docs/                 ← architektúra és koncept dokumentáció (EN + HU)
features/             ← feature specifikációk
mk/infra.mk           ← Makefile implementáció
```

## Python környezet

Az MCP szerver és a KB generátor a lokális `p_venv/`-et használja (nem Docker):
```bash
p_venv/bin/python make_source.py
p_venv/bin/python mcp-server/server.py
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
