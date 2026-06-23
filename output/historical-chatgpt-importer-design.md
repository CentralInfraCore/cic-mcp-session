# historical-chatgpt-export-importer-001 Output

## Scope

Ez egy KONTRAKTUS-szintű DESIGN report, NEM importer-implementáció. A kimenet azt
definiálja, hogyan fordítaná egy jövőbeli importer egy VALÓDI ChatGPT export-bundle
(sharded `conversations-NNN.json` + kísérő fájlok) tartalmát `SessionIngressEnvelope`
rekordokká. Nincs futtatható kód, nincs élesben futtatott importer, nincs valós
adatbázis-írás. A `SessionIngressEnvelope` schema NEM módosult. A `historical-dedupe-
idempotency-001` job (a tényleges dedupe-implementáció) hatóköre KÜLÖN marad.

Biztonsági határ (betartva a teljes job alatt): a forrás-bundle egy valódi, személyes
ChatGPT export. A jelen report KIZÁRÓLAG mező-neveket, struktúrát, enum-szerű
nem-egyedi értékeket és aggregált statisztikákat idéz a bundle-ből. SEMMILYEN tényleges
beszélgetés-szöveg, `title` mező érték, `conversation_id`/`id` UUID-érték, melléklet-
fájlnév, vagy a `user.json`/`user_settings.json` tartalma NEM szerepel ebben a
fájlban. A bundle könyvtárából semmi nem került bemásolásra git-tracked helyre.

## Inputs Read

- `jobs/index.yaml` (`cic-mcp-factory`) — prerequisite-ellenőrzéshez, `- id: "..."`
  kulcsok alapján (lásd "Prerequisite Check").
- `jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml`
  — a TELJES `SessionIngressEnvelope` schema (299 sor), végig elolvasva, különös
  figyelemmel az `idempotency_key` (214-247. sor), `payload` (143-154. sor),
  `provider`/`provider_session_id`/`provider_event_name` (66-91. sor) mezőkre.
- `jobs/session-postgres-storage-design-001/output/session-postgres-storage-design.md`
  — a meglévő perzisztencia-réteg reportja (megtalálható volt, idézve lentebb az
  "Envelope Field Mapping" és "Next Jobs" hivatkozásoknál).
- `.cic-context/corpus/normalized/thead-review-2026-06-20.yaml` — `rag_implications`
  és `recommended_next_actions` szekciók.
- `.cic-context/factory-docs/execution-phases.md` — "Phase 5 - Historical Import"
  szekció (132-150. sor).
- `.cic-context/factory-docs/job-slices.yaml` — `historical-chatgpt-export-importer-001`
  bejegyzés (792-813. sor): `acceptance_gates`, `required_evidence`,
  `forbidden_shortcuts`.
- A VALÓDI ChatGPT export-bundle (`${CHATGPT_EXPORT_DIR}`) — KIZÁRÓLAG struktúra-
  vizsgálat: `ls`/`find` névlistázás, és Python kulcs-listázó parancsok
  (`sorted(obj.keys())`, `len(...)`, `type(...).__name__`), SOHA érték-kiíratás.

## Prerequisite Check

```
$ grep -n '\- id: "session-ingress-envelope-contract-001"' -A 3 jobs/index.yaml
134:  - id: "session-ingress-envelope-contract-001"
135-    level: "capability"
136-    status: "done"
137-    parent: "session-infra-pipeline-fix-001"

$ grep -n '\- id: "session-postgres-storage-design-001"' -A 3 jobs/index.yaml
179:  - id: "session-postgres-storage-design-001"
180-    level: "capability"
181-    status: "done"
182-    parent: "session-ingress-envelope-contract-001"
```

Mindkét prerequisite `status: "done"`. **Döntés: GO.** A `SessionIngressEnvelope`
schema és a Postgres tárolási réteg design stabil — ez a job rájuk épülhet
kontraktus-szinten.

## Export Bundle Structure (Structural Only — No Content Quoted)

Az alábbiak a `${CHATGPT_EXPORT_DIR}` TÉNYLEGES, saját, független vizsgálatának
eredményei (nem az orchestrátor-prompt leírásának megismétlése).

**Top-level fájlszerkezet** (`ls`/`find` névlistázás, 596 top-level entry):

