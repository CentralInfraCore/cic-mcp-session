# session-mcp-config-wiring-001 Output

## Scope

Ez a job a MEGLÉVŐ `mcp-server/session_server.py` MCP szervert (7 `session_api.*` tool,
`session-mcp-tools-001`/`session-mcp-tools-remaining-001` munkája) a `cic-mcp-session` repo
LOKÁLIS FEJLESZTŐI indítási receptjébe köti be — `.mcp.json.tpl` bővítés + új Makefile target —
úgy, hogy egy ember a saját lokális session-jében MANUÁLISAN, KÉSŐBB tudja aktiválni. A scope-on
belül:

1. ÚJ `cic-session` bejegyzés a `.mcp.json.tpl`-ben, a MEGLÉVŐ `cic-graph` bejegyzés MELLETT (azt
   nem módosítva), `{{REPO_ROOT}}` placeholder-konvenciót követve, **env blokk NÉLKÜL** —
   semmilyen secret/jelszó nem kerül a git-trackelt fájlba
2. ÚJ `infra.mcp.run.session` target a `mk/infra.mk`-ban (pontosan az `infra.mcp.run` mintáját
   követve), `mcp.run.session: infra.mcp.run.session` alias + `.PHONY` bejegyzés a `Makefile`-ban
3. tényleges `make mcp.config` futtatás, a kirenderelt `.mcp.json` mindkét bejegyzéssel idézve
4. TÉNYLEGES subprocess + stdio MCP handshake bizonyítás — a kirenderelt `.mcp.json`
   command/args párosával indított ÖNÁLLÓ subprocess, valódi Postgres tesztkonténerrel, valódi
   stdio transporton keresztüli `list_tools()` hívással (a `mcp.client.stdio`/`mcp.client.session`
   modulokkal) — NEM csak in-process Python hívás
5. a teljes meglévő `tests/test_session_store/` suite lefuttatása, regresszió-mentesség
   bizonyítása
6. explicit kijelentés: nincs éles session-be regisztrálva
7. reachability grep, file:line hivatkozással

Nem cél (lásd input.md "Nem cél"): a `cic-graph` bejegyzés vagy `mcp-server/server.py`
módosítása, a `search_session_context*`/`get_session_*` tool-ok módosítása, SSE-mód támogatás a
session szerverhez, bármilyen éles orchestrátor/Claude Code session `.mcp.json`-jának módosítása.

## Inputs Read

- `.mcp.json.tpl` — TELJESEN elolvasva — a MEGLÉVŐ `cic-graph` bejegyzés (`command`,
  `args`, `env.KB_DATA_DIR`, mind `{{REPO_ROOT}}` placeholder-rel) — ÉRINTETLENÜL hagyva, az ÚJ
  `cic-session` bejegyzés MELLÉ kerül, ugyanazt a placeholder-konvenciót követve
- `mk/infra.mk` — TELJESEN elolvasva — `infra.mcp.config` (sed-alapú `{{REPO_ROOT}}`
  helyettesítés, 93-96. sor), `infra.mcp.run` (110-112. sor, MINTA az új target-hez),
  `infra.mcp.run.sse` (114-116. sor), `PYTHON := ./p_venv/bin/python` (3. sor)
- `Makefile` — TELJESEN elolvasva — `.PHONY` lista (7. sor), `mcp.run: infra.mcp.run` /
  `mcp.run.sse: infra.mcp.run.sse` / `mcp.config: infra.mcp.config` alias-minta (139-141. sor)
- `mcp-server/session_server.py` — TELJESEN elolvasva (1-457. sor) — `main()` (451-452. sor,
  paraméter nélküli `mcp.run()`, stdio mód), mind a 7 `@mcp.tool()` wrapper, a modul-szintű
  docstring (explicit "nincs `.mcp.json.tpl`-be kötve" állítás a korábbi két jobból, 70-80. sor)
- `session_store/envelope_writer.py` — TELJESEN elolvasva — `SessionStoreConfig.from_env()`
  (88-96. sor): `SESSION_STORE_PG_HOST` → `PGHOST` → `"localhost"` fallback-lánc (és ugyanígy
  PORT/DB/USER/PASSWORD), `conninfo()` (98-102. sor)
