# BonnVoyage 🚗🅿️

**Home-Assistant-Custom-Integration für die Bonner Verkehrslage** — bringt Open Data Bonn
und Echtzeitverkehrsdaten zusammen: konfigurierbare Strecken mit Live-Reisezeit,
Parkhäuser mit Live-Belegung und Geodaten, Baustellen und Verkehrsmeldungen auf der Karte.

> *Bonn + „Bon voyage" — gute Fahrt durch Bonn.*

## Status

🚀 **M1–M3 implementiert** (Parkhäuser, Meldungen, Verkehrslage & Strecken) — siehe [PLAN.md](PLAN.md).
Tests: 98 grün, Coverage 97 % (Config-Flow 100 %), `mypy --strict` und `ruff` sauber.

**Verkehrslage kommt aus zwei Quellen:**
- **Stadt Bonn Live-Verkehrslage** (111 Straßensegmente, auth-frei): gestörte
  Abschnitte automatisch auf der Karte + „Stadtstrecke beobachten" per Karten-Punkt
- **TomTom Routing** (optional, API-Key): Tür-zu-Tür-Reisezeit, Verzögerung,
  Verkehrslage-Ampel und Stau-Alarm für frei definierte Strecken.
  Free Tier: 20 000 Requests/Monat ≈ 2 Strecken bei 5-min-Intervall rund um
  die Uhr — Pendelzeitfenster-Polling ist als Erweiterung vorgemerkt.

## Installation (HACS)

1. HACS → Integrationen → ⋮ → „Benutzerdefinierte Repositories"
2. `https://github.com/thorstenhornung1/bonnvoyage` als Integration hinzufügen
3. „BonnVoyage" installieren, Home Assistant neu starten
4. Einstellungen → Geräte & Dienste → Integration hinzufügen → BonnVoyage

```bash
# Entwicklung: Tests lokal ausführen
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest tests/ --cov-report=json && .venv/bin/python scripts/check_coverage.py
```

Qualitätsziel: [Home Assistant Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/) **Platinum**.

## Features (geplant)

- 🛣️ **Strecken**: Reisezeit, Verzögerung und Verkehrslage-Ampel für frei definierbare Routen
  (z. B. Pendelstrecke über A565/A59/A555)
- 🅿️ **Parkhäuser**: Live-Belegung der Bonner Parkhäuser (BCP) mit freien Plätzen,
  Auslastung, Trend — inkl. Koordinaten für die Map-Card
- 🚧 **Baustellen & Meldungen**: GeoJSON der Stadt Bonn und Autobahn-API-Warnungen
  als `geo_location`-Entities im konfigurierbaren Radius
- 🗺️ **Visualisierung**: Alle Entities mit Geodaten → direkt auf der HA-Karte nutzbar

## Datenquellen

| Quelle | Daten | Auth |
|---|---|---|
| BCP Bonn | Parkhaus-Live-Belegung | keine |
| Stadt Bonn Geoportal | Baustellen (GeoJSON) | keine |
| Autobahn-API (bund.dev) | Warnungen/Sperrungen A565, A59, A555 | keine |
| Routing-Provider (optional) | Reisezeiten für Strecken | API-Key |

## Projektstruktur

```
bonnvoyage/
├── PLAN.md                  # Projektplan
├── docs/                    # Spezialisten-Reviews (Architektur, Security, HA, QA, UX)
└── custom_components/
    └── bonnvoyage/          # Integration (folgt)
```

## Lizenz

MIT — siehe [LICENSE](LICENSE)
