# Review „BonnVoyage" aus Home-Assistant-Sicht (Qualitätsziel: Platinum)

> Reviewer: HA-Integrations-Spezialist · Basis: PLAN.md (Entwurf v0.1) · Stand: 2026-08-09
> Referenzen: offizielle Integration Quality Scale (developers.home-assistant.io), Config-Subentry-API (HA 2025.3+), Geolocation-Entity-Doku

---

## 0. Gesamturteil

Der Plan ist architektonisch auf dem richtigen Gleis: Subentries für Strecken/Parkhäuser, Coordinator je Quelle, `geo_location` für Baustellen und der Verzicht auf „letzte Aktualisierung"-Sensoren sind moderne, korrekte Entscheidungen. **Platinum ist erreichbar**, aber der Plan ist an mehreren Stellen zu unkonkret oder veraltet:

1. Die Subentry-API ist im Plan nur dem Namen nach richtig — die konkrete API (`async_get_supported_subentry_types`, `config_subentry_id` bei `async_add_entities`, Entry-Reload-Semantik, Mindest-HA-Version 2025.3) fehlt; die genannten Vorbilder (AccuWeather/NWS) sind falsch.
2. Das Entitäten-Modell verwendet deutsche Rohzustände und feste deutsche Entity-IDs — das kollidiert mit `has_entity_name`/`translation_key` (Bronze/Gold-Regeln).
3. Zentrale Coordinator-/Entity-Bausteine fehlen: typisiertes `runtime_data`, `config_entry=`-Pflichtargument, `_async_setup`, `PARALLEL_UPDATES`, `available`-Override, `async_unload_entry`.
4. Die Platinum-Checkliste in §6 des Plans deckt nur ~40 % der offiziellen Regeln ab (die Scale hat 54 Regeln, nicht ~15 Stichpunkte).

