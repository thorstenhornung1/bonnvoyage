# BonnVoyage — Security- & Privacy-Review (v0.1)

> Review des Plan-Entwurfs `PLAN.md` (v0.1) aus Sicht IT-Security & Datenschutz.
> Scope: defensives Review für ein Heimnetz-Produkt (HA-Custom-Integration, Domain `bonnvoyage`).
> Stand: 2026-08-09. Reviewer: Security-Rolle im Projektteam.

---

## 1. Threat Model (STRIDE-light)

### 1.1 Datenflüsse & Assets

```
[HA / BonnVoyage]
   ├── DF1: HTTPS GET → bcp-bonn.de (Parkhaus-XML, unauthentifiziert)
   ├── DF2: HTTPS GET → stadtplan.bonn.de (GeoJSON, unauthentifiziert)
   ├── DF3: HTTPS GET → verkehr.autobahn.de (JSON, unauthentifiziert)
   ├── DF4: HTTPS GET/POST → TomTom/HERE (Routing; API-Key + Start/Ziel-Koordinaten)
   └── DF5: HA-Storage (.storage/core.config_entries): ConfigEntry inkl. API-Key,
            Home-/Ziel-Koordinaten; Recorder-DB: Entity-States/-Attribute
```

**Schutzgüter (Assets):**

| Asset | Vertraulichkeit | Integrität | Verfügbarkeit |
|---|---|---|---|
| Routing-API-Key | hoch (Kostenmissbrauch, Quota-Diebstahl) | – | – |
| Home-/Routen-Koordinaten | hoch (Bewegungs-/Wohnprofil) | mittel | – |
| API-Antworten (XML/GeoJSON/JSON) | – | hoch (untrusted input!) | mittel |
| HA-Prozess / Event-Loop | – | hoch (Parser = Angriffsfläche) | hoch |
| Reputation ggü. Datenanbietern | – | – | mittel (IP-Sperre bei ToS-Verstoß) |

### 1.2 STRIDE-Matrix

| Bedrohung | Szenario | Relevanz | Gegenmaßnahme (Kurz) |
|---|---|---|---|
| **S**poofing | MITM/DNS-Spoofing liefert gefälschte API-Antworten; gefälschter Routing-Endpunkt greift Key ab | mittel | Strikte TLS-Verifikation (Standard-SSLContext von HA, niemals `ssl=False`); Hosts hart kodiert, keine Nutzer-URLs; Redirects begrenzen/prüfen (§2.5) |
| **T**ampering | Manipulierte/bösartige Response: XXE, Billion-Laughs, überlange Strings, absurde Koordinaten → Parser-Exploit oder „Entity-Poisoning" (falsche Werte, kaputte Map) | **hoch** | defusedxml, Schema-/Plausibilitätsvalidierung, Größen-Caps, Bounding-Box-Check (§2.1–§2.3) |
| **R**epudiation | Gering relevant (kein Multi-User-Schreibpfad). Restpunkt: nachvollziehbare Fehler-Logs ohne Secrets | niedrig | Strukturierte Logs, Repair-Issues statt Log-Spam; keine Secrets/Koordinaten in Logs (§3) |
| **I**nformation Disclosure | API-Key in Logs/Diagnostics/URLs/Backups; Home-Koordinaten an Dritt-Provider; Diagnostics-Export geteilt in Foren/GitHub-Issues | **hoch** | Redaction (`async_redact_data`), Key nie in Log-URLs, Privacy-Hinweis + Datenminimierung (§3) |
| **D**oS | a) Riesige/langsame Responses blockieren Coordinator bzw. füllen RAM; b) Integration selbst DoS-t kommunale Endpunkte (zu aggressives Polling) → IP-Sperre für alle Nutzer | mittel–hoch | Timeouts, Response-Size-Limits, Backoff+Jitter, konservative Intervalle, ETag/If-Modified-Since (§2.4, §3.4) |
| **E**levation of Privilege | Codeausführung via Parser-Lücke oder kompromittierte Dependency/Release (Supply Chain) → volle HA-Rechte (HA läuft oft privilegiert, Zugriff aufs ganze Smart Home) | mittel, Impact **sehr hoch** | defusedxml, minimale Dependencies, Pinning, CI-Scans, Release-Integrität (§4) |

