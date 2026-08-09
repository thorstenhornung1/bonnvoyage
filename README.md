# BonnVoyage 🚗🅿️

**Home-Assistant-Custom-Integration für die Bonner Verkehrslage** — bringt Open Data Bonn
und Echtzeitverkehrsdaten zusammen: Parkhäuser mit Live-Belegung, Baustellen und
Verkehrsmeldungen auf der Karte, die städtische Live-Verkehrslage und optional
Tür-zu-Tür-Reisezeiten für deine Pendelstrecken.

> *Bonn + „Bon voyage" — gute Fahrt durch Bonn.*

Tests: 100+ grün, Coverage ≥ 97 % (Config-Flow 100 %), `mypy --strict`, `ruff`.
Qualitätsziel: [Home Assistant Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) Platinum.

## Was die Integration liefert

| Baustein | Quelle | Key nötig? |
|---|---|---|
| 🅿️ Parkhäuser (freie Plätze, Auslastung, Trend, offen/voll) | BCP Bonn | nein |
| 🚧 Baustellen auf der Karte | Stadt Bonn Geoportal | nein |
| 🛣️ Autobahn-Meldungen A555/A559/A565 (Baustellen, Sperrungen, Warnungen) | Autobahn GmbH | nein |
| 🚦 Live-Verkehrslage auf 111 Bonner Hauptstraßen-Abschnitten | Stadt Bonn | nein |
| ⏱️ Reisezeit, Verzögerung und Stau-Alarm für eigene Strecken | TomTom (optional) | ja (eigener Free-Tier-Key) |
| 🚌 Live-Abfahrten & Verspätungen für Bahnen und Busse | transitous.org (DELFI) | nein |

Alle Entities mit Geodaten erscheinen direkt auf der **Home-Assistant-Standardkarte**.

## Installation

### HACS (empfohlen)

1. HACS → Integrationen → ⋮ → „Benutzerdefinierte Repositories"
2. `https://github.com/thorstenhornung1/bonnvoyage` als Integration hinzufügen
3. „BonnVoyage" installieren und Home Assistant neu starten
4. Einstellungen → Geräte & Dienste → Integration hinzufügen → **BonnVoyage**

### Manuell

Den Ordner `custom_components/bonnvoyage` in dein HA-Konfigurationsverzeichnis
kopieren und Home Assistant neu starten. Mindestversion: Home Assistant 2025.3.

## Einrichtung

Die Ersteinrichtung braucht **keinen API-Schlüssel** und liefert nach drei Klicks
ein sichtbares Ergebnis:

1. **Bereich für Meldungen** — ein Karten-Kreis (Default: Zuhause + 10 km).
   Baustellen, Verkehrsmeldungen und Verkehrsstörungen erscheinen nur in diesem Bereich.
2. **Parkhäuser auswählen** — nach Entfernung sortiert, die nächstgelegenen sind
   vorausgewählt.
3. **Strecken (optional)** — nur hier wird ein TomTom-API-Schlüssel gebraucht;
   „Ohne Strecken abschließen" ist jederzeit nachholbar.

Danach lassen sich auf der Integrationsseite jederzeit hinzufügen:

- **Parkhaus hinzufügen** — weiteres Parkhaus aus der Live-Liste
- **Stadtstrecke beobachten** — Punkt auf der Karte wählen; der nächstgelegene
  gemessene Straßenabschnitt (max. 2 km entfernt) wird als eigenes Gerät beobachtet
- **Strecke hinzufügen** — 3-Schritte-Assistent: Start (Zone oder Karte) → Ziel →
  Name + Stau-Schwelle (Default 10 min); optional ein Zwischenpunkt. Beim Speichern
  wird eine Proberoute gerechnet, damit keine toten Sensoren entstehen.
- **Haltestelle beobachten** — Haltestelle per Namenssuche wählen (z. B.
  „Bonn Stadthaus“); Verspätungs-Alarm-Schwelle konfigurierbar (Default 5 min).

**Optionen** (Zahnrad): Bereich für Meldungen ändern. **Reauth**:
Bei ungültig gewordenem TomTom-Schlüssel startet Home Assistant automatisch den
Erneuerungs-Dialog; Parkhäuser und Meldungen laufen währenddessen weiter.

