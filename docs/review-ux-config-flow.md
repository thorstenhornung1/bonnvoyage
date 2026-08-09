# Usability-Review: BonnVoyage Config-Flow (Plan v0.1, Abschnitt 5)

Review-Basis: `/Users/thorstenhornung/Documents/projects/hass_explore/bonnvoyage/PLAN.md`

---

## Zusammenfassung (MUSS-Punkte)

1. **Parkhaus-Auswahl in die Ersteinrichtung ziehen** (Multi-Select, beim Abschluss als Subentries angelegt). Sonst liefert „Weiter-Weiter-Fertig" nur Meldungen — Subentry-Buttons werden von Einsteigern nicht gefunden.
2. **Provider-Schritt als Menü, nicht als Formular mit optionalem Key-Feld.** Zwei Optionen: „Reisezeit-Anbieter jetzt einrichten (API-Schlüssel erforderlich)" / „Ohne Strecken abschließen — jederzeit nachholbar". Leere optionale Key-Felder sind die klassische Abbruchfalle.
3. **Kein Dead-End im Subentry „Strecke hinzufügen":** fehlt der Key, startet der Flow inline mit dem Provider-Schritt statt mit einem Abort.
4. **Strecken-Eingabe als 3-Schritt-Wizard** (Start → Ziel → Name/Schwelle), je Endpunkt Auswahl „Zone oder Karte" — keine Koordinaten-Textfelder, keine zwei Karten in einem Formular.
5. **Ein `LocationSelector` mit Radius** für den Meldungsbereich (Default: Home, 10 km) statt getrennter Koordinaten-/Zahlenfelder.
6. **`single_config_entry: true`** im Manifest (Region ist fix Bonn) + `unique_id` je Parkhaus-Subentry (BCP-ID) gegen Duplikate.
7. **Update-Intervalle NICHT im Options-Flow anbieten** — widerspricht der HA-Entwicklerrichtlinie („appropriate polling", Intervalle nicht nutzerkonfigurierbar) und gefährdet das Platinum-Ziel. Feste Intervalle; Power-User nutzen „Automatische Aktualisierung deaktivieren" + `homeassistant.update_entity`.
8. **Key-Validierung mit differenzierten Fehlern** (`invalid_auth` / `cannot_connect` / `rate_limited`) und Reauth-Flow bei später ungültigem Key; Streckenanlage mit Proberouting validieren (`route_not_found`, `points_too_close`).
9. **`data_description` für jedes Feld**, keine Fachbegriffe (kein „GeoJSON", „Coordinator", „Subentry") in Nutzertexten.

---

## 1. Schritt-für-Schritt-Review des geplanten Flows

### 1.1 Ersteinrichtung — Schritt 1 „Basis" (Plan: Mittelpunkt & Radius)

**Gut:** Kein API-Key für den Grundumfang, Default HA-Home. Das ist der richtige Einstieg.

**Reibungspunkte:**

| # | Problem | Risiko | Lösung |
|---|---|---|---|
| 1 | „Mittelpunkt & Radius für Meldungen" — Einsteiger wissen nicht, was „Meldungen" sind und warum sie einen Radius wählen sollen | Fehlbedienung, Verunsicherung | Beschreibungstext erklärt den Nutzen konkret („Baustellen und Verkehrswarnungen im gewählten Bereich erscheinen auf der Karte"), nicht die Technik |
| 2 | Getrennte Felder für Lat/Lon/Radius wären hochgradig fehleranfällig | Zahlendreher, vertauschte Koordinaten | **Ein** `LocationSelector` mit `radius: true` — Pin ziehen, Kreis aufziehen, fertig. Default: Home + 10 km (deckt Stadtgebiet + A555/A559/A565 ab) |
| 3 | Kein Erwartungsmanagement, was nach diesem Schritt kommt | Abbruch („Wo trage ich den Key ein?") | Beschreibung nennt die nächsten Schritte in einem Satz |
| 4 | Mehrfacheinrichtung derselben Integration möglich | Doppelte Entities, Verwirrung | `single_config_entry: true` im Manifest → HA blendet den „Hinzufügen"-Button aus, sauberer Abort-Text |
| 5 | Verbindungstest: Was, wenn stadtplan.bonn.de gerade down ist? | Setup-Blockade wegen unkritischer Quelle | Diesen Schritt **nicht** an Konnektivität koppeln. Nur der Parkhaus-Schritt braucht Live-Daten (s. u.); Meldungsquellen dürfen beim Setup offline sein (Coordinator holt später nach) |

### 1.2 Ersteinrichtung — Schritt 2 „optional Reisezeit-Provider"

**Kernproblem:** Ein Formular mit Provider-Dropdown und optionalem Key-Feld ist die häufigste UX-Falle in HA-Flows. Nutzer interpretieren ein sichtbares Eingabefeld als Pflicht („Ich habe keinen Key — ich breche ab") oder tippen etwas Beliebiges ein und laufen in einen Validierungsfehler.

**Lösung:** `async_show_menu` — ein Menü-Schritt mit zwei klar beschrifteten Optionen (siehe UI-Texte, Schritt `routes_menu`). Die Menü-Beschreibung ist gleichzeitig der zentrale Ort für Punkt (a) — Kommunikation der Key-Anforderung. Wer „ohne Strecken abschließen" wählt, darf **keine Sackgasse** erleben: Der Subentry-Flow „Strecke hinzufügen" holt den Provider-Schritt inline nach (siehe 1.3).

**Zusätzlich fehlt im Plan:** ein Parkhaus-Auswahlschritt in der Ersteinrichtung. Subentries sind das richtige Verwaltungsmodell, aber der „Hinzufügen"-Button auf der Integrationskachel ist ein Discovery-Problem — Einsteiger finden ihn nicht. Empfehlung: Schritt „Parkhäuser auswählen" zwischen Basis und Provider-Menü; beim `async_create_entry` werden die gewählten Parkhäuser per `subentries=[...]` direkt als Subentries angelegt (etabliertes Core-Muster, z. B. Google Generative AI legt so Default-Subentries an).

**Ziel-Flow Ersteinrichtung (4 Schritte, davon 2 mit reinem „Weiter"):**

```
user (Bereich, Default Home+10km)
  → parking (Multi-Select, vorsortiert & vorausgewählt)
  → routes_menu (Menü: Provider jetzt / ohne Strecken abschließen)
  → [provider (Auswahl + Key + Testrequest)]
  → Entry + Parkhaus-Subentries angelegt
```

„Weiter-Weiter-Fertig" (Defaults akzeptieren, „ohne Strecken abschließen" klicken) liefert damit: Meldungs-Entities + ca. 3–5 Parkhäuser mit allen Sensoren. Das ist ein sofort sichtbares, nützliches Ergebnis (Karte füllt sich).

### 1.3 Subentry „Strecke hinzufügen"

**Reibungspunkte im Plan-Entwurf (ein Formular mit Name, Start, Ziel, Via, Schwellwert):**

1. **Zwei bis drei `LocationSelector` in einem Formular sind praktisch unbenutzbar** — jeder rendert eine eigene Karte; auf dem Handy entsteht eine Scroll-Wüste, Pins werden auf der falschen Karte verschoben.
2. **Name zuerst abzufragen ist verkehrt herum** — der Nutzer weiß den besten Namen erst, wenn Start/Ziel feststehen. Namensvorschlag automatisch generieren („Zuhause → Karte 50.71, 7.12" bzw. Zonennamen).
3. **Kein Umgang mit fehlendem API-Key definiert** → Dead-End-Risiko (MUSS-Punkt 3): Erster Schritt des Subentry-Flows prüft, ob ein Provider konfiguriert ist; wenn nein, wird der Provider-Schritt (identisches Schema wie in der Ersteinrichtung, wiederverwendete Schema-Helper) vorgeschaltet — mit Erklärtext, warum.
4. **Keine Validierung der Route geplant.** Vor `async_create_entry` eine Proberoute rechnen: fängt unroutbare Punkte (Pin im Rhein, im Gebäudekern), zu nahe Punkte und Key-Probleme ab, bevor tote Sensoren entstehen.

**Empfohlener Wizard (max. 5 Formulare, typisch 3):**

```
[provider — nur falls kein Key]
  → start   : SelectSelector aus Zonen (zone.home zuerst) + Option „Auf Karte wählen…", Default: Zuhause
  → [start_map : LocationSelector, zentriert auf Home]
  → dest    : gleiches Muster, Default: „Auf Karte wählen…" (Ziele sind selten Zonen; existiert z. B. zone.arbeit, steht sie oben)
  → [dest_map : LocationSelector, zentriert auf gewähltem Start]
  → settings: Name (vorbefüllt), Stau-Schwelle (Default 10 min), Checkbox „Zwischenpunkt festlegen" (→ optionaler via_map-Schritt)
  → Proberouting → Subentry (Titel = Streckenname)
```

Details: Schrittzähler im Titel („1/3", „2/3", „3/3") gegen das „Wie lange dauert das noch?"-Gefühl. Zonen-Auswahl per `SelectSelector` (aus `zone.*`-Entities gebaut, Label = Friendly Name) statt `EntitySelector`, weil nur so die Sonderoption „Auf Karte wählen…" in dieselbe Liste passt. Via ist bewusst hinter einer Checkbox versteckt (KANN-Feature, 95 % der Strecken brauchen es nicht). Duplikatprüfung auf Namen (`name_exists`).

### 1.4 Subentry „Parkhaus hinzufügen"

**Technischer Konflikt im Plan:** „Multi-Select aus live geladener Liste" — ein Subentry-Flow erzeugt aber genau **einen** Subentry pro Durchlauf. Auflösung:
- **Ersteinrichtung:** Multi-Select (dort ist er möglich, via `subentries=[...]` beim Entry-Anlegen).
- **Subentry-Flow:** Single-Select-Dropdown (Nachrüsten ist der Ein-Stück-Fall; wer drei nachrüsten will, klickt dreimal — akzeptabel).

**Gestaltung der Auswahl (Details in Abschnitt 2):** sortiert nach Entfernung, Distanz im Label, bereits konfigurierte gefiltert, `unique_id` = BCP-ID. Sonderfälle: Liste leer / API down / alles schon konfiguriert → eigene Fehler-/Abort-Texte (Abschnitt 3).

### 1.5 Options-Flow

**Kritik am Plan („Intervalle, Radius, Schwellwerte"):**

1. **Intervalle streichen (MUSS für Platinum-Anspruch):** Die HA-Entwicklerdokumentation rät explizit davon ab, Polling-Intervalle nutzerkonfigurierbar zu machen. Feste, quellengerechte Intervalle (2/5/30 min laut Plan) + Doku-Hinweis auf systemseitiges „Automatische Aktualisierung deaktivieren" und `homeassistant.update_entity`.
2. **Schwellwerte gehören nicht in den globalen Options-Flow.** Eine Options-Seite mit Feldern für alle Strecken skaliert nicht und ist mehrdeutig. Stattdessen: **Subentry-Reconfigure** („Strecke anpassen" direkt am Subentry) — dort denselben Wizard mit vorbefüllten Werten anbieten, mindestens aber Name + Schwelle.
3. **Provider/Key-Wechsel** gehört in den Reconfigure-Flow des Haupt-Entries bzw. bei 401 in den **Reauth-Flow** (Silver-Anforderung, im Plan bereits genannt) — nicht in Options. Options bleibt schlank: nur Meldungsbereich (derselbe `LocationSelector` wie im Setup).

---

## 2. Die vier kritischen Punkte — konkrete Lösungen

### (a) „Strecken brauchen einen Key, Parkhäuser nicht" verständlich machen

Drei Kommunikationsebenen, gestaffelt:

1. **Schritt 1 (Basis), erster Satz der Beschreibung:** „Parkhaus-Belegung, Baustellen und Verkehrsmeldungen für Bonn — ohne Anmeldung und ohne API-Schlüssel." Damit ist die Grundaussage gesetzt, bevor überhaupt ein Key erwähnt wird.
2. **Menü-Schritt `routes_menu`:** Die Key-Anforderung steht **in der Options-Beschriftung selbst** („… (API-Schlüssel erforderlich)") und wird im Beschreibungstext begründet: Reisezeiten kommen von einem externen Routing-Dienst, der Free-Tier reicht, der Schlüssel wird nur lokal gespeichert. Die Ausstiegsoption trägt die Beruhigung im Label: „jederzeit nachholbar".
3. **Subentry „Strecke hinzufügen" ohne Key:** Der inline vorgeschaltete Provider-Schritt beginnt mit dem Warum: „Für Reisezeiten wird ein Routing-Dienst benötigt. Deine Parkhäuser und Meldungen laufen unabhängig davon weiter."

Grundregel: nie nur „optional" schreiben — immer sagen, *was ohne den Key trotzdem funktioniert* und *was der Key zusätzlich bringt*.

### (b) Parkhaus-Auswahl bei ~15–20 Einträgen

- **Ein `SelectSelector`** (Ersteinrichtung: `multiple: true`; Subentry: single, `mode: dropdown` mit Suche). 15–20 Einträge sind für eine durchsuchbare Liste unkritisch — keine Pagination, keine Karte nötig.
- **Sortierung:** aufsteigend nach Luftlinien-Entfernung zu HA-Home (Haversine aus BCP-Koordinaten, beim Flow-Start einmal geladen).
- **Label-Format:** `Marktgarage (0,6 km)` — Distanz mit einer Nachkommastelle, unter 1 km ggf. in Metern (`(450 m)`). Bei geschlossenen Häusern Suffix `– derzeit geschlossen`. **Keine** Live-Belegung ins Label (veraltet während des Flows, erzeugt visuelles Rauschen, verleitet zu Momentaufnahme-Entscheidungen).
- **Vorauswahl (nur Ersteinrichtung):** alle Parkhäuser im Umkreis von 3 km um Home, gedeckelt auf 5. Liegt keins im Radius (Nutzer wohnt außerhalb): keine Vorauswahl — dann ist eine bewusste Wahl besser als eine falsche. Die Vorauswahl muss im Beschreibungstext benannt sein („… sind vorausgewählt — passe die Auswahl an"), damit sie nicht als Zwang wirkt.
- **Duplikatschutz:** Subentry-`unique_id` = BCP-Parkhaus-ID; im Nachrüst-Dropdown bereits konfigurierte ausblenden; sind alle konfiguriert → Abort `all_configured` mit freundlichem Text.

### (c) Start/Ziel fehlerarm eingeben

Bewertung der drei Kandidaten:

| Methode | Fehlerarm? | Urteil |
|---|---|---|
| Koordinaten-Textfelder | Nein (Zahlendreher, Lat/Lon vertauscht, Komma/Punkt) | **Nicht anbieten** |
| `LocationSelector` (Karte) | Ja — Pin ziehen ist selbsterklärend, Ergebnis visuell verifizierbar | Basis-Methode |
| Zonen-Entities | Ja — semantisch („Zuhause", „Arbeit"), null Eingabeaufwand | Bevorzugter Schnellweg, wenn vorhanden |

**Empfehlung: Hybrid als Zwei-Stufen-Muster pro Endpunkt** (siehe Wizard in 1.3): Dropdown mit allen Zonen plus fester Option „Auf Karte wählen…"; nur bei Kartenwahl folgt der Map-Schritt. Defaults: Start = `zone.home`, Ziel = „Auf Karte wählen…". Die Ziel-Karte zentriert auf dem gewählten Start (kurzer Weg zum Pin). Kein einziges Freitext-Koordinatenfeld im gesamten Flow. Fortgeschrittene verlieren nichts — der Karten-Pin ist beliebig präzise. Sicherheitsnetz bleibt das Proberouting mit den Fehlern `route_not_found` / `points_too_close` (unter ~100 m).

Adress-Geocoding („Straße eingeben") bewusst **nicht** in v1: zusätzlicher API-Verbrauch, eigene Fehlerklasse (Mehrdeutigkeit), und der Karten-Pin löst denselben Job.

### (d) Defaults für „Weiter-Weiter-Fertig"

| Einstellung | Default | Begründung |
|---|---|---|
| Meldungsbereich | HA-Home, 10 km | deckt Bonn + A555/A559/A565-Korridore ab |
| Parkhäuser | vorausgewählt: ≤ 3 km um Home, max. 5 | sofort gefüllte Karte, überschaubare Entity-Zahl |
| Strecken | keine (Menü-Ausstieg) | Key-Hürde nicht in den Schnellpfad legen |
| Stau-Schwelle je Strecke | 10 min Verzögerung | robuster Alltagswert; unter 5 min alarmiert Bonn-Berufsverkehr täglich |
| Strecken-Name | generiert aus Endpunkten („Zuhause → Büro") | Name zuletzt, vorbefüllt, überschreibbar |
| Via | aus (Checkbox) | Nischenbedarf |
| Intervalle | fix (2/5/30 min), nicht konfigurierbar | HA-Richtlinie, s. o. |

Ergebnis des Schnellpfads: funktionierende Karte mit Parkhäusern und Baustellen nach drei Klicks — ohne eine einzige Tastatureingabe.

---

## 3. Fehler- und Sonderfälle mit Meldungstexten (de)

Grundsätze: sagen, **was** passiert ist, **ob es am Nutzer liegt** und **was er jetzt tun kann**. Setup nie wegen unkritischer Quellen blockieren. Fehler als `errors={"base"/feld: key}` im selben Formular (Eingaben bleiben erhalten), Abort nur für echte Endzustände.

| Fall | Verhalten | Text (de) |
|---|---|---|
| BCP nicht erreichbar (Parkhaus-Schritt) | Fehler im Formular + Menü-Ausweg „Ohne Parkhäuser fortfahren" | „Die Parkhaus-Daten (BCP Bonn) sind derzeit nicht erreichbar. Versuche es erneut oder fahre ohne Parkhäuser fort — du kannst sie später jederzeit hinzufügen." |
| 0 Parkhäuser in geladener Liste | wie oben (Fehler, kein Abort in der Ersteinrichtung) | „Es wurden aktuell keine Parkhäuser gemeldet. Das ist meist eine vorübergehende Störung der BCP-Schnittstelle. Du kannst Parkhäuser später über „Parkhaus hinzufügen" ergänzen." |
| Ungültiger API-Key (Testrequest 401/403) | Fehler am Feld `api_key` | „Der Anbieter hat den API-Schlüssel abgelehnt. Prüfe, ob der Schlüssel vollständig kopiert wurde und für die Routing-Schnittstelle freigeschaltet ist." |
| Provider nicht erreichbar (Timeout/5xx) | Fehler `base` — klar von „Key falsch" trennen! | „Der Reisezeit-Anbieter ist derzeit nicht erreichbar. Das liegt nicht an deinem Schlüssel — prüfe deine Internetverbindung und versuche es in ein paar Minuten erneut." |
| Rate-Limit (429) beim Testrequest | Fehler `base` | „Der Anbieter meldet zu viele Anfragen (Kontingent erreicht). Warte einige Minuten und versuche es dann erneut." |
| Keine Route berechenbar | Fehler `base` im settings-Schritt | „Für die gewählten Punkte konnte keine Route berechnet werden. Prüfe, ob Start und Ziel auf oder nahe einer befahrbaren Straße liegen, und verschiebe die Markierung gegebenenfalls." |
| Start/Ziel zu nah beieinander | Fehler `base` | „Start und Ziel liegen zu nah beieinander. Wähle zwei unterschiedliche Orte mit mindestens einigen hundert Metern Abstand." |
| Streckenname bereits vergeben | Fehler am Feld `name` | „Eine Strecke mit diesem Namen existiert bereits. Wähle einen anderen Namen." |
| Alle Parkhäuser schon konfiguriert | Abort im Subentry-Flow | „Alle derzeit gemeldeten Parkhäuser sind bereits eingerichtet." |
| Key wird später ungültig | Repair-Issue + Reauth-Flow; Parkhäuser/Meldungen laufen weiter | siehe `reauth_confirm` in Abschnitt 4 |
| Zweite Einrichtung versucht | Abort (single instance) | „BonnVoyage ist bereits eingerichtet. Strecken und Parkhäuser fügst du über die bestehende Integration hinzu." |

---

## 4. UI-Texte (de) — Vorlage für `translations/de.json`

Feldschlüssel sind Vorschläge und an die Schema-Keys anzupassen. Englische `strings.json` analog pflegen (Quelle für den Übersetzungsworkflow).

```json
{
  "config": {
    "step": {
      "user": {
        "title": "BonnVoyage einrichten",
        "description": "Parkhaus-Belegung, Baustellen und Verkehrsmeldungen für Bonn – ohne Anmeldung und ohne API-Schlüssel. Lege zuerst fest, für welchen Bereich Baustellen und Verkehrsmeldungen erscheinen sollen. Danach wählst du Parkhäuser aus; Strecken mit Reisezeiten sind optional.",
        "data": {
          "area": "Bereich für Meldungen"
        },
        "data_description": {
          "area": "Verschiebe die Markierung und ziehe den Kreis auf. Voreingestellt ist dein Zuhause mit 10 km Radius – das deckt das Stadtgebiet und die Autobahnen A555, A559 und A565 ab."
        }
      },
      "parking": {
        "title": "Parkhäuser auswählen",
        "description": "Live-Belegung der Bonner Parkhäuser. Die Liste ist nach Entfernung zu deinem Zuhause sortiert; Parkhäuser im Umkreis von 3 km sind vorausgewählt – passe die Auswahl einfach an. Du kannst später jederzeit Parkhäuser hinzufügen oder entfernen.",
        "data": {
          "parkings": "Parkhäuser"
        },
        "data_description": {
          "parkings": "Für jedes gewählte Parkhaus entstehen Sensoren für freie Plätze, Auslastung, Trend und Öffnungsstatus – inklusive Position für die Karte."
        }
      },
      "routes_menu": {
        "title": "Strecken mit Reisezeiten?",
        "description": "Für Reisezeiten und Stau-Warnungen auf eigenen Strecken (z. B. Zuhause → Büro) nutzt BonnVoyage einen externen Routing-Dienst. Dafür ist ein API-Schlüssel des Anbieters nötig – der kostenlose Tarif reicht für mehrere Strecken. Parkhäuser, Baustellen und Meldungen funktionieren ohne Schlüssel.",
        "menu_options": {
          "provider": "Reisezeit-Anbieter jetzt einrichten (API-Schlüssel erforderlich)",
          "finish": "Ohne Strecken abschließen – jederzeit nachholbar"
        }
      },
      "provider": {
        "title": "Reisezeit-Anbieter",
        "description": "Wähle einen Anbieter und trage deinen API-Schlüssel ein. Einen kostenlosen Schlüssel erhältst du im Entwicklerportal des Anbieters. Der Schlüssel wird nur lokal in Home Assistant gespeichert und ausschließlich für Routenanfragen verwendet.",
        "data": {
          "provider": "Anbieter",
          "api_key": "API-Schlüssel"
        },
        "data_description": {
          "provider": "TomTom wird als erster Anbieter unterstützt; weitere folgen.",
          "api_key": "Wird beim Fortfahren mit einer Testanfrage geprüft."
        }
      },
      "reauth_confirm": {
        "title": "API-Schlüssel erneuern",
        "description": "Der Reisezeit-Anbieter akzeptiert den hinterlegten API-Schlüssel nicht mehr (abgelaufen, gesperrt oder Kontingent dauerhaft überschritten). Trage einen gültigen Schlüssel ein, damit die Strecken-Sensoren wieder aktualisiert werden. Parkhäuser und Meldungen sind davon nicht betroffen.",
        "data": {
          "api_key": "API-Schlüssel"
        }
      }
    },
    "error": {
      "bcp_unavailable": "Die Parkhaus-Daten (BCP Bonn) sind derzeit nicht erreichbar. Versuche es erneut oder fahre ohne Parkhäuser fort – du kannst sie später jederzeit hinzufügen.",
      "no_parkings": "Es wurden aktuell keine Parkhäuser gemeldet. Das ist meist eine vorübergehende Störung der BCP-Schnittstelle. Du kannst Parkhäuser später über „Parkhaus hinzufügen“ ergänzen.",
      "invalid_auth": "Der Anbieter hat den API-Schlüssel abgelehnt. Prüfe, ob der Schlüssel vollständig kopiert wurde und für die Routing-Schnittstelle freigeschaltet ist.",
      "cannot_connect": "Der Reisezeit-Anbieter ist derzeit nicht erreichbar. Das liegt nicht an deinem Schlüssel – prüfe deine Internetverbindung und versuche es in ein paar Minuten erneut.",
      "rate_limited": "Der Anbieter meldet zu viele Anfragen (Kontingent erreicht). Warte einige Minuten und versuche es dann erneut.",
      "unknown": "Unerwarteter Fehler. Details findest du im Home-Assistant-Protokoll."
    },
    "abort": {
      "single_instance_allowed": "BonnVoyage ist bereits eingerichtet. Strecken und Parkhäuser fügst du über die bestehende Integration hinzu.",
      "reauth_successful": "Der API-Schlüssel wurde erfolgreich aktualisiert."
    }
  },
  "config_subentries": {
    "route": {
      "initiate_flow": {
        "user": "Strecke hinzufügen",
        "reconfigure": "Strecke anpassen"
      },
      "entry_type": "Strecke",
      "step": {
        "provider": {
          "title": "Reisezeit-Anbieter einrichten",
          "description": "Für Reisezeiten wird ein externer Routing-Dienst benötigt – dieser Schritt ist nur einmal nötig. Deine Parkhäuser und Meldungen laufen unabhängig davon weiter. Einen kostenlosen API-Schlüssel erhältst du im Entwicklerportal des Anbieters.",
          "data": {
            "provider": "Anbieter",
            "api_key": "API-Schlüssel"
          },
          "data_description": {
            "api_key": "Wird beim Fortfahren mit einer Testanfrage geprüft."
          }
        },
        "start": {
          "title": "Strecke hinzufügen – Start (1/3)",
          "description": "Wo beginnt die Strecke? Wähle eine deiner Zonen oder setze im nächsten Schritt einen Punkt auf der Karte.",
          "data": {
            "start": "Startpunkt"
          },
          "data_description": {
            "start": "Zonen sind deine in Home Assistant hinterlegten Orte, z. B. Zuhause oder Arbeit."
          }
        },
        "start_map": {
          "title": "Start auf der Karte wählen (1/3)",
          "description": "Verschiebe die Markierung an den Startpunkt. Der Punkt sollte auf oder nahe einer befahrbaren Straße liegen.",
          "data": {
            "location": "Startpunkt"
          }
        },
        "dest": {
          "title": "Strecke hinzufügen – Ziel (2/3)",
          "description": "Wohin führt die Strecke? Wähle eine Zone oder setze im nächsten Schritt einen Punkt auf der Karte.",
          "data": {
            "destination": "Zielpunkt"
          }
        },
        "dest_map": {
          "title": "Ziel auf der Karte wählen (2/3)",
          "description": "Verschiebe die Markierung an den Zielpunkt. Der Punkt sollte auf oder nahe einer befahrbaren Straße liegen.",
          "data": {
            "location": "Zielpunkt"
          }
        },
        "settings": {
          "title": "Strecke abschließen (3/3)",
          "description": "Fast fertig: Name prüfen und Stau-Schwelle festlegen. Beim Speichern wird eine Proberoute berechnet, um die Strecke zu überprüfen.",
          "data": {
            "name": "Name der Strecke",
            "delay_threshold": "Stau-Warnung ab Verzögerung",
            "add_via": "Zwischenpunkt festlegen"
          },
          "data_description": {
            "name": "Wird für die Sensornamen verwendet, z. B. „Zuhause → Büro“.",
            "delay_threshold": "Der Stau-Sensor meldet ein Problem, sobald die Verzögerung gegenüber freier Fahrt diesen Wert (in Minuten) erreicht.",
            "add_via": "Erzwingt die Route über einen Zwischenpunkt, z. B. über die A565. Für die meisten Strecken nicht nötig."
          }
        },
        "via_map": {
          "title": "Zwischenpunkt auf der Karte wählen",
          "description": "Verschiebe die Markierung an den Punkt, über den die Route führen soll.",
          "data": {
            "location": "Zwischenpunkt"
          }
        }
      },
      "error": {
        "invalid_auth": "Der Anbieter hat den API-Schlüssel abgelehnt. Prüfe, ob der Schlüssel vollständig kopiert wurde und für die Routing-Schnittstelle freigeschaltet ist.",
        "cannot_connect": "Der Reisezeit-Anbieter ist derzeit nicht erreichbar. Prüfe deine Internetverbindung und versuche es in ein paar Minuten erneut.",
        "rate_limited": "Der Anbieter meldet zu viele Anfragen (Kontingent erreicht). Warte einige Minuten und versuche es dann erneut.",
        "route_not_found": "Für die gewählten Punkte konnte keine Route berechnet werden. Prüfe, ob Start und Ziel auf oder nahe einer befahrbaren Straße liegen, und verschiebe die Markierung gegebenenfalls.",
        "points_too_close": "Start und Ziel liegen zu nah beieinander. Wähle zwei unterschiedliche Orte mit mindestens einigen hundert Metern Abstand.",
        "name_exists": "Eine Strecke mit diesem Namen existiert bereits. Wähle einen anderen Namen."
      },
      "abort": {
        "reconfigure_successful": "Die Strecke wurde aktualisiert."
      }
    },
    "parking": {
      "initiate_flow": {
        "user": "Parkhaus hinzufügen"
      },
      "entry_type": "Parkhaus",
      "step": {
        "user": {
          "title": "Parkhaus hinzufügen",
          "description": "Die Liste ist nach Entfernung zu deinem Zuhause sortiert. Bereits eingerichtete Parkhäuser werden nicht mehr angezeigt.",
          "data": {
            "parking": "Parkhaus"
          }
        }
      },
      "error": {
        "bcp_unavailable": "Die Parkhaus-Daten (BCP Bonn) sind derzeit nicht erreichbar. Prüfe deine Internetverbindung und versuche es erneut."
      },
      "abort": {
        "all_configured": "Alle derzeit gemeldeten Parkhäuser sind bereits eingerichtet.",
        "no_parkings": "Es wurden aktuell keine Parkhäuser gemeldet. Das ist meist eine vorübergehende Störung der BCP-Schnittstelle – versuche es später erneut."
      }
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "BonnVoyage – Optionen",
        "description": "Bereich für Baustellen und Verkehrsmeldungen anpassen. Strecken und Parkhäuser verwaltest du direkt auf der Integrationsseite; den API-Schlüssel änderst du über „Neu konfigurieren“.",
        "data": {
          "area": "Bereich für Meldungen"
        },
        "data_description": {
          "area": "Mittelpunkt und Radius, in dem Baustellen und Verkehrswarnungen erscheinen."
        }
      }
    }
  }
}
```

Hinweise zur Umsetzung: Die dynamischen Options-Labels (Parkhäuser mit Distanz, Zonen, „Auf Karte wählen…") entstehen im Code via `SelectOptionDict` — die Sonderoption „Auf Karte wählen…" kann alternativ über den `selector`-Übersetzungsblock lokalisiert werden. Für Reconfigure-Titel `description_placeholders` nutzen (z. B. „Strecke „{name}" anpassen").

---

## 5. Priorisierte Liste für den Hauptplan

### MUSS (blockiert gute UX bzw. Platinum)

1. Parkhaus-Multi-Select in die Ersteinrichtung; Anlage als Subentries beim Entry-Create (`subentries=[...]`).
2. Provider-Schritt als Menü (`async_show_menu`) statt Formular mit optionalem Key-Feld; Key-Anforderung in Options-Label und Beschreibung.
3. Subentry „Strecke hinzufügen" ohne konfigurierten Provider: Provider-Schritt inline vorschalten — kein Abort, keine Sackgasse.
4. Strecken-Wizard mehrstufig (Start → Ziel → Einstellungen), Endpunkte als „Zone oder Karte", keine Koordinaten-Textfelder; Namensvorschlag automatisch; Proberouting als Validierung.
5. `LocationSelector` mit Radius für den Meldungsbereich (ein Feld, Default Home + 10 km).
6. `single_config_entry: true`; Subentry-`unique_id` je Parkhaus (BCP-ID) und Namens-Duplikatprüfung je Strecke.
7. Intervalle aus dem Options-Flow streichen (feste Intervalle; HA-Richtlinie).
8. Differenzierte Fehlerbehandlung (`invalid_auth` vs. `cannot_connect` vs. `rate_limited`; BCP-Ausfall mit „Ohne Parkhäuser fortfahren"); Reauth-Flow bei ungültig gewordenem Key.
9. `data_description` für jedes Feld; de/en-Übersetzungen vollständig; keine Fachbegriffe in Nutzertexten.

### SOLL (deutlicher Qualitätsgewinn)

1. Vorauswahl der Parkhäuser ≤ 3 km (max. 5) in der Ersteinrichtung, im Text transparent gemacht.
2. Subentry-Reconfigure für Strecken („Strecke anpassen": Schwelle, Name, Endpunkte) und Reconfigure des Haupt-Entries für Provider/Key.
3. Schwellwerte aus dem globalen Options-Flow entfernen (gehören an die Strecke); Options nur noch Meldungsbereich.
4. Nach erfolgreicher Provider-Einrichtung in der Ersteinrichtung direkt die erste Strecke anlegen (Momentum nutzen; Schema-Helper mit dem Subentry-Flow teilen).
5. Distanz-Labels im Parkhaus-Dropdown („(0,6 km)", „– derzeit geschlossen"); Ziel-Karte auf gewähltem Start zentrieren; Schrittzähler „(1/3)" in Wizard-Titeln.
6. Repair-Issue mit klarem deutschen Text bei dauerhaftem BCP-/Quellen-Ausfall (im Plan angelegt, Texte konsistent zu Abschnitt 3 formulieren).

### KANN (nice to have)

1. Via-Zwischenpunkt hinter Checkbox + eigenem Kartenschritt (wie in der Vorlage skizziert).
2. Option „Gegenrichtung ebenfalls anlegen" beim Strecken-Abschluss (Achtung: ein Subentry-Flow erzeugt einen Subentry — zweiter müsste programmatisch via `async_add_subentry` entstehen).
3. `async_show_progress` während BCP-Laden/Key-Test, falls Latenzen > ~2 s auftreten.
4. Quota-Hinweis im Provider-Schritt („Bei 3 Strecken und 5-Minuten-Intervall ca. 860 Anfragen/Tag — Free-Tier-tauglich").
5. Warnung bei zwei Strecken mit nahezu identischen Endpunkten.
6. Aktive Zeitfenster je Strecke (nur Pendelzeiten abfragen) — spart Quota, aber Komplexität; frühestens M3+.

---

**Gesamturteil:** Der Plan-Entwurf hat die richtige Architektur (ein Entry, Subentries, Key nur für Strecken). Die drei größten Abbruchrisiken im aktuellen Entwurf sind der als Formular gedachte „optionale" Key-Schritt, die fehlende Parkhaus-Auswahl im Erstflow (leeres Ergebnis nach dem Schnellpfad) und die Ein-Formular-Strecke mit mehreren Karten. Mit dem Menü-Schritt, dem Parkhaus-Schritt und dem 3-Schritt-Strecken-Wizard erreicht der Flow das Platinum-Anspruchsniveau: Einsteiger kommen in drei Klicks ohne Tastatur zu einem sichtbaren Ergebnis, Fortgeschrittene verlieren keine Kontrolle.