- **Sharded `conversations-NNN.json` fájlok vannak, NEM egyetlen `conversations.json`.**
  Pontosan **20 darab**, `conversations-000.json` … `conversations-019.json`
  névmintával (zero-padded, 3 számjegy). Egyetlen `conversations.json` (unsharded)
  NEM létezik a bundle-ben — ezt explicit ellenőriztem (`ls ... | grep
  '^conversations\.json$'` → nincs hit).
- **Egyéb fix nevű top-level fájlok, csak a LÉTÜK dokumentálva, tartalmuk NEM nyitva
  meg/idézve**: `export_manifest.json`, `message_feedback.json`,
  `shared_conversations.json`, `user.json` (137 byte), `user_settings.json`
  (3887 byte). (`user.json`/`user_settings.json` mérete file-system metaadat, nem a
  fájl tartalmának idézése.)
- **`message_feedback.json`**: top-level típusa `list`, hossza 5 elem (csak az
  aggregát szám, elem-tartalom nem nyitva).
- **`shared_conversations.json`**: top-level típusa `list`, hossza 104 elem (csak
  aggregát szám).
- **`export_manifest.json` top-level kulcsai** (kulcs-listázás, séma-feltárás —
  engedélyezett kategória): `export_files`, `logical_files`, `manifest_file`,
  `version`.
- **Melléklet-fájlok**: a bundle TÖBB SZÁZ (351 darab) `file-*`/`file_*` prefixű
  melléklet-fájlt tartalmaz közvetlenül a top-level könyvtárban (NEM
  per-conversation-id alkönyvtárakban — ez eltér attól, amit az orchestrátor
  pre-confirm szövege feltételezett; saját vizsgálatom ezt korrigálja). Ezen
  felül van egy kisebb számú (~30) UUID-mintázatú alkönyvtár is a top-level alatt.
  A `file-*`/`file_*` fájlnevek NEM kerülnek idézésre ebben a reportban (a
  fájlnevek gyakran az eredeti, felhasználó által feltöltött fájl nevét is
  tartalmazzák — ez pontosan az a kategória, amit a biztonsági határ explicit
  tilt).
- **Markdown/HTML export jelenléte**: VAN egy `chat.html` nevű fájl a top-level
  alatt (370 928 820 byte — kb. 354 MB), amely a ChatGPT saját
  ember-olvasható/HTML-exportja. Strukturált markdown export (`.md` kiterjesztésű,
  NEM melléklet) a top-level alatt NEM található — az egyetlen ember-olvasható
  "bootstrap" jellegű artifact a `chat.html`.

**Egy `conversations-NNN.json` fájl szerkezete** (saját, független kulcs-listázás,
`conversations-000.json`-on végrehajtva):

- Top-level típus: `list`. Az adott shardban **100 conversation-objektum** van.
- Egy conversation-objektum (`d[0]`) kulcsai (`sorted(d[0].keys())`):
  `async_status`, `atlas_mode_enabled`, `blocked_urls`, `context_scopes`,
  `conversation_id`, `conversation_origin`, `conversation_template_id`,
  `create_time`, `current_node`, `default_model_slug`, `disabled_tool_ids`,
  `gizmo_id`, `gizmo_type`, `id`, `is_archived`, `is_do_not_remember`,
  `is_read_only`, `is_starred`, `is_study_mode`, `mapping`, `memory_scope`,
  `moderation_results`, `owner`, `pinned_time`, `plugin_ids`, `safe_urls`,
  `sugar_item_id`, `sugar_item_visible`, `title`, `update_time`, `voice`.
  Ez TÖBB mezőt tartalmaz, mint amit az orchestrátor-prompt pre-confirm szövege
  feltételezett (az csak `mapping`/`id`-t emelte ki) — a TELJES kulcslista a fenti.
- `mapping`: `dict`, kulcsai node-id-k. Az első conversation `mapping`-jében 4
  node van; aggregát statisztika a shard 100 conversation-jére: `mapping`
  mérete min. 4, max. 134, átlagosan ~20.5 node/conversation.
- Egy `mapping`-node kulcsai (`sorted(node.keys())`): `children`, `id`, `message`,
  `parent`. Ez megfelel a pre-confirm leírásnak (`{id, message, parent, children}`).