## Entities

**Pro Parkhaus** (Gerät): Freie Plätze (mit Koordinaten für die Map-Card),
Auslastung %, Trend (füllt sich/gleichbleibend/leert sich, siehe
[docs/spec-trend.md](docs/spec-trend.md)), Geöffnet, Voll.

**Pro Stadtstrecke** (Gerät): Verkehrsstatus (normal/erhöht/Staugefahr/Stau/nicht
ermittelbar), Geschwindigkeit km/h, Stau (Problem-Sensor).

**Pro Strecke** (Gerät): Reisezeit und Verzögerung (Minuten, statistikfähig),
Verkehrslage (frei/dicht/stockend/Stau aus dem Verhältnis Live- zu
Freifahrt-Zeit), Stau (Problem-Sensor ab konfigurierter Verzögerung).

**Pro Haltestelle** (Gerät): Nächste Abfahrt (Zeitstempel, HA zeigt „in X min“;
Abfahrtstafel mit Linie, Ziel, Soll/Ist und Verspätung als Attribut), Größte
Verspätung (min), Verspätungs-Alarm (Problem-Sensor ab konfigurierter Schwelle).

**Gerät „Verkehrsmeldungen Bonn"**: Aktive Meldungen (Anzahl), Gestörte
Abschnitte (Anzahl), Event „Neue Verkehrsmeldung" (Automationstrigger; feuert
nicht beim Neustart).

**Karte**: Baustellen, Autobahn-Meldungen und gestörte Stadtabschnitte als
`geo_location`-Entities (Quelle `bonnvoyage`, nutzbar im Geolocation-Trigger).

## Datenaktualisierung

| Quelle | Intervall |
|---|---|
| Parkhäuser | 2 min |
| Stadt-Verkehrslage | 5 min |
| Strecken (TomTom) | 5 min |
| Haltestellen-Abfahrten | 2 min |
| Baustellen & Autobahn-Meldungen | 30 min |

Die Intervalle sind fest (HA-Richtlinie). Einzelne Entities lassen sich über
„Automatische Aktualisierung deaktivieren" + `homeassistant.update_entity` manuell takten.
Kurze Quellen-Ausfälle werden mit den letzten Daten überbrückt; dauerhafte
Ausfälle machen nur die Entities der betroffenen Quelle `unavailable` — alles
andere läuft weiter. Nach mehr als 6 Stunden Dauerausfall erscheint eine
Reparatur-Meldung, die sich selbst auflöst, sobald die Quelle zurück ist.

## Beispiel-Automationen

```yaml
# Stau-Warnung vor Feierabend
automation:
  - alias: "Pendelstrecke staut"
    triggers:
      - trigger: state
        entity_id: binary_sensor.zuhause_buro_stau
        to: "on"
    conditions:
      - condition: time
        after: "16:00:00"
        before: "19:00:00"
    actions:
      - action: notify.mobile_app
        data:
          message: >
            Stau auf der Pendelstrecke: aktuell
            {{ states('sensor.zuhause_buro_reisezeit') }} min
            (+{{ states('sensor.zuhause_buro_verzogerung') }} min).

# Parkhaus-Tipp bei Ankunft in der Stadt
  - alias: "Freies Parkhaus melden"
    triggers:
      - trigger: zone
        entity_id: person.ich
        zone: zone.bonn_zentrum
        event: enter
    actions:
      - action: notify.mobile_app
        data:
          message: >
            Marktgarage: {{ states('sensor.marktgarage_freie_platze') }} frei
            ({{ states('sensor.marktgarage_trend') }}).

# Neue Verkehrsmeldung im Umkreis
  - alias: "Verkehrsmeldung"
    triggers:
      - trigger: state
        entity_id: event.verkehrsmeldungen_bonn_neue_verkehrsmeldung
    actions:
      - action: notify.mobile_app
        data:
          message: "{{ trigger.to_state.attributes.title }}"
```

## Bekannte Grenzen