**Kernaussage:** Die Integration ist ein reiner *Konsument* öffentlicher Daten — die dominante
Angriffsfläche ist **untrusted input** (T, E) und **Leakage eigener Daten** (I). Alles, was von
den vier Endpunkten kommt, ist als potenziell bösartig zu behandeln, auch wenn die Betreiber
vertrauenswürdig sind (kompromittierter Server, MITM in Fremdnetzen, CDN-Fehlkonfiguration).

---

## 2. Technische Risiken & Gegenmaßnahmen

### 2.1 XML-Parsing der BCP-Daten (DF1) — **kritischster Punkt**

**Risiko:** `xml.etree`/`lxml` mit Default-Konfiguration ist verwundbar für:
- **XXE (XML External Entity)**: `<!ENTITY x SYSTEM "file:///config/.storage/core.config_entries">`
  → lokale Datei-Exfiltration; `SYSTEM "http://attacker/..."` → Blind-SSRF aus dem Heimnetz
  (interne IPs, HA-eigene API, Router-Admin-Interfaces erreichbar!).
- **Billion Laughs / Entity-Expansion**: wenige KB XML → GB RAM → OOM-Kill des HA-Prozesses.
  (Neuere CPython-/expat-Versionen mildern das teilweise; darauf darf man sich nicht verlassen.)

**Maßnahmen (MUSS):**
- `defusedxml` als gepinnte Requirement im `manifest.json`; ausschließlich
  `defusedxml.ElementTree.fromstring()` verwenden. DTDs verbieten (`forbid_dtd=True`),
  Entities und externe Referenzen sind bei defusedxml per Default blockiert.
- Parsing **nur nach** bestandenem Größen-Check (§2.4).
- Parser-Fehler = erwarteter Betriebszustand: `UpdateFailed` werfen, Entities `unavailable`,
  niemals Roh-Response in die Exception/den Log (Log-Injection, Log-Größe).
- Unit-Test mit bösartigen Fixtures: XXE-Payload, Billion-Laughs, 10-MB-Blob, ungültiges UTF-8.

### 2.2 Schema- & Plausibilitätsvalidierung aller Antworten (DF1–DF4)

**Risiko:** Auch strukturell „gültige" Antworten können semantisch bösartig/kaputt sein:
100 000 GeoJSON-Features, 1-MB-Parkhausname, Koordinaten in Sibirien, `frei_plaetze = -3`
oder `= 10^9`, Steuerzeichen/Bidi-Zeichen in Namen (Log-/UI-Spoofing).

**Maßnahmen (MUSS):**
- **Zentrale Parse-Schicht** in `api/`: rohe Response → validiertes, typisiertes Dataclass-Modell.
  Kein Durchreichen von Roh-Dicts an Entities.
- Feld-Validierung: Typen erzwingen, Wertebereiche (`0 ≤ frei ≤ kapazitaet ≤ 50 000`),
  Strings auf sinnvolle Länge kappen (z. B. 256 Zeichen), Steuerzeichen strippen,
  Entity-IDs ausschließlich via `slugify()` aus API-Namen ableiten.
- **Geo-Plausibilität:** Bounding-Box-Check (Raum Bonn/NRW, z. B. lat 50.4–50.9, lon 6.9–7.4;
  Autobahn-API entsprechend weiter) bevor Koordinaten in `geo_location`-Entities oder
  Map-Attribute fließen — verhindert Karten-Spoofing und kaputte proximity-Automationen.
- **Mengen-Caps:** max. Feature-Anzahl pro GeoJSON-Abruf (z. B. 500), max. Meldungen pro
  Autobahn-Abruf; Überschuss verwerfen + einmaliges Warn-Log.
- Validierungsfehler einzelner Datensätze: Datensatz skippen, nicht den ganzen Update-Zyklus
  abbrechen (Resilienz); erst bei komplett unbrauchbarer Antwort `UpdateFailed`.

### 2.3 GeoJSON/JSON-spezifisch (DF2, DF3)

- `json.loads` ist robust gegen klassische Parser-Exploits, aber nicht gegen Größe/Tiefe:
  Size-Cap vor dem Parsen (§2.4), keine rekursiven Eigenkonstruktionen.