- `message` objektum kulcsai: `author`, `channel`, `content`, `create_time`,
  `end_turn`, `id`, `metadata`, `recipient`, `status`, `update_time`, `weight`.
- `message.author` kulcsai: `metadata`, `name`, `role`.
- `message.author.role` ENUM-értékek, a shardban ténylegesen előfordulók
  (nem-egyedi kategória-érték, engedélyezett idézet): `assistant`, `system`,
  `tool`, `user`.
- `message.content` kulcsai: `content_type`, `parts`.
- `message.content.content_type` ENUM-szerű értékek, a shardban ténylegesen
  előfordulók (nem-egyedi kategória-érték): `code`, `execution_output`,
  `multimodal_text`, `system_error`, `tether_browsing_display`,
  `tether_quote`, `text`. Ez SZÉLESEBB halmaz, mint a pre-confirm szöveg
  `{text, code, ...}` placeholder-e — saját vizsgálatom a teljes, tényleges
  enum-halmazt adja.
- `message.metadata` egy mintavett node-on: kulcsai `can_save`,
  `is_visually_hidden_from_conversation` (csak a kulcsnevek, nem az értékek).

**Aggregált összesítés**: a 20 shard összesen **1959 conversation-objektumot**
tartalmaz (Python-nal összegezve, `len(d)` minden shardra, majd összeadva — ez
egy aggregált szám, NEM tartalom-idézet).

## conversations-*.json To SessionIngressEnvelope Mapping

| Export mező | `SessionIngressEnvelope` mező | Megjegyzés |
|---|---|---|
| `conversation_id` / `id` (conversation-objektum szintű) | `provider_session_id` | A `provider` konstans `"chatgpt-export"` alatt. A TÉNYLEGES UUID-érték NEM kerül idézésre ebben a reportban — csak a mező-megfelelés. |
| `mapping[node].message.create_time` | `occurred_at` | ChatGPT epoch-float timestamp → RFC3339 UTC konverzió szükséges importer-szinten (a schema `occurred_at` mezője `format: date-time`, RFC3339 UTC-t vár, schema 124-129. sor). |
| `mapping[node].message.author.role` | NEM direkt `SessionIngressEnvelope` mező — `turn_projector`-oldali bemenet | Az `author.role` (`system`/`user`/`assistant`/`tool`) a jövőbeli `session_core` projekciós logikának (turn_projector worker, lásd `session-postgres-storage-design.md` "Next Jobs": `session-turn-projector-001`) ad bemenetet — NEM az ingress-envelope szintjén interpretálódik, megfelelve a schema "the hook should not semantically interpret" elvének (schema 8-13. sor, dec-thead-0002). |
| TELJES `mapping[node].message` objektum (vagy releváns alrésze) | `payload` | A schema 144-154. sora explicit garantálja: *"The raw provider payload, stored AS-IS (structurally preserved, not semantically summarized or reduced) ... the original payload structure or content MUST NOT be discarded or replaced by a derived summary at ingress level."* — a teljes `message` objektum (author/channel/content/metadata/stb.) a `payload`-ba kerül módosítatlanul. |
| (konstans) | `provider` = `"chatgpt-export"` | A schema `provider` mező `examples` listája (69. sor) MÁR tartalmazza pontosan ezt az értéket mintaként — nincs schema-módosítás, csak egy meglévő példa-érték használata. |
| `mapping[node].message.id` VAGY `author.role` | `provider_event_name` | Két ésszerű választás: (a) az `author.role` érték maga (pl. `"user"`, `"assistant"`) — előnye, hogy egységes a `idempotency_key` szempontjából egy adott role-üzenetre; (b) egy fix `"historical_message"` konstans minden historikus importált sorra, megkülönböztetve a live-hook eseményektől (`PostToolUse`, `Stop`, stb., schema 87. sor `examples`). **Javasolt választás: `author.role`** — mert a `provider_event_name` schema-leírása (85-91. sor) "the provider's own event/hook name, if one exists, preserved verbatim" — egy ChatGPT exportnál a "saját esemény-név" legközelebbi megfelelője az üzenet szerepe, nem egy importer-specifikus címke. Indoklás részletesen a "Decisions Proposed"-ben. |

