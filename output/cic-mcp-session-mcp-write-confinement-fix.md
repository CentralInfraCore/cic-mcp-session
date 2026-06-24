# cic-mcp-session-mcp-write-confinement-fix-001 Output

## Scope

Ez a job a `cic-mcp-session` repóban zárja a `mcp-server/server.py` `update_companion()` és
`record_decision()` MCP tool-jaiban talált path-traversal / write-confinement hibát: mindkét
függvény egy MCP-klienstől kapott `file_path`/`companion_path` paramétert ABSZOLÚT útvonalként
fogadott el, minden `SOURCE_DIR`-en-belüliség-ellenőrzés nélkül, majd `p.open("w")`-vel írt rá —
egy MCP-kliens tetszőleges, a futó processz által írható fájlt felülírhatott a hoszton.

A jobba bundle-ölve egy KÜLÖN, alacsony kockázatú drift-javítás is bekerült: `project.yaml`
`metadata.name: base` → `metadata.name: cic-mcp-session`.

A `mcp-server/server.py` egyéb funkciói (search/focus_pack/stb.) és a `claim_task`/
`complete_task`/`fail_task` tool-ok NEM kerültek módosításra — ez utóbbi hármat csak grep-pel
ellenőriztük (lásd "Findings").

## Inputs Read

- `jobs/cic-mcp-session-mcp-write-confinement-fix-001/input.md` (teljes job spec)
- `mcp-server/server.py` — teljes fájl, kiemelten: `SOURCE_DIR` (1167. sor), `update_companion()`
  (eredetileg 1486-1556. sor), `record_decision()` (eredetileg 1560-1637. sor)
- `project.yaml` — `metadata.name: base` (1-3. sor)
- `tests/test_tools/test_mcp_server.py` — meglévő teszt-konvenció (`import server as mcp_server`,
  `sys.path` illesztés `mcp-server/`-re, `patch.object(mcp_server, "load_kb", ...)` minta)
- `CLAUDE.md` (repo root) — Python env (`.venv-host`), MCP szerver tool-tábla

## Vulnerability Reproduction (Before Fix)

Grep megerősítés (a két érintett függvény, teszt-fájlok kizárva):

```
$ grep -rn "def update_companion\|def record_decision" --include="*.py" mcp-server/ | grep -v test_
mcp-server/server.py:1486:def update_companion(
mcp-server/server.py:1560:def record_decision(
```

Saját, futtatott reprodukció a JAVÍTÁS ELŐTTI kódon (a `cic-mcp-session/.venv-host`-tal,
`git stash`-elt módosítások mellett futtatva):

**`update_companion()` — tényleges write SOURCE_DIR-en kívül:**

```
SOURCE_DIR: .../cic-mcp-session/source
target exists before: False
seeded victim file outside SOURCE_DIR at /tmp/outside_source_dir_poc.yaml
update_companion result: {'success': True, 'path': '/tmp/outside_source_dir_poc.yaml',
  'updated_fields': ['description', 'tags'],
  'message': 'Updated 2 field(s). Commit to trigger Vault Transit signing.'}
target exists after: True
--- target contents after call ---
category: []
used_in: []
related_nodes: []
tags:
- poc
description: PWNED BY MCP CLIENT - PATH TRAVERSAL POC
```

**`record_decision()` — tényleges write SOURCE_DIR-en kívül** (`load_kb()` mockolva, mert a
függvény mindig hívja, akkor is, ha `companion_path` explicit megadott — ez maga is megfigyelt
mellékhatás, de nem ennek a jobnak a hatóköre):

```
target exists before: False
seeded victim file outside SOURCE_DIR at /tmp/outside_source_dir_poc2.yaml
record_decision result: {'success': True, 'path': '/tmp/outside_source_dir_poc2.yaml',
  'message': 'Decision recorded in agent_decisions[0]. Commit to persist.'}
--- target contents after record_decision call ---
agent_decisions:
- node_id: n1
  decision: PWNED record_decision PATH TRAVERSAL POC
  timestamp: '2026-06-24T17:34:40.484683+00:00'
```