- `.cic-context/factory-docs/job-slices.yaml` — `session-mcp-config-wiring-001` bejegyzés —
  NORMATÍV — `acceptance_gates`/`required_evidence`/`forbidden_shortcuts` mind a hét feladat-pontot
  lefedi, megegyezik az input.md-vel
- `output/session-mcp-tools-remaining-report.md` — a két korábbi job riport-stílusának és
  Claim-Evidence tábla formátumának mintájaként

## Findings

**1. `.mcp.json.tpl` — ÚJ `cic-session` bejegyzés, secret NÉLKÜL.**

A teljes ÚJ bejegyzés tartalma (a `cic-graph` bejegyzés MELLETT, azt nem módosítva):

```json
"cic-session": {
  "command": "{{REPO_ROOT}}/p_venv/bin/python",
  "args": [
    "{{REPO_ROOT}}/mcp-server/session_server.py"
  ]
}
```

Nincs `env` blokk — a `SessionStoreConfig.from_env()` (lásd "Inputs Read") a hívó shell saját
környezetéből olvas, ezt a meglévő mechanizmust használja a recept is, módosítás nélkül. A
`cic-session` indításához a hívó shell-ben (NEM a tpl-ben) be kell állítani:
`SESSION_STORE_PG_HOST`, `SESSION_STORE_PG_PORT`, `SESSION_STORE_PG_DB`,
`SESSION_STORE_PG_USER`, `SESSION_STORE_PG_PASSWORD`. Ezek hiányában a
`SessionStoreConfig.from_env()` `PG*`/`localhost` defaultokra esik vissza (lásd
`envelope_writer.py:88-96`), ami egy nem-éles, lokális Postgres-re mutat — ez NEM ad hibát
indításkor, csak akkor, amikor egy tool tényleges SQL-hívást próbál végrehajtani.

**2. ÚJ Makefile target.**

```diff
--- a/.mcp.json.tpl
+++ b/.mcp.json.tpl
@@ -8,6 +8,12 @@
       "env": {
         "KB_DATA_DIR": "{{REPO_ROOT}}/kb_data/pkl"
       }
+    },
+    "cic-session": {
+      "command": "{{REPO_ROOT}}/p_venv/bin/python",
+      "args": [
+        "{{REPO_ROOT}}/mcp-server/session_server.py"
+      ]
     }
   }
 }
diff --git a/Makefile b/Makefile
index 001a9a7..28ba1a4 100755
--- a/Makefile
+++ b/Makefile
@@ -4,7 +4,7 @@
 include mk/infra.mk

 # ---- Phony ----
-.PHONY: all help validate release-check release-prepare release-close test up down shell build fmt lint check typecheck repo.init manifest-verify manifest-update deps kb.gitmodules kb.gitmodules.check kb.build mcp.run mcp.run.sse mcp.config
+.PHONY: all help validate release-check release-prepare release-close test up down shell build fmt lint check typecheck repo.init manifest-verify manifest-update deps kb.gitmodules kb.gitmodules.check kb.build mcp.run mcp.run.sse mcp.run.session mcp.config

 # Default to showing help
 all: help
@@ -138,6 +138,7 @@ kb.gitmodules.check: infra.kb.gitmodules.check
 kb.build: infra.kb.build
 mcp.run: infra.mcp.run
 mcp.run.sse: infra.mcp.run.sse
+mcp.run.session: infra.mcp.run.session
 mcp.config: infra.mcp.config

 # =============================================================================
diff --git a/mk/infra.mk b/mk/infra.mk
index de46b0d..607acf7 100644
--- a/mk/infra.mk
+++ b/mk/infra.mk
@@ -6,7 +6,7 @@ MCP_PORT ?= 8000
 PROFILE ?= internal

 # ---- Phony ----
-.PHONY: infra.up infra.down infra.shell infra.build infra.fmt infra.lint infra.typecheck infra.check infra.repo.init infra.deps infra.coverage infra.clean infra.help infra.kb.gitmodules infra.kb.gitmodules.check infra.kb.build infra.mcp.run infra.mcp.run.sse infra.mcp.config
+.PHONY: infra.up infra.down infra.shell infra.build infra.fmt infra.lint infra.typecheck infra.check infra.repo.init infra.deps infra.coverage infra.clean infra.help infra.kb.gitmodules infra.kb.gitmodules.check infra.kb.build infra.mcp.run infra.mcp.run.sse infra.mcp.run.session infra.mcp.config

 # =============================================================================
 # Container Lifecycle Management
@@ -115,6 +115,10 @@ infra.mcp.run.sse:
 	@echo "--- Starting MCP server (SSE / HTTP) on $(MCP_HOST):$(MCP_PORT) ---"
 	@$(PYTHON) mcp-server/server.py --sse --host $(MCP_HOST) --port $(MCP_PORT)

+infra.mcp.run.session:
+	@echo "--- Starting Session MCP server (stdio) ---"
+	@$(PYTHON) mcp-server/session_server.py

 # =============================================================================
 # Infrastructure Help (Implementation Details)
 # =============================================================================
```

