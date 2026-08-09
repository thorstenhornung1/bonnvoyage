# Spezifikation: Parkhaus-Trend (QA-Gate vor M1-Implementierung)

Der Trend-Sensor beantwortet: „Füllt sich das Parkhaus gerade, oder leert es sich?"

## Datengrundlage

- Eigene Berechnung aus den `frei`-Werten des BCP-Feeds (Abtastung im
  2-Minuten-Intervall des Coordinators).
- Das Feed-Feld `tendenz` wird **nicht** verwendet: Semantik undokumentiert
  (beobachtet: konstant `3` über alle Parkhäuser). Es wird auch nicht als
  Attribut veröffentlicht, solange die Bedeutung unklar ist.

## Algorithmus

1. Pro Parkhaus ein Ring-Buffer der letzten 20 Samples `(zeitstempel, frei)`
   (≈ 40 min bei 2-min-Intervall). Nur bei geöffnetem Parkhaus und bekanntem
   `frei`-Wert wird gesampelt.
2. Referenzwert: das Sample, dessen Zeitstempel am nächsten an
   `jetzt − 15 min` (`TREND_WINDOW`) liegt.
3. Mindest-Spanne: Liegt das Referenz-Sample weniger als 10 min
   (`TREND_MIN_SPAN`) zurück (z. B. nach Neustart), ist der Trend **unknown**.
4. Hysterese-Schwelle: `max(5 Plätze, 3 % der Kapazität)` — verhindert
   Flattern durch einzelne Ein-/Ausfahrten.
5. Bewertung von `delta = frei(jetzt) − frei(Referenz)`:
   - `delta ≤ −Schwelle` → `filling` (füllt sich)
   - `delta ≥ +Schwelle` → `emptying` (leert sich)
   - sonst → `steady`

## Sonderfälle

| Fall | Verhalten |
|---|---|
| Neustart von Home Assistant | Buffer leer → Trend `unknown` für ~10 min (bewusst: keine Persistenz in M1) |
| Parkhaus geschlossen (`is_open != True`) | Buffer wird verworfen, Trend `unknown` — beim Öffnen beginnt die Messung neu |
| `frei` fehlt/ungültig | wie geschlossen behandelt (kein Sample, Buffer verworfen) |
| Datenlücke (Feed-Ausfall < Stale-Toleranz) | keine neuen Samples; alte bleiben im Buffer; Referenz „nächstes Sample an −15 min" bleibt korrekt |
| Feed-Zeitstempel eingefroren | greift nicht den Trend an — Availability-Regel (2 h) nimmt die Entities vorher offline |

## Bewusste Nicht-Ziele (M1)

- Keine Persistenz des Buffers über Neustarts (restore state) — Aufwand/Nutzen,
  nach 10–15 min ist der Trend wieder da. Kandidat für M4, falls Nutzerbedarf.
- Keine Glättung über mehrere Referenzpunkte (lineare Regression) — erst
  erwägen, wenn das einfache Delta in der Praxis flattert.