Mindkét esetben a célfájl a `SOURCE_DIR`-en (`.../cic-mcp-session/source`) KÍVÜL volt
(`/tmp/...`), és a hívás TÉNYLEGESEN írt rá — nem csak megkísérelte. A sebezhetőség
LÉTEZŐKÉNT bizonyítva, javítás előtt.

## Confinement Check Implementation

Új helper: `_resolve_within_source_dir(file_path: str) -> Path`, `mcp-server/server.py`
`SOURCE_DIR` definíciója után, a `_COMPANION_LANGS` elé:

- a path-ot UGYANÚGY építi fel, mint a régi kód (abszolút marad abszolút, relatív
  `SOURCE_DIR`-hez illesztve)
- `.resolve()`-ja MINDKÉT oldalt: a kapott path-ot ÉS `SOURCE_DIR`-t (symlink-eket felold,
  `..`-szegmenseket összevon)
- `Path.is_relative_to()`-val ellenőrzi a tényleges, feloldott containment-et — NEM
  string-prefix-szel
- ha a feloldott path NEM `SOURCE_DIR`-en belül van, `ValueError`-t dob

Bevezetve MINDKÉT helyre:
- `update_companion()` (`mcp-server/server.py` ~1538-1542. sor): a path-felépítés UTÁN, a
  `p.exists()`/`p.open()` ELŐTT; `ValueError` esetén
  `{"success": False, "message": "path escapes SOURCE_DIR, refused"}`, írás/olvasás
  megkísérlése NÉLKÜL.
- `record_decision()` (`mcp-server/server.py` ~1614-1638. sor): MINDKÉT path-forrásra —
  az explicit `companion_path` ágra ÉS a `node_id`-ből levezetett `candidate`/`candidate2`
  ágra is (ez utóbbi a node metadata-ból (`source_file`/`file_path`) épít path-ot, ami
  ugyanúgy lehetne escape-elő, ezért ugyanazt a helper-t kapja).

`claim_task`/`complete_task`/`fail_task` változatlan — lásd "Findings", grep-pel megerősítve,
hogy nem vesznek át kliens-megadott abszolút path-ot.

## Real Test Proof — Rejection AND No-Regression

Új teszt-osztályok a `tests/test_tools/test_mcp_server.py`-ban (a meglévő `import server as
mcp_server` mintát követve): `TestWriteConfinement` (8 teszt) és `TestResolveWithinSourceDir`
(2 teszt). Mindkét érintett függvényre MINDKÉT eset (rejection + no-regression), plusz egy
symlink-escape unit teszt a helper-en (ezt pont egy str-prefix check NEM venné észre).

Tényleges, futtatott pytest kimenet (`cic-mcp-session/.venv-host/bin/python -m pytest
tests/test_tools/test_mcp_server.py::TestWriteConfinement
tests/test_tools/test_mcp_server.py::TestResolveWithinSourceDir -v --no-cov`):

```
collected 10 items

tests/test_tools/test_mcp_server.py::TestWriteConfinement::test_update_companion_rejects_path_outside_source_dir PASSED [ 10%]
tests/test_tools/test_mcp_server.py::TestWriteConfinement::test_update_companion_rejects_traversal_relative_path PASSED [ 20%]
tests/test_tools/test_mcp_server.py::TestWriteConfinement::test_update_companion_legit_write_inside_source_dir_still_works PASSED [ 30%]
tests/test_tools/test_mcp_server.py::TestWriteConfinement::test_update_companion_legit_write_with_absolute_path_inside_source_dir PASSED [ 40%]
tests/test_tools/test_mcp_server.py::TestWriteConfinement::test_record_decision_rejects_path_outside_source_dir PASSED [ 50%]
tests/test_tools/test_mcp_server.py::TestWriteConfinement::test_record_decision_rejects_traversal_relative_path PASSED [ 60%]
tests/test_tools/test_mcp_server.py::TestWriteConfinement::test_record_decision_legit_write_inside_source_dir_still_works PASSED [ 70%]
tests/test_tools/test_mcp_server.py::TestWriteConfinement::test_record_decision_legit_write_with_absolute_path_inside_source_dir PASSED [ 80%]
tests/test_tools/test_mcp_server.py::TestResolveWithinSourceDir::test_rejects_symlink_escape PASSED [ 90%]
tests/test_tools/test_mcp_server.py::TestResolveWithinSourceDir::test_accepts_path_inside_source_dir PASSED [100%]

============================= 10 passed in 10.31s ==============================
```