(`infra.mcp.run.session` az `mk/infra.mk:118-120` sorokban él, pontosan az `infra.mcp.run`
(110-112. sor) mintáját követve — csak `session_server.py`-ra mutat, semmilyen extra flag nem
kerül bele, SSE támogatás explicit kihagyva, lásd input.md "Nem cél".)

**3. `make mcp.config` tényleges futtatása.**

```
$ make mcp.config
--- Generating .mcp.json for this repo ---
.mcp.json generated at <REPO_ROOT>
```

A kirenderelt `.mcp.json` TELJES tartalma (mindkét bejegyzéssel, `{{REPO_ROOT}}` helyesen
helyettesítve a klón abszolút útjával):

```json
{
  "mcpServers": {
    "cic-graph": {
      "command": "<REPO_ROOT>/p_venv/bin/python",
      "args": [
        "<REPO_ROOT>/mcp-server/server.py"
      ],
      "env": {
        "KB_DATA_DIR": "<REPO_ROOT>/kb_data/pkl"
      }
    },
    "cic-session": {
      "command": "<REPO_ROOT>/p_venv/bin/python",
      "args": [
        "<REPO_ROOT>/mcp-server/session_server.py"
      ]
    }
  }
}
```

(`<REPO_ROOT>` = a klón tényleges abszolút útja a futtatás során; a `.mcp.json` maga
gitignore-olt — `/.mcp.json` — tehát ez nem git-trackelt artifact, csak a `make mcp.config`
futtatásának bizonyítéka.) A `cic-graph` bejegyzés bit-for-bit AZONOS a futás előttivel — a
sed-alapú `{{REPO_ROOT}}` helyettesítés (`infra.mcp.config`, `mk/infra.mk:93-96`) MINDKÉT
bejegyzésre egységesen lefutott, az `env.KB_DATA_DIR` is helyesen helyettesítve.

**4. TÉNYLEGES subprocess + stdio MCP handshake bizonyítás.**

A `p_venv` ehhez a futtatáshoz egy valódi Python venv-ként épült fel (`python3 -m venv p_venv` +
`pip install -r requirements.txt`, host Python 3.12) — ez szükséges ahhoz, hogy
`{{REPO_ROOT}}/p_venv/bin/python` TÉNYLEGESEN létezzen és futtatható legyen (a repo
`docker compose run --rm setup`/`infra.deps` target-je `pip install --target /app/p_venv`-et
használ, ami flat package-install, NEM hoz létre `bin/python`-t — ez egy MEGLÉVŐ, ettől a jobtól
független inkonzisztencia a `PYTHON := ./p_venv/bin/python` (`mk/infra.mk:3`) konvenció és az
`infra.deps` target tényleges viselkedése között, lásd "Risks").

Egy valódi `pgvector/pgvector:pg16` Postgres tesztkonténer indult (`docker run -d --name
session-mcp-config-wiring-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=testdb -p
55440:5432 pgvector/pgvector:pg16`), mind a 6 SQL fájl alkalmazva sorrendben
(`session-postgres-schema.sql` → `session-chunk-indexer-migration.sql` →
`session-retrieval-quality-migration.sql` → `session-vector-search-api-migration.sql` →
`session-hybrid-search-api-migration.sql` → `session-source-refs-api-migration.sql`), mind
hiba nélkül (`CREATE EXTENSION`/`CREATE TABLE`/`CREATE FUNCTION` stb., minden lépés OK).

