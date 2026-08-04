# Bewertung: `juherr/awesome-ev-charging` für den EV Charge Tracker

**Repo:** https://github.com/juherr/awesome-ev-charging (curated „Awesome"-Liste, ~155★)
**Frage:** Ist die Liste für unseren Charge Tracker nützlich? Können wir etwas davon übernehmen?
**Stand:** 2026-08-04 · geprüft gegen App-Version 3.0.70 (Fortschreibung: zweite Übernahme ergänzt)

---

## Kurzfassung

Die Liste ist gut gepflegt, aber sie ist eine **Infrastruktur-/Protokoll-Liste** — sie richtet
sich an Leute, die *Ladesäulen, Backends und Roaming-Netze* bauen (CPO/EMSP-Seite). Unser Tracker
ist ein **Verbrauchs-Logbuch auf Fahrzeugseite**: er zieht Telemetrie aus der Hersteller-App des
Autos und protokolliert Ladungen, Kosten, CO₂, PV, Fahrten, Wartung. Die Domänen überlappen kaum.

**≈ 90 % der Liste ist für uns irrelevant** (OCPP, OCPI, ISO 15118, OICP/eMIP, Eichrecht, Wallbox-
Firmware, CPO/EMSP-Backends, Batterie-Emulatoren, Zephyr/RTOS, DATEX II …). Diese Dinge setzen
voraus, dass man selbst eine Ladesäule oder ein Ladenetz betreibt — das tun wir nicht.

**Zwei Einträge sind echte, sofort umsetzbare Gewinne — beide inzwischen übernommen:**
1. **Open Charge Map** (v3.0.70) — Betreiber-Autoerkennung am Lade-Formular über GPS.
2. **open-ev-data** (dieser Commit) — Akku-kWh + AC/DC-Leistung beim Fahrzeug-Anlegen vorbefüllen.

Beide docken 1:1 an Funktionen an, die wir schon haben (GPS-Erfassung des Ladeorts, Anbieter-
Preis-Autofill; bzw. das manuelle `battery_kwh`/`max_ac_kw`-Feld, aus dem jede Lade-kWh berechnet
wird). Beide sind **offline/no-cloud** umgesetzt — passend zur Grundhaltung der App.

Daneben gibt es 2–3 „interessant, aber später/größer"-Kandidaten (v. a. **evcc** als Datenquelle
für Heim-Ladungen) und ein paar reine Referenzen.

---

## Systematische Einordnung der Liste

| Bereich der Liste | Beispiele | Für uns relevant? |
|---|---|---|
| OCPP (Säule ↔ Backend) | 21 Server, 14 Simulatoren, Libs in 10 Sprachen | **Nein** — wir betreiben keine Säule/kein Backend |
| OCPI / OICP / eMIP (Roaming) | CPO/EMSP-Server, Hubs | **Nein** — Roaming zwischen Netzen, nicht unser Thema |
| ISO 15118 / Plug&Charge | RISE V2G, PLC-Sniffer | **Nein** — Fahrzeug↔Säule-Handshake, HW-nah |
| Eichrecht / OCMF | transparenzsoftware, OCMF-Spec | **Randfall** — s. u. (signierte Zählerwerte aus Ladequittungen) |
| Wallbox-/EVSE-Firmware | OpenEVSE, esp32-evse, SmartEVSE | **Nein** — HW-Firmware |
| Energie-Management | evcc, OpenEMS, SolarNetwork | **Teilweise** — evcc als Datenquelle, s. u. |
| Home Automation / EEBUS | Homey-SmartEVSE, eebus-go | **Nein** — Steuerung, nicht Tracking |
| Batterie | Battery-Emulator, open-battery-information | **Nein** — Second-Life-Speicher |
| **Ladeort-Register / Datensätze** | **Open Charge Map**, **open-ev-data**, chargeprice, EVMap, cars-dataset | **JA** — hier liegt der Nutzen |
| Specs / Fehlercodes / DATEX II | schema-irve, unified-error-codes | **Nein** — für CPO-Datenaustausch |

---

## Was wir übernehmen — nach Nutzen sortiert

### 1. Open Charge Map — Betreiber-Autoerkennung  ✅ umgesetzt
- **Repo:** `openchargemap/ocm-system`, Export `openchargemap/ocm-export`, API `api.openchargemap.io/v3`
- **Was es ist:** freies, community-gepflegtes offenes Register von ~600 k Ladepunkten weltweit —
  je Station Betreibername, Koordinaten, Steckertyp/Leistung, teils Preis.