Teljes regresszió-futás a fájlra (a teljes `test_mcp_server.py`, 23 teszt):

```
22 passed, 1 failed in 10.99s
```

Az 1 hibás teszt (`TestSearchQuerySemantic::test_result_has_required_fields`) NEM ehhez a
job-hoz tartozik — egy `file_path` vs. `file_paths` kulcsnév-drift a `search_query()`
függvényben, amit ez a job NEM módosított. Megerősítve: a hiba a JAVÍTÁS ELŐTTI,
`git stash`-elt baseline kódon is ugyanúgy elbukik (azonos assertion error). Lásd "Rejected /
Out Of Scope".

## project.yaml Fix

```diff
 metadata:
-  name: base
+  name: cic-mcp-session
   description: Schema Compiler & Signing Infrastructure Template
```

Egyetlen mező módosult (`git diff project.yaml` ellenőrizve) — `description`/`tags`/`version`/
`license`/`owner`/`validatedBy` érintetlen, ahogy a spec előírja. (A `compiler_settings.
component_name: base` mező — egy másik, nem `metadata.name` alatti mező — szándékosan
ÉRINTETLEN maradt, mert a spec kizárólag `metadata.name`-et jelöli ki hatókörként.)

## Findings

- A sebezhetőség MINDKÉT függvényben (`update_companion`, `record_decision`) valós volt: a
  fix előtt mindkettő tényleges write-ot végzett egy `SOURCE_DIR`-en kívüli, attacker-választott
  abszolút path-ra.
- `record_decision()` egy mellékes észrevétel: mindig hívja `load_kb()`-t, akkor is, ha
  `companion_path` explicit megadott (a `node_id`-alapú lookup nem szükséges ebben az esetben).
  Ez NEM biztonsági hiba, csak egy felesleges KB-betöltés — ki van véve a hatókörből (lásd
  "Rejected / Out Of Scope").
- `claim_task`/`complete_task`/`fail_task` biztonsága megerősítve grep-pel:
  ```
  $ grep -n "^def claim_task\|^def complete_task\|^def fail_task" -A 3 mcp-server/server.py
  1426:def claim_task(task_id: str, repo: str = "") -> dict:
  1445:def complete_task(task_id: str, repo: str = "", result_note: str = "") -> dict:
  1466:def fail_task(task_id: str, reason: str, repo: str = "") -> dict:
  ```
  Mindhárom szignatúrája `task_id`/`repo`/`reason`/`result_note` — NINCS kliens-megadott
  `file_path`/`path` paraméter. A path-feloldás belső, `_find_promptmaps()`-on keresztül megy
  (env var `PROMPTMAP_PATHS` VAGY `SOURCE_DIR.rglob("PROMPTMAP.yaml")`), amit a kliens nem tud
  közvetlenül felülírni hívás-paraméterrel. Megerősítve, NEM módosítva.