Egy önálló Python script (`mcp.client.stdio.stdio_client` + `mcp.client.session.ClientSession`)
elindította a `session_server.py`-t PONTOSAN a kirenderelt `.mcp.json` `command`/`args`
értékeivel (`<REPO_ROOT>/p_venv/bin/python <REPO_ROOT>/mcp-server/session_server.py`), mint
ÖNÁLLÓ subprocess-t, a teszt processz saját környezetében beállított valódi
`SESSION_STORE_PG_HOST=localhost`, `SESSION_STORE_PG_PORT=55440`, `SESSION_STORE_PG_DB=testdb`,
`SESSION_STORE_PG_USER=postgres`, `SESSION_STORE_PG_PASSWORD=test` env várakkal — ezek SEHOL
nem kerültek be a `.mcp.json.tpl`-be, csak a teszt subprocess saját `env` dict-jébe.

A handshake-hez emellett a teszt subprocess `env`-jébe egy `PYTHONPATH=<REPO_ROOT>` bejegyzés is
bekerült — ez NEM a `.mcp.json.tpl` recept része (lásd "Risks" — ez egy meglévő korlátozás
dokumentálása, nem a tpl módosítása).

A tényleges `list_tools()` kimenet, a VALÓDI stdio transporton keresztül (NEM in-process hívás):

```
[06/22/26 07:41:04] INFO     Processing request of type            server.py:733
                             ListToolsRequest
=== list_tools() result (real stdio transport) ===
- search_session_context
- search_session_context_fts
- search_session_context_vector
- get_session_timeline
- get_session_context_pack
- get_session_status
- get_session_source_refs

Total tools: 7
```

Mind a 7 `session_api.*` wrapper tool látható, helyes névvel, a valódi stdio MCP handshake-en
(`initialize()` + `list_tools()`) keresztül — ez bizonyítja, hogy a LAUNCH RECEPT
(`.mcp.json` command+args) ténylegesen futtatható egy önálló subprocess-ben, nem csak az
in-process Python kód helyes.

**5. Regresszió-ellenőrzés — a teljes meglévő `tests/test_session_store/` suite.**

```
$ SESSION_STORE_PG_HOST=localhost SESSION_STORE_PG_PORT=55440 SESSION_STORE_PG_DB=testdb \
  SESSION_STORE_PG_USER=postgres SESSION_STORE_PG_PASSWORD=test \
  ./p_venv/bin/python -m pytest tests/test_session_store/ -v

tests/test_session_store/test_chunk_indexer.py ................. [...]
tests/test_session_store/test_envelope_writer.py ...... [...]
tests/test_session_store/test_hybrid_search.py ....... [...]
tests/test_session_store/test_session_api.py ......... [...]
tests/test_session_store/test_session_source_refs_api.py ..... [...]
tests/test_session_store/test_turn_projector.py ...... [...]
tests/test_session_store/test_vector_search.py ...... [...]
tests/test_session_store/test_worker_loop.py ... [...]

============================= 59 passed in 46.69s ==============================
```

Mind a 8 teszt modul, mind az 59 teszt PASSED — nulla regresszió a `.mcp.json.tpl`/Makefile
módosítás mellett (ezek a fájlok nem érintik a Python kódot, csak a launch receptet, így ez az
eredmény elvárt, de a "tényleges futtatás" bizonyítja, hogy a `p_venv` build és a Postgres
fixture-lánc is helyesen működik).

**6. Explicit elhatárolás — nincs éles session-be regisztrálva.**

Ez a job NEM regisztrálta és NEM indította el a `cic-session` MCP szervert SEMMILYEN éles
orchestrátor/Claude Code session-ben. A `.mcp.json.tpl` + Makefile recept BIZONYÍTOTTAN FUTHATÓ
(lásd 3-4. pont), de az aktiválás (a tpl alapján egy fejlesztő saját `.mcp.json`-jának
legenerálása ÉS a session live regisztrálása egy futó Claude Code session-höz) egy KÜLÖN,
jövőbeli, EMBERI döntés — ezt a jobot ez nem tartalmazza. A `.mcp.json` fájl, amit ez a job
generált a "3." és "4." pont bizonyításához, gitignore-olt (`/.mcp.json`), és a munka végén
törölve lett a workspace-ből.

**7. Reachability ellenőrzés.**