- **TomTom Free Tier**: 20 000 Anfragen/Monat ≈ **2 Strecken** bei
  5-Minuten-Abfrage rund um die Uhr. Pendelzeitfenster-Polling ist als
  Erweiterung vorgemerkt.
- **TomTom-Nutzungsbedingungen**: Das dauerhafte Speichern von API-Ergebnissen
  (auch als Zeitreihe im Recorder) ist laut Klausel 11.4 der TomTom-ToS streng
  gelesen nur im Rahmen der Cache-Header gedeckt. Du nutzt deinen eigenen Key
  und bist selbst Vertragspartner — wenn du auf Nummer sicher gehen willst,
  schließe die Strecken-Sensoren per
  [`recorder`-Konfiguration](https://www.home-assistant.io/integrations/recorder/#exclude)
  aus oder kläre es mit dem TomTom-Support.
- **Stadt-Verkehrslage**: Die 111 Segmente tragen keine Namen — die Auswahl
  läuft über den Karten-Punkt. Nur ausgewählte Hauptstraßen werden gemessen.
- Der BCP-Feed führt einzelne tote Einträge (z. B. „karstadt", eingefroren
  seit 2018) — sie werden automatisch ausgeblendet.
- **transitous.org** ist ein community-betriebener Dienst ohne
  Verfügbarkeitsgarantie (Fair Use). Kurze Ausfälle überbrückt die Integration;
  bitte nicht mehr als eine Handvoll Haltestellen beobachten.
- Es ist genau eine BonnVoyage-Instanz möglich (Region ist fest Bonn).

## Fehlerbehebung

- **Entities `unavailable`**: Quelle gestört oder Internet weg — siehe
  Reparatur-Bereich und „Einstellungen → System → Systemzustand" (Erreichbarkeit
  je Quelle). Kurze Ausfälle heilen sich selbst.
- **Strecken-Sensoren weg + Reauth-Dialog**: TomTom-Schlüssel abgelaufen oder
  Kontingent dauerhaft überschritten — neuen Schlüssel eintragen.
- **Diagnose für Bug-Reports**: Integrationsseite → ⋮ → „Diagnose herunterladen"
  (API-Schlüssel und alle Koordinaten sind automatisch geschwärzt).
- Debug-Logs: `logger:` → `custom_components.bonnvoyage: debug`.

## Entfernen

Integration auf der Integrationsseite löschen — alle Geräte und Entities werden
entfernt. Es bleiben keine Daten zurück (der API-Schlüssel wird mit dem
Config-Eintrag gelöscht). Anschließend ggf. das HACS-Repository deinstallieren.

## Datenquellen, Lizenzen & Attribution

| Quelle | Lizenz | Attribution (wird an den Entities gesetzt) |
|---|---|---|
| Baustellen (Stadt Bonn) | CC0 1.0 | — (freiwillig: Stadt Bonn) |
| Straßenverkehrslage (Stadt Bonn) | CC BY | „Datenquelle: Bundesstadt Bonn, Amt 66, https://opendata.bonn.de" |
| Parkhaus-Belegung (BCP) | **CC BY-NC 4.0** | „Bonner City Parkraum GmbH" |
| Autobahn-API (bund.dev) | GovData-Portal | „Autobahn GmbH des Bundes" |
| TomTom Routing | TomTom-ToS (eigener Key) | „© TomTom" |
| ÖPNV-Abfahrten | transitous.org (Community, Fair Use) / DELFI | „transitous.org (DELFI)“ |

**Hinweis zur BCP-Lizenz (NC)**: Die Parkhausdaten sind nur für
nicht-kommerzielle Nutzung frei — für den privaten Smart-Home-Einsatz
unproblematisch; eine kommerzielle Nutzung braucht die Genehmigung der BCP.

## Entwicklung

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest tests/ --cov-report=json && .venv/bin/python scripts/check_coverage.py
.venv/bin/mypy custom_components/bonnvoyage && .venv/bin/ruff check custom_components tests
```

Projektplan und Architektur-Reviews: [PLAN.md](PLAN.md), [docs/](docs/).

## Lizenz

MIT — siehe [LICENSE](LICENSE). Die Lizenzen der abgerufenen Daten (Tabelle
oben) gelten unabhängig davon.