- A `record_decision()` node_id-alapú ágában (`candidate`/`candidate2`, a node metadata
  `source_file`/`file_path` mezőjéből épülő path) is alkalmaztuk a confinement-checket, mert
  ez is escape-elhetne, ha a node metadata sérült/manipulált lenne — a spec "a path-felépítés
  UTÁN, a `p.open()` ELŐTT" szabálya ezt is lefedi, nem csak az explicit `companion_path` ágat.

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| `update_companion()`/`record_decision()` a fix előtt SOURCE_DIR-en kívülre írtak | proven | `output/cic-mcp-session-mcp-write-confinement-fix.md` "Vulnerability Reproduction" szekció, tényleges pre-fix futási kimenet | manuális Python hívás, `git stash`-elt pre-fix kódon, fájltartalom ellenőrzéssel | n/a (ez a kiinduló állapot leírása) |
| `_resolve_within_source_dir()` `Path.resolve()` + `Path.is_relative_to()` alapú, NEM string-prefix | proven | `mcp-server/server.py:1170-1197` (helper kód) + `TestResolveWithinSourceDir::test_rejects_symlink_escape` (symlink-escape teszt, amit egy str-prefix check nem fogna meg) | kód olvasás + futtatott pytest | low |
| `update_companion()` elutasítja a SOURCE_DIR-en kívüli path-ot, fájl nem módosul | proven | `TestWriteConfinement::test_update_companion_rejects_path_outside_source_dir`, `test_update_companion_rejects_traversal_relative_path` — PASSED | futtatott pytest, fájltartalom before/after összehasonlítás | low |
| `update_companion()` legitim, SOURCE_DIR-en belüli írás továbbra is működik (no regression) | proven | `TestWriteConfinement::test_update_companion_legit_write_inside_source_dir_still_works`, `..._with_absolute_path_inside_source_dir` — PASSED | futtatott pytest | low |
| `record_decision()` elutasítja a SOURCE_DIR-en kívüli path-ot, fájl nem módosul | proven | `TestWriteConfinement::test_record_decision_rejects_path_outside_source_dir`, `test_record_decision_rejects_traversal_relative_path` — PASSED | futtatott pytest | low |
| `record_decision()` legitim, SOURCE_DIR-en belüli írás továbbra is működik (no regression) | proven | `TestWriteConfinement::test_record_decision_legit_write_inside_source_dir_still_works`, `..._with_absolute_path_inside_source_dir` — PASSED | futtatott pytest | low |
| `claim_task`/`complete_task`/`fail_task` már biztonságos, nem kliens-path-alapú | proven | grep kimenet a "Findings" szekcióban, szignatúra-ellenőrzés | grep + kód olvasás | low |
| `project.yaml` `metadata.name` javítva, más mező érintetlen | proven | `git diff project.yaml` — egyetlen sor változott | `git diff` futtatva | low |
| A `test_result_has_required_fields` hiba ehhez a jobhoz NEM kapcsolódik | proven | azonos assertion error a `git stash`-elt pre-fix baseline-on is | futtatott pytest a baseline-on, összehasonlítva | low |

## Decisions Proposed

- A `_resolve_within_source_dir()` helper-t a `record_decision()` node_id-alapú ágára is
  alkalmazni kell, nem csak az explicit `companion_path`-ra — mert a spec "a path-felépítés
  UTÁN, a `p.open()` ELŐTT" szabálya mindkét ágra vonatkozik, és a node metadata-ból épülő
  path elméletileg ugyanúgy escape-elhetne.
- A `record_decision()` mindig hívja `load_kb()`-t még akkor is, ha `companion_path` explicit
  meg van adva — ez egy KÜLÖN, NEM biztonsági, hatékonysági javítás-jelölt, amit egy KÜLÖN
  jobnak kellene kezelnie (ki van zárva ebből a hatókörből).

## Rejected / Out Of Scope

- `claim_task`/`complete_task`/`fail_task` módosítása — a spec szerint MÁR biztonságos, csak
  grep-pel megerősítendő, NEM módosítandó. Nem módosítottuk.
- `project.yaml` `description`/`tags`/`version`/`license`/`owner`/`validatedBy` mezőinek
  javítása — KIZÁRÓLAG `metadata.name` a hatókör. Nem módosítottuk a többi mezőt.
- A másik 3 érintett repó (`cic-mcp-knowledge`/`cic-mcp-shared`/`cic-mcp-gateway`) javítása —
  KÜLÖN, párhuzamos jobok feladata. Nem nyúltunk hozzájuk.
