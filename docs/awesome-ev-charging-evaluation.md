# Bewertung: `juherr/awesome-ev-charging` für den EV Charge Tracker

**Repo:** https://github.com/juherr/awesome-ev-charging (curated „Awesome"-Liste, ~155★)
**Frage:** Ist die Liste für unseren Charge Tracker nützlich? Können wir etwas davon übernehmen?
**Stand:** 2026-08-04 · geprüft gegen App-Version 3.0.69

---

## Kurzfassung

Die Liste ist gut gepflegt, aber sie ist eine **Infrastruktur-/Protokoll-Liste** — sie richtet
sich an Leute, die *Ladesäulen, Backends und Roaming-Netze* bauen (CPO/EMSP-Seite). Unser Tracker
ist ein **Verbrauchs-Logbuch auf Fahrzeugseite**: er zieht Telemetrie aus der Hersteller-App des
Autos und protokolliert Ladungen, Kosten, CO₂, PV, Fahrten, Wartung. Die Domänen überlappen kaum.

**≈ 90 % der Liste ist für uns irrelevant** (OCPP, OCPI, ISO 15118, OICP/eMIP, Eichrecht, Wallbox-
Firmware, CPO/EMSP-Backends, Batterie-Emulatoren, Zephyr/RTOS, DATEX II …). Diese Dinge setzen
voraus, dass man selbst eine Ladesäule oder ein Ladenetz betreibt — das tun wir nicht.

**Genau ein Eintrag ist ein echter, sofort umsetzbarer Gewinn: Open Charge Map.** Er dockt
1:1 an Funktionen an, die wir schon haben (GPS-Erfassung des Ladeorts am Formular, Anbieter-
Verzeichnis mit Preis-Autofill, Nominatim-Reverse-Geocoding). → **Umgesetzt in diesem Commit.**

Daneben gibt es 3–4 „interessant, aber später/größer"-Kandidaten (v. a. **evcc** als Datenquelle
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
| **Ladeort-Register / Datensätze** | **Open Charge Map**, chargeprice, EVMap, cars-dataset | **JA** — hier liegt der Nutzen |
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

### 2. evcc — Datenquelle für Heim-Ladungen  🔶 Kandidat, größer
- **Repo:** `evcc-io/evcc` (Go, sehr populär, PV-Überschussladen)
- **Idee:** Wenn Robert evcc an der Wallbox betreibt (vgl. Vault-Projekt *„Wallbox + Boiler auf
  Riemann-Power umstellen"*), protokolliert evcc **jede Heim-Ladesession** inkl. kWh, Kosten und
  **Solaranteil**. Das ist genau, was wir heim aktuell **manuell** eintippen. evcc hat eine REST-/
  MQTT-API mit Session-Historie → potenziell automatischer Import von Heim-Ladungen (inkl. PV-Split,
  den wir sonst schätzen).
- **Warum nicht jetzt:** hängt davon ab, ob/wie evcc läuft; eigener Connector + Dedup gegen bestehende
  Ladungen; UX-Entscheidung, wie Auto-Import und Handeintrag koexistieren. Lohnt einen eigenen
  Mini-Spike, kein Blind-Bau.

### 3. cars-dataset — Fahrzeug-Stammdaten vorbefüllen  🔷 nice-to-have
- **Repo:** `vbalagovic/cars-dataset` (globale KFZ-Spezifikationen + REST-API)
- **Idee:** Beim Anlegen eines Fahrzeugs (Setup-Wizard Schritt 3 / Fahrzeug-HW) Akku-kWh, max. AC etc.
  aus Modellnamen vorschlagen. Reiner Komfort; Datenqualität/Abdeckung vorher prüfen.

### 4. Eichrecht / OCMF — signierte Ladequittungen importieren  🔷 Nische
- **Repo:** `SAFE-eV/OCMF-Open-Charge-Metering-Format`
- **Idee:** Öffentliche Ladequittungen tragen kryptografisch signierte Zählerwerte (OCMF). Wer diese
  Datei/den QR hat, könnte die **exakt geeichte kWh** statt der Auto-SoC-Schätzung importieren.
  Sehr genau, aber Nischen-Workflow (man müsste die OCMF-Daten der Quittung erfassen).

### 5. chargeprice — Tarife/Preise je Station  🔷 kommerziell
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
Verbrauchs-Tracker zugeschnitten. Der einzige direkte Treffer — **Open Charge Map zur Betreiber-
Autoerkennung** — ist umgesetzt und getestet; ein OCM-Key in den Einstellungen aktiviert es. Der
spannendste *nächste* Schritt wäre **evcc als Auto-Import für Heim-Ladungen**, falls evcc an der
Wallbox läuft; das ist aber ein eigenes kleines Projekt, kein Beifang.