**Megjegyzés `mapping`-bejárásról**: mivel `mapping` egy fa-struktúra
(`parent`/`children` mezőkkel, nem lineáris lista), egy historikus importernek a
fát kell bejárnia (pl. `current_node`-tól visszafelé `parent`-en, vagy
gyökértől `children`-en előre) ahhoz, hogy a node-okat időrendi/szál-sorrendbe
állítsa egy `SessionIngressEnvelope` sorozattá — ez egy implementációs döntés,
amit a jelen kontraktus-report NEM dönt el (lásd "Nem cél"), csak megjegyzi
mint szükséges lépést egy jövőbeli importer-jobnak.

## Dedupe/Idempotency Strategy

```
$ grep -rn "idempotency_key" jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml | grep -v test_
jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml:32:  - idempotency_key
jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml:60:      governed by idempotency_key (see below), not by event_id, because
jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml:83:      requires combining with `provider` (see idempotency_key).
jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml:173:      idempotency_key (see below).
jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml:214:  idempotency_key:
jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml:220:        idempotency_key = sha256(
jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml:245:      A session_raw ingest MUST treat idempotency_key as a unique constraint:
jobs/session-ingress-envelope-contract-001/output/session-ingress-envelope.schema.yaml:246:      a second envelope with the same idempotency_key is a no-op (or upsert
```

A 214-247. sor a mező TÉNYLEGES definíciója (nem csak megemlítés). **Fontos
korrekció a job-indító prompthoz képest**: az input.md (147. sor) és az
orchestrátor-prompt is egy RÖVIDÍTETT formulát idéz (`sha256(provider +
provider_session_id + provider_event_name + raw_payload_hash)`), de a séma
TÉNYLEGES, idézett szövege (220-226. sor) egy ÖTÖDIK komponenst is tartalmaz:

```
idempotency_key = sha256(
  provider + "\x1f" +
  provider_session_id + "\x1f" +
  (provider_event_name or "") + "\x1f" +
  occurred_at + "\x1f" +
  raw_payload_hash
)
```

A TÉNYLEGES schema-definíció (`occurred_at`-tal együtt, ASCII unit-separator
`0x1F` join-nal) az autoritatív forrás — ez a report ezt használja, nem a
rövidített prompt-parafrázist.

**Döntés: a meglévő formula ELÉGSÉGES egy historikus importernél, kiegészítés
NÉLKÜL.** Indoklás:

1. **Részben átfedő re-export eset** (a job-spec explicit forgatókönyve): ha a
   felhasználó egy frissebb exportot tölt fel, amely részben átfedi a korábbit,
   az átfedő `conversation_id`/`message.id` párokra a `(provider,
   provider_session_id, provider_event_name, occurred_at, raw_payload_hash)`
   ötös pontosan ugyanazt a kombinációt produkálja, FELTÉVE hogy:
   - `provider_session_id` = a `conversation_id` (export-determinisztikus, nem
     véletlenszerű minden exportnál),
   - `occurred_at` = a `message.create_time` normalizált RFC3339-formája
     (ChatGPT export-determinisztikus, nem változik újraexportkor),
   - `raw_payload_hash` = a `payload` (a teljes `message`-objektum) tartalmának
     hash-e, amely identikus üzenet-tartalomra identikus.
   Ebből következik: egy ÚJRA-exportált, változatlan historikus üzenet
   pontosan ugyanazt az `idempotency_key`-t generálja → a session_raw UNIQUE
   constraint (lásd `session-postgres-storage-design.md` Claim-Evidence Matrix,
   "idempotency_key UNIQUE constraint" sor, `session-postgres-schema.sql:102`)
   no-op-ként kezeli, NEM duplikál.
2. **`occurred_at` szerepe kritikus historikus importnál**: mivel egy
   historikus üzenetnek FIX, a forrásból eredő `occurred_at`-ja van (nem az
   importálás időpontja), az `occurred_at` komponens itt NEM "zaj" — pontosan
   ez disztingválja a "ugyanaz az üzenet, ugyanaz az idő" duplikációt egy
   "hasonló tartalom, más időpont" eseménytől. Az `event_id` és `ingested_at`
   (amelyek szándékosan KI vannak zárva a hash-ből, schema 242-243. sor) helyesen
   maradnak ki, mert az importer minden futásnál új `event_id`-t generál, de ez
   nem jelent valódi tartalmi különbséget.