- GeoJSON: nur benötigte Geometrie-Typen akzeptieren (Point/LineString/Polygon), Koordinaten-
  Arrays in der Länge begrenzen (Polyline-Vereinfachung bereits im API-Client, nicht erst im UI).
- `Content-Type` prüfen (Soll-Check, kein Hard-Fail — kommunale Server setzen Header oft falsch).

### 2.4 Timeouts & Response-Size-Limits (alle DF)

**Risiko:** aiohttp hat **kein Default-Limit** für Response-Bodies bei `.read()`/`.text()`;
ein (kompromittierter) Endpunkt kann den HA-Host per RAM-Füllung oder Slow-Loris-artigem
Tröpfeln lahmlegen.

**Maßnahmen (MUSS):**
- `aiohttp.ClientTimeout(total=30, connect=10, sock_read=10)` pro Request (Werte je Quelle
  justieren, aber nie unbegrenzt); Coordinator-`update_timeout` zusätzlich als Fangnetz.
- Body-Limits durchsetzen: `Content-Length` prüfen **und** gestreamt lesen mit hartem Cap
  (Content-Length ist Client-seitig nicht garantiert):
  - BCP-XML: 512 KB · Bonn-GeoJSON: 5 MB · Autobahn-JSON: 2 MB · Routing: 2 MB.
  - Bei Überschreitung: Abbruch, `UpdateFailed`, kein Retry-Sturm (Backoff greift).
- Kein `resp.text()` auf ungeprüfte Responses; erst Bytes mit Cap, dann dekodieren
  (`errors="replace"` oder Hard-Fail bei ungültigem Encoding).

### 2.5 TLS-/Zertifikatsverhalten (aiohttp)

**Anforderungen:**
- **MUSS:** ausschließlich `async_get_clientsession(hass)` (injizierte Session, Platinum-
  Anforderung deckt sich hier mit Security): nutzt HAs SSLContext mit certifi-Store,
  Zertifikatsprüfung an. **Niemals** `ssl=False`, `verify_ssl=False`, eigener permissiver
  SSLContext oder Hostname-Check-Deaktivierung — auch nicht „nur für bcp-bonn.de".
