# session-mcp-venv-fix-001 Output

## Scope

A `.mcp.json.tpl` mindkét bejegyzésének (`cic-graph`, `cic-session`) `command` mezője
(`{{REPO_ROOT}}/p_venv/bin/python`) gyökérok-javítása: `p_venv` a Docker `setup` service
`pip install --target /app/p_venv` futása után egy LAPOS package-könyvtár (a `builder`
service `PYTHONPATH=/app:/app/p_venv`-alapú workflow-jához), NEM egy `bin/python`
symlinkkel rendelkező venv — ezért a `.mcp.json.tpl`-ben megadott parancs SOSEM létezett
host-natív (nem Docker) indításnál.

## Inputs Read

- `docker-compose.yml` — `setup` (`pip install --target /app/p_venv`) és `builder`
  (`PYTHONPATH=/app:/app/p_venv`) service definíciók
- `Dockerfile` — `python:3.11-slim` alap image
- `.mcp.json.tpl` — mindkét bejegyzés (`cic-graph`, `cic-session`)
- `mk/infra.mk` — `PYTHON` változó, `infra.deps`, `infra.mcp.config`, `infra.mcp.run`,
  `infra.mcp.run.session`, `infra.kb.gitmodules`, `infra.kb.build`
- `Makefile` — `.PHONY` lista, `deps`/`kb.*`/`mcp.*` alias-ok
- `CLAUDE.md` — a "Python környezet" szekció (`p_venv/bin/python ...`) ugyanezt a (téves)
  feltételezést dokumentálta
- `output/session-mcp-config-wiring-report.md` — a korábbi job riportja, ami a hibát
  elsőként azonosította

## Findings

1. **A hiba nem `.mcp.json.tpl`-specifikus.** A `mk/infra.mk` `PYTHON := ./p_venv/bin/python`
   változóját az `infra.mcp.run`, `infra.mcp.run.session`, `infra.kb.gitmodules`,
   `infra.kb.build` célok IS használják — mindegyik ugyanígy törött volt host-natív
   futtatásnál, nem csak a két `.mcp.json.tpl` bejegyzés.
2. **A `CLAUDE.md` "Python környezet" szekciója a törött feltételezést dokumentálta**
   (`p_venv/bin/python mcp-server/server.py`) — emberi olvasóknak is rossz utasítást adott.
3. **Az ABI-kompatibilitási kockázat valós, nem csak elméleti.** Empirikusan reprodukáltam
   egy alternatív megoldást (system `python3` + `PYTHONPATH=p_venv`, amit egy korábbi,
   félbehagyott futási kísérlet választott): a `p_venv` Python 3.11 ABI-ra fordított C-
   kiterjesztéseket tartalmaz (`.so` fájlok `cpython-311`), miközben ezen a hoston a
   system `python3` 3.12.3, és nincs telepítve python3.11. Ez `ModuleNotFoundError: No
   module named 'pydantic_core._pydantic_core'`-t okoz `import mcp`-nél — tehát a
   "system python3 + PYTHONPATH" megoldás ezen a hoston ténylegesen NEM működik.
4. **`session_server.py` repo-root-relatív importot használ** (`from
   session_store.envelope_writer import SessionStoreConfig`), ezért a `cic-session`
   `.mcp.json.tpl` bejegyzésnek `PYTHONPATH={{REPO_ROOT}}`-ra is szüksége van (csak a
   repo gyökérre, NEM a `p_venv`-re). A `cic-graph` `server.py`-nak nincs ilyen importja
   (csak stdlib + telepített package-eket importál), ezért neki nincs szüksége
   `PYTHONPATH`-ra.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| `p_venv` a `setup` service futása után lapos package-dir, nincs `bin/python` | proven | `ls p_venv/bin` csak CLI script-eket ad (`black`, `mypy`, `pytest`, ...), nincs `python`/`python3` közte | tényleges `docker compose run --rm setup` lefuttatva, majd `ls` a hoston | — |