3. **Mikor LENNE szükség kiegészítésre (de itt NEM az eset)**: ha egy export
   formátum NEM adna stabil, ismételhető `occurred_at`-ot (pl. csak relatív
   sorszámot), vagy ha a `conversation_id` exportonként újragenerálódna. A
   ChatGPT export `create_time` epoch-timestamp és a `conversation_id` mindkettő
   forrás-stabil (a séma szerkezete alapján, lásd "Export Bundle Structure"),
   így ez a kockázat NEM releváns ennél a providernél.
4. **A tényleges dedupe-IMPLEMENTÁCIÓ** (pl. batch-insert ON CONFLICT kezelés,
   teljesítmény-optimalizálás nagy exportoknál) a `historical-dedupe-
   idempotency-001` job hatóköre — ez a report csak azt állítja, hogy a
   FORMULA elégséges, nem implementálja a dedupe-logikát.

## Markdown Export As Backup, Not Source-of-Truth

**Explicit kimondva**: ebben a bundle-ben VAN strukturált, sharded
`conversations-NNN.json` export (20 fájl, lásd "Export Bundle Structure"). Egy
jövőbeli importer EZT a strukturált JSON-forrást használja ELSŐDLEGES
forrásként. A bundle-ben talált `chat.html` (ember-olvasható HTML-export, 354 MB)
KIZÁRÓLAG BACKUP/bootstrap-corpus szerepet tölthet be — NEM az elsődleges
import-útvonal, mert a `chat.html`-ből hiányoznak a strukturált mezők
(`author.role`, `content_type`, `create_time` machine-readable formában,
node-fa `parent`/`children` kapcsolatok), amelyek a `SessionIngressEnvelope`
mezőleképezéshez szükségesek.

Ez összhangban van a `thead-review-2026-06-20.yaml` `rag_implications`
szekciójának vonatkozó sorával:

> "Markdown exports are useful as human-readable backup and bootstrap corpus."

(`thead-review-2026-06-20.yaml`, `rag_implications` lista, 95. sor — idézve a
forrás saját szövegéből, ami egy factory-belső review-artifact, nem a
felhasználó privát export-bundle-je, tehát idézhető.)

Ez egyben a `job-slices.yaml` `forbidden_shortcuts` listájának betartása is:
*"markdown export as source-of-truth when structured export exists"* — ez a
report NEM kezeli a `chat.html`-t forrásként, pontosan azért, mert a
strukturált `conversations-NNN.json` export elérhető.

## Findings

- A bundle TÉNYLEGES szerkezete (20 sharded `conversations-NNN.json`, 1959
  conversation-objektum összesen, 351 top-level melléklet-fájl, `chat.html`
  mint humán-olvasható export) GAZDAGABB és RÉSZBEN ELTÉRŐ az orchestrátor
  pre-confirm leírásától: (a) a conversation-objektum kulcslistája jóval
  szélesebb (`atlas_mode_enabled`, `gizmo_id`, `memory_scope`, `voice` stb.,
  nem csak `mapping`/`id`); (b) a `content_type` enum-halmaz szélesebb
  (`execution_output`, `multimodal_text`, `system_error`,
  `tether_browsing_display`, `tether_quote` is előfordul, nem csak `text`/
  `code`); (c) a melléklet-fájlok a top-level könyvtárban flat módon vannak,
  NEM per-conversation-id alkönyvtárakban, ahogy a pre-confirm szöveg
  feltételezte (bár van egy kisebb halmaz UUID-mintázatú alkönyvtár is). Ez a
  job saját, független kulcs-listázása ezt korrigálja/pontosítja — pont azért
  kellett a saját vizsgálat, amit a job-spec is megkövetelt.
- Az `idempotency_key` formula az input.md/orchestrátor-prompt-ban idézett
  rövidített verziónál EGY KOMPONENSSEL TÖBBET tartalmaz (`occurred_at`) a
  TÉNYLEGES schema-fájlban — ez nem hiba a job-specifikációban, hanem egy
  tudatosan rövidített parafrázis volt a promptban; ez a report a teljes,
  autoritatív schema-szöveget használja.
- A `session-postgres-storage-design-001` riport (`output/session-postgres-
  storage-design.md`) MEGTALÁLHATÓ volt — nem kellett a `.sql`-ből helyettesítő
  idézést alkalmazni.
