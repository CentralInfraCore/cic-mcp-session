# session_raw.envelopes — Retention Policy

Job: session-data-protection-001, "Feladat" 4.

## Alapértelmezett retention időtartam

**90 nap** a `session_raw.envelopes` rétegben, `occurred_at` mező alapján mérve (NEM
`ingested_at` — a `occurred_at` az esemény TÉNYLEGES idejét rögzíti, ami a retention
szempontjából a releváns "hány napos ez az adat" kérdés, nem az, hogy mikor került be a
rendszerbe).

**Indoklás**: a `session_raw.envelopes` a NYERS, redaction UTÁNI (de még mindig potenciálisan
érzékeny, mert nem teljes secret-scanning) hook-payload-ot tárolja. A 90 nap egy gyakori
ipari kiindulópont rövid-élettartamú, operatív (nem hosszú-távú audit célú) nyers logokra —
elég hosszú, hogy egy debugging/incident-vizsgálat visszamenőleg elérje az adatot, de nem
tartja a nyers adatot határtalan ideig. A `session_core.*` PROJEKTÁLT réteg (turns/chunks)
NEM esik ez alá a policy alá — az a "interpretált, strukturált" réteg, külön retention-döntés
tárgya lehet egy KÉSŐBBI jobban, ez a job KIZÁRÓLAG a `session_raw.envelopes`-re vonatkozik.

## Kikényszerítés — TERV, NEM implementálva ebben a jobban

**Ez a job NEM épít automatikus purge-mechanizmust.** Az alábbi terv a KÖVETŐ job
("Next Jobs") feladata:

1. Egy ütemezett (cron/systemd timer, vagy a `session_store/worker_loop.py` mintáját követő
   periodikus iteráció) purge-job, ami:
   ```sql
   DELETE FROM session_raw.envelopes WHERE occurred_at < now() - INTERVAL '90 days';
   ```
   futtatná, RENDSZERESEN (pl. naponta egyszer).
2. A purge-job MAGA is egy `session_audit.raw_reads`-hez hasonló audit-sort írna egy
   (ebben a jobban még nem létező) `session_audit.raw_purges` táblába, mielőtt töröl —
   "ki/mikor/hány sort purge-olt" nyomot hagyva.
3. A purge ELŐTT érdemes megfontolni egy "archívum" lépést (pl. egy külön, hidegebb
   tárolóba exportálás) — ez egy KÜLÖN döntés, NEM ennek a jobnak a hatóköre, csak
   megjegyezve itt.
4. A `rollback_conversation()` (session_store/rollback.py:72) MÁR létező, MANUÁLIS törlési
   primitívum egyetlen beszélgetésre — ez NEM helyettesíti az automatikus, IDŐ-alapú
   purge-ot, mert (provider, provider_session_id)-kulcsú, nem `occurred_at`-alapú. A két
   mechanizmus EGYMÁS MELLETT élhet: `rollback_conversation()` egy CÉLZOTT, azonnali törlés
   (pl. GDPR-kérésre), a tervezett purge-job egy IDŐ-alapú, automatikus háztartás.

## Mi VAN már kész ehhez a jobhoz kapcsolódóan

- `rollback_conversation()` (session_store/rollback.py:72-138) — VÁLTOZATLAN, megerősített
  törlési primitívum egyetlen beszélgetésre (lásd output/session-data-protection.md
  "Findings" 4. pont).
- Secret-redaction az INSERT előtt (session_store/redaction.py) — ez NEM retention, hanem
  egy ORTOGONÁLIS védelmi réteg: a redaction azt csökkenti, MENNYIRE érzékeny az, amit a 90
  napig tárolunk, a retention pedig azt korlátozza, MEDDIG tároljuk.
- `session_audit.raw_reads` audit-log — megfigyelhetőség arra, KI olvasta a nyers adatot a
  retention-időszak alatt.

## Next Jobs (explicit jelölve)

- **Egy KÖVETŐ job, amely a fenti 1-3. pontot TÉNYLEGESEN implementálja** (ütemezett
  purge-job + `session_audit.raw_purges` audit-tábla) — ez a hiányzó láncszem, amiért ez a
  job `status_after_merge: experimental`, NEM `candidate`.