- A generikus KB-szerver egyéb funkcióinak (search/focus_pack/stb.) módosítása — nem volt
  a hatókörben, nem módosítottuk.
- `TestSearchQuerySemantic::test_result_has_required_fields` javítása — pre-existing,
  független drift (`file_path` vs `file_paths` kulcsnév), nem ehhez a jobhoz tartozik;
  megerősítve hogy a baseline kódon is elbukik.
- `record_decision()` felesleges `load_kb()` hívásának optimalizálása explicit
  `companion_path` esetén — funkcionálisan helyes (csak felesleges), nem biztonsági hiba,
  külön jobnak hagyva.

## Risks

- A `_resolve_within_source_dir()` minden hívásnál `SOURCE_DIR.resolve()`-t futtat — ez egy
  fájlrendszer-stat-szintű, elhanyagolható overhead, NEM teljesítmény-kockázat ezen a
  hívási gyakoriságon (companion-írás, nem hot-path).
  
- A hibaüzenet (`"path escapes SOURCE_DIR, refused"`) NEM ad vissza a kliensnek a feloldott
  abszolút path-ot a `update_companion`/`record_decision` válaszban (csak a `ValueError`
  internal message-ben, amit elnyelünk) — ez SZÁNDÉKOS, hogy ne szivárogtassunk ki
  hoszt-fájlrendszer-struktúra infót egy elutasított, potenciálisan rosszindulatú hívás
  válaszában.

- A `record_decision()` node_id-alapú ágában a `candidate`/`candidate2` most már
  `_resolve_within_source_dir()`-en megy át, ami `ValueError`-t dobhat akkor is, ha a node
  metadata legitim, de valamiért (symlink, stale adat) SOURCE_DIR-en kívülre mutatna — ez egy
  SZÁNDÉKOS, biztonság-első viselkedés (fail closed), de elméletileg egy korábban "működő"
  edge-case-t most elutasíthat. Ez összhangban van a job céljával (write-confinement), nem
  regresszió a hatókörön belül.

## Definition Of Done Check

- [x] a sebezhetőség REPRODUKÁLVA a javítás ELŐTT, TÉNYLEGES kimenettel — lásd
      "Vulnerability Reproduction (Before Fix)"
- [x] `_resolve_within_source_dir()` implementálva, file:line hivatkozással —
      `mcp-server/server.py:1170-1197` (a `SOURCE_DIR` definíció utáni új blokk)
- [x] MINDKÉT érintett függvény javítva — `update_companion()` (~1538-1542. sor),
      `record_decision()` (~1614-1638. sor)
- [x] valós teszt: path-traversal ELUTASÍTVA ÉS legitim eset TOVÁBBRA IS működik, MINDKÉT
      függvényre, TÉNYLEGES pytest kimenettel — lásd "Real Test Proof", 10/10 PASSED
- [x] `claim_task`/`complete_task`/`fail_task` biztonsága megerősítve grep-pel (NEM módosítva)
      — lásd "Findings"
- [x] `project.yaml` `metadata.name` javítva, más mező érintetlen — lásd "project.yaml Fix",
      `git diff` ellenőrizve
- [x] claim-evidence tábla kitöltve, nem üres — lásd fentebb, 9 sor

## Next Jobs

- `cic-mcp-knowledge-mcp-write-confinement-fix-001` — azonos logika a `cic-mcp-knowledge`
  repóban (párhuzamos job, már külön futtatva)
- `cic-mcp-shared-mcp-write-confinement-fix-001` — azonos logika a `cic-mcp-shared` repóban
- `cic-mcp-gateway-mcp-write-confinement-fix-001` — azonos logika a `cic-mcp-gateway` repóban
- (opcionális, KÜLÖN job) `record_decision()` felesleges `load_kb()` hívásának kiváltása
  explicit `companion_path` esetén — hatékonysági, nem biztonsági javítás
