# Architektur-Review „BonnVoyage" (PLAN.md v0.1)

Reviewer: Architektur-Review (Team-Rolle IT-Architektur) — Stand: 2026-08-09
Gegenstand: `/Users/thorstenhornung/Documents/projects/hass_explore/bonnvoyage/PLAN.md`, Abschnitt 4 (Grobentwurf) sowie Fehler-, Daten- und Erweiterungsarchitektur.

---

## 1. Gesamturteil

Der Grobentwurf ist **tragfähig und folgt den richtigen HA-Patterns** (ein ConfigEntry + Subentries, Coordinator je Quelle, HA-freie API-Clients). Die größten Lücken sind nicht strukturell, sondern **Spezifikationslücken**: Es fehlen (a) eine explizite Domänenmodell-Schicht, (b) eine definierte Fehler-Taxonomie mit Availability-Zustandsmodell, (c) die Entscheidung zur Coordinator-Topologie für Strecken (Request-Budget!), und (d) eine Stable-ID-Strategie für `geo_location`/Events. Diese vier Punkte sind MUSS-Korrekturen, bevor M1 startet — sie sind nachträglich teuer zu ändern.

---

## 2. Bewertung Grobentwurf (Abschnitt 4)

### 2.1 Modulschnitt

**Positiv:**
- `api/` ohne HA-Abhängigkeit ist korrektes Ports-&-Adapters-Fundament und Platinum-konform (injizierte `aiohttp.ClientSession`, einzeln testbar, später als PyPI-Lib auslagerbar).
- Plattform-Dateien (`sensor.py`, `geo_location.py`, …) als dünne Adapter über Coordinator-Daten: richtig.

**Lücken im Modulbaum:**

| Fehlt | Warum nötig |
|---|---|
| `api/models.py` | Ohne explizite Domänentypen geben Clients `dict`/ElementTree zurück → Parsing-Wissen leckt in die HA-Schicht, mypy-strict wird unmöglich. Clients dürfen **ausschließlich Domänentypen** zurückgeben. |
| `api/exceptions.py` | Gemeinsame Fehler-Taxonomie ist Teil des Ports (siehe Kap. 3). |
| `entity.py` | Basisklassen (`CoordinatorEntity`-Ableitungen, DeviceInfo, `_attr_has_entity_name`) — sonst Copy-Paste über 4 Plattformen. |
| `const.py` | Domain, Intervalle, Timeouts, Defaults zentral. |
| Typisiertes `runtime_data` | `ConfigEntry.runtime_data` als frozen dataclass (`BonnVoyageRuntimeData` mit allen Coordinatoren) statt `hass.data[DOMAIN]` — Platinum-Typing-Voraussetzung. |

**Wichtige Schnittregel (explizit in den Plan aufnehmen):** Abgeleitete Signale — Trend (`fuellt_sich`/…), Verkehrslage-Enum aus Delay, Stau-Binary via Schwellwert — gehören in die **HA-Schicht** (Coordinator/Entity), nicht in die API-Clients. Die Clients liefern Fakten (Zahlen, Zeiten, Geometrien), die Integration leitet Signale ab. Das hält die Clients quellen-treu und die Ableitungslogik zentral testbar.

### 2.2 Coordinator-pro-Quelle

Ansatz korrekt: Isolation von Intervall, Fehlerzustand und Availability je Quelle. Drei Präzisierungen nötig:

1. **Strecken-Topologie ist unentschieden.** Optionen:
   - (a) Ein Coordinator **pro Route-Subentry** (empfohlen): Availability pro Route sauber, Subentry-Unload trivial (Coordinator mit-entsorgen), Fehler einer Route (z. B. unroutbare Koordinaten) betreffen andere Routen nicht.
   - (b) Ein Sammel-Coordinator für alle Routen: weniger Objekte, aber Alles-oder-nichts-Availability und Subentry-Lifecycle verwoben.
   - Bei (a) zwingend: **gemeinsamer Provider-Client + globaler Rate-Limiter** (Semaphore + Mindestabstand), damit N Routen den Provider nicht parallel fluten.
   - **Request-Budget rechnen und in den Plan schreiben:** 5-min-Intervall = 288 Requests/Tag/Route. TomTom Free ~2.500/Tag → max. ~8 Routen; HERE Free ~1.000/Tag → max. ~3. Konsequenz: Intervall ggf. pro Provider ableiten, nicht global fix.