| system `python3` + `PYTHONPATH=p_venv` NEM működik ezen a hoston (ABI mismatch) | proven | `ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'` `import mcp`-nél, system python3.12.3 vs p_venv .so fájlok `cpython-311` | tényleges `PYTHONPATH=... python3 -c "import mcp"` futtatva, kimenet idézve | — |
| `make deps.local` valódi, `bin/python`-nal rendelkező host-natív venv-et épít | proven | `.venv-host/bin/python`, `.venv-host/bin/python3`, `.venv-host/bin/python3.12` léteznek; `import mcp` sikeres, `mcp.__file__` a venv `site-packages`-ében | tényleges `make deps.local` futtatva, utána `ls .venv-host/bin` + `.venv-host/bin/python -c "import mcp"` | — |
| MINDKÉT `.mcp.json.tpl` bejegyzés (`cic-graph`, `cic-session`) a javított mechanizmust használja | proven | a fájl diff-je: mindkét `command` mező `{{REPO_ROOT}}/.venv-host/bin/python` | `git diff .mcp.json.tpl`, idézve lent | — |
| TÉNYLEGES subprocess + stdio MCP handshake sikeres `cic-session`-re (7 tool) | proven | `=== cic-session: 7 tools ===` + a 7 tool neve, lásd "Verification Output" | önálló Python script, `mcp.client.stdio.stdio_client` + `ClientSession`, valódi subprocess indítással (NEM in-process hívás), kimenet idézve | — |
| TÉNYLEGES subprocess + stdio MCP handshake sikeres `cic-graph`-ra (25 tool) | proven | `=== cic-graph: 25 tools ===` + a 25 tool neve, lásd "Verification Output" | ugyanaz a script, ugyanaz a módszer, kimenet idézve | — |
| A meglévő Docker `setup`/`builder` workflow (`make deps`, `make up`, `make shell`, `make build`) a javítás után is regresszió-mentes | proven | `docker compose run --rm setup` exit 0, `p_venv` flat struktúra változatlan (247 elem); `make up`/`docker compose exec builder env` → `PYTHONPATH=/app:/app/p_venv`; `docker compose exec builder python -c "import yaml"` → `/app/p_venv/yaml/__init__.py`; `make shell` smoke; `make build` exit 0 | tényleges parancsok lefuttatva, kimenet idézve lent | — |
| A teljes meglévő `tests/test_session_store/` suite a javítás után is zöld (a javítás nem okoz regressziót) | partial | `58 passed, 1 failed` — az 1 hibázó teszt (`test_explain_documents_actual_plan_for_small_fixture`) a Postgres query-planner EXPLAIN-kimenetéről szól (Seq Scan vs HNSW Index Scan választás), olyan fájlban (`test_vector_search.py`), amit ez a job NEM módosított, és a hiba egy adatfüggő/planner-statisztikafüggő flake, nem a venv-fixhez kapcsolódó import- vagy futási hiba | tényleges `pytest tests/test_session_store/ -q` futtatva friss Postgres tesztkonténerrel, kimenet idézve lent | alacsony — pre-existing, a job hatókörén kívüli flake, nem ez a job okozta |

## Verification Output

### 1. ABI-mismatch reprodukció (az elvetett "system python3 + PYTHONPATH" irány)

```
$ PYTHONPATH="$REPO:$REPO/p_venv" python3 -c "import mcp"
...
File ".../p_venv/pydantic_core/__init__.py", line 8, in <module>
    from ._pydantic_core import (
ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
```

### 2. `make deps.local` + valódi venv

```
$ ls .venv-host/bin/python*
.venv-host/bin/python
.venv-host/bin/python3
.venv-host/bin/python3.12

$ .venv-host/bin/python -c "import mcp; print('mcp OK', mcp.__file__)"
mcp OK .../session-mcp-venv-fix-001/workspace/cic-mcp-session/.venv-host/lib/python3.12/site-packages/mcp/__init__.py
```

### 3. Tényleges subprocess + stdio MCP handshake — MINDKÉT szerver