- **Umgang mit kaputten kommunalen Zertifikaten** (typisch: fehlendes Intermediate,
  abgelaufenes Zertifikat):
  - Kein globaler und kein per-Host-Verify-Off-Schalter im Config-/Options-Flow anbieten.
    Ein solcher Schalter wird von Nutzern dauerhaft aktiviert und öffnet MITM (und damit
    bei DF4 Key-Diebstahl).
  - Stattdessen: `ClientConnectorCertificateError` gezielt fangen → **Repair-Issue** mit
    verständlichem Text („Zertifikat von … ungültig; Integration pausiert diese Quelle,
    bitte Betreiber informieren") + Entities `unavailable`. Fail closed, nicht fail open.
  - Falls es je einen dokumentierten Dauerfall gibt: einzige akzeptable Lösung wäre
    **Certificate-Pinning auf das bekannte Leaf/CA** dieser einen Domain (eigener SSLContext
    mit zusätzlichem Anker) — nie generisches Abschalten. (Erwartung: nicht nötig; alle vier
    Endpunkte liefern derzeit valide Ketten.)
- **SOLL:** Redirects begrenzen (`max_redirects=3`) und bei DF4 Redirects auf fremde Hosts
  abbrechen — TomTom/HERE tragen den API-Key als Query-Parameter; ein Redirect zu einem
  Fremdhost würde den Key im `Location`-Follow leaken.

### 2.6 SSRF-Vermeidung: keine benutzerdefinierten URLs

- **MUSS:** Alle Basis-URLs (DF1–DF3) sind **hart kodierte Konstanten** im Code. Der
  Routing-Provider (DF4) ist eine **Enum-Auswahl** (`tomtom` / `here` / …) mit fest
  hinterlegter Basis-URL je Provider. Kein freies URL-Feld im Config-Flow, keine URL aus
  Options, keine URL aus API-Antworten übernehmen (auch nicht „next page"-Links blind folgen).
- Damit entfällt die SSRF-Klasse fast vollständig; Rest-SSRF-Vektor XML-External-Entities
  ist durch defusedxml geschlossen (§2.1).
- **Falls später Self-Hosted-OSRM gewünscht** (Plan §2 „optional"): dann als bewusste
  Ausnahme mit Allowlist-Design: nur `http(s)://`-Schema, Host-Eingabe validieren,
  explizite Options-Flow-Warnung, dass HA diesen Host kontaktiert; keine Weiterleitung von
  Redirects; Dokumentation, dass der Nutzer damit selbst für den Endpunkt verantwortlich ist.
  Bewertung: akzeptabel, weil kein Cloud-Key involviert ist und der Nutzer den Host selbst
  betreibt — aber erst ab M3+, nicht im Grundumfang.

### 2.7 Sonstiges

- **Kein Sync-I/O, keine Blocking-Parser** im Event-Loop (deckt sich mit Bronze-Kriterium);
  großes GeoJSON-Parsing ggf. via `hass.async_add_executor_job`.
- **Parallelität begrenzen** (`parallel_updates`), damit vier Coordinators nicht gleichzeitig
  bei Fehlern in Retry-Stürme laufen; Backoff mit Jitter ist im Plan bereits vorgesehen — gut.
- Webcam-URLs aus der Autobahn-API (falls M2+ genutzt): als Attribut nur durchreichen,
  **nie** serverseitig abrufen (wäre sonst SSRF via API-Antwort).

---

## 3. Secrets & Privacy

### 3.1 API-Key-Speicherung (DF5)

- **MUSS:** Key ausschließlich in `ConfigEntry.data[CONF_API_KEY]` — das ist der
  HA-Standardort. Bewusst akzeptiertes Rest-Risiko: `.storage/core.config_entries` ist
  Klartext-JSON und landet in **HA-Backups**; das ist Plattform-Design, nicht durch die
  Integration lösbar. Konsequenz für uns: Key **nirgendwo duplizieren** — nicht in
  Entity-Attributen, nicht in `hass.data`-Strukturen, die in Diagnostics laufen, nicht in
  Options, nicht in Log-Meldungen.
- **MUSS:** Key-Validierung im Config-Flow per Test-Request (im Plan vorhanden) — dabei
  Fehlerantworten des Providers nicht ungefiltert in die Flow-UI/Logs spiegeln.
- **MUSS (M3):** **Reauth-Flow** bei 401/403 vom Provider (Silver-Kriterium, deckt sich mit
  Security: abgelaufene/rotierte Keys sauber behandeln statt Retry-Hammering, das Keys
  sperren lassen kann).
- **SOLL:** Doku-Abschnitt „Key-Rotation": neuen Key beim Provider erzeugen → Reconfigure-Flow
  → alten Key beim Provider löschen. Empfehlung an Nutzer: Key mit minimalem Scope anlegen
  (nur Routing-Produkt, Quota-Limit im Provider-Portal setzen — begrenzt Schaden bei Leak).

### 3.2 Redaction in Diagnostics & Logs

- **MUSS:** `diagnostics.py` mit `async_redact_data()` und vollständiger `TO_REDACT`-Liste:
  `api_key`, **Start-/Ziel-/Home-Koordinaten**, Routen-Namen (können „Zuhause"/Arbeitgeber
  verraten), Mittelpunkt der Meldungs-Geofence. Diagnostics werden erfahrungsgemäß ungelesen
  in GitHub-Issues gepostet — Koordinaten sind hier genauso sensibel wie der Key.
- **MUSS:** Logging-Hygiene: niemals volle Request-URLs loggen (TomTom/HERE-Key steht im
  Query-String!). Debug-Logs loggen Host + Pfad ohne Query, oder mit `key=***`.
  `aiohttp`-eigene Logger nicht auf DEBUG zwingen.
- **SOLL:** Exceptions aus `api/` so gestalten, dass `str(exc)` nie URL/Key/Response-Body
  enthält (eigene Exception-Typen mit kuratierten Meldungen).

### 3.3 Privacy: „Home-Koordinaten gehen an Dritt-Provider"

Das ist die zentrale Datenschutz-Implikation des Projekts (DF4). DF1–DF3 sind unkritisch
(reine GET-Abrufe öffentlicher Daten; einzige Preisgabe: eigene IP + Abrufmuster).

- **MUSS (M3): Transparenz im Config-Flow.** Im Schritt „Reisezeit-Provider" ein klarer
  Hinweistext (de/en, via `data_description`/Flow-Beschreibung): *„Start-, Via- und
  Zielkoordinaten deiner Strecken werden zur Routenberechnung an <Provider> übertragen.
  Es gelten dessen Datenschutzbestimmungen (Link)."* Kein Opt-out-Theater — wer Strecken
  will, muss das akzeptieren; aber es muss **vor** Key-Eingabe sichtbar sein. Zusätzlich
  identischer Abschnitt „Datenschutz" im README.
- **MUSS (M3): Datenminimierung.** An den Provider gehen ausschließlich Koordinatenpaare —
  keine Routen-Namen („Zuhause→Büro"), keine HA-Identifier, keine Freitexte. User-Agent
  gegenüber dem Routing-Provider generisch halten (Integrationsname + Version reicht).
- **SOLL: Koordinaten-Rundung als Option.** Opt-in „Koordinaten auf 3 Nachkommastellen
  runden (~70–110 m)" für die Übertragung an den Provider. Bewertung: 4 Nachkommastellen
  (~8–11 m) sind für Routing praktisch verlustfrei und sollten der **Default** für die
  Übertragung sein (mehr Präzision hat kein Routing-Nutzen); 3 Nachkommastellen als
  Privacy-Option mit Hinweis, dass Start/Ziel dann auf der Nachbarstraße liegen können.
  Wichtig: Rundung nur für den *externen* Request — intern/Map-Darstellung volle Präzision.
- **SOLL: Abruf-Minimierung.** Reisezeit nur pollen, wenn jemand sie nutzen kann —
  Optionen wie Zeitfenster (z. B. nur 6–10 / 15–20 Uhr) oder „nur wenn HA-Zone belegt"
  reduzieren sowohl Provider-Kosten als auch das an Dritte gehende Bewegungsmuster
  (Polling-Frequenz verrät Nutzungsverhalten).
- **KANN:** OSRM-Unterstützung (self-hosted, §2.6) als datenschutzfreundliche Alternative
  prominent dokumentieren — dann verlassen Koordinaten das Heimnetz gar nicht.
- **Recorder-Hinweis (Doku, KANN):** Routen-/Home-Koordinaten landen als Entity-Attribute
  in der Recorder-DB und ggf. in der TimescaleDB des Nutzers — bei Weitergabe von
  DB-Exporten beachten.

### 3.4 Lizenz-/ToS-Konformität beim Polling

- **MUSS:** Eigener **User-Agent** bei allen Requests an DF1–DF3:
  `BonnVoyage/<version> (+https://github.com/<owner>/bonnvoyage)` — Betreiber können uns
  identifizieren und kontaktieren statt pauschal zu sperren. (Sich als Browser auszugeben
  wäre ein ToS-Red-Flag.)
- **MUSS:** Konservative, quellenspezifische Intervalle (Plan: 2/5/30 min — ok) **mit
  Jitter beim Start**, damit nicht tausende Installationen synchron zur vollen Minute
  abrufen (Thundering Herd gegen kommunale Server). Backoff bei 429/5xx respektieren,
  `Retry-After` honorieren.
- **MUSS (vor HACS-Release, M4):** Offene Frage aus Plan §9 klären: Lizenz/ToS von
  BCP-XML und stadtplan.bonn.de (DL-DE-BY erfordert Quellenvermerk → Attribution in
  README + ggf. Entity-Attribut `attribution` setzen; HA hat dafür
  `ATTR_ATTRIBUTION`/`_attr_attribution`). Autobahn-API (bund.dev) ist offen, Attribution
  trotzdem setzen. Ohne geklärte Lizenz kein öffentliches Release.
- **SOLL:** ETag/If-Modified-Since (im Plan vorgesehen) — reduziert Last und ToS-Risiko.
- **SOLL:** Routing-Provider-ToS für den Free Tier prüfen (Caching-Verbote, Attribution
  „Powered by TomTom" u. ä. sind dort üblich und teils verpflichtend).

---

## 4. Supply Chain

### 4.1 Abhängigkeits-Hygiene

- **MUSS:** `manifest.json`-`requirements` **exakt gepinnt** (`defusedxml==0.7.x`, …).
  So wenige Dependencies wie möglich: aiohttp kommt von HA, kein `requests`, kein
  `lxml` (C-Extension, größere Angriffsfläche), kein pydantic — Standard-`json` +
  Dataclasses + eigene Validierung genügen.
- **MUSS (ab Repo-Anlage):** **Dependabot** (oder Renovate) für `pip` **und**
  `github-actions`-Ecosystem aktivieren; Security-Updates mit Priorität mergen.
- **SOLL:** `pip-audit` (oder `osv-scanner`) als CI-Job über die gepinnten Requirements;
  zusätzlich CodeQL (Python) und `bandit` — kostenlos für öffentliche Repos.
- **SOLL:** Falls die API-Clients wie geplant als PyPI-Lib ausgelagert werden: PyPI-Projekt
  mit **Trusted Publishing** (OIDC statt API-Token) und 2FA-Pflicht.

### 4.2 CI/CD- & Repository-Härtung

- **MUSS:** GitHub Actions: `permissions:` minimal (default `contents: read`), Actions
  **per Commit-SHA gepinnt** (nicht `@v4`-Tags — Tag-Hijacking, vgl. tj-actions-Vorfall),
  kein `pull_request_target` mit Checkout von Fremd-Code.
- **SOLL:** Branch-Protection auf `main` (PR-Pflicht, Status-Checks: hassfest,
  HACS-Validation, Tests), 2FA für alle Maintainer (bei GitHub inzwischen weitgehend Pflicht).

### 4.3 HACS-Release-Integrität

Realistische Einordnung: **HACS verifiziert keine Signaturen** — es lädt Tags/Release-Assets
von GitHub. Die Integrität hängt also an der Kompromittierungs-Resistenz des GitHub-Repos
und der Nachvollziehbarkeit der Artefakte:

- **MUSS (M4):** Releases ausschließlich als **immutable GitHub-Releases mit Tag** aus CI
  bauen (kein manuell hochgeladenes Zip vom Entwickler-Laptop); `zip_release` in `hacs.json`
  nur, wenn das Zip reproduzierbar aus dem Tag gebaut wird.
- **SOLL:** Signierte Tags/Commits (GPG oder Sigstore `gitsign`); Release-Workflow erzeugt
  **SHA256-Checksummen** der Assets in den Release-Notes; optional SLSA-Provenance via
  `actions/attest-build-provenance`. Nutzer können so verifizieren, HACS zwar (noch) nicht —
  aber im Kompromittierungsfall ist Forensik/Beweisbarkeit da.
- **KANN:** Aufnahme in den HACS-Default-Store (Review-Prozess durch HACS-Team = zusätzliches
  Augenpaar) statt dauerhaft Custom-Repository.

---

## 5. Priorisierte Maßnahmenliste (mit Roadmap-Bezug)

Legende: **MUSS** = Release-Blocker für den jeweiligen Milestone · **SOLL** = dringend
empfohlen, zeitnah nachziehen · **KANN** = wünschenswert.

### MUSS

| # | Maßnahme | Milestone | Ref |
|---|---|---|---|
| M-1 | `defusedxml` (gepinnt) für BCP-XML; Malicious-XML-Testfixtures (XXE, Billion-Laughs) | **M1** | §2.1 |
| M-2 | Timeouts (`ClientTimeout`) + harte Response-Size-Caps (gestreamt lesen) für alle Quellen | **M1** | §2.4 |
| M-3 | Strikte TLS-Verifikation via injizierte HA-Session; kein Verify-Off-Pfad im Code oder UI; Cert-Fehler → Repair-Issue, fail closed | **M1** | §2.5 |
| M-4 | Zentrale Validierungsschicht: Typen, Wertebereiche, String-Caps, `slugify`, Bounding-Box für alle Koordinaten, Mengen-Caps | **M1** (BCP) / **M2** (GeoJSON, Autobahn) | §2.2–2.3 |
| M-5 | Keine benutzerdefinierten URLs: Endpunkte hart kodiert, Routing-Provider als Enum mit fixer Basis-URL | **M1**-Design, bindend ab **M3** | §2.6 |
| M-6 | Eigener User-Agent + Jitter/Backoff/`Retry-After` bei allen öffentlichen Quellen | **M1** | §3.4 |
| M-7 | API-Key nur in `ConfigEntry.data`; nie in Logs/URL-Logs/Attributen; Reauth-Flow bei 401/403 | **M3** | §3.1–3.2 |
| M-8 | Privacy-Hinweis im Config-Flow (Koordinaten → Provider) vor Key-Eingabe + README-Abschnitt; Datenminimierung (nur Koordinaten, keine Namen/IDs) | **M3** | §3.3 |
| M-9 | Diagnostics mit `async_redact_data`: Key **und** alle Koordinaten/Routen-Namen redacted | **M4** (Diagnostics-Feature), Redaction ab erster Diagnostics-Zeile | §3.2 |
| M-10 | Lizenz-/ToS-Klärung BCP + stadtplan.bonn.de inkl. Attribution — vor HACS-Release | **M4** | §3.4 |
| M-11 | Requirements exakt gepinnt; Dependabot (pip + actions) ab Repo-Anlage; Actions SHA-gepinnt, minimale Workflow-Permissions | **M1**–laufend | §4.1–4.2 |
| M-12 | HACS-Release nur als CI-gebautes, getaggtes GitHub-Release | **M4** | §4.3 |

### SOLL

| # | Maßnahme | Milestone | Ref |
|---|---|---|---|
| S-1 | Koordinaten-Rundung Richtung Provider (Default 4 Nachkommastellen, Option 3) | M3 | §3.3 |
| S-2 | Redirect-Limit + Fremdhost-Redirect-Abbruch bei Routing-Requests (Key im Query!) | M3 | §2.5 |
| S-3 | `pip-audit`/CodeQL/`bandit` in CI | M1–M4 | §4.1 |
| S-4 | ETag/If-Modified-Since; Content-Type-Soll-Check | M1/M2 | §2.3, §3.4 |
| S-5 | Signierte Tags + SHA256-Checksummen/Provenance im Release | M4 | §4.3 |
| S-6 | Key-Rotation-Doku + Quota-Limit-Empfehlung im Provider-Portal | M3 | §3.1 |
| S-7 | Eigene Exception-Typen ohne URL/Body in `str(exc)`; Log-Hygiene-Review | M1–M4 | §3.2 |
| S-8 | Abruf-Minimierung Reisezeit (Zeitfenster/Zonen-Bedingung) | M3 | §3.3 |
| S-9 | Branch-Protection + Pflicht-Checks (hassfest, HACS-Validation, Tests) | M1 | §4.2 |

### KANN

| # | Maßnahme | Milestone | Ref |
|---|---|---|---|
| K-1 | OSRM-Self-Hosted-Support als Privacy-Alternative (mit Allowlist-Design nach §2.6) | M3+ | §3.3 |
| K-2 | Aufnahme in HACS-Default-Store | nach M4 | §4.3 |
| K-3 | Recorder-/TimescaleDB-Privacy-Hinweis in Doku | M4 | §3.3 |
| K-4 | Certificate-Pinning-Fallback-Konzept für dauerhaft kaputte kommunale Zertifikate | bei Bedarf | §2.5 |

---

## 6. Bewertung des Plan-Entwurfs (Zusammenfassung)

Der Plan ist sicherheitlich gut aufgestellt: getrennte API-Clients, injizierte Session,
Coordinator-Pattern, Diagnostics „redacted" und Repair-Issues sind bereits vorgesehen und
decken sich mit den Platinum-Zielen. **Lücken, die der Plan noch nicht benennt:**
defusedxml/XML-Härtung, Response-Size-Limits, das Verbot eines Verify-Off-Schalters,
Redaction auch von *Koordinaten* (nicht nur des Keys), der Privacy-Hinweis im Config-Flow,
User-Agent-Kennung sowie die komplette Supply-Chain-Schiene (Pinning, Dependabot,
Release-Integrität). Diese Punkte sollten als Security-Abschnitt in `PLAN.md` §6
(Qualität) und als Akzeptanzkriterien der Milestones übernommen werden.