Wichtig fürs Erwartungsmanagement: Die Quality Scale wird **formal nur für Core-Integrationen vergeben**. Eine Custom Integration kann alle Regeln erfüllen und das per `quality_scale.yaml` dokumentieren („Platinum-konform"), bekommt aber kein offizielles Label; HACS zeigt die Scale nicht an. Das Ziel „Platinum-konform, Core-ready" ist trotzdem sinnvoll und sollte so formuliert werden.

---

## 1. Review Entitäten-Modell (Plan §3)

### 1.1 Strecken (§3.1)

| Entity (Plan) | Bewertung | Korrektur/Konkretisierung |
|---|---|---|
| `sensor.<route>_reisezeit` | Plattform korrekt | `device_class=SensorDeviceClass.DURATION`, `native_unit_of_measurement=UnitOfTime.MINUTES`, `state_class=SensorStateClass.MEASUREMENT`, `suggested_display_precision=0` |
| `sensor.<route>_verzoegerung` | Plattform korrekt | dito (`duration`, min, `measurement`); Wert nie negativ clampen |
| `sensor.<route>_verkehrslage` | Plattform korrekt, Zustände falsch | `device_class=SensorDeviceClass.ENUM` + `options`-Liste, **kein** `state_class` (ENUM und state_class schließen sich aus). Zustände als **englische/neutrale Slugs** (`free_flow`, `heavy`, `queuing`, `stationary`), deutsch nur via `translations/de.json` (`entity.sensor.<key>.state.<value>`). Deutsche Rohzustände wie `frei`/`stau` sind nicht übersetzbar und brechen `entity-translations` |
| `binary_sensor.<route>_stau` | korrekt | `BinarySensorDeviceClass.PROBLEM` ist die richtige Wahl |
| Attribute (Polyline/Geometrie) | Risiko | Große Geometrien blähen die Recorder-DB auf. Polyline entweder weglassen oder per `_unrecorded_attributes = frozenset({"polyline", ...})` von der Aufzeichnung ausschließen. Nur JSON-serialisierbare Werte |

**Grundsatzfehler im Plan:** Der Plan schreibt Entity-IDs fest (`sensor.<route>_reisezeit`). Mit `_attr_has_entity_name = True` (Bronze-Pflicht `has-entity-name`) ergibt sich der Name aus **Device-Name + übersetztem Entity-Namen** (`translation_key`); Entity-IDs werden daraus generiert und sind nicht Teil des API-Designs. Der Plan sollte stattdessen pro Entity den `translation_key` und die Device-Zuordnung definieren:

- **Ein Device pro Strecke** (`DeviceInfo(identifiers={(DOMAIN, subentry.subentry_id)}, name=<Streckenname>, entry_type=DeviceEntryType.SERVICE)`), Entities daran.
- Der „Haupt"-Sensor einer Strecke (Reisezeit) kann `_attr_name = None` erhalten → trägt den Device-Namen.

### 1.2 Parkhäuser (§3.2)

| Entity (Plan) | Bewertung | Korrektur/Konkretisierung |
|---|---|---|
| `..._freie_plaetze` | korrekt | Kein passender `device_class` (es gibt keinen für Parkplätze) → ohne device_class, `state_class=MEASUREMENT`, `native_unit_of_measurement` z. B. Rohstring (nicht übersetzbar — bewusst wählen oder weglassen), Icon via `icons.json` |
| `..._auslastung` | korrekt | `native_unit_of_measurement=PERCENTAGE`, `state_class=MEASUREMENT`, kein device_class |
| `..._trend` | korrekt | `SensorDeviceClass.ENUM`, Slugs `filling`/`stable`/`emptying`, kein state_class. Hinweis: Das BCP-XML liefert bereits ein Trend-Feld — prüfen, ob eigene Berechnung überhaupt nötig ist (Offene Frage §9 evtl. hinfällig). Kandidat für `entity_registry_enabled_default=False` (Gold `entity-disabled-by-default`) |
| `binary_sensor.<ph>_offen` | ok | Kein passender device_class (nicht `door`!). Ohne device_class + `translation_key="open"`. Semantik dokumentieren (on = geöffnet) |
| `binary_sensor.<ph>_voll` | ok | Ohne device_class oder `PROBLEM` (Geschmackssache; `PROBLEM` erzeugt „Problem erkannt"-Semantik — für „voll" vertretbar). Empfehlung: ohne device_class, `translation_key="full"` |
| Attribute lat/lon | korrekt | `latitude`/`longitude` als Attribute ist das etablierte Pattern für Map-Card/`proximity`. Kapazität/Adresse/Betreiber als statische Attribute ok (klein, unkritisch) |

**Ein Device pro Parkhaus** (Subentry), `via_device` entfällt (kein Hub-Gerät nötig). Statische Stammdaten (Kapazität) gehören in `_async_setup` des Coordinators, nicht in jeden Poll.

### 1.3 Baustellen/Meldungen (§3.3) — ist `geo_location` richtig?

**Ja, `geo_location` ist genau das richtige Pattern** — es ist exakt für „externe georeferenzierte, kurzlebige Ereignisse aus Feeds" gebaut (Vorbilder in Core: `gdacs`, `usgs_earthquakes_feed`, `nsw_rural_fire_service_feed`). Vorteile gegenüber Alternativen:

- Automatische Map-Darstellung + State = Distanz zum Referenzpunkt.
- Nativer `geo_location`-Automationstrigger (Zone enter/leave).
- Radius-Filterung ist im Feed-Manager-Pattern bereits vorgesehen.

**Aber der Plan muss das Lifecycle-Pattern benennen:**

- **Feed-Manager-Pattern**: Coordinator-Update → Diff gegen Bestand → neue Entities via `async_add_entities`, verschwundene via `async_remove(force_remove=True)` entfernen (nicht nur unavailable!).
- `geo_location`-Entities sind kurzlebig und werden üblicherweise **ohne Entity-Registry-Eintrag** geführt. Das kollidiert formal mit Bronze-Regel `entity-unique-id` → in `quality_scale.yaml` als dokumentierte **Exemption** führen (Core-Feed-Integrationen machen das genauso). Falls doch unique_ids vergeben werden: beim Entfernen auch den Registry-Eintrag löschen.
- `_attr_source = DOMAIN` („bonnvoyage") setzen — Pflicht-Property der Plattform.
- Quelle unterscheiden (Stadt-Baustelle vs. Autobahn-Warnung) über Attribut + Icon, nicht über zwei Domains.

`sensor.bonnvoyage_meldungen_aktiv`: korrekt (`state_class=MEASUREMENT`, Anzahl). Meldungsliste als Attribut **begrenzen** (z. B. max. 10 Einträge) und per `_unrecorded_attributes` von der Aufzeichnung ausschließen.

`event`-Entity: gute Wahl als Automationstrigger. Konkretisieren: eine Event-Entity am „Meldungen"-Service-Device, `event_types` explizit deklarieren (z. B. `new_roadwork`, `new_warning`, `warning_cleared`), Payload klein und JSON-serialisierbar halten. Event-Entities sind persistent → normales unique_id/Registry-Handling (kein Konflikt wie bei geo_location).

### 1.4 Diagnostik & Meta (§3.4)

- Verzicht auf `letzte_aktualisierung`-Sensoren: **richtig** (Standard-Availability-Mechanik + `last_updated` reichen; solche Sensoren gelten als Anti-Pattern).
- Ergänzen: `EntityCategory.DIAGNOSTIC` für etwaige Meta-Entities (Gold `entity-category`); die Fach-Sensoren selbst bekommen **keine** Kategorie.
- Diagnostics: `async_get_config_entry_diagnostics` + `async_redact_data` — zu redigieren sind **API-Key des Routing-Providers UND alle Koordinaten** (Home, Start/Ziel der Strecken = personenbezogene Standortdaten!). Der Plan sagt nur „redacted" — die Redact-Liste gehört in den Plan.

---

## 2. Review Architektur (Plan §4)

### 2.1 Config-Subentries — Pattern richtig, API-Beschreibung veraltet/falsch

**Die Wahl ist richtig**: Ein Entry „BonnVoyage" mit Subentries je Strecke und je Parkhaus ist genau der Anwendungsfall, für den Subentries gebaut wurden („Konfigurationsstücke, die sich eine gemeinsame Ressource teilen").

Korrekturen zum aktuellen API-Stand (eingeführt **HA 2025.3**, Dev-Blog 16.02.2025; 2026 stabil):

1. **Registrierung** über Klassenmethode am ConfigFlow — nicht (wie in älteren Entwürfen) `async_get_subentry_flow`:
   ```python
   @classmethod
   @callback
   def async_get_supported_subentry_types(
       cls, config_entry: ConfigEntry
   ) -> dict[str, type[ConfigSubentryFlow]]:
       return {"route": RouteSubentryFlow, "parking": ParkingSubentryFlow}
   ```
2. **Flow-Schritte**: `async_step_user` (Anlegen) und `async_step_reconfigure` (Ändern). Subentry-Flows kennen **kein** Reauth und **keine** Discovery.
3. **strings.json**: Texte unter `config_subentries.<type>` inkl. `initiate_flow.user` / `initiate_flow.reconfigure` und `entry_type`.
4. **Entity-/Device-Zuordnung**: Beim Aufsetzen der Plattformen über `entry.subentries` iterieren und `async_add_entities(entities, config_subentry_id=subentry.subentry_id)` aufrufen. Nur so werden Entities/Devices beim Entfernen eines Subentries automatisch aufgeräumt (erledigt zugleich Gold `stale-devices` für Strecken/Parkhäuser).
5. **Subentry-`unique_id`** muss nur innerhalb des Entries eindeutig sein (Parkhaus: BCP-ID; Strecke: Hash aus Koordinaten oder Slug).
6. **Reload-Semantik**: Anlegen/Ändern/Entfernen eines Subentries löst einen **Reload des Config-Entries** aus (kein HA-Neustart, aber auch nicht „ohne Neustart des Entry", wie der Plan formuliert). Konsequenz: `async_unload_entry` muss sauber implementiert sein (Silver `config-entry-unloading`) und Setup muss schnell/robust sein.
7. **Vorbilder korrigieren**: AccuWeather/NWS nutzen *keine* Subentries (dort: ein Entry pro Standort). Referenz-Integrationen für Subentries sind `openai_conversation` / `google_generative_ai_conversation` (Subentry je Agent) und `mqtt` (Subentry je Device).
8. **Konsequenz fürs Manifest**: Mindest-HA-Version ≥ 2025.3 (praktisch: aktuelle Version pinnen), `"single_config_entry": true` (Region ist fix Bonn → erledigt zugleich Bronze `unique-config-entry`), `"integration_type": "service"`, `"iot_class": "cloud_polling"`.

### 2.2 Coordinator je Quelle — richtig, aber unvollständig

Multi-Coordinator (BCP ~2 min / Verkehrslage ~5 min / GeoJSON+Autobahn ~30 min) ist korrekt. Es fehlen genau die Bausteine, die die Quality Scale prüft:

- **Typisiertes `runtime_data`** (Bronze `runtime-data`):
  ```python
  @dataclass
  class BonnVoyageRuntimeData:
      parking: BcpCoordinator
      incidents: IncidentsCoordinator
      routes: dict[str, RouteCoordinator]  # key = subentry_id

  type BonnVoyageConfigEntry = ConfigEntry[BonnVoyageRuntimeData]
  ```
  Kein `hass.data[DOMAIN]`.
- **`config_entry=entry`** ist Pflichtargument des `DataUpdateCoordinator`-Konstruktors (Deadline war HA 2025.11 — 2026 ist das ein Fehler, kein Warning mehr).
- **`_async_setup()`** (seit HA 2024.8) für einmalige Initialisierung je Coordinator: BCP-Stammdaten/Parkhausliste laden, Routen-Geometrie einmalig berechnen — nicht in jedem `_async_update_data`.
- **`await coordinator.async_config_entry_first_refresh()`** in `async_setup_entry` → wirft `ConfigEntryNotReady` (Bronze `test-before-setup`).
- **Fehler-Mapping**: `UpdateFailed` (transient, mit `translation_domain`/`translation_key` → Gold `exception-translations`), `ConfigEntryAuthFailed` bei ungültigem Routing-Key (→ Silver `reauthentication-flow`, Reauth am Haupt-Entry, wo der Key liegt). Timeouts explizit (`async with asyncio.timeout(...)`).
- **`PARALLEL_UPDATES = 0`** als Modul-Konstante in **jeder** Plattform-Datei (sensor/binary_sensor/geo_location/event) — bei Coordinator-basierten Read-only-Plattformen der Standard (Silver `parallel-updates`).
- **`available`-Override** je Objekt-Entity zusätzlich zu `CoordinatorEntity.available`:
  ```python
  @property
  def available(self) -> bool:
      return super().available and self._garage_id in self.coordinator.data
  ```
  (Silver `entity-unavailable`: Parkhaus fällt aus dem Feed → nur dessen Entities unavailable, nicht alle.)
- **`log-when-unavailable`** erledigt der `DataUpdateCoordinator` automatisch (loggt einmal beim Ausfall, einmal bei Rückkehr) — im Plan als „erledigt durch Coordinator" vermerken, eigene Fehler-Logs je Zyklus vermeiden.
- **Routing-Coordinator-Schnitt**: Empfehlung **ein `RouteCoordinator` pro Strecken-Subentry** (in `runtime_data.routes` gekeyt nach `subentry_id`). Vorteile: sauberes Lifecycle beim Subentry-Entfernen, individuelles Backoff, Rate-Limit-Budget pro Route kalkulierbar (TomTom Free Tier ≈ 2.500 Requests/Tag → bei 5-min-Intervall ≈ 288/Tag/Route → max. ~8 Routen; im Options-Flow validieren!).
- **Websession**: überall `async_get_clientsession(hass)` injizieren (Platinum `inject-websession`); keine eigenen Sessions, kein `requests`.
- ETag/Cache für GeoJSON gehört in die API-Client-Schicht (Coordinator hat keinen eingebauten Cache) — Ansatz ist gut.

### 2.3 Sonstiges zur Struktur (§4-Baum)

- `api/` ohne HA-Abhängigkeit: gut (testbar, später PyPI-fähig → Platinum `async-dependency` schon jetzt erfüllt, da aiohttp-basiert).
- Es fehlen im Baum: `entity.py` (gemeinsame Basisklasse, Bronze `common-modules`), `icons.json` (Gold `icon-translations`, inkl. `state`-Icons für die Enum-Sensoren), `diagnostics.py` ist da — gut, `tests/` fehlt komplett (Bronze `config-flow-test-coverage`, Silver `test-coverage` > 95 % — Basis: `pytest-homeassistant-custom-component`).
- Tooling für Platinum `strict-typing`: `mypy --strict` (oder pyright strict) + `ruff` im CI; `py.typed` ist nur relevant, wenn die API-Clients als PyPI-Lib ausgelagert werden (PEP 561) — innerhalb von `custom_components` reicht strikte CI-Prüfung.

---

## 3. Platinum-Checkliste (offizielle Quality Scale, alle Regeln)

Legende: ✅ durch Plan abgedeckt · ⚠️ teilweise/unkonkret · ❌ fehlt im Plan · ➖ n. a. (mit Begründung; in `quality_scale.yaml` als `exempt` + `comment` führen)

### Bronze

| Regel-ID | Status | Anmerkung |
|---|---|---|
| `action-setup` | ➖ | Keine eigenen Service-Actions geplant. Falls später (z. B. `bonnvoyage.refresh`): in `async_setup` registrieren, nicht in `async_setup_entry` |
| `appropriate-polling` | ✅ | Intervalle je Quelle definiert (2/5/30 min) — angemessen |
| `brands` | ❌ | PR an `home-assistant/brands` unter `custom_integrations/bonnvoyage` (Logo/Icon) einplanen |
| `common-modules` | ⚠️ | `coordinator.py` vorhanden; gemeinsame Entity-Basisklasse in `entity.py` fehlt im Baum |
| `config-flow` | ✅ | §5 vorhanden; `data_description` für alle Felder in strings.json ergänzen |
| `config-flow-test-coverage` | ⚠️ | §6 nennt „Flow 100 %", aber kein Test-Setup im Plan (pytest-homeassistant-custom-component, alle Flow-Enden inkl. Abort/Fehler) |
| `dependency-transparency` | ✅/➖ | Keine externen Requirements geplant (API-Clients inline). Falls PyPI-Auslagerung: öffentliches Repo, CI-Builds, PyPI |
| `docs-high-level-description` | ❌ | Doku-Struktur (README) nicht spezifiziert |
| `docs-installation-instructions` | ❌ | dito (HACS + manuell) |
| `docs-removal-instructions` | ❌ | dito |
| `docs-actions` | ➖ | Keine Actions → n. a., dokumentieren |
| `docs-triggers` / `docs-conditions` | ➖ | Keine eigenen Trigger/Conditions (Regeln 2026 neu, nur relevant bei eigenem Trigger-Framework) |
| `entity-event-setup` | ➖ | Keine externen Event-Subscriptions; CoordinatorEntity regelt Lifecycle |
| `entity-unique-id` | ⚠️ | Nicht spezifiziert. Schema festlegen: `{subentry_id}_{key}` bzw. `{bcp_id}_{key}`. **Exemption für geo_location** dokumentieren (kurzlebige Feed-Entities) |
| `has-entity-name` | ❌ | Nirgends erwähnt — Pflicht: `_attr_has_entity_name = True` + Device pro Strecke/Parkhaus + `translation_key` |
| `runtime-data` | ❌ | Nicht erwähnt — typisiertes `entry.runtime_data` (s. o.) |
| `test-before-configure` | ⚠️ | Key-Validierung erwähnt; zusätzlich BCP-/GeoJSON-Erreichbarkeit im Flow testen |
| `test-before-setup` | ❌ | `async_config_entry_first_refresh` → `ConfigEntryNotReady` nicht erwähnt |
| `unique-config-entry` | ❌ | Über `"single_config_entry": true` im Manifest lösen (Region fix) |

### Silver

| Regel-ID | Status | Anmerkung |
|---|---|---|
| `action-exceptions` | ➖ | Keine Actions; falls später: `HomeAssistantError`/`ServiceValidationError` mit translation_key |
| `config-entry-unloading` | ❌ | `async_unload_entry` + `entry.async_on_unload` fehlen im Plan — wegen Subentry-Reload-Semantik besonders wichtig |
| `docs-configuration-parameters` | ❌ | Options (Intervalle, Radius, Schwellwerte) dokumentieren |
| `docs-installation-parameters` | ❌ | Setup-Parameter dokumentieren |
| `entity-unavailable` | ⚠️ | §3.4 nennt es; `available`-Override je Objekt konkretisieren |
| `integration-owner` | ✅ | Über `codeowners` im Manifest |
| `log-when-unavailable` | ✅ | Durch DataUpdateCoordinator automatisch erfüllt — keine eigenen wiederholten Error-Logs |
| `parallel-updates` | ❌ | `PARALLEL_UPDATES = 0` je Plattform-Datei fehlt |
| `reauthentication-flow` | ⚠️ | §6 erwähnt Reauth für Routing-Key — konkretisieren (`ConfigEntryAuthFailed` im RouteCoordinator → Reauth am Haupt-Entry). n. a. für keylose Quellen |
| `test-coverage` | ⚠️ | Plan nennt nur Flow-Coverage; Silver verlangt **> 95 % über alle Module** |

### Gold

| Regel-ID | Status | Anmerkung |
|---|---|---|
| `devices` | ❌ | Nicht spezifiziert: Device je Strecke/Parkhaus (via Subentry), Service-Device „Meldungen"; `entry_type=SERVICE` |
| `diagnostics` | ✅ | §3.4; Redact-Liste konkretisieren (API-Key + **alle Koordinaten**) |
| `discovery` | ➖ | Cloud-/Web-Datenquellen sind nicht discoverbar (kein mDNS/SSDP) |
| `discovery-update-info` | ➖ | folgt aus `discovery` n. a. |
| `docs-data-update` | ❌ | Update-Mechanik (Polling-Intervalle je Quelle) dokumentieren |
| `docs-examples` | ⚠️ | §7 liefert Ansätze (Map-Card, Ampel) — als Automation-Beispiele in die Doku übernehmen |
| `docs-known-limitations` | ❌ | z. B. Rate-Limits Routing-Provider, BCP-Datenqualität |
| `docs-supported-devices` | ➖/❌ | Sinngemäß: unterstützte Datenquellen dokumentieren |
| `docs-supported-functions` | ❌ | Entity-Übersicht in Doku |
| `docs-troubleshooting` | ❌ | Fehlt |
| `docs-use-cases` | ⚠️ | Vision (§1) taugt als Grundlage |
| `dynamic-devices` | ➖/⚠️ | Nutzer wählt Parkhäuser/Strecken explizit via Subentry → bewusste Design-Entscheidung, als Exemption begründen. Optional (KANN): neue BCP-Parkhäuser automatisch als Subentry-Vorschlag |
| `entity-category` | ⚠️ | §6 nennt „Entity-Kategorien"; konkret: DIAGNOSTIC nur für Meta-Entities, Fachsensoren ohne Kategorie |
| `entity-device-class` | ⚠️ | `problem`/`enum` genannt; `duration` für Reisezeit/Verzögerung fehlt |
| `entity-disabled-by-default` | ❌ | Kandidaten: Trend, Auslastung (Zweitwert zu freien Plätzen) |
| `entity-translations` | ⚠️ | de/en genannt, aber ohne `has_entity_name`/`translation_key`-Mechanik (s. MUSS-1/2) |
| `exception-translations` | ❌ | `exceptions`-Block in strings.json + translation_key in allen geworfenen HA-Exceptions |
| `icon-translations` | ❌ | `icons.json` inkl. `state`-Icons für Enum-Sensoren (Verkehrslage-Ampel, Trend-Pfeile) |
| `reconfiguration-flow` | ⚠️ | Options-Flow geplant; zusätzlich `async_step_reconfigure` am Haupt-Entry (Mittelpunkt/Radius/Provider-Key) und in beiden Subentry-Flows |
| `repair-issues` | ✅ | §3.4 (dauerhafte API-Fehler; auch: Parkhaus dauerhaft aus BCP-Feed verschwunden) |
| `stale-devices` | ⚠️ | Für Subentries automatisch via `config_subentry_id`-Zuordnung; zusätzlich `async_remove_config_entry_device` implementieren |

### Platinum

| Regel-ID | Status | Anmerkung |
|---|---|---|
| `async-dependency` | ✅ | API-Clients aiohttp-basiert, vollständig async |
| `inject-websession` | ⚠️ | §4 erwähnt es; konkret: alle Clients bekommen `async_get_clientsession(hass)` injiziert |
| `strict-typing` | ⚠️ | §6 nennt mypy strict; CI-Konfiguration (mypy strict + ruff) und typisierte Coordinator-Daten (`DataUpdateCoordinator[BcpData]` etc.) in den Plan aufnehmen; `py.typed` nur bei PyPI-Auslagerung relevant |

**Bilanz**: 6 ✅ · 14 ⚠️ · 19 ❌ · 15 ➖ — der Plan trifft die richtige Richtung, aber die Mehrheit der Regeln ist noch nicht operationalisiert. Alle ➖-Regeln müssen in `quality_scale.yaml` als `exempt` mit `comment` begründet werden, sonst zählt die Regel als offen.

---

## 4. Plan-Korrekturen (priorisiert)

### MUSS (blockierend für Platinum-Anspruch bzw. sachlich falsch)

1. **Subentry-API aktualisieren (§4)**: `async_get_supported_subentry_types` (classmethod, dict `{"route": ..., "parking": ...}`), Schritte `async_step_user`/`async_step_reconfigure`, strings unter `config_subentries.<type>`, **`async_add_entities(..., config_subentry_id=...)`** für Entity-/Device-Zuordnung, Mindest-HA ≥ 2025.3 im Manifest, Formulierung „ohne Neustart des Entry" korrigieren (Subentry-Änderung ⇒ Entry-Reload). Vorbilder: `openai_conversation`, `mqtt` (nicht AccuWeather/NWS).
2. **Entitäten-Namensmodell umstellen (§3)**: keine festen (deutschen) Entity-IDs im Plan; stattdessen je Entity `translation_key` + `_attr_has_entity_name = True` + ein Device pro Strecke/Parkhaus. Enum-Zustände als englische Slugs (`free_flow`/`heavy`/`queuing`/`stationary`, `filling`/`stable`/`emptying`), Deutsch nur via Übersetzung. ENUM-Sensoren ohne `state_class`; Reisezeit/Verzögerung mit `device_class=duration`.
3. **Coordinator-Bausteine ergänzen (§4)**: typisiertes `entry.runtime_data` (Dataclass mit allen Coordinatoren; **kein** `hass.data`), `config_entry=entry` im Konstruktor (2026 Pflicht), `_async_setup()` für Stammdaten, `async_config_entry_first_refresh()` → `ConfigEntryNotReady`, sauberes `async_unload_entry`.
4. **`PARALLEL_UPDATES = 0`** in jeder Plattform-Datei und **`available`-Override** je Objekt-Entity (Parkhaus fehlt im Feed ⇒ nur dessen Entities unavailable).
5. **`"single_config_entry": true`** im Manifest (löst `unique-config-entry`); Reauth-Flow für den Routing-Key am Haupt-Entry (`ConfigEntryAuthFailed` aus dem RouteCoordinator).
6. **geo_location-Lifecycle festschreiben**: Feed-Manager-Diff mit `async_remove(force_remove=True)`; Exemption zu `entity-unique-id` für die kurzlebigen Feed-Entities in `quality_scale.yaml` dokumentieren.
7. **Recorder-Hygiene**: Polyline/Meldungslisten-Attribute begrenzen und via `_unrecorded_attributes` von der Aufzeichnung ausschließen; Attribute strikt JSON-serialisierbar.
8. **Testziel korrigieren (§6/§8 M4)**: nicht nur „Flow 100 %", sondern **> 95 % Gesamt-Coverage** (Silver `test-coverage`) mit `pytest-homeassistant-custom-component`; CI mit mypy strict + ruff + hassfest/HACS-Action.
9. **Diagnostics-Redaction konkretisieren**: API-Key **und** sämtliche Koordinaten (Home, Start/Ziel) — Standortdaten sind personenbezogen.
10. **Doku-Arbeitsstrang aufnehmen**: 13 der 54 Regeln sind `docs-*` — README-Struktur entlang der Regel-Liste (Installation, Parameter, Datenaktualisierung, Beispiele, Limitierungen, Troubleshooting, Entfernen) als eigenes Roadmap-Item.

### SOLL (für sauberes Gold/Platinum-Bild)

11. `icons.json` mit `state`-Icons für Verkehrslage (Ampel) und Trend (Pfeile); `entity-disabled-by-default` für Trend/Auslastung.
12. `exception-translations`: alle geworfenen `UpdateFailed`/`ConfigEntryNotReady`/`HomeAssistantError` mit `translation_domain`/`translation_key` + `exceptions`-Block in strings.json.
13. Event-Entity konkretisieren: `event_types` fest definieren, am Service-Device „Meldungen" aufhängen; kleine Payloads.
14. Ein `RouteCoordinator` **pro Strecken-Subentry** (Lifecycle + Rate-Limit-Budget); Intervall-Optionen im Options-Flow gegen Provider-Limits validieren (TomTom Free ≈ 2.500 req/Tag ⇒ Routenanzahl × Intervall prüfen).
15. `reconfiguration-flow` zusätzlich zum Options-Flow (Haupt-Entry und Subentries).
16. Brands-PR (`custom_integrations/bonnvoyage`) für Logo im UI.
17. `stale-devices`: `async_remove_config_entry_device` implementieren; Repair-Issue, wenn ein konfiguriertes Parkhaus dauerhaft aus dem BCP-Feed verschwindet.
18. Erwartungsmanagement in §6: „Platinum-**konform**" formulieren (offizielle Scale nur für Core; `quality_scale.yaml` als Selbstauskunft mit `exempt`-Begründungen für alle ➖-Regeln).

### KANN

19. Automatischer Subentry-Vorschlag für neue BCP-Parkhäuser (adressiert `dynamic-devices` positiv statt per Exemption).
20. Device-Diagnostics je Parkhaus/Strecke (`async_get_device_diagnostics`) zusätzlich zum Entry-Diagnostics.
21. System-Health-Handler (Erreichbarkeit der drei Quellen).
22. ETag/If-Modified-Since im GeoJSON-Client; API-Clients später als typisierte PyPI-Lib (`py.typed`, PEP 561) auslagern.
23. BCP-Trend-Feld aus dem XML nutzen statt eigener Fensterberechnung (vereinfacht §9-Frage zur Persistenz).

---

## Anhang: Quellen

- Integration Quality Scale + Regel-Checkliste: https://developers.home-assistant.io/docs/core/integration-quality-scale/ (Checklist-Unterseite mit allen Regel-IDs)
- Config-Entries/Subentry-Flows: https://developers.home-assistant.io/docs/config_entries_config_flow_handler
- Subentry-Einführung (HA 2025.3): https://developers.home-assistant.io/blog/2025/02/16/config-subentries/
- Geolocation-Entity: https://developers.home-assistant.io/docs/core/entity/geo-location
- Diagnostics: https://developers.home-assistant.io/docs/core/integration_diagnostics
