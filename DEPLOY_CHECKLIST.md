# BrickHaus – Deploy-sjekkliste

Går gjennom disse punktene **før** `git push` til Railway. Tar ca. 5 minutter.

---

## 1. DB-migrasjoner

- [ ] Er det kjørt en ny `.sql`-migrasjon siden siste deploy?
  - Ja → gå gjennom punktene under **Etter DB-migrasjon** nedenfor
  - Nei → hopp videre til punkt 2

### Etter DB-migrasjon
- [ ] Er alle nye kolonner som er NOT NULL også lagt til i kode-INSERT-kallene?
- [ ] Er alle nye enum-verdier også lagt til i `TYPE_LABEL`, `CONDITION_LABEL` o.l. i `main.py`?
- [ ] Kjør en full registrering (se punkt 2) for å bekrefte at INSERT fortsatt virker

---

## 2. Registreringsflyt — manuell test

Test **alle tre** sporene med en ekte del/sett:

### Sett
- [ ] Gå til + Registrer → velg Sett
- [ ] Søk opp et settnummer (f.eks. 10318)
- [ ] Fullfør alle steg (detaljer, plassering, kjøp)
- [ ] Bekreft at settet dukker opp i Samling-listen

### Løs del
- [ ] Gå til + Registrer → velg Løs del
- [ ] Last opp et bilde → bekreft at AI gjenkjenner og at søkefeltet fylles ut
- [ ] Velg en del fra søkeresultatene
- [ ] Bekreft at farge er pre-valgt (eller velg manuelt)
- [ ] Fullfør alle steg
- [ ] Bekreft at delen dukker opp i Samling-listen

### Bulk
- [ ] Gå til + Registrer → velg Bulk
- [ ] Fyll ut navn og lagre
- [ ] Bekreft at oppføringen dukker opp i Samling-listen

---

## 3. Visning og redigering

- [ ] Åpne et eksisterende objekt → detaljer vises uten feil
- [ ] Rediger ett felt → lagret OK (ingen 400/500)

---

## 4. Kjente risikopunkter

Disse delene av koden har historisk vært kilde til feil — vær ekstra obs om du har rørt dem:

| Område | Risiko |
|---|---|
| `save_object()` i `main.py` | Manglende kolonner ved skjema-endringer |
| `sb_post()` / `sb_patch()` | Feil kolonnenavn eller typemismatch |
| `identify_lego_from_image()` | JSON-parsing feiler hvis AI-svar endrer format |
| `rb_fetch_colors()` | API-feil gir tom liste → farge-UI forsvinner |
| `next_ownership_id()` | Kollisjon mellom LG-* og BH-* IDer |

---

## 5. Etter deploy

- [ ] Åpne Railway-loggen og se etter stack traces de første 2 minuttene
- [ ] Gjør én rask registrering i prod for å bekrefte at Railway-miljøet er OK