2. **Backoff/Jitter/ETag sind keine `DataUpdateCoordinator`-Bordmittel** — der Plan nennt sie, sagt aber nicht, wo sie leben:
   - **Jitter**: nur beim Scheduling des Erst-Refresh (versetzter Start der Coordinatoren), nicht pro Tick — HA verteilt Intervall-Ticks bereits.
   - **Backoff**: Coordinator wertet `ApiRateLimitError.retry_after` aus und setzt ein „not before"-Fenster im Client; innerhalb des Fensters liefert der Client sofort Cache bzw. der Coordinator überspringt. Kein Retry-in-Loop im Client (blockiert den Update-Slot).
   - **ETag/Last-Modified**: als Conditional GET **im Client** (Client hält ETag + letzten Parse; 304 → letzten Zustand zurückgeben). Achtung: erst **verifizieren**, ob `bcp-bonn.de` und `stadtplan.bonn.de` das überhaupt unterstützen — sonst nur client-seitiger TTL-Cache. Dazu: eigener `User-Agent` (`BonnVoyage/x.y (+repo-url)`) — höfliches Polling, da viele HA-Instanzen dieselben städtischen Endpunkte treffen könnten.

3. **Intervall-Option im Options-Flow streichen (Abschnitt 5.5).** Die HA Quality Scale (Rule `appropriate-polling`) sieht **feste, angemessene Intervalle** vor; Nutzer können Polling deaktivieren und `homeassistant.update_entity` automatisieren. Radius/Schwellwerte im Options-Flow: ok.

### 2.3 Provider-Abstraktion Reisezeit (Ports & Adapters)

`routing.py` als **eine Datei** ist zu klein geschnitten. Empfehlung: Paket

```
api/routing/
├── __init__.py      # Registry: PROVIDERS: dict[str, type[RoutingProvider]]
├── base.py          # Port (Protocol) + RouteRequest/RouteStatus (oder Re-Export aus models)
├── tomtom.py        # Referenz-Adapter (M3)
├── here.py          # später
└── osrm.py          # später
```

**Port-Skizze (Interface aus Domänensicht, nicht aus Provider-Sicht):**

```python
class RoutingProvider(Protocol):
    """Port für Reisezeit-Dienste. Adapter werfen ausschließlich BonnVoyageApiError-Subtypen."""

    @property
    def supports_live_traffic(self) -> bool: ...   # OSRM: False!
    @property
    def supports_geometry(self) -> bool: ...

    async def validate_credentials(self) -> None: ...          # Config-Flow-Check
    async def get_route_status(self, request: RouteRequest) -> RouteStatus: ...
```

**Tragfähigkeits-Bewertung:** Ja, tragfähig — unter vier Bedingungen:
1. Das Interface wird von den **Entity-Bedürfnissen** definiert (Reisezeit, Free-Flow-Zeit, Distanz, Geometrie), nicht von Provider-Feature-Sets. Kein Durchreichen provider-spezifischer Rohdaten durch den Port.
2. **Capability-Flags** statt kleinster gemeinsamer Nenner: OSRM (selbst gehostet) hat **keine Live-Verkehrslage** → `free_flow_time=None`, Delay-/Stau-Entities werden für diese Routen gar nicht erst angelegt (statt dauerhaft `0`/irreführend). Das muss der Config-Flow kommunizieren.
3. Die **Fehler-Taxonomie ist Teil des Ports**: Adapter normalisieren Provider-Fehler (Quota, Key ungültig, unroutbar) auf die gemeinsamen Exceptions — sonst leckt Provider-Wissen in den Coordinator.
4. **Registry-Factory** (`PROVIDERS`-Dict): neuer Provider = neue Datei + Registry-Eintrag + Strings, kein Umbau von Config-Flow/Coordinator.