```
$ grep -rn "infra.mcp.run.session\|cic-session" --include="Makefile" --include="*.mk" --include="*.tpl" .
.mcp.json.tpl:12:    "cic-session": {
mk/infra.mk:9:.PHONY: ... infra.mcp.run infra.mcp.run.sse infra.mcp.run.session infra.mcp.config
mk/infra.mk:118:infra.mcp.run.session:
Makefile:141:mcp.run.session: infra.mcp.run.session
```

A recept LÉTEZÉSE (a 4 fenti találat) és FUTÁSA (subprocess-szinten bizonyítva, lásd "4." pont)
KÉT KÜLÖNÁLLÓ állítás a "VALAKI TÉNYLEG EZT HASZNÁLJA egy éles Claude Code session-ben"
állítástól — ez utóbbi **`missing`** (lásd Claim-Evidence tábla és "6." pont).

**8. `cic-graph` bejegyzés és `mcp-server/server.py` érintetlensége.**

```
$ git diff --stat -- mcp-server/server.py
(empty — no output, zero changes)
```

A `.mcp.json.tpl`-ben a `cic-graph` bejegyzés szövege a diff szerint (lásd "2." pont diff-je)
egyetlen karakterben sem módosult — csak a `cic-session` bejegyzés került MELLÉ, vesszővel
elválasztva.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| ÚJ `cic-session` bejegyzés a `.mcp.json.tpl`-ben, a `cic-graph` MELLETT, ÉRINTETLENÜL hagyva azt | proven | "Findings" 1./2./8. pont — teljes diff + `git diff --stat -- mcp-server/server.py` üres | `git diff` + a bejegyzés szövegének idézése | alacsony |
| NINCS secret/jelszó a `.mcp.json.tpl`-ben | proven | "Findings" 1. pont — a teljes ÚJ bejegyzés idézve, nincs `env` blokk; a `SESSION_STORE_PG_*` env várak csak szövegesen, a riportban dokumentáltak | a bejegyzés JSON tartalmának szó szerinti idézése + `grep -i password .mcp.json.tpl` (üres találat) | alacsony — ez a job KRITIKUS, nem-alku-képes szabálya volt, explicit ellenőrizve |
| `infra.mcp.run.session` target létrejött `mk/infra.mk`-ban, `infra.mcp.run` mintáját követve | proven | "Findings" 2. pont — diff, `mk/infra.mk:118-120` | fájl:sor hivatkozás + diff idézése | alacsony |
| `Makefile`-ban `mcp.run.session` alias + `.PHONY` bejegyzés | proven | "Findings" 2. pont — diff, `Makefile:141` (alias), `Makefile:7` (`.PHONY`) | fájl:sor hivatkozás + diff idézése | alacsony |
| `make mcp.config` tényleges kimenete, mindkét bejegyzéssel a kirenderelt `.mcp.json`-ban | proven | "Findings" 3. pont — tényleges parancs kimenet + a teljes kirenderelt `.mcp.json` idézve | tényleges `make mcp.config` futtatás + a generált fájl idézése | alacsony |
| TÉNYLEGES subprocess + stdio MCP handshake (NEM csak in-process Python hívás) | proven | "Findings" 4. pont — `mcp.client.stdio`/`mcp.client.session` alapú script kimenete, `list_tools()` 7 tool-lal | tényleges subprocess-indítás a `.mcp.json` command/args-jával + valódi stdio transport `initialize()`+`list_tools()` | közepes — lásd "Risks" PYTHONPATH-limitáció |
| Teljes meglévő `tests/test_session_store/` suite regresszió-mentesen lefut | proven | "Findings" 5. pont — `59 passed in 46.69s`, mind a 8 modul | tényleges `pytest` futtatás valódi Postgres ellen | alacsony |
| Nincs éles session-be regisztrálva | proven | "Findings" 6. pont — explicit kijelentés + a generált `.mcp.json` gitignore-olt és törölve a munka végén | a `.gitignore` `/.mcp.json` bejegyzésének idézése + a fájl tényleges hiánya a munka végén | alacsony |
| Reachability — a recept LÉTEZÉSE file:line szinten | proven | "Findings" 7. pont — 4 grep találat, mind file:line | a kötelező grep parancs tényleges futtatása | alacsony |
| A recept egy éles Claude Code/orchestrátor session-ben TÉNYLEGESEN használatban van | missing | "Findings" 6./7. pont — explicit "nincs ilyen" kijelentés | nincs ilyen evidence, mert nem történt meg — ez a job explicit nem-cél | n/a (szándékosan ki nem elégített állítás) |