- **Warum es passt:** Wir erfassen am Lade-Formular ohnehin schon die GPS-Position (aus der letzten
  Fahrzeug-Position bzw. Handy-GPS) und machen daraus per Nominatim eine Adresse. OCM liefert zur
  *gleichen Koordinate* zusätzlich den **CPO** — genau das Feld, das den Preis-Autofill auslöst.
- **Umsetzung (dieser Commit):**
  - `services/openchargemap_service.py` — spiegelt `geocode_service.py`: Rate-Limiter, permanenter
    DB-Cache (`OcmCache`), jeder Fehlerpfad → `None` (das Formular darf nie an einer Fremd-API hängen).
    Reine Kernlogik (Haversine, nächste Station, Betreiber-Extraktion, Distanz-Gate) ist offline
    unit-getestet: `tests/test_openchargemap.py`.
  - Read-only-Endpoint `GET /api/locations/operator?lat=&lon=` (analog `/api/locations/reverse`).
  - Charge-Formular (`input.html`): nach GPS-Abruf wird der Betreiber vorgeschlagen — **nur** wenn
    das Feld leer ist und die Station < 150 m entfernt liegt; ein manuell gewählter Anbieter gewinnt
    immer. Rein additiv, `try/catch`-gekapselt.
  - Einstellungen: optionaler **OCM-API-Key** (`AppConfig['ocm_api_key']`). OCM verlangt inzwischen
    einen (kostenlosen) Key; ohne Key macht das Feature schlicht nichts → keine Regression.
  - i18n in allen 6 Sprachen.
- **Aufwand/Risiko:** klein, auf bestehendem Muster; kein DB-Migrationsschritt nötig (`create_all()`
  legt `ocm_cache` automatisch an).

### 2. open-ev-data — Fahrzeug-Stammdaten vorbefüllen  ✅ umgesetzt
- **Repo:** `open-ev-data/open-ev-data-dataset` (Lizenz **CDLA-Permissive-2.0**), steht in der Liste
  unter „Data & Analytics".
- **Was es ist:** versionierter offener Datensatz mit ~1 200 EV-Varianten — je Auto u. a. **nutzbare
  Akku-Kapazität (net kWh)** und **max. AC/DC-Ladeleistung**.
- **Warum es passt:** Beim Fahrzeug-Anlegen tippt der User `battery_kwh` und `max_ac_kw` bisher von
  Hand ab (Default 64). Aus `battery_kwh` × SoC-Delta wird aber **jede** Lade-kWh — und damit jede
  Kosten-, CO₂- und Verlust-Zahl — berechnet; eine falsche Kapazität verzerrt alles. Der Datensatz
  liefert genau diese zwei Zahlen zum Nachschlagen. (Sanity-Check: Ioniq 5 → 72,6 kWh, Enyaq → 77 kWh
  stimmen mit der realen Flotte überein.)
- **Warum open-ev-data statt cars-dataset (Kandidat aus der letzten Runde):** open-ev-data ist
  **EV-spezifisch** (net/gross kWh, AC/DC statt allgemeiner KFZ-Specs), **permissiv lizenziert** und
  vor allem **komplett offline bündelbar** — kein API-Key, keine Cloud, im Einklang mit der no-cloud-
  Haltung der App. cars-dataset hängt an einer REST-API. Deshalb dieser Datensatz.
- **Umsetzung (dieser Commit):**
  - Slim-Extrakt `services/vehicle/ev_specs.json` (~120 kB, nur make/model/variant + net_kwh/ac_kw/
    dc_kw) — der Upstream-Release ist ~3 MB mit Dutzenden ungenutzten Feldern, die nicht jede VM
    braucht. Reproduzierbar via `services/vehicle/build_ev_specs.py` (Dev-Tool, braucht Netz).
  - `services/vehicle/ev_specs_service.py` — lädt das Bundle einmal (`lru_cache`), akzent-tolerante
    Substring-Suche (`skoda` findet `Škoda`); fehlt/kaputt das Bundle → `[]`, Formular läuft weiter.
    Offline unit-getestet: `tests/test_ev_specs.py`.
  - Read-only-Endpoint `GET /api/vehicles/spec_search?q=` (analog zum OCM-Muster).
  - Fahrzeug-Formular (`settings.html`): Nachschlage-Feld mit Datalist; Auswahl füllt `battery_kwh`
    + `max_ac_kw`, Marke/Modell nur wenn leer. Rein additiv, der User editiert/speichert wie gehabt.
  - i18n in allen 6 Sprachen.