- Egy historikus importer számára a `mapping` fa-bejárási sorrendje (DFS a
  gyökértől, vagy visszafelé `current_node`-tól) nyitott implementációs döntés
  — ezt a report explicit NEM dönti el (lásd "Nem cél" — implementáció nem
  ennek a jobnak a hatóköre).

## Claim-Evidence Matrix

| Claim | Status | Evidence | Verification Method | Risk |
|---|---|---|---|---|
| Mindkét prerequisite (`session-ingress-envelope-contract-001`, `session-postgres-storage-design-001`) `status: "done"` | proven | `jobs/index.yaml:134-137` és `:179-182`, idézve a "Prerequisite Check" szekcióban | grep `-n '- id: "..."' -A 3 jobs/index.yaml`, kimenet idézve | alacsony |
| A bundle sharded `conversations-NNN.json` formátumú, NEM egységes `conversations.json` | proven | `ls "$CHATGPT_EXPORT_DIR" \| grep -E '^conversations-[0-9]+\.json$'` → 20 fájl, `conversations-000.json`…`conversations-019.json`; `grep '^conversations\.json$'` → nincs hit | saját, független fájlnév-listázás (NEM csak az orchestrátor pre-confirm leírásának elfogadása) | alacsony |
| Egy conversation-objektum kulcsai dokumentáltak | proven | `python3 -c "... sorted(d[0].keys())"` kimenete a "Export Bundle Structure" szekcióban, teljes lista idézve | saját kulcs-listázó parancs futtatása, SOHA érték-kiíratás | alacsony |
| `mapping`-node szerkezete `{id, message, parent, children}` | proven | `sorted(node.keys())` kimenete = `['children', 'id', 'message', 'parent']` | saját kulcs-listázás | alacsony |
| `message`/`author`/`content` mező-nevek dokumentáltak | proven | `sorted(msg.keys())`, `sorted(author.keys())`, `sorted(content.keys())` kimenetei idézve | saját kulcs-listázás | alacsony |
| `role` és `content_type` enum-halmaz dokumentálva, tényleges előfordulás alapján | proven | aggregált `set()` gyűjtés a shard összes node-jára: `role` = `{assistant, system, tool, user}`, `content_type` = 7 érték | saját Python-script, csak enum-aggregátum, érték-kiíratás nélkül | alacsony |
| Top-level fix nevű fájlok (`user.json`, `user_settings.json`, `export_manifest.json`, `message_feedback.json`, `shared_conversations.json`) léte megerősítve, tartalmuk NEM nyitva | proven | `find`/`ls`/`stat` névlistázás és méret, `export_manifest.json` csak kulcs-listázva, a többi csak típus+hossz (`user.json`/`user_settings.json` tartalma EGYÁLTALÁN nem parse-olva) | saját parancsok kimenete idézve | alacsony |
| `chat.html` mint humán-olvasható export jelen van, strukturált forrás mellett csak backup | proven | `find -iname '*.md'` → nincs nem-attachment markdown; `chat.html` létezik, 370928820 byte | saját fájl-létezés/méret ellenőrzés | alacsony |
| `idempotency_key` mező a schema-ban tényleges sorban definiált (nem csak megemlítve) | proven | grep-kimenet idézve, `:214-247` sorhivatkozással, teljes formula idézve | grep + manuális sor-olvasás | alacsony |
| `payload` mező "stored AS-IS" garanciája idézve a schema-ból | proven | schema 144-154. sor idézve a mezőleképezési táblában | manuális sor-idézés | alacsony |
| `provider` = `"chatgpt-export"` MÁR szerepel a schema `examples` listájában | proven | schema 69. sor: `examples: ["claude-code", "chatgpt-export", "codex-cli", "manual"]` | manuális sor-idézés | alacsony |
| Markdown/HTML export csak backup, NEM source-of-truth, a `thead-review` idézve | proven | `thead-review-2026-06-20.yaml` 95. sor: "Markdown exports are useful as human-readable backup and bootstrap corpus." | fájlban közvetlenül idézhető (nem személyes export-bundle) | alacsony |
| Dedupe-formula elégséges historikus importnál kiegészítés nélkül | partial | indoklás a "Dedupe/Idempotency Strategy"-ben, de NINCS valós, futtatott teszt két egymást átfedő export-futásra (csak logikai levezetés a schema garanciáiból) | manuális logikai levezetés, nincs futtatott bizonyíték | közepes — ha egy jövőbeli export-formátum-verzió instabil `occurred_at`/`conversation_id`-t adna, a feltevés megdőlne |
| A report SEMMILYEN tényleges beszélgetés-tartalmat/UUID-t/fájlnevet nem idéz | proven | a teljes report manuális, kétszeri átolvasása a commitolás előtt (lásd Definition Of Done Check) | manuális review | alacsony — emberi/AI hiba kockázata mindig megmarad, ezért a kétszeri átolvasás kötelező volt |