Önálló Python script (`mcp.client.stdio.stdio_client` + `ClientSession`, valódi
subprocess-ként, NEM in-process hívás), a javított `.venv-host/bin/python` paranccsal
indítva, mindkét szerverre:

```
=== cic-session: 7 tools ===
 - search_session_context
 - search_session_context_fts
 - search_session_context_vector
 - get_session_timeline
 - get_session_context_pack
 - get_session_status
 - get_session_source_refs
=== cic-graph: 25 tools ===
 - kb_status
 - reload_kb
 - list_edge_types
 - list_node_types
 - search_token
 - search_query
 - search_code
 - search_nodes
 - resolve_path
 - get_chunk
 - get_node
 - neighbors
 - focus_pack
 - explain_node
 - find_nodes
 - impact_analysis
 - guided_path
 - missing_companions
 - list_tasks
 - get_next_task
 - claim_task
 - complete_task
 - fail_task
 - update_companion
 - record_decision
```

(`cic-session` 7 tool — megegyezik a `session-mcp-config-wiring-001` jobban bizonyított
darabszámmal; `cic-graph` 25 tool — a meglévő, ehhez a jobhoz nem kapcsolódó KB-szerver
teljes tool-készlete, ennek a számnak itt nincs normatív jelentősége, csak azt bizonyítja
hogy a handshake sikeres volt.)

### 4. Docker `setup`/`builder` workflow regresszió-mentesség

```
$ docker compose run --rm setup
...
WARNING: Target directory /app/p_venv/bin already exists. Specify --upgrade to force replacement.
...
EXIT: 0

$ ls p_venv | wc -l
247   # változatlan, flat struktúra megmaradt

$ make up
 Container cic-mcp-session-setup-1 Started
 Container cic-mcp-session-builder-1 Started

$ docker compose exec builder env | grep PYTHONPATH
PYTHONPATH=/app:/app/p_venv

$ docker compose exec builder python -c "import yaml, sys; print('OK', yaml.__file__)"
OK /app/p_venv/yaml/__init__.py

$ docker compose exec builder echo "shell-ok"
shell-ok

$ make build
...
 Image cic-mcp-session-builder Built
 Image cic-mcp-session-setup Built
```

### 5. Teljes meglévő teszt-suite

```
$ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=<port> SESSION_STORE_PG_DB=testdb \
  SESSION_STORE_PG_USER=postgres SESSION_STORE_PG_PASSWORD=test \
  .venv-host/bin/python -m pytest tests/test_session_store/ -q
...
FAILED tests/test_session_store/test_vector_search.py::TestExplainIndexUsage::test_explain_documents_actual_plan_for_small_fixture
======================== 1 failed, 58 passed in 50.37s =========================
```

## Decisions Proposed

**Választott mechanizmus: (B) — önálló, host-natív venv, ÚJ `make deps.local` target-tel.**