- **Aufwand/Risiko:** klein, additiv; kein DB-Schema, keine Migration, kein Netz zur Laufzeit.

### 3. evcc — Datenquelle für Heim-Ladungen  🔶 Kandidat, größer
- **Repo:** `evcc-io/evcc` (Go, sehr populär, PV-Überschussladen)
- **Idee:** Wenn Robert evcc an der Wallbox betreibt (vgl. Vault-Projekt *„Wallbox + Boiler auf
  Riemann-Power umstellen"*), protokolliert evcc **jede Heim-Ladesession** inkl. kWh, Kosten und
  **Solaranteil**. Das ist genau, was wir heim aktuell **manuell** eintippen. evcc hat eine REST-/
  MQTT-API mit Session-Historie → potenziell automatischer Import von Heim-Ladungen (inkl. PV-Split,
  den wir sonst schätzen).
- **Warum nicht jetzt:** hängt davon ab, ob/wie evcc läuft; eigener Connector + Dedup gegen bestehende
  Ladungen; UX-Entscheidung, wie Auto-Import und Handeintrag koexistieren. Lohnt einen eigenen
  Mini-Spike, kein Blind-Bau.

### 4. cars-dataset — Fahrzeug-Stammdaten vorbefüllen  ⤳ ersetzt durch #2
- **Repo:** `vbalagovic/cars-dataset` (globale KFZ-Spezifikationen + REST-API)
- **Status:** Dieselbe Idee (Akku-kWh/AC beim Anlegen vorschlagen) ist mit **open-ev-data (#2)**
  umgesetzt — EV-spezifischer, permissiv lizenziert, offline. cars-dataset bleibt nur als Fallback
  relevant, falls einzelne Modelle in open-ev-data fehlen (z. B. Kia Niro EV ist dort aktuell nicht
  enthalten → Feld bleibt manuell).

### 5. Eichrecht / OCMF — signierte Ladequittungen importieren  🔷 Nische
- **Repo:** `SAFE-eV/OCMF-Open-Charge-Metering-Format`
- **Idee:** Öffentliche Ladequittungen tragen kryptografisch signierte Zählerwerte (OCMF). Wer diese
  Datei/den QR hat, könnte die **exakt geeichte kWh** statt der Auto-SoC-Schätzung importieren.
  Sehr genau, aber Nischen-Workflow (man müsste die OCMF-Daten der Quittung erfassen).

### 6. chargeprice — Tarife/Preise je Station  🔷 kommerziell
- **Repo:** `chargeprice/chargeprice-api-docs`
- **Idee:** €/kWh je Station automatisch. Aber kommerzielle API mit Freigabe/Anmeldung → mehr Reibung
  als unser bestehendes Anbieter-Preis-Verzeichnis. Nur wenn OCM-Preise nicht reichen.

### Reine Referenzen (nichts zu übernehmen)
- **EVMap** (Android-App), **Pumperly** (Routenplaner) — fertige Apps, nicht integrierbar; höchstens
  als UX-Ideengeber.
- **OpenEMS / SolarNetwork / EEBUS / OpenEVSE** — Steuerungs-/HW-Welt, gehören ins HA-/Wallbox-Setup,
  nicht in ein Tracking-Tool.

---

## Fazit für Robert

Die Liste ist als *Lesezeichen-Sammlung fürs E-Mobility-Ökosystem* wertvoll, aber sie ist nicht auf
Verbrauchs-Tracker zugeschnitten. Die zwei direkten Treffer sind jetzt umgesetzt und getestet:
**Open Charge Map** (Betreiber-Autoerkennung, v3.0.70) und **open-ev-data** (Akku-/Ladeleistungs-
Vorbefüllung beim Fahrzeug-Anlegen, dieser Commit). Beide laufen offline bzw. nur mit optionalem
freien Key und können nichts kaputt machen. Der spannendste *nächste* Schritt bleibt **evcc als
Auto-Import für Heim-Ladungen**, falls evcc an der Wallbox läuft — ein eigenes kleines Projekt,
kein Beifang.