## Decisions Proposed

1. **`provider_event_name` = `message.author.role`** egy historikus importernél,
   NEM egy fix `"historical_message"` konstans. Indoklás: a schema
   leírása ("the provider's own event/hook name, if one exists, preserved
   verbatim") arra utal, hogy ez a mező a provider saját esemény-kategorizálását
   tükrözze; egy ChatGPT exportnál ez legközelebb az üzenet szerepéhez áll,
   ami egyben hasznos disztinkciót is ad a `idempotency_key`-ben (egy
   `system`/`user`/`assistant`/`tool` üzenet más `provider_event_name`-mel
   kapja a hash-jét, csökkentve a véletlen kollízió esélyét, bár a
   `raw_payload_hash` ettől függetlenül is tie-breaker).
2. **`provider_session_id` = a conversation-objektum `conversation_id` (vagy
   `id`) mezője.** Ez a `(provider, provider_session_id)` pár alapján
   csoportosítja az összes envelope-ot egy ChatGPT-beszélgetéshez — ez
   megfelel a schema "provider-scoped, not globally unique on its own"
   leírásának (76-83. sor).
3. **`payload` = a teljes `mapping[node].message` objektum**, NEM csak a
   `content.parts`. Indoklás: a schema "stored AS-IS" garanciája explicit
   kizárja a derived-summary mintát (lásd a `log-event.py` `summarize()`
   ellen-mintát a schema 152-154. sorában) — egy historikus importernek is
   meg kell tartania a teljes message-objektumot (author/metadata/channel
   stb.), nem csak a szöveges tartalmat.
4. **`occurred_at` = `message.create_time` normalizálva RFC3339 UTC-re,
   másodperc-pontossággal** (a schema idempotency_key leírása explicit
   megköveteli ezt a normalizálást, 230. sor) — egy jövőbeli importernek
   konvertálnia kell a ChatGPT epoch-float timestamp-et.
5. **A `mapping` fa-bejárási sorrendje NYITOTT KÉRDÉS marad** ennek a
   kontraktus-reportnak a számára — nem dönti el, csak megjegyzi mint
   szükséges implementációs lépést egy jövőbeli importer-jobnak.

## Rejected / Out Of Scope

- Tényleges importer-kód implementálása — explicit "Nem cél" az input.md-ben.
- A `SessionIngressEnvelope` schema módosítása — a séma változatlan marad,
  a `"chatgpt-export"` provider-érték MÁR szerepelt az `examples` listában.
- `historical-dedupe-idempotency-001` hatóköre (a tényleges dedupe-
  implementáció, batch-insert logika, performance-optimalizáció) — külön
  Phase 5 job.
- Bármilyen fájl/adat bemásolása az export-bundle-ből egy git-tracked helyre —
  ez a report KIZÁRÓLAG struktúra-leírást tartalmaz, semmilyen nyers JSON
  vagy fájlrészlet nem került bemásolásra.
- A `mapping` fa-bejárási algoritmus konkrét specifikációja — implementációs
  részlet, egy jövőbeli importer-jobra hagyva.
- A `chat.html` (354 MB humán-olvasható export) tényleges feldolgozása vagy
  parse-olása — csak a létezése és mérete dokumentált, mert ez NEM az
  elsődleges import-útvonal.

## Risks

- **Epoch-timestamp konverzió pontossága**: a `create_time` ChatGPT-oldali
  reprezentációja (feltehetően Unix epoch float) → RFC3339 UTC konverzió
  implementációs részlet, ami időzóna/kerekítési hibákat hordozhat — nincs
  futtatott teszt ennek bizonyítására (ez egy jövőbeli implementációs jobra
  marad).
- **`mapping` fa-bejárási sorrend nem specifikált**: ha egy jövőbeli importer
  helytelenül választja meg a bejárási irányt (pl. csak `current_node`-tól
  visszafelé, elveszítve elágazó branch-eket), a historikus adat egy része
  kimaradhat az importból — ez a report NEM dönt erről, csak jelzi mint
  implementációs kockázatot.
- **Dedupe-formula elégségessége csak logikailag levezetett, nem futtatva
  tesztelt** (lásd Claim-Evidence Matrix "partial" sor) — ha egy jövőbeli
  export-formátum-verzió instabil mezőket adna, a feltevés felülvizsgálatra
  szorulna.
- **A bundle saját strukturális gazdagsága** (pl. `gizmo_id`,
  `conversation_template_id`, `memory_scope` mezők egy conversation-
  objektumon) olyan ChatGPT-funkciókra utal (custom GPT-k, memória-funkció),
  amelyek mezőleképezése ebben a reportban NEM részletezett — egy jövőbeli
  importer-implementációnak ezekkel is foglalkoznia kell, ha teljes
  fidelitást akar elérni, de a `payload` "AS-IS" tárolása miatt ez nem vész
  el, csak nincs explicit `SessionIngressEnvelope`-szintű kibontva.
- **Top-level melléklet-fájlok mennyisége (351 darab) és mérete** (nem mérve
  ebben a reportban összesítve, hogy elkerüljük a fájlnév-mintázat további
  feltárását) — egy jövőbeli importernek külön döntést kell hoznia, hogy a
  mellékleteket hogyan kezeli (külön blob-store, vagy a `payload`-on belüli
  hivatkozás), ez explicit NEM ennek a jobnak a hatóköre.

## Definition Of Done Check

- [x] mindkét prerequisite `id:` kulccsal megerősítve, GO döntés indokolva —
      lásd "Prerequisite Check".
- [x] az export-bundle TÉNYLEGES struktúrája (sharded, NEM egységes
      `conversations.json`) dokumentálva, kulcs-listázó parancs kimenetével,
      TARTALOM idézése nélkül — lásd "Export Bundle Structure".
- [x] mezőleképezési tábla kész, az `idempotency_key`/`payload` garanciákra
      hivatkozva — lásd "conversations-*.json To SessionIngressEnvelope
      Mapping".
- [x] dedupe-stratégia indokolva — lásd "Dedupe/Idempotency Strategy".
- [x] markdown-export-mint-backup állítás explicit kimondva — lásd "Markdown
      Export As Backup, Not Source-of-Truth", `thead-review` idézve.
- [x] claim-evidence tábla kitöltve, nem üres (13 sor) — lásd fent.
- [x] SEMMILYEN tényleges beszélgetés-tartalom, cím, UUID, vagy
      melléklet-fájlnév NEM jelenik meg a riportban — ez a report a
      commitolás előtt MÉG EGYSZER, teljes egészében átolvasva lett pontosan
      ennek ellenőrzésére; egyetlen `conversation_id`/`title`/`file-*`
      fájlnév-érték sem szerepel benne, csak mező-nevek, enum-kategóriák és
      aggregált számok.

## Next Jobs

- `historical-dedupe-idempotency-001` (Phase 5, második job) — a tényleges
  dedupe-implementáció (batch insert, ON CONFLICT/upsert logika,
  performance-optimalizáció nagy exportokra), a jelen reportban levont
  "a formula elégséges" döntésre építve.
- Egy jövőbeli `historical-chatgpt-export-importer-implementation-001`-szerű
  job (még nem létezik a `job-slices.yaml`-ban) — a tényleges importer-kód
  megírása, ami a `mapping` fa-bejárási sorrendet konkretizálja, az
  epoch-timestamp konverziót implementálja, és a `payload` szerkezetét a
  jelen mezőleképezési tábla szerint tölti fel.
- Egy jövőbeli `candidate`-szintű job, ami a jelen kontraktus-designt egy
  valós (de KIZÁRÓLAG struktúra-szintű, tartalom nélküli) futtatott
  bizonyítékkal validálja — pl. egy szintetikus/anonimizált teszt-export
  ellen futtatva, a `gateway-session-adapter-contract-001` →
  `session-context-pack-v1-001` mintát követve (lásd input.md "status
  indoklás").