---

## 3. Fehlerarchitektur

### 3.1 Taxonomie (`api/exceptions.py`)

```python
class BonnVoyageApiError(Exception): ...                 # Basis
class ApiConnectionError(BonnVoyageApiError): ...        # DNS/Timeout/5xx
class ApiInvalidDataError(BonnVoyageApiError): ...       # Schema/Parsing (XML/GeoJSON kaputt)
class ApiAuthError(BonnVoyageApiError): ...              # 401/403 (nur Routing-Provider)
class ApiRateLimitError(BonnVoyageApiError):             # 429 / Quota
    retry_after: timedelta | None
class RouteNotFoundError(BonnVoyageApiError): ...        # unroutbare Koordinaten (Config-Fehler!)
```

**Mapping auf HA-Semantik — ausschließlich im Coordinator/Setup, nie im Client:**

| API-Fehler | Setup (`async_setup_entry`) | Laufzeit (Coordinator) |
|---|---|---|
| `ApiConnectionError` | `ConfigEntryNotReady` (HA retry't mit Backoff) | `UpdateFailed` (ggf. mit Stale-Toleranz, s. u.) |
| `ApiInvalidDataError` | `ConfigEntryNotReady` | `UpdateFailed` + einmaliges WARNING (Format-Drift der Stadt-Endpunkte!) |
| `ApiAuthError` | `ConfigEntryAuthFailed` → Reauth-Flow | `ConfigEntryAuthFailed` → Reauth-Flow |
| `ApiRateLimitError` | `ConfigEntryNotReady` | „not before"-Fenster setzen, Daten stale halten |
| `RouteNotFoundError` | — (Subentry-Flow validiert vorab) | Repair-Issue am Subentry (Nutzeraktion nötig), kein Dauer-Retry |

### 3.2 Partielle Ausfälle

- **Isolation per Konstruktion:** Jede Entity hängt an **genau einem** Coordinator. BCP down → nur Parkhaus-Entities `unavailable`; Baustellen, Autobahn, Routen laufen weiter. Das leistet der Coordinator-pro-Quelle-Schnitt bereits — im Plan festschreiben als Invariante: *„Keine Entity liest Daten aus mehr als einem Coordinator für ihren State."*
- **Cross-Source-Enrichment** (Route-Attribut „betroffene Meldungen", 3.1): einzige gewollte Quellen-Kopplung. Regel: Enrichment ist **best effort und nur lesend** — Meldungs-Coordinator down ⇒ Attribut leer/entfällt, Reisezeit-State unberührt. Keine Update-Abhängigkeiten zwischen Coordinatoren (kein `await other_coordinator.refresh()`), nur Lesen des letzten Snapshots.
- **Gesamtsystem:** Setup schlägt nur fehl, wenn **alle** konfigurierten Pflicht-Quellen scheitern; einzelne Quellen dürfen mit `ConfigEntryNotReady`-Semantik pro Coordinator nachziehen (First Refresh tolerant gestalten: Quelle down beim Start ⇒ Entities `unavailable`, Setup läuft trotzdem durch — sonst reißt eine Quelle die Integration).

### 3.3 Zustandsmodell je Quelle (Availability mit Stale-Toleranz)

```
            Fehler                Fehler ≥ N              Erfolg
   OK ───────────────▶ DEGRADED ────────────▶ DOWN ────────────────▶ OK
   ▲                   (Cache dienen,         (Entities unavailable;   (Repair-Issue
   └── Erfolg ────────  Entities available)    nach ≥ T zusätzlich      auto-close)
                                               Repair-Issue)
```

- **DEGRADED:** Bis `N` aufeinanderfolgende Fehlschläge liefert `_async_update_data` die **letzten validen Daten** zurück (Entities bleiben `available`, Werte „stale") statt `UpdateFailed` zu werfen. `N` pro Quelle: Parkhäuser `N=2` (~4 min stale ok), Verkehrslage `N=1` (Reisezeit soll ehrlich sein), Baustellen `N=3` (langsam veränderlich, ~90 min stale ok).
- **DOWN:** ab `N` Fehlern `UpdateFailed` → Entities `unavailable` (HA-Standardsemantik, Plan 3.4 bleibt gültig — nur präzisiert).
- **Repair-Issue** erst bei anhaltendem Ausfall (z. B. > 6 h DOWN), mit Quelle+letztem Fehler; automatisch schließen bei Recovery. Verhindert Issue-Spam bei nächtlichen Wartungsfenstern der Stadt-Server.
- **Log-Hygiene:** erster Fehler WARNING, Folgefehler DEBUG, Recovery INFO (Coordinator-Bordmittel nutzen, nicht selbst loggen).

---

## 4. Datenmodell (mypy-strict-tauglich)

Ort: `api/models.py`. Prinzipien: `@dataclass(frozen=True, slots=True)`, `StrEnum`, **`tuple` statt `list`** (Immutabilität = Cache-/Snapshot-Sicherheit im Coordinator), **timezone-aware `datetime` (UTC)**, `timedelta` statt Minuten-Floats, kein `Any` in öffentlichen Signaturen, `py.typed`-Marker.

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

# ── Geometrie ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class GeoPoint:
    latitude: float
    longitude: float

@dataclass(frozen=True, slots=True)
class PointGeometry:
    point: GeoPoint

@dataclass(frozen=True, slots=True)
class LineGeometry:
    points: tuple[GeoPoint, ...]

@dataclass(frozen=True, slots=True)
class PolygonGeometry:
    exterior: tuple[GeoPoint, ...]
    holes: tuple[tuple[GeoPoint, ...], ...] = ()

Geometry = PointGeometry | LineGeometry | PolygonGeometry
# Helper (Modul-Funktionen): representative_point(Geometry) -> GeoPoint
# und distance_km(GeoPoint, GeoPoint) — geo_location braucht genau einen Punkt
# + Distanz für den Radius-Filter; Linien/Polygone werden auf Repräsentanzpunkt reduziert.

# ── Parken ───────────────────────────────────────────────────────────────────

class FacilityState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"

@dataclass(frozen=True, slots=True)
class ParkingFacility:          # Stammdaten (BCP-XML + optional CKAN-Anreicherung)
    facility_id: str            # stabile ID (BCP-Kennung)
    name: str
    location: GeoPoint | None
    capacity: int | None
    address: str | None = None
    operator: str | None = None

@dataclass(frozen=True, slots=True)
class ParkingStatus:            # Live-Zustand, getrennt von Stammdaten
    facility_id: str
    free_spaces: int | None
    state: FacilityState
    fetched_at: datetime        # UTC
    # Auslastung/Trend/voll: abgeleitet in der HA-Schicht, NICHT hier.

# ── Strecken ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RouteRequest:
    origin: GeoPoint
    destination: GeoPoint
    via: tuple[GeoPoint, ...] = ()

@dataclass(frozen=True, slots=True)
class RouteStatus:
    travel_time: timedelta
    free_flow_time: timedelta | None    # None ⇔ Provider ohne Live-Traffic (OSRM)
    distance_m: float
    geometry: LineGeometry | None
    fetched_at: datetime                # UTC

    @property
    def delay(self) -> timedelta | None:
        if self.free_flow_time is None:
            return None
        return self.travel_time - self.free_flow_time
    # Verkehrslage-Enum (frei/dicht/stockend/stau): Schwellwert-Ableitung in der
    # HA-Schicht (konfigurierbar), nicht im Modell.

# ── Meldungen / Baustellen ───────────────────────────────────────────────────

class MessageSource(StrEnum):
    BONN_GEOPORTAL = "bonn_geoportal"
    AUTOBAHN_API = "autobahn_api"

class MessageKind(StrEnum):
    ROADWORK = "roadwork"
    CLOSURE = "closure"
    WARNING = "warning"
    OTHER = "other"

class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"

@dataclass(frozen=True, slots=True)
class TrafficMessage:           # vereinheitlicht Bonn-GeoJSON UND Autobahn-API
    message_id: str             # STABIL über Polls (siehe ADR-007!)
    source: MessageSource
    kind: MessageKind
    title: str
    geometry: Geometry
    description: str | None = None
    roads: tuple[str, ...] = ()             # z. B. ("A565",)
    severity: Severity = Severity.UNKNOWN
    starts_at: datetime | None = None
    ends_at: datetime | None = None
```

**Begründung der Schnitte:**
- `ParkingFacility` (Stammdaten) vs. `ParkingStatus` (Live) getrennt: unterschiedliche Änderungsraten, CKAN-Anreicherung ohne Live-Pfad-Umbau, Subentry-Auswahl braucht nur Stammdaten.
- `TrafficMessage` als **eine** vereinheitlichte Struktur für beide Meldungsquellen: `geo_location.py` und der Meldungs-Sensor konsumieren genau einen Typ; Quellen-Spezifika enden im Client.
- Kein `raw`-Feld mit Provider-Rohdaten im Modell (mypy-strict-Falle `Any`, Speicher, Kopplung). Für Diagnostics kann der Client die letzte Rohantwort separat gepuffert anbieten.

---

## 5. Erweiterbarkeit (weitere Open-Data-Quellen)

**Konkretes Muster für stadtplan.bonn.de:** genau **ein** generischer Client + deklarative Layer-Beschreibung:

```python
@dataclass(frozen=True, slots=True)
class GeoLayerDescriptor:
    layer_id: str               # interner Key, translation_key
    theme_id: int               # ?Thema=14403
    default_kind: MessageKind
    update_interval: timedelta

BONN_LAYERS: tuple[GeoLayerDescriptor, ...] = (
    GeoLayerDescriptor("baustellen", 14403, MessageKind.ROADWORK, timedelta(minutes=30)),
    # neuer Layer = 1 Zeile + Strings, kein Codeumbau
)
```

`BonnGeoClient` nimmt einen Descriptor und liefert `tuple[TrafficMessage, ...]`. Der Meldungs-Coordinator aggregiert über alle aktivierten Layer. Layer-Aktivierung optional im Options-Flow (Multi-Select).

**Drei Erweiterungsachsen, drei Mechanismen — alle ohne Umbau:**
1. Neuer Geoportal-Layer → `GeoLayerDescriptor`-Eintrag.
2. Neuer Routing-Provider → Adapter-Datei + `PROVIDERS`-Registry-Eintrag.
3. Neue eigenständige Quelle (eigenes Format, z. B. CKAN) → neuer Client, der auf **bestehende Domänentypen** mappt, + eigener Coordinator. Das Domänenmodell ist die Stabilitätsgrenze: Entities kennen nur `models.py`, nie Quellenformate.

**Bewusst kein Plugin-System** (Entry-Points, dynamisches Laden): YAGNI für eine Custom Integration; modul-lokale Registries genügen und bleiben mypy-prüfbar.

---

## 6. Architektonische Risiken

| # | Risiko | Auswirkung | Mitigation |
|---|---|---|---|
| R1 | BCP-XML & Geoportal sind **inoffizielle Endpunkte ohne SLA/Schema-Garantie**; Lizenzlage offen (Plan 9) | Format-Drift bricht Parsing; HACS-Release ggf. lizenzwidrig | Toleranter Parser + `ApiInvalidDataError` + Repair-Issue; Lizenzklärung **vor** HACS-Release (M4-Gate); Attribution in Doku |
| R2 | Routing-Request-Budget (Free Tier) vs. Routen×Intervall | Quota-Sperre, tote Sensoren | Budget-Rechnung im Plan, globaler Rate-Limiter, Intervall pro Provider, 429-Handling (Kap. 2.2/3.1) |
| R3 | Instabile Meldungs-IDs → `geo_location`-Entity-Churn, Event-Duplikate | Karten-Flackern, Automations-Spam | ADR-007 Stable-ID-Strategie |
| R4 | Config-Subentries sind ein junges HA-API | Bricht auf älteren HA-Versionen | Mindest-HA-Version im `manifest.json` festnageln (ADR-010) |
| R5 | Versteckte Kopplung durch Route↔Meldungs-Enrichment | Kaskadierender Ausfall | Best-effort-Regel (Kap. 3.2), Invariante „eine Entity, ein Coordinator" |
| R6 | Trend-Berechnung verführt zu Persistenz-Overengineering | Komplexität ohne Nutzen | In-Memory-Ringpuffer; nach Neustart `unknown` bis Fenster gefüllt (Langzeit liegt ohnehin in TimescaleDB des Nutzers) |
| R7 | OSRM ohne Live-Traffic → irreführende Delay-/Stau-Entities | Vertrauensverlust | Capability-Flags; Entities konditional anlegen (Kap. 2.3) |
| R8 | „Platinum" ist für Custom Integrations formal nicht verleihbar (Scale gilt Core) | Falsche Außendarstellung | Formulierung „Platinum-Kriterien als interner Standard"; `quality_scale.yaml` trotzdem pflegen |
| R9 | Viele HA-Instanzen pollen dieselben städtischen Server | Blocking durch Betreiber | Conditional GET/TTL-Cache, eigener User-Agent, konservative Intervalle |

---

## 7. ADR-Liste (Entscheidung + Begründung, je als eigenes ADR auszuformulieren)

- **ADR-001 — Ein ConfigEntry + Config-Subentries je Strecke/Parkhaus.** Lifecycle ohne Entry-Neustart, modernes HA-Pattern; Alternative (Entry pro Objekt) dupliziert Quell-Coordinatoren.
- **ADR-002 — Coordinator je Quelle; Strecken: ein Coordinator pro Route-Subentry mit gemeinsamem Provider-Client + globalem Rate-Limiter.** Availability-Isolation pro Route, triviales Subentry-Unload; Limiter verhindert Quota-Flut.
- **ADR-003 — Domänenmodelle als frozen/slots-Dataclasses in `api/models.py`; Clients geben ausschließlich Domänentypen zurück.** mypy-strict, Immutabilität als Snapshot-Garantie, Quellenformate enden im Client.
- **ADR-004 — Routing-Port als `Protocol` mit Capability-Flags; Adapter im Paket `api/routing/`, Auswahl via Registry.** Ports & Adapters ohne kleinsten gemeinsamen Nenner; OSRM-Sonderfall sauber abbildbar.
- **ADR-005 — Fehler-Taxonomie in `api/exceptions.py`; Mapping auf HA-Semantik (`ConfigEntryNotReady`/`AuthFailed`/`UpdateFailed`) nur in Coordinator/Setup.** HA-freie Clients bleiben HA-frei; einheitliches Verhalten aller Quellen.
- **ADR-006 — Stale-Toleranz per Quelle (N aufeinanderfolgende Fehler bis `unavailable`: Parken 2, Routen 1, Meldungen 3).** Kurzstörungen städtischer Server sollen keine Entity-Flaps erzeugen; Reisezeit muss ehrlich bleiben.
- **ADR-007 — Stable-ID-Strategie für `TrafficMessage`: native Quell-ID, wenn vorhanden; sonst SHA-1 über normalisierte Felder (Quelle, Kind, gerundete Repräsentanz-Koordinate, Titel).** Verhindert `geo_location`-Churn und Event-Duplikate; muss vor M2 stehen.
- **ADR-008 — Abgeleitete Signale (Trend, Verkehrslage-Enum, Stau/Voll-Binary) ausschließlich in der HA-Schicht.** Clients liefern Fakten; Schwellwerte sind Nutzer-Konfiguration; Ableitung zentral testbar.
- **ADR-009 — Höfliches Polling: Conditional GET (falls unterstützt) bzw. TTL-Cache, eigener User-Agent, feste konservative Intervalle (keine Intervall-Option im Options-Flow).** Quality-Scale-Rule `appropriate-polling` + Schutz der Gratis-Endpunkte.
- **ADR-010 — Mindest-HA-Version wegen Subentries-API im `manifest.json` (aktuelle Stable zum M1-Zeitpunkt).** Junges API; kein Fallback-Code für Alt-Versionen (Wartungslast).

---

## 8. Plan-Korrekturen (priorisiert)

### MUSS (vor M1-Start in PLAN.md einarbeiten)

1. **M1 — Domänenmodell-Schicht ergänzen:** `api/models.py` + `api/exceptions.py` in den Modulbaum (Abschnitt 4); Regel „Clients geben nur Domänentypen zurück, werfen nur Taxonomie-Exceptions" als Architektur-Invariante. (Kap. 2.1, 4)
2. **M2 — Fehler-/Availability-Strategie spezifizieren:** Exception-Mapping-Tabelle, Zustandsmodell OK→DEGRADED→DOWN mit Stale-Toleranz N je Quelle, Repair-Issue-Kriterium (> 6 h DOWN, auto-close), Invariante „eine Entity ↔ ein Coordinator", Enrichment nur best effort. (Kap. 3)
3. **M3 — Coordinator-Topologie für Strecken entscheiden und Request-Budget dokumentieren:** ein Coordinator pro Route-Subentry + gemeinsamer Client + globaler Rate-Limiter; Tabelle Requests/Tag je Provider-Free-Tier → max. Routenzahl bzw. Intervall je Provider. (Kap. 2.2)
4. **M4 — Routing-Port ausformulieren:** `routing.py` → Paket `api/routing/` mit `Protocol`, Capability-Flags (`supports_live_traffic`), Registry; OSRM-Fall: Delay-/Stau-Entities konditional. (Kap. 2.3)
5. **M5 — Stable-ID-Strategie für Meldungen (ADR-007) vor M2 der Roadmap festlegen** — nachträgliche Änderung bricht Entity-Registry und Event-Deduplizierung.
6. **M6 — Mindest-HA-Version (Subentries) im Plan/`manifest.json` festlegen** (ADR-010).
7. **M7 — Lizenzklärung BCP/Geoportal als hartes Gate vor HACS-Release** in Roadmap M4 aufnehmen (derzeit nur „offene Frage").

### SOLL

1. **S1 — Generischer Geoportal-Client mit `GeoLayerDescriptor`-Registry** statt hartkodiertem Baustellen-Client (Kap. 5) — macht „weitere Themen-Layer" (Abschnitt 2) zur Ein-Zeilen-Erweiterung.
2. **S2 — Intervall-Option aus dem Options-Flow streichen** (Quality-Scale `appropriate-polling`); Radius/Schwellwerte bleiben.
3. **S3 — ETag-Unterstützung der Endpunkte empirisch prüfen** (BCP/Geoportal), sonst TTL-Cache; User-Agent-String definieren; Timeout-Defaults (`aiohttp.ClientTimeout(total=10)`) festlegen.
4. **S4 — Typisiertes `runtime_data` + `entity.py`-Basisklassen** in den Modulbaum aufnehmen.
5. **S5 — Trend-Berechnung als In-Memory-Ringpuffer festschreiben** (nach Neustart `unknown`); beantwortet offene Frage 3 ohne Persistenz-Komplexität.
6. **S6 — Diagnostics-Redaction-Liste definieren:** API-Keys **und** Home-/Routen-Koordinaten (Standortdaten sind sensibel).
7. **S7 — Formulierung „Platinum" präzisieren:** „Platinum-Kriterien als interner Qualitätsstandard" (Scale ist formal Core-only).

### KANN

1. **K1 — `api/` später als PyPI-Lib** (`aiobonnvoyage`) auslagern — der Schnitt erlaubt es; nicht vorziehen.
2. **K2 — Autobahn-API-Webcams** als `image`-Entities in eine spätere Roadmap-Stufe (M5) aufnehmen.
3. **K3 — CKAN-Stammdaten-Anreicherung** (Kapazität/Adresse) als optionaler zweiter Client auf `ParkingFacility` — Modell ist vorbereitet.

---

*Ende des Reviews.*
