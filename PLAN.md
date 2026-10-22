# BonnVoyage — Projektplan (v1.0, konsolidiert)

> Home-Assistant-Custom-Integration, die **Open Data Bonn** und die **Echtzeitverkehrslage**
> zusammenbringt: konfigurierbare Verkehrslage für definierte Strecken sowie Parkhäuser
> mit Geodaten — visualisierbar auf der HA-Karte und in Dashboards.
>
> Qualitätsziel: **Home Assistant Quality Scale — Platinum**.
>
> **v1.0** = Entwurf v0.1 nach Einarbeitung der fünf Spezialisten-Reviews:
> [Home Assistant](docs/review-home-assistant.md) · [Architektur](docs/review-architecture.md) ·
> [Security](docs/review-security.md) · [QA](docs/review-qa.md) ·
> [Usability/Config-Flow](docs/review-ux-config-flow.md)

## 1. Vision

„Wie komme ich jetzt am besten durch Bonn — und wo parke ich?"

- **Strecken (Routen)**: Nutzerdefinierte Strecken (z. B. „Zuhause → Büro über A565")
  mit Live-Reisezeit, Verzögerung und Verkehrslage-Level.
- **Parkhäuser**: Live-Belegung der Bonner Parkhäuser (BCP) inkl. Geodaten
  (Koordinaten als Entity-Attribute → Map-Card, proximity-Automationen).
- **Baustellen & Verkehrsmeldungen**: GeoJSON der Stadt Bonn und Autobahn-API-Warnungen
  als `geo_location`-Entities im konfigurierbaren Radius.

Designprinzip: **abgeleitete, automatisierbare Signale statt maximaler Datensammlung** —
jede Entity ermöglicht eine Automation oder Visualisierung.

## 2. Datenquellen

### Verifiziert (Stand 08/2026)

| Quelle | Endpunkt | Auth | Inhalt | Intervall |
|---|---|---|---|---|
| BCP Bonn (Parkhäuser) | `https://www.bcp-bonn.de/stellplatz/bcpext.xml` | keine | Live-Belegung (XML) | 2 min |
| Stadt Bonn Geoportal (Baustellen) | `https://stadtplan.bonn.de/geojson?Thema=14403` | keine | Baustellen-GeoJSON | 30 min |
| Autobahn-API (bund.dev) | `https://verkehr.autobahn.de/o/autobahn/` | keine | Warnungen/Sperrungen A565/A59/A555 | 30 min |

### Optional (für Strecken)

| Quelle | Zweck | Anmerkung |
|---|---|---|
| Routing-Provider (TomTom zuerst; HERE/OSRM später) | Reisezeit & Delay pro Strecke | API-Key; Provider-Abstraktion mit Capability-Flags; 5-min-Intervall |

**Request-Budget** (Architektur-Review): 5-min-Intervall = 288 Requests/Tag/Route →
TomTom Free Tier trägt max. ~8 Routen, HERE ~3. Globaler Rate-Limiter, Budget in Doku ausweisen.

**Harte Gates**: Lizenz/ToS-Klärung BCP + stadtplan.bonn.de (inkl. Attribution) ist
Release-Blocker vor HACS-Veröffentlichung — nicht nur „offene Frage".
Alle Endpunkte sind **hart kodiert** (keine benutzerdefinierten URLs → SSRF-Klasse eliminiert);
Routing-Provider nur als Enum mit fixer Basis-URL.

## 3. Funktionsumfang (Entitäten-Modell)

Namensmodell (HA-Review): durchgängig `has_entity_name=True` + `translation_key`
(de/en), **ein Device pro Strecke und pro Parkhaus**, keine fest kodierten deutschen
Entity-IDs. Enum-Zustände sind englische Slugs mit deutscher Übersetzung.

### 3.1 Strecken (pro Route-Subentry, je ein Device)

| Entity | Details |
|---|---|
| Reisezeit | `sensor`, `device_class: duration`, min, `state_class: measurement` |
| Verzögerung | `sensor`, `device_class: duration`, min, `state_class: measurement` |
| Verkehrslage | `sensor`, `device_class: enum`, **ohne** `state_class`; Zustände `free_flow` / `heavy` / `queuing` / `congested` |
| Stau | `binary_sensor`, `device_class: problem`; Schwelle je Strecke konfigurierbar (Default 10 min) |

Attribute: Start-/Ziel-Koordinaten, Routen-Geometrie (Polyline via
`_unrecorded_attributes` vom Recorder ausgeschlossen), betroffene Meldungen (best effort).
Bei Providern ohne Live-Traffic (OSRM): Delay-/Stau-Entities **konditional nicht anlegen**.

### 3.2 Parkhäuser (pro Parkhaus-Subentry, je ein Device)

| Entity | Details |
|---|---|
| Freie Plätze | `sensor`, `state_class: measurement` |
| Auslastung | `sensor`, %, `state_class: measurement` |
| Trend | `sensor`, `device_class: enum`: `filling` / `steady` / `emptying` |
| Geöffnet | `binary_sensor` |
| Voll | `binary_sensor` |

Attribute: `latitude`/`longitude` (→ Map-Card), Kapazität, Adresse, Betreiber.
**Trend-Logik wird vor Implementierung spezifiziert** (QA-Gate): Fensterbreite,
Hysterese, Verhalten bei Neustart/Datenlücken.

### 3.3 Verkehrsmeldungen & Baustellen

- `geo_location`-Entities (Quelle `bonnvoyage`) im Radius um konfigurierbaren
  Mittelpunkt (Default: HA-Home, 10 km); Feed-Manager-Lifecycle mit
  `async_remove(force_remove=True)`; `entity-unique-id`-Exemption in `quality_scale.yaml`.
- **Stable-ID-Strategie** (Architektur-MUSS, vor M2): native Quell-ID, sonst Hash
  normalisierter Felder — verhindert Entity-Churn und Event-Duplikate.
- `sensor` „Aktive Meldungen" (Anzahl; Meldungsliste als unrecorded Attribut)
- `event`-Entity für neue relevante Meldungen (Automationstrigger)

### 3.4 Diagnostik & Meta

- Diagnostics-Export mit Redaction von **API-Key und allen Koordinaten/Routen-Namen**
  (ab der ersten Diagnostics-Zeile, nicht nachgerüstet)
- Repair-Issues bei dauerhaften API-Fehlern (erst nach > 6 h Ausfall, s. § 4)
- System Health: Erreichbarkeit der Quellen

## 4. Architektur

```
custom_components/bonnvoyage/
├── __init__.py           # Setup, entry.runtime_data (typisiert), async_unload_entry
├── config_flow.py        # Flow + Subentry-Flows (route, parking), Reauth, Reconfigure
├── coordinator.py        # DataUpdateCoordinatoren (s. Topologie)
├── api/                  # Reine API-Schicht, HA-frei, einzeln testbar
│   ├── models.py         # Frozen/slots-Dataclasses: ParkingFacility/ParkingStatus,
│   │                     #   RouteRequest/RouteStatus, TrafficMessage, Geometrie-Union
│   ├── exceptions.py     # Taxonomie: ParseError/ConnectionError/AuthError/RateLimitError
│   ├── validation.py     # Zentrale Validierung: Typen, Wertebereiche, String-/Mengen-Caps,
│   │                     #   Bounding-Box-Check aller Koordinaten
│   ├── bcp.py            # Parkhaus-XML (defusedxml!)
│   ├── bonn_geo.py       # GeoJSON stadtplan.bonn.de
│   ├── autobahn.py       # Autobahn-API
│   └── routing/          # Port: Protocol + Capability-Flags (supports_live_traffic)
│       ├── __init__.py   #   + Provider-Registry
│       └── tomtom.py     # Referenz-Adapter
├── entity.py             # Basisklassen (has_entity_name, translation_key, available)
├── sensor.py / binary_sensor.py / geo_location.py / event.py   # je PARALLEL_UPDATES = 0
├── diagnostics.py, repairs.py, system_health.py
├── manifest.json         # single_config_entry: true, Mindest-HA-Version (Subentry-API!)
├── strings.json, translations/ (de, en)
└── quality_scale.yaml
```

### Kernentscheidungen (ADR-würdig, Details in den Reviews)

1. **Ein `ConfigEntry`** (`single_config_entry: true`) mit **Config-Subentries** je
   Strecke/Parkhaus. Korrekte API (HA ≥ 2025.3): `async_get_supported_subentry_types`
   (classmethod), Schritte `async_step_user`/`async_step_reconfigure`, Entities via
   `async_add_entities(..., config_subentry_id=...)`; Subentry-Änderung löst
   Entry-Reload aus. Vorbilder: `openai_conversation`, `mqtt`.
2. **Coordinator-Topologie**: je ein Coordinator für BCP, Baustellen, Autobahn-Meldungen;
   **ein Coordinator pro Route-Subentry** + gemeinsamer Provider-Client + globaler
   Rate-Limiter. Invariante: **eine Entity ↔ ein Coordinator**; Route↔Meldungs-Enrichment
   nur best effort.
3. **Coordinator-Bausteine**: `config_entry=entry` (Pflicht), `_async_setup()` für
   Stammdaten, `async_config_entry_first_refresh()` → `ConfigEntryNotReady`;
   Exceptions werden **nur im Coordinator** auf `UpdateFailed`/`ConfigEntryAuthFailed`
   gemappt — API-Clients werfen ausschließlich die eigene Taxonomie und geben nur
   Domänentypen zurück.
4. **Fehler-/Availability-Zustandsmodell**: OK → DEGRADED → DOWN mit Stale-Toleranz
   je Quelle (Parken 2, Routen 1, Meldungen 3 Fehlschläge, dann `unavailable`);
   Repair-Issue erst nach > 6 h. Quellen-Ausfälle sind **isoliert**: Routing down
   berührt Parkhaus-Entities nicht. `available`-Override pro Objekt (fehlt ein
   Parkhaus im Feed ⇒ nur dessen Entities unavailable).
5. **Feste Update-Intervalle** (2/5/30 min) — **nicht** nutzerkonfigurierbar
   (Quality-Scale-Regel `appropriate-polling`; Power-User: `homeassistant.update_entity`).
6. **Erweiterbarkeit**: neue stadtplan.bonn.de-Themen-Layer = neuer Client + Coordinator
   hinter derselben Validierungs-/Modell-Schicht, ohne Umbau.

### Netzwerk & Robustheit (Security-MUSS, ab M1)

- Injizierte HA-`aiohttp`-Session; **strikte TLS-Verifikation, kein Verify-Off-Schalter**
  (weder Code noch UI); kaputtes Zertifikat → Repair-Issue, fail closed
- **Timeouts + harte Response-Size-Caps** (gestreamt lesen) für alle Quellen
- **defusedxml** (gepinnt) für BCP-XML (XXE/Billion-Laughs)
- User-Agent-Kennung (`BonnVoyage/<version>`), Jitter, Exponential Backoff,
  `Retry-After`-Respekt gegenüber den kommunalen APIs
- `slugify` für alle IDs aus Fremddaten

## 5. Config-Flow (UX-final, Details + fertige de-Texte im [UX-Review](docs/review-ux-config-flow.md))

**Ersteinrichtung** (Schnellpfad: 3 Klicks, keine Tastatur, sofort gefüllte Karte):

```
user        Meldungsbereich: EIN LocationSelector mit Radius (Default Home + 10 km)
  → parking     Parkhaus-Multi-Select, nach Entfernung sortiert („Marktgarage (0,6 km)"),
                ≤ 3 km vorausgewählt (max. 5); BCP down ⇒ „Ohne Parkhäuser fortfahren"
  → routes_menu MENÜ (async_show_menu), kein Formular mit optionalem Key-Feld:
                „Reisezeit-Anbieter jetzt einrichten (API-Schlüssel erforderlich)"
                / „Ohne Strecken abschließen – jederzeit nachholbar"
  → [provider]  Anbieter + Key, Validierung per Testrequest; differenzierte Fehler
                (invalid_auth / cannot_connect / rate_limited); Privacy-Hinweis:
                Strecken-Koordinaten gehen an den Anbieter
  → Entry + Parkhaus-Subentries (subentries=[...] beim Create)
```

**Subentry „Strecke hinzufügen"** — 3-Schritt-Wizard, kein Dead-End:

- Fehlt der Provider-Key: Provider-Schritt **inline vorschalten** (kein Abort)
- Start (1/3) und Ziel (2/3) je als „Zone oder Karte" (SelectSelector aus `zone.*`
  + Option „Auf Karte wählen…" → LocationSelector); **keine Koordinaten-Textfelder**
- Abschluss (3/3): Name (automatisch vorbefüllt, z. B. „Zuhause → Büro"),
  Stau-Schwelle (Default 10 min), optional Via hinter Checkbox
- **Proberouting** vor dem Anlegen (`route_not_found`, `points_too_close`);
  Namens-Duplikatprüfung

**Subentry „Parkhaus hinzufügen"**: Single-Select (ein Subentry pro Durchlauf),
bereits konfigurierte ausgeblendet, `unique_id` = BCP-ID.

**Options-Flow**: **nur** Meldungsbereich. Keine Intervalle (s. § 4), keine
Schwellwerte (→ Subentry-Reconfigure „Strecke anpassen"). Provider/Key-Wechsel via
Reconfigure des Haupt-Entries; ungültig gewordener Key → **Reauth-Flow** + Repair-Issue
(Parkhäuser/Meldungen laufen weiter).

`data_description` für jedes Feld; keine Fachbegriffe in Nutzertexten; de/en vollständig.

## 6. Security & Privacy (Kurzfassung, Details im [Security-Review](docs/review-security.md))

- API-Key nur in `ConfigEntry.data`; nie in Logs/Attributen; **keine vollen
  Provider-URLs loggen** (TomTom/HERE tragen den Key im Query-String)
- Privacy-Hinweis im Config-Flow **vor** Key-Eingabe; Datenminimierung: nur Koordinaten
  an den Provider (keine Namen/IDs), Rundung auf 4 Nachkommastellen als Default
- Diagnostics-Redaction: Key **und** Home-/Routen-Koordinaten/Routen-Namen
- Supply Chain: Requirements exakt gepinnt; Dependabot (pip + actions) ab Repo-Anlage;
  GitHub Actions SHA-gepinnt, minimale Workflow-Permissions; HACS-Release nur als
  CI-gebautes, getaggtes GitHub-Release

## 7. QA & CI (Kurzfassung, Details im [QA-Review](docs/review-qa.md))

**Test-Infrastruktur ist M1-Deliverable** (100 % Config-Flow-Coverage ist Bronze-Regel!):

- pytest + pytest-homeassistant-custom-component, syrupy-Snapshots, aioresponses;
  **pytest-socket** (keine Live-Calls in Unit-Tests)
- **Fixture-Korpus aus echten API-Antworten** (bcpext.xml, GeoJSON, Autobahn-JSON)
  inkl. kaputter/leerer/übergroßer/bösartiger (XXE) **und Encoding-Varianten** (Umlaute!)
- Coverage-Gates maschinell erzwungen: Flows 100 %, `api/` ≥ 95 %, Rest ≥ 90 %,
  global ≥ 92 %, Branch-Coverage; Gesamtziel > 95 % (Platinum-Anspruch)
- CI ab erstem Commit: hassfest, HACS-Validation, ruff, **mypy --strict**,
  pytest-Matrix (aktuelle + vorherige HA-Version, monatlicher Bump + dev-Frühwarn-Job)
- Nightly-Contract-Job gegen Live-APIs (nicht-blockierend, Auto-Issue bei API-Drift)
- Reauth-Flow-Tests mit 100 % Coverage (M3)

## 8. Quality Scale — Stand & Weg (54-Regeln-Checkliste im [HA-Review](docs/review-home-assistant.md))

Ausgangslage laut Review: 6 Regeln durch den Plan erfüllt, 14 teilweise, 19 fehlend,
15 n. a. (Exemptions in `quality_scale.yaml` begründen — u. a. `entity-unique-id`
für `geo_location`). Die 13 `docs-*`-Regeln bilden einen eigenen
**Doku-Arbeitsstrang** in der Roadmap (Installation, Beispiele, Automations-Rezepte,
Troubleshooting, Entfernen-Anleitung).

## 9. Visualisierung (Doku-Beispiele)

**Klarstellung Kartenbasis:** `stadtplan.bonn.de` ist ausschließlich eine
**Daten-API** (GeoJSON-Geometrien der Baustellen) — es wird nie eine Karte von
dort angezeigt. Die gesamte Darstellung läuft über die
**Home-Assistant-Standardkarte** (Map-Card auf OpenStreetMap-Basis): Parkhäuser
tragen `latitude`/`longitude` als Attribute, Baustellen kommen als
`geo_location`-Entities — beides erscheint damit nativ auf der HA-Karte, ohne
zusätzliche Kartenkomponente. Wichtiger Befund (M1): Der BCP-Feed selbst
liefert **keine Koordinaten**; die Integration bringt daher statische
Stammdaten der ~14 BCP-Parkhäuser mit (Verifikation gegen OSM = Release-Gate).

- Map-Card mit Parkhaus-Devices + `geo_location`-Baustellen
- „Pendel-Ampel"-Dashboard (Verkehrslage-Enum → Farbe)
- Grafana/TimescaleDB: Langzeit-Belegungskurven (bestehende Infra des Nutzers)

## 10. Roadmap

| Milestone | Inhalt | Gates |
|---|---|---|
| **M1 — Parkhäuser** ✅ *(implementiert 09.08.2026)* | BCP-Client (defusedxml, Size-Caps, Validierungsschicht), Modelle + Exception-Taxonomie, Coordinator inkl. Trend + Stale-Toleranz, Ersteinrichtungs-Flow (user → parking, Fehler-Menü), Parkhaus-Subentry-Flow, Options-Flow, Parkhaus-Devices (de/en) | ✅ CI (mypy strict, ruff, hassfest, HACS, Coverage-Gates per Skript), ✅ Fixture-Korpus (echt/kaputt/leer/XXE/übergroß/ungültig), ✅ [Trend-Spezifikation](docs/spec-trend.md), ✅ Flow 100 %, gesamt 98 % — **offen: Koordinaten-Verifikation gegen OSM (vor Release)**. M1-Befunde: Feed ohne Koordinaten (→ statische Stammdaten), iso-8859-1, „karstadt" seit 2018 eingefroren (→ 24-h-Filter im Flow, 2-h-Stale-Regel zur Laufzeit), `status` 0=offen/2=zu, `tendenz` undokumentiert (ignoriert). `routes_menu` bewusst auf M3 verschoben (kein totes Menü ausliefern). |
| **M2 — Meldungen** ✅ *(implementiert 09.08.2026)* | Baustellen- + Autobahn-Clients (gemeinsamer Size-Cap-HTTP-Helper), `MessagesCoordinator`-Basis mit Radius-Filter und Stale-Toleranz 3, `geo_location`-Lifecycle (Add via Coordinator-Listener, Remove via Registry-Entfernung → HA force-removed die Entity; Orphan-Cleanup beim Setup), Event-Entity „Neue Verkehrsmeldung" (Baseline nach Neustart feuert nicht), Sensor „Aktive Meldungen", Setup-Robustheit (Meldungsquellen nicht setup-kritisch; BCP nur bei vorhandenen Parkhaus-Subentries) | ✅ Stable IDs nativ (`baustelle_id`, Autobahn-`identifier` — Eindeutigkeit live verifiziert), ✅ Lifecycle-/Isolations-/Radius-Tests mit echten Fixtures. M2-Befunde: Baustellen-Layer liefert aktuell nur `Point`-Geometrien (Line/Polygon defensiv abgedeckt); Autobahn-`isBlocked` kommt als **String** `"false"`; `warning`/`roadworks`/`closure` sind getrennte Services (9 Requests parallel, Teilausfall toleriert). `entity-unique-id`-Exemption entfällt — geo_location-Entities tragen entry-scoped unique_ids. |
| **M3 — Verkehrslage & Strecken** ✅ *(implementiert 09.08.2026, Scope per User-Entscheid erweitert)* | **Teil A (neu, auth-frei):** Stadt-Verkehrslage-Layer Thema 19584 (111 Segmente, 5-min-Takt) — gestörte Segmente automatisch als `geo_location` im Radius + Zähler-Sensor; Subentry „Stadtstrecke beobachten" (Karten-Punkt → nächstgelegenes Segment als Device mit Status/Tempo/Stau). **Teil B (optional):** Routing-Port (`api/routing/` Protocol+Registry) + TomTom-Adapter (Koordinaten-Rundung 4 Dez., `summaryOnly`, Semaphore-Rate-Limiter, Key nie in Logs), RouteCoordinator je Subentry (`ApiAuthError`→Reauth), Strecken-Wizard (Zone-oder-Karte, Via, Proberouting), Reauth-Flow, Privacy+Budget-Hinweis im Flow | ✅ Reauth & Flow 100 % Coverage, ✅ Proberouting-Fehlerpfade, ✅ Request-Budget-Doku (**TomTom-Tarif geändert: 20 000/Monat ≈ 2 Strecken 24/7** → Zeitfenster-Polling als M4-Kandidat). Befunde: Segment-Layer ohne Namen (Auswahl via Karte), Status-Vokabular defensiv gemappt (unbekannt → unknown). **Offen: TomTom-Live-Contract-Test, sobald API-Key vorhanden** (Fixtures doku-basiert). |
| **M4 — Platinum & Release** ✅ *(implementiert 09.08.2026)* | ✅ Diagnostics (Key + alle Koordinaten redigiert, per Test verifiziert), ✅ Repair-Issues nach 6 h Dauerausfall je Quelle (SourceHealthMixin, selbstauflösend), ✅ System Health, ✅ icons.json, ✅ Doku-Strang (README: Installation, Konfiguration, Entities, Intervalle, Beispiel-Automationen, Grenzen, Troubleshooting, Entfernen, Lizenzen), ✅ quality_scale.yaml finalisiert | ✅ **Lizenzen geklärt (09.08.2026)**: Baustellen CC0; Verkehrslage CC BY (Pflichttext „Datenquelle: Bundesstadt Bonn, Amt 66, https://opendata.bonn.de“ an den Entities gesetzt); BCP **CC BY-NC 4.0** (Attribution „Bonner City Parkraum GmbH“ gesetzt; NC: privat ok, kommerziell nur mit BCP-Genehmigung, im README dokumentiert); TomTom: BYO-Key-Modell zulässig (FAQ: auch kommerziell), Attribution „© TomTom“ gesetzt, **Klausel 11.4 (Caching) deckt Recorder-Langzeitspeicherung streng gelesen nicht** — als Grauzone im README dokumentiert (Recorder-Exclude-Hinweis). ✅ Parkhaus-Koordinaten gegen OSM verifiziert (12/14 via operator-Tag; beetplatz+karstadt Schätzung). Offen: Brands-PR (für HACS-Default-Aufnahme), Repo public schalten (User-Entscheidung), TomTom-Live-Contract-Test (Key fehlt). |
| **0.7.2 — geo_location-Churn** ✅ *(22.10.2026, Live-Befund: Hunderte `deleted_entities`)* | Kein Pauschal-Wipe mehr beim Setup/Reload: Orphan-Cleanup je Quelle erst nach dem ersten vollständigen Abruf, nie nach Fehl- oder Teilabruf. Entfernen erst nach 2 aufeinanderfolgenden vollständigen Abfragen ohne die Meldung/den Abschnitt (`GEO_REMOVAL_GRACE_POLLS`); fällt eine Autobahn/ein Service aus, bleiben deren Entities stehen. INRIX-`vi-avl`-Warnungen (Zähler im `identifier` wechselt bei jedem Update, ~80 % des Churns) ohne Karten-Entity, aber weiter in Event und `active_messages`. Abschnittsname ohne Tempo; `speed_kmh`/`measured_at` als `_unrecorded_attributes`. Altlasten in `deleted_entities` nur dokumentiert (README), kein Auto-Cleanup. | Tests: kein Wipe beim Setup/Reload, Grace-Removal, Flattern, Teilausfall, INRIX ohne geo-Entity, unrecorded attributes. Offen für 0.8: stabile INRIX-Basis-ID (falls die API eine liefert), Altlasten-Cleanup als Repair-Flow. |
| **M5c.1 — Korridorbreiten-Slider** ✅ *(09.08.2026, User-Feedback: Reuterstraße fehlte)* | Korridor = Luftlinie (dokumentiert); im Abschnitts-Schritt jetzt Breiten-Slider 200–3000 m — Änderung + OK lädt die Kandidatenliste neu (HA-Dialoge können nicht live rendern), Abwahlen bleiben erhalten, neue Kandidaten vorausgewählt. |
| **M5c — Korridor-Modus** ✅ *(09.08.2026, User-Feedback zum 0.6-UI)* | Stadtstrecken-Flow erneut umgebaut: Statt Punkt-und-Kandidaten jetzt Start→Ziel (Zone-oder-Karte) → Multiauswahl aller Messabschnitte im 800-m-Korridor (sortiert von Start nach Ziel, alle vorausgewählt, Namen aus Cache/Nominatim) → EIN Aggregat-Device pro Achse (worst status, min speed, disrupted count, Stau-Binary; Details als Attribut). Löst das Richtungs-Verwirrspiel: beide Richtungen einfach beide angehakt. Legacy-Einzel-Subentries laufen im Kompat-Pfad weiter. Grafische Auswahl im Dialog bleibt HA-Plattform-Grenze (LocationSelector ohne Overlays). |
| **M5b — Segment-Auswahl-Redesign** ✅ *(09.08.2026, UX-Review-getrieben)* | 3-Schritte-Flow Stadtstrecke: Punkt, dann benannte Kandidaten (Straßenname via OSM Nominatim zoom=16, verifiziert; Richtung als 8er-Kompass aus der Geometrie-Peilung; Entfernung Punkt-zu-Polylinie statt Punkt-zu-Stützpunkt; Länge; Live-Status), dann expliziter Bestätigungs- und Namensschritt (vorbefüllt). Persistenter Namens-Cache (Store, verfällt nie), Geocoder-Ausfall blockiert nie (Fallback Abschnitt #N + Hinweis), Escape-Hatch Punkt-neu-setzen, beobachtete Abschnitte sichtbar markiert. street_name/direction im Subentry persistiert. | Nominatim statt Photon (eigener Test: Photon liefert Gebäude- statt Straßentreffer). Befund: freezegun friert time.monotonic ein, asyncio-Sleeps hängen in Tests — Rate-Limiter-Sleep entfernt, Fair Use über Cache + Sequenzialität. |
| **M5 — ÖPNV** ✅ *(implementiert 09.08.2026, Quelle geändert)* | Statt VRS-Vertrags-API: **transitous.org** (MOTIS/DELFI, key- und vertragsfrei, live verifiziert — SWB-Trams und Busse mit Echtzeit). Subentry „Haltestelle beobachten“ (Namenssuche → Auswahl nach Entfernung), pro Stop: Nächste Abfahrt (Timestamp + Abfahrtstafel-Attribut), Größte Verspätung, Verspätungs-Alarm (Schwelle konfigurierbar, Default 5 min). Intervall 2 min, Stale-Toleranz 2, nicht setup-kritisch (Community-Dienst ohne SLA), SourceHealth-Repairs inklusive. Attribution „transitous.org (DELFI)“. | Fixtures aus echten Antworten; Flow-/Entity-/Fehlerpfad-Tests; Fair-Use im README dokumentiert. VRS-Vertrags-API bleibt Alternative, falls transitous entfällt. |

## 11. Entschiedene / verbliebene Fragen

**Entschieden durch Reviews:**
- Update-Intervalle fest, nicht konfigurierbar (`appropriate-polling`)
- Erster Routing-Provider: TomTom (Free Tier ~8 Routen); OSRM später ohne Delay-Entities
- Parkhaus-Auswahl in der Ersteinrichtung als Multi-Select, Subentry-Flow single
- Koordinaten-Eingabe ausschließlich Zone-oder-Karte, nie Freitext

**Noch offen (vor jeweiligem Milestone zu klären):**
- Lizenzlage BCP-XML & stadtplan.bonn.de → **vor M4-Release zwingend** (schriftliche Anfrage erwägen)
- Trend-Spezifikation (Fenster, Hysterese, Persistenz) → vor M1-Implementierung
- Mindest-HA-Version exakt festlegen (Subentry-API-Features) → vor M1