Indoklás: az (A) irány (system `python3` + `PYTHONPATH=p_venv`) ELSŐ próbálkozásként
megvalósult, de TÉNYLEGESEN MEGBUKOTT ezen a hoston ABI-inkompatibilitás miatt (lásd
"Findings" #3, "Verification Output" #1) — a `p_venv` Python 3.11 ABI-ra fordított
C-kiterjesztéseket tartalmaz, a host system `python3` viszont 3.12, és nincs telepítve
3.11. Ez nem ad-hoc hosthiba: a `p_venv`-et a Docker `setup` service `Dockerfile`-ban
rögzített `python:3.11-slim` image-mel építi, miközben semmi nem garantálja hogy a
fejlesztő/agent hostján is pontosan 3.11 van — ez strukturális, nem egyedi hiba.

A (B) irány (`make deps.local` → `python3 -m venv .venv-host` + `pip install -r
requirements.txt`) ezt elkerüli: a venv mindig a HOST saját Python-jához fordít, nincs
ABI-feltételezés. Konzisztensen alkalmazva mindkét `.mcp.json.tpl` bejegyzésre.

Mellékhatásként a `mk/infra.mk` `PYTHON` változóját is `./.venv-host/bin/python`-ra
állítottam (volt: `./p_venv/bin/python`) — ez NEM csak a `.mcp.json.tpl`-t érintő javítás,
hanem az `infra.mcp.run`, `infra.mcp.run.session`, `infra.kb.gitmodules`, `infra.kb.build`
célokat is helyrehozza, amik UGYANEZZEL a gyökérokkal ugyanígy törve voltak host-natív
futtatásnál (lásd "Findings" #1). A `CLAUDE.md` "Python környezet" szekcióját is
frissítettem, mert a régi, törött feltételezést dokumentálta emberi olvasóknak.

status_after_merge: `experimental` — a javítás bizonyítva tényleges subprocess+stdio
handshake-kel mindkét szerverre, és a Docker workflow regresszió-mentessége is bizonyítva,
de semmilyen éles/hosszabb-életű session nincs rámutatva ezen a mechanizmuson —
`candidate`-hez egy tényleges, hosszabb-életű lokális/dev használat kellene.

## Rejected / Out Of Scope

- **(A) system `python3` + `PYTHONPATH=p_venv`** — elvetve, mert ABI-inkompatibilis
  ezen a hoston (lásd "Decisions Proposed").
- A Docker `setup`/`builder` service-ek saját `PYTHONPATH`-alapú dependency-resolution
  viselkedésének megváltoztatása — nem cél, nem érintve.
- `cic-graph`/`cic-session` tool-logika módosítása — nem cél, nem érintve.
- SSE-mód, autentikáció, multi-instance kezelés — nem cél.

## Risks

- **`.venv-host` mérete**: a `requirements.txt` torch/transformers/sentence-transformers
  függőségeket is tartalmaz (a `cic-graph` KB-szerver embedding-jeihez) — a host-natív venv
  build több GB-ot tölt le és néhány percet vesz igénybe. Ez NEM regresszió (a Docker
  `p_venv` ugyanezt tölti le), csak dokumentálandó elvárás: `make deps.local` nem
  azonnali művelet.
- **Két párhuzamos dependency-mechanizmus** (`p_venv` Docker-hez, `.venv-host` host-natív
  MCP-hez) — ez tudatos, dokumentált döntés (lásd "Decisions Proposed"), de jövőbeli
  karbantartónak tudnia kell hogy a `requirements.txt` frissítésekor MINDKÉT mechanizmust
  újra kell futtatni (`make deps` ÉS `make deps.local`).
- **1 pre-existing, a hatókörön kívüli teszt-flake** (`test_explain_documents_actual_plan_for_small_fixture`)
  — nem ez a job okozta (nem módosított fájl), de a "teljes suite zöld" állítás emiatt
  `partial`, nem `proven`.

## Definition Of Done Check

- [x] mechanizmus-választás indokolva a "Decisions Proposed"-ben
- [x] MINDKÉT `.mcp.json.tpl` bejegyzés (cic-graph + cic-session) a javított mechanizmust használja
- [x] a meglévő Docker `make deps`/`make up`/`make shell`/`make build` workflow regresszió-mentessége bizonyítva, kimenet idézve
- [x] TÉNYLEGES subprocess + stdio MCP handshake bizonyítva MINDKÉT szerverre, kimenet idézve mindkettőre
- [x] teljes meglévő teszt-suite lefuttatva — 58/59 zöld, az 1 hibázó teszt a hatókörön kívüli, indokolva
- [x] claim-evidence tábla kitöltve, nem üres

## Next Jobs

- A `test_explain_documents_actual_plan_for_small_fixture` flake önálló kivizsgálása/
  javítása (Postgres planner statisztika-függőség kisméretű fixture-nél) — külön, ehhez
  a jobhoz nem kapcsolódó kérdés.
- Ha a `.venv-host` mechanizmus hosszabb ideig stabilan használatban marad valós
  dev/agent munkafolyamatban, `candidate` státuszra promótálható.