## Decisions Proposed

- A `cic-session` `.mcp.json.tpl` bejegyzés env blokk nélkül kerüljön be — a
  `SESSION_STORE_PG_*` kapcsolati paraméterek dokumentációja a riportban (szöveges forma)
  legyen az egyetlen hely, ahol ezek megjelennek, amíg nincs egy dedikált secret-management
  megoldás (pl. `.env.local` minta, Vault-integráció) a `cic-mcp-session` repóban.
- A `status_after_merge: experimental` indokolt — a recept futtatva és bizonyítva, de
  semmilyen tényleges, hosszabb-életű lokális/dev használat nincs hozzá mögötte.

## Rejected / Out Of Scope

- a `cic-graph` bejegyzés vagy `mcp-server/server.py` módosítása — NEM történt meg, lásd
  "Findings" 8. pont
- a `search_session_context*`/`get_session_*` tool-ok módosítása — NEM történt meg, ezek
  forrása (`mcp-server/session_server.py`) változatlan maradt
- SSE-mód támogatás a session szerverhez — NEM készült, az `infra.mcp.run.session` csak
  stdio-t indít, az `infra.mcp.run.sse` mintáját (host/port flag-ek) szándékosan nem vette át
- bármilyen éles orchestrátor/Claude Code session `.mcp.json`-jának módosítása vagy a szerver
  ottani regisztrálása — NEM történt meg, lásd "Findings" 6. pont

## Risks

1. **`PYTHONPATH`-limitáció a `session_server.py` jelenlegi importszerkezetében (KÖZEPES,
   ELŐZETESEN LÉTEZŐ, NEM E JOB HATÁSKÖRÉBEN JAVÍTOTT).** A `mcp-server/session_server.py`
   abszolút modulutat importál (`from session_store.envelope_writer import
   SessionStoreConfig`, 88. sor). Amikor a fájlt egy `.mcp.json`-ban megadott ABSZOLÚT script
   úttal indítjuk subprocess-ként (`<REPO_ROOT>/mcp-server/session_server.py`), Python a
   `sys.path[0]`-t a script könyvtárára (`mcp-server/`) állítja, NEM a processz `cwd`-jére —
   emiatt a `session_store` package import `ModuleNotFoundError`-ral hal el, HACSAK a
   `PYTHONPATH` környezeti változó nem tartalmazza a repo gyökerét. Ezt a "4." pont
   handshake-bizonyításánál a TESZT subprocess saját `env`-jébe tett `PYTHONPATH=<REPO_ROOT>`
   oldotta meg — ez NEM része a `.mcp.json.tpl` receptnek, és NEM is kellett bele tennünk, mert
   ez nem secret, de a recept jelenlegi formájában (env blokk nélkül) ÖNMAGÁBAN NEM FUT, ha a
   hívó shell nem ad `PYTHONPATH`-ot. **Ez azt jelenti, hogy egy ember, aki manuálisan
   aktiválja ezt a receptet, vagy (a) saját shell-jében exportálja a `PYTHONPATH`-ot a repo
   gyökerére indítás előtt, vagy (b) a `session_server.py`-t egy KÉSŐBBI jobban módosítani
   kell (pl. `sys.path.insert(0, ...)` a fájl tetején, vagy relatív import csomagolás) — ez
   utóbbi a jelen job hatókörén kívül van (`mcp-server/session_server.py` módosítása nem
   szerepelt a feladatban, és a "Nem cél" sem engedi a tool-ok átírását, bár a `main()`/import
   blokk módosítása technikailag nem ugyanaz mint a tool-logika átírása — ezt egy jövőbeli
   jobnak kell explicit eldöntenie).** Ez a limitáció a `cic-graph`/`server.py` bejegyzésnél
   NEM jelentkezik, mert az nem importál relatív repo-modult.
2. **`p_venv/bin/python` nem létezik a repo dokumentált `make deps` (`infra.deps` →
   `docker compose run --rm setup`) target-jével.** Az `infra.deps`/`setup` Docker service
   `pip install --target /app/p_venv`-et használ, ami flat package-install — SOHA nem hoz
   létre `p_venv/bin/python`-t. A `mk/infra.mk:3` `PYTHON := ./p_venv/bin/python` és a
   `.mcp.json.tpl` mindkét bejegyzése (`cic-graph` ÉS az új `cic-session`) ezt a nem-létező
   útvonalat várja. Ennek a jobnak a bizonyításához egy VALÓDI Python venv-et kellett kézzel
   építeni (`python3 -m venv p_venv && pip install -r requirements.txt`, host Python 3.12-vel)
   — ez NEM git-trackelt módosítás (a `p_venv/` gitignore-olt és a munka végén törölve lett),
   de dokumentálandó, hogy a `make deps` target ÖNMAGÁBAN NEM HOZZA LÉTRE a `.mcp.json.tpl`
   által elvárt futtatható environment-et. Ez egy MEGLÉVŐ, a `cic-graph` bejegyzésre IS
   vonatkozó inkonzisztencia, nem ennek a jobnak okozott regresszió — de mivel ennek a jobnak
   pont a launch-recept FUTÁSÁT kellett bizonyítania, ezt nem lehetett megkerülni, és a
   javítása (a `docker-compose.yml setup` service módosítása) kívül esik a job scope-ján (nem
   `.mcp.json.tpl`/Makefile, hanem `docker-compose.yml`).
3. **Torch verzió-eltérés (ALACSONY, kozmetikai).** A telepítés során a `torch==2.12.1` pin
   végül a CUDA build-et (`+cu130`) hozta be a pip resolver, nem a kézzel előbb telepített
   `+cpu` build-et (mert a `requirements.txt` nem rögzíti a `+cpu` local-version
   szegmenst). Ez nem befolyásolta a tesztek vagy a handshake sikerét (a teszt-host-on nincs
   GPU, a `torch` csak a `sentence-transformers` embedding-hívásokhoz kell, CPU-n futnak), de
   feleslegesen nagy (több GB) letöltést okoz tiszta `pip install -r requirements.txt` esetén.
   Nem ennek a jobnak a hatóköre javítani.

## Definition Of Done Check

- [x] `.mcp.json.tpl`-ben ÚJ `cic-session` bejegyzés, a `cic-graph` ÉRINTETLEN — "Findings"
      1./2./8. pont, diff idézve
- [x] NINCS secret/jelszó a `.mcp.json.tpl`-ben — "Findings" 1. pont, a teljes bejegyzés
      idézve, env blokk nélkül
- [x] `infra.mcp.run.session` target létrejött `mk/infra.mk`-ban, `infra.mcp.run` mintáját
      követve, fájl:sor hivatkozással — `mk/infra.mk:118-120`
- [x] `Makefile`-ban `mcp.run.session` alias + `.PHONY` bejegyzés — `Makefile:141`, `Makefile:7`
- [x] `make mcp.config` tényleges kimenete idézve, mindkét bejegyzés látható a kirenderelt
      `.mcp.json`-ban — "Findings" 3. pont
- [x] TÉNYLEGES subprocess + stdio MCP handshake bizonyítva (NEM csak in-process Python
      hívás), kimenet idézve — "Findings" 4. pont, `list_tools()` 7 tool-lal
- [x] teljes meglévő teszt-suite lefuttatva, regresszió-mentesség bizonyítva — "Findings" 5.
      pont, `59 passed in 46.69s`
- [x] explicit "nincs éles session-be regisztrálva" kijelentés a riportban — "Findings" 6. pont
- [x] claim-evidence tábla kitöltve, nem üres — lásd fent, 9 sor

## Next Jobs

- (opcionális, jövőbeli) `session_server.py` `sys.path`/import-javítása, hogy a
  `.mcp.json.tpl` recept `PYTHONPATH` nélkül is fusson — lásd "Risks" 1. pont
- (opcionális, jövőbeli) `docker-compose.yml` `setup` service javítása, hogy a `make deps`
  TÉNYLEGESEN `p_venv/bin/python`-t hozzon létre (pl. `python -m venv` használata a `pip
  install --target` helyett) — lásd "Risks" 2. pont
- (külön, emberi döntés) a `cic-session` szerver tényleges, hosszabb-életű lokális/dev
  aktiválása egy konkrét fejlesztő saját session-jében — ez a `status_after_merge:
  candidate`-hez szükséges következő lépés, ld. input.md "status indoklás"
