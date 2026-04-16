# Changelog

## v2.17.6 (2026-04-16)

### Fix: Hyundai Token-Fetch — warten auf CCSP-Code, nicht auf ctbapi-Code

Revert von v2.17.5 plus Grund-Ursache. Der Hyundai CTB-Flow hat **zwei Codes** in der Redirect-Kette:
1. `ctbapi.hyundai-europe.com/api/auth?code=X` — Code für `client_id=peuhyundaiidm-ctb` (der Login-Client). Dieser Code gehört NICHT zum Token-POST.
2. Danach Server-Redirect auf `prd.eu-ccapi.hyundai.com:8080/.../oauth2/token?code=Y` — Y ist der CCSP-Code für `client_id=6d477c38-...` (der API-Client). Das ist der Code, den das Token-Endpoint erwartet.

In v2.17.4 hatte ich die URL-Prüfung auf „enthält `code=`" gelockert — Selenium hat dadurch Code X von ctbapi gegriffen. Mein v2.17.5-Versuch mit `redirect_uri=ctbapi` beim Token-POST ging in die Hose, weil der API-Client ctbapi gar nicht als Redirect registriert hat (→ „Invalid redirect uri").

Richtiger Fix:
1. Wait-Bedingung zurückgenommen auf **URL enthält `prd.eu-ccapi.hyundai.com` UND `code=`**. So wartet Selenium den zweiten Redirect ab und bekommt den richtigen CCSP-Code Y.
2. Token-POST benutzt wieder **`redirect_uri=redirect_final`** (entspricht der URL, auf der der CCSP-Code ausgestellt wurde). v2.17.5-Branching rückgängig.
3. Error-Meldung bei Wait-Timeout zeigt jetzt explizit welche URL erreicht wurde, damit wir im Log-Fall sofort sehen ob's an einem dritten Redirect-Host hing.

Kia (oneid, 2-Step-Authorize) unverändert.

## v2.17.5 (2026-04-16)

### Fix: Hyundai Token-POST benutzt falsches `redirect_uri`

Hyundai-Token-Endpoint gab 400 zurück mit `"Mismatched token redirect uri. authorize: https://ctbapi.hyundai-europe.com/api/auth token: https://prd.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/token"`. OAuth2 verlangt, dass der `redirect_uri`-Parameter beim Token-Austausch **exakt** gleich ist wie beim vorangehenden Authorize-Request.

Mein Code hat blind `cfg['redirect_final']` für den POST benutzt — das stimmt für Kia (dessen zweiter Authorize-Schritt tatsächlich mit `redirect_final` als redirect_uri läuft), aber nicht für Hyundai CTB. Hyundai hat nur **einen** Authorize-Schritt mit `redirect_uri=login_redirect` (`ctbapi.hyundai-europe.com/api/auth`). Der Browser landet danach zwar auf `prd.eu-ccapi.hyundai.com:8080/.../oauth2/token?code=...` (das ist das CTB-Display-URL), aber der Code wurde von idpconnect gegen `ctbapi...` ausgestellt.

Fix: beim Token-POST wird pro Flow entschieden — `ctb` → `login_redirect`, `oneid` (Kia) → `redirect_final`. Kia bleibt byte-genau wie vorher.

## v2.17.4 (2026-04-16)

### Hyundai-Token-Fetch: URL-Match robuster + bessere Fehlermeldungen

Zwei Fixes in einem Release:

**1. URL-Match relaxt.** In v2.17.2 hat der Wait verlangt, dass die Final-URL mit `https://prd.eu-ccapi.hyundai.com` startet **und** `code=` enthält. In der Praxis landet der Browser je nach Flow-Variante manchmal auf `ctbapi.hyundai-europe.com/api/auth?code=XXX` statt direkt auf prd.eu-ccapi — mein Match hat das nicht akzeptiert und gelaufen bis zum 5-Minuten-Timeout. Jetzt reicht: URL enthält `code=`, egal auf welchem Host.

**2. Leere Fehlermeldungen aufgelöst.** User berichtete eine rote „message:"-Anzeige ohne weiteren Text neben dem Token-Button — das war entweder eine Selenium-`TimeoutException` mit leerer Message, oder ein verschluckter Exception-Body. Alle Error-Paths im Token-Fetch-Flow geben jetzt explizit `{Typ}: {Message}` zurück, plus Kontext (letzte URL bei Timeout, HTTP-Body bei Token-POST-Fehler, usw.). Bei völlig leerem `str(e)` fällt der Code auf den Exception-Typnamen zurück. Zusätzlich wird der komplette Traceback auf dem Server geloggt (`journalctl -u ev-tracker.service`) damit auch Server-seitige Diagnose möglich ist.

Kia-Pfad unverändert.

## v2.17.3 (2026-04-16)

### Fix: VAG-Connector — Importpfad für CarConnectivity-Klasse

In `carconnectivity >= 0.11` ist die `CarConnectivity`-Klasse nicht mehr im Top-Level-Package, sondern im Submodul `carconnectivity.carconnectivity`. Der alte Import `carconnectivity.CarConnectivity(...)` warf: `module 'carconnectivity' has no attribute 'CarConnectivity'` — was mit v2.17.1 (dem Error-Surfacing-Fix) jetzt überhaupt erst sichtbar wurde; in v2.17.0 und davor hat das generische „Benutzer und Passwort prüfen"-Flash den eigentlichen Fehler verdeckt.

Fix: Import mit Fallback — erst das neue Submodul probieren, dann das alte Top-Level-Import. Damit funktioniert's auf beiden Library-Versionen.

## v2.17.2 (2026-04-16)

### Fix: Hyundai Token-Fetch hängt im Selenium-Wait

v2.17.0 hat für Hyundai als „Login erkannt"-Kondition auf `button.mail_check` oder `button.ctb_button` gewartet — Selektoren aus dem RustyDust-Script, die aber auf einer Zwischen-Confirmation-Seite sitzen, die Hyundai offenbar in manchen Flows **überspringt**. Der Browser landet direkt auf `prd.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/token?code=XXX` und zeigt den JSON-Body `{"result":"E","data":null,"message":"url is not defined"}` — was übrigens **kein Fehler** ist, sondern der erwartete End-Zustand (der Server strippt den `code`-Query-Param beim Rendern). Selenium hat aber weiter auf Buttons gewartet, die nie kommen, und ist nach 5 min in den Timeout gerannt.

Fix: Per-Flow-Logik in `_do_fetch()`. Für den CTB-Flow (Hyundai) warte nicht auf DOM-Elemente sondern auf die URL-Änderung — sobald `driver.current_url` auf `prd.eu-ccapi.hyundai.com` startet und `code=` enthält, ist der Login durch. Selenium extrahiert direkt aus der URL und überspringt den separaten `driver.get(redirect_url)`-Schritt (den Hyundai im CTB-Flow eh schon selbst macht). Der Kia-oneid-Flow bleibt 1:1 wie vorher: CSS-Wait auf `a.logout.user`, dann manuelle Navigation zum CCSP-Authorize-Endpoint.

## v2.17.1 (2026-04-16)

### Fix: VAG (VW/Skoda/Seat/Cupra/Audi) zeigt echten Fehler statt generischem „Passwort prüfen"

VW-Group's Identity-Server (`identity.vwgroup.io`) fordert regelmäßig — nach Passwort-Änderungen, AGB-Updates oder neuen Datenschutzbestimmungen — ein **erneutes Akzeptieren** durch den Nutzer. Die CarConnectivity-Library wirft in dem Fall eine Exception mit der exakten URL zum Akzeptieren (`Try visiting: https://identity.vwgroup.io/...`). Bisher hat `VAGConnector.test_connection()` diese Exception aber mit `except Exception: return False` stumm verworfen und die App flashte das generische „Verbindung fehlgeschlagen. Zugangsdaten prüfen." — wodurch jeder Nutzer naheliegenderweise dachte Benutzername/Passwort wären falsch, was dann beim Testen Login-Throttling getriggert hat.

Fix: `test_connection()` fängt die Exception nicht mehr, lässt sie zur App-Route durchpropagieren, die sie in der flash-Message mit `flash.error` ausgibt — inklusive der Consent-URL. `authenticate()` (das für den Background-Sync benutzt wird) bleibt defensiv und speichert jetzt zusätzlich `self._last_error` als Hinweis für Log-Auswertung.

**Nutzer-seitig**: wenn das Error nochmal kommt, steht in der flash-Message jetzt die URL, die der Nutzer im Browser öffnen, sich einloggen und den Consent klicken muss. Dann geht die Skoda/VW/Audi/Seat/Cupra-Verbindung wieder.

## v2.17.0 (2026-04-15)

### Hyundai Refresh-Token: richtige OAuth-URLs (CTB-Flow)

Der „Token holen"-Button funktioniert jetzt auch für Hyundai EU. Hintergrund: in v2.16.0 und davor hatte `services/vehicle/token_fetch.py` für Hyundai einfach die Kia-Konfiguration kopiert und nur die Domain getauscht — das konnte nie funktionieren, weil Kia und Hyundai EU **komplett unterschiedliche OAuth-Flows** verwenden, obwohl sie beide zur selben Mutterfirma gehören und auf derselben `hyundai_kia_connect_api`-Library aufsetzen.

**Die Unterschiede:**

| Feld | Kia EU (oneid) | Hyundai EU (CTB) |
|---|---|---|
| Flow | oneid/online-sales auf kia.com | CTB (Connected Car Telematics Business) auf ctbapi.hyundai-europe.com |
| `login_client_id` | `peukiaidm-online-sales` | `peuhyundaiidm-ctb` |
| `login_redirect` | `www.kia.com/api/bin/oneid/login` | `ctbapi.hyundai-europe.com/api/auth` |
| `state` | Base64-URL + `_default`-Suffix | Kurzer Country-Code + `_` (z.B. `EN_`) |
| `redirect_final` | `.../oauth2/redirect` | `.../oauth2/token` |
| `client_secret` | Literal-String `"secret"` | Echter 48-Zeichen-Key `KUy49Xx...` |
| User-Agent | Mobile Android | Desktop Chrome |
| Extra authorize-Params | keine | `connector_client_id`, `captcha=1`, `ui_locales`, `nonce` |

Die alte Config hat sechs von sieben Feldern falsch gehabt — nur `client_id` war korrekt. Der Token-Austausch scheiterte außerdem immer an der hart kodierten `client_secret: 'secret'`, weil Hyundai's Endpoint bei falschem Secret 401 zurückgibt.

**Fix:**

- `services/vehicle/token_fetch.py` — `BRAND_CONFIG['hyundai']` komplett ersetzt, `BRAND_CONFIG['kia']` explizit `client_secret: 'secret'` hinzugefügt (früher hart kodiert, jetzt konsistent). Neues Feld `user_agent` pro Marke (Mobile für Kia, Desktop für Hyundai, beide behalten das `_CCS_APP_AOS`-Suffix das den „use the app"-Block umgeht). Neues Feld `flow` pro Marke als Discriminator. Neue Helper-Funktion `_build_login_url(cfg)` baut die Login-URL per Flow — CTB braucht `connector_client_id`, `captcha=1`, `ui_locales` etc., die der Kia-oneid-Flow gar nicht kennt. Der Token-Exchange-POST zieht jetzt `cfg.get('client_secret', 'secret')` statt der Hardcoding.
- `services/vehicle/connector_hyundai_kia.py` — Docstring aktualisiert, beide Connectors (Kia + Hyundai) teilen sich wieder den Refresh-Token-Flow, haben aber weiterhin eigene `credential_fields()`-Overrides für saubere Labels.
- `templates/settings.html` — `updateVehicleFields()` zurück auf `isKiaHyundai` für Token-Hint-Section und Refresh-Token-Label. Hyundai-User sehen den „Token holen"-Button wieder (war in v2.16.2 fälschlich ausgeblendet, weil ich damals dachte, Hyundai ginge mit Passwort-Login).

**Quellen**: zwei unabhängige Working-Scripts aus der hyundai_kia_connect_api-Community (Hyundai Token Solution Subfolder im upstream repo + RustyDust/bluelink_refresh_token) bestätigen alle Werte identisch. Dazu die Library-Source selbst (`KiaUvoApiEU.py`) mit `CCSP_SERVICE_ID` und `CCS_SERVICE_SECRET` als Runtime-Konstanten — die werden bei jedem späteren API-Call validiert, sind also garantiert aktuell.

Kia-Flow bleibt **1:1 unverändert** bis auf das Auslagern von `client_secret` in die Config — der funktionierende Pfad wird nicht angefasst.

## v2.16.2 (2026-04-15)

### Hyundai: Login mit Passwort + PIN statt Refresh-Token

Bis jetzt hat die App sowohl für Kia als auch für Hyundai ein Refresh-Token verlangt (beides lief über `CREDENTIAL_FIELDS` mit Label „Refresh-Token"). Für **Kia EU** ist das seit 2025 Pflicht weil reCAPTCHA den direkten Passwort-Login blockt, für **Hyundai EU** funktioniert aber weiterhin der klassische Flow mit E-Mail + Passwort + 4-stelliger PIN.

Fix: credential_fields pro Marke trennen.

- `services/vehicle/connector_hyundai_kia.py` — zwei separate Listen: `KIA_CREDENTIAL_FIELDS` (Refresh-Token, Help-Text verweist auf den Token-Fetch-Button) und `HYUNDAI_CREDENTIAL_FIELDS` (normales Passwort-Feld). Beide Connector-Klassen überschreiben `credential_fields()` mit ihrer eigenen Liste.
- `templates/settings.html` — Frontend-Logik in `updateVehicleFields()` splittet `isKiaHyundai` in `isKia` und `isHyundai`. Token-Hint-Section und „Refresh-Token"-Label jetzt nur noch für Kia (und Tesla) — Hyundai zeigt normales „Passwort"-Feld, kein „Token holen"-Button.
- Kia-Flow bleibt **exakt** wie er ist (unangetastet, funktioniert).

Falls Hyundai EU irgendwann auch reCAPTCHA aktiviert, fliegt das hier beim Auth-Versuch mit einem Fehler auf und wir müssen Hyundai in den Token-Flow schieben. Aktuell reicht aber User + Passwort.

## v2.16.1 (2026-04-15)

### Fix: /api/system/updates/status crasht bei Permission-Denied auf UU-Log

`/var/log/unattended-upgrades/unattended-upgrades.log` ist standardmäßig `root:adm` mit Mode 640 — der ev-tracker-User kann es nicht lesen. In v2.16.0 fing mein Code `PermissionError` nur beim `open()` ab, nicht aber beim vorangehenden `.is_file()` auf dem Path-Objekt (das auf einem 640-Verzeichnis ebenfalls knallt). Ergebnis: 500 auf der Status-Route, Card blieb bei „Status wird geladen …" hängen.

Fix: `.is_file()` komplett entfernt, stattdessen direkt `open()` mit einem umfassenden `except (FileNotFoundError, PermissionError, OSError)`. Wenn das Log unlesbar ist, zeigt die Card halt „nie" als letzten Lauf — das ist kein Fehler, weil die `pending_count` aus dem Dry-Run eh die aktuellen Infos liefert.

## v2.16.0 (2026-04-15)

### System-Updates (Debian Security-Only) im Settings-Menü

Neue Settings-Card „System-Updates (Sicherheit)" zwischen Benachrichtigungen und Backup. Debian-Sicherheitsupdates lassen sich jetzt aus dem Browser heraus manuell prüfen, installieren und ein eventuell erforderlicher Neustart auslösen — bei gleichzeitig minimaler Angriffsfläche.

**Design-Entscheidung: strikt security-only.** Kein voller apt-Zugriff aus der Web-UI. Grund: wer das Web-Login knackt, bekäme sonst effektiv Root-Rechte aufs OS (apt kann beliebige Pakete installieren + Post-Install-Scripts als root laufen lassen). Stattdessen wird auf der VM das Debian-Standard-Tool `unattended-upgrades` eingerichtet, das ausschließlich aus `${distro_id}:${distro_codename}-security` zieht. Die Sudoers-Regel erlaubt dem ev-tracker-User exakt **einen** Befehl: `/usr/bin/unattended-upgrade -v`. Ein Angreifer mit Web-Login kann bestenfalls einen Security-Patch-Lauf auslösen — kein Paket seiner Wahl installieren.

**Neue Features:**

- Card zeigt: Anzahl verfügbarer Security-Patches, Datum des letzten automatischen Laufs, „Reboot erforderlich"-Warnbanner wenn `/var/run/reboot-required` vorhanden ist
- „Security-Updates jetzt installieren"-Button startet `unattended-upgrade -v` in einem Background-Thread. Die UI pollt alle 2,5 s den Status und zeigt das Log live an.
- „Jetzt neu starten"-Button erscheint nur wenn ein Reboot nötig ist, mit doppelter Bestätigung (User muss ja die LUKS-Passphrase nach dem Boot neu eingeben)
- Unattended-upgrades läuft auch ganz normal weiter automatisch via Debian's `apt-daily.timer` und `apt-daily-upgrade.timer` — die UI ist nur der manuelle Override plus Statusanzeige

**Technik:**

- `services/system_update_service.py` — kapselt das Lesen des UU-Logs (`/var/log/unattended-upgrades/unattended-upgrades.log`), das Zählen der pending Updates (via `unattended-upgrade --dry-run -v`), den Background-Thread-Runner für Apply, und den Reboot-Scheduler. State liegt in einem thread-safe Modul-Dict, kein DB-Eintrag nötig.
- `app.py` — neue Routen: `GET /api/system/updates/status`, `POST /api/system/updates/apply`, `POST /api/system/reboot`. Alle drei hinter dem Auth-Guard.
- `templates/settings.html` — neue Card plus separater `<script>`-Block (nach dem gleichen Pattern wie die Notify-Card in v2.15.2, damit ein JS-Error weiter oben die Sysupd-Handler nicht killt)
- **19 neue Übersetzungs-Keys** (`set.sysupd_*`) in allen 6 Sprachen

**Eingriff auf den VMs (Paste-Block als root):**

- `apt install -y unattended-upgrades` falls fehlt
- `/etc/apt/apt.conf.d/20auto-upgrades` aktivieren (`APT::Periodic::Update-Package-Lists "1"; APT::Periodic::Unattended-Upgrade "1";`)
- `/etc/apt/apt.conf.d/50unattended-upgrades` checken: `${distro_id}:${distro_codename}-security` muss aktiv sein, andere Origins müssen kommentiert bleiben
- Sudoers-Zeilen hinzufügen: `/usr/bin/unattended-upgrade -v`, `/usr/bin/unattended-upgrade --dry-run *`, `/sbin/shutdown -r now`

## v2.15.2 (2026-04-15)

### Fix: Notify-Card Handler liefen gar nicht mehr

v2.15.1 hat die `<form>` entfernt und den Save-Button auf `type="button"` umgestellt. Das hat den Reload verhindert, aber jetzt passierte **gar nichts** beim Klick — Button reagierte nicht. Safari-Konsole bestätigte: Button-Element existiert, aber der Click-Handler war nicht angehängt. Das heißt: die IIFE hat nicht bis zum `addEventListener` durchlaufen.

Ursache vermutlich: im großen `<script>`-Block von `settings.html` läuft weiter oben Code mit Leaflet, Location-Map und diversen Formularen. Ein Fehler irgendwo früher hat den Parse der Notify-IIFE in Safari blockiert. Backup-Form war zufällig noch OK (vielleicht anderer Codepfad), Notify nicht.

Fix: Notify-Handler wurde komplett aus dem großen Script rausgezogen und läuft jetzt in einem **eigenen `<script>`-Block am Ende der Seite**. Kein IIFE-Pyramiding, kein Promise-basiertes `.then()` statt `async/await` (falls Safari da irgendeinen Edge-Case hat), explizite `credentials: 'same-origin'` in den fetch-Calls, plus console.log an strategischen Stellen (`[notify] init start`, `[notify] handlers attached`, `[notify] save click`) damit man beim nächsten Problem sofort in der Konsole sieht, was passiert.

## v2.15.1 (2026-04-15)

### Fix: Benachrichtigungen-Card speicherte nicht

In v2.15.0 war die Benachrichtigungen-Card als echtes `<form>`-Element mit einem `<button type="submit">` gebaut. Aus noch unverstandenen Gründen hat der JS-Submit-Handler in Safari nicht gegriffen (vermutlich ein Reihenfolge-Problem mit einer vorangehenden IIFE im gleichen `<script>`-Block, die in bestimmten Fällen den weiteren Parse abbricht). Effekt: beim Klick auf „Speichern" machte der Browser ein natives Form-Submit (GET ohne Body), die Seite lud neu, die Felder waren wieder leer — obwohl der Backend-Code und die Routen einwandfrei funktionierten (per fetch aus der Devtools-Konsole direkt bestätigt: POST und GET liefern `{ok:true, ...}`).

Fix ist pragmatisch statt chirurgisch: `<form>` → `<div>`, `<button type="submit">` → `<button type="button">` mit direktem Click-Handler. Kein Form-Submit-Event mehr = kein möglicher Reload, egal was sonst im Script passiert. Funktional identisch, nur ohne die versteckte Reload-Falle.

## v2.15.0 (2026-04-15)

### Push-Benachrichtigung bei VM-Neustart (ntfy.sh)

Die VMs auf dem NAS kommen nach einem Reboot (Stromausfall, NAS-Update, manueller Neustart) automatisch wieder hoch, aber das LUKS-Volume ist dann versiegelt — der Nutzer muss manuell im Browser auf die Unlock-Seite und die Passphrase eintippen. Das Problem: ohne Rückkanal merkt der Nutzer das erst, wenn er das nächste Mal die App aufruft. Diese Version baut einen leichten Push-Kanal über **ntfy.sh**:

- Neue Settings-Card **„Benachrichtigungen"** (zwischen Zugangsschutz und Backup). Checkbox zum Aktivieren, Feld für den ntfy-Topic-Namen, optional eigener ntfy-Server, Speichern- und Test-Button. Der Topic-Name ist frei wählbar; er ist das einzige „Geheimnis" des Push-Kanals — die UI weist explizit darauf hin, einen schwer zu erratenden Namen zu wählen.
- Der Nutzer installiert die kostenlose **ntfy-App** (iOS/Android), abonniert dort den gleichen Topic-Namen — fertig. Kein Account, kein Server, keine Gebühren.
- **Config lebt außerhalb des LUKS-Volumes** unter `/var/lib/ev-tracker/notify.json`. Das ist wichtig, weil der Unlock-Helper (`ev-unlock-web`) genau dann läuft, wenn LUKS versiegelt ist — er könnte keine Config aus der App-DB lesen. Der Ordner gehört `ev-tracker:ev-tracker` mit Mode 0750, so dass weder sudo noch root nötig sind. Der Trade-off: der Topic-Name liegt im Klartext außerhalb der Verschlüsselung. Wer Root auf der VM hat, kann ihn lesen — wer Root hat, hat aber ohnehin gewonnen, insofern ist das akzeptabel.
- Technik: `services/notify_service.py` kapselt Lesen/Schreiben der JSON-Datei (mit Fallback auf `data/notify.json` für lokale Entwicklung) und den tatsächlichen HTTP-POST via `urllib.request` — kein curl, keine zusätzliche Dependency. Neue Routen `GET/POST /api/settings/notify` (Config laden/speichern) und `POST /api/settings/notify/test` (Testnachricht).
- **15 neue Übersetzungskeys** pro Sprache in allen 6 Sprachen (`set.notify_*`).

**Eingriff auf den VMs (per Paste-Block als root):**

Da der eigentliche Push aus dem Boot-Pfad feuern muss (bevor LUKS entsperrt ist, also außerhalb des App-Updates), kommt dazu eine kleine neue systemd-Unit `ev-notify-boot.service` plus das Helper-Script `/usr/local/bin/ev-notify-boot`. Die Unit läuft als Oneshot vor `ev-unlock-web.service`, aber nur wenn LUKS noch versiegelt ist (`ConditionPathExists=!/srv/ev-data/app/venv/bin/python`). Sie liest `/var/lib/ev-tracker/notify.json`, und wenn `enabled:true` und ein Topic gesetzt ist, schickt sie einen einzigen POST an `<server>/<topic>` mit Hostname + Uhrzeit in der Message. Schlägt der POST fehl → exit 0, damit ein ausgefallener ntfy-Server niemals den Boot blockiert.

## v2.14.0 (2026-04-15)

### Wizard-Schritt 2 wird „Web-Login anlegen" + Backup/Restore-Feature

**Wizard-Umbau**

Der Setup-Wizard auf frisch provisionierten VMs hat jetzt einen anderen zweiten Schritt. Bisher wollte er das `ev-tracker`-Unix-SSH-Passwort ändern, was aber genau die Admin-SSH-Verbindung gekappt und die Wartung unnötig erschwert hat. Stattdessen:

- **Schritt 1** bleibt: LUKS-Passphrase ändern. Muss der Nutzer durchführen.
- **Schritt 2 NEU**: der Nutzer legt einen **Web-UI-Benutzer + Web-UI-Passwort** an. Die Auswahl zum Ändern des Shell-Passworts ist komplett entfernt — der Shell-User bleibt unangetastet, damit der Admin mit dem ev-provision-Temp-Passwort weiterhin per SSH für Wartung auf die VM kann. Der Web-Login ist ab sofort der einzige Weg ins Dashboard.

Technische Details:

- `templates/setup.html` — Schritt 2 komplett umgebaut: Eingabefelder für Username + Passwort + Confirm, Submit ruft jetzt `POST /api/setup/create_web_login`. Progress-Pills und die Stepwelcome-Liste nennen den neuen Schritt namentlich. Der Wizard-Header zeigt jetzt auch die App-Version als Badge.
- `services/setup_service.py` — `change_user_password()` und die sudoers-Abhängigkeit auf `chpasswd` sind weg. Wizard-State-Key heißt jetzt `weblogin_done` statt `password_done`. Der Modul-Docstring ist aktualisiert und erklärt explizit, dass der Wizard den Unix-Login **nicht** anfasst.
- `app.py` — neuer Endpoint `POST /api/setup/create_web_login` ersetzt `POST /api/setup/change_password`. Er ruft `auth_service.set_credentials()` auf (das den Guard automatisch scharfschaltet), loggt den Nutzer direkt ein und räumt bei abgeschlossener Wizard-State-Kombination den Setup-Marker auf. Die `app_version` wird jetzt auch an das Wizard-Template durchgereicht.

Settings → Zugangsschutz bleibt unverändert und erlaubt dem Nutzer jederzeit, seinen Web-User/Pw zu ändern, hinzuzufügen oder zu deaktivieren.

**Backup & Wiederherstellung der Datenbank**

Neues Feature für VM-Umzüge, Backups und Wiederherstellung nach Fehler:

- Neue Settings-Card „Backup & Wiederherstellung" (platziert zwischen Zugangsschutz und App-Info).
- **Export**: `GET /api/backup/export` flushed die SQLite-WAL via `PRAGMA wal_checkpoint(TRUNCATE)` und schickt die komplette `data/ev_tracker.db` als Download mit Zeitstempel im Dateinamen (`ev-tracker-backup-YYYYMMDD-HHMMSS.db`). Enthält absolut alles: Ladungen, Fahrtenlog, Wartungslogbuch, AppConfig (inkl. Vehicle-API-Credentials, Home/Work-Koordinaten, ENTSO-E-Key, ThgQuoten, Zugangsschutz-Hash, Session-Secret), Geocode- und Weather-Cache, VehicleSync-Historie. Ein einziger File.
- **Import**: `POST /api/backup/import` als Multipart-Upload. Validiert die Datei als echte SQLite-DB und prüft, dass die Pflichttabellen `charges`, `app_config`, `vehicle_syncs` drin sind. Legt vor der Überschreibung eine Sicherheitskopie der aktuellen DB in `data/backups/ev_tracker-pre-import-<ts>.db` an, schließt dann das SQLAlchemy-Engine (wichtig auf POSIX, sonst hält die alte Inode die DB am Leben) und kopiert die neue DB drüber. Anschließend Background-Thread mit 500ms Verzögerung → `sudo systemctl restart ev-tracker.service`. Der Browser lädt nach 4.5 Sekunden automatisch neu.
- **Warnung im UI** ist bewusst drastisch: der Import überschreibt Zugangsschutz-Credentials und Vehicle-API-Keys. Nach einem Import gilt der Web-Login aus dem Backup, nicht der bisherige.

Neu in `config.py`: `DATA_DIR` ist jetzt exportiert, damit `app.py` den DB-Pfad sauber für Export/Import-Routen auflösen kann.

**Übersetzungen**

25 neue Keys in allen 6 Sprachen (de/en/fr/es/it/nl): `wiz.welcome_step1_luks`, `wiz.welcome_step2_weblogin`, `wiz.weblogin_title`, `wiz.weblogin_desc`, `wiz.weblogin_username`, `wiz.weblogin_password`, `wiz.weblogin_password_hint`, `wiz.weblogin_password_confirm`, `wiz.weblogin_info`, `wiz.weblogin_submit`, `wiz.status_creating`, `wiz.err_user_empty`, und 13 `set.backup_*`-Keys.

**Upgrade auf laufenden VMs**

Die alten Tags v2.11.x / v2.12.0 / v2.13.0 wurden gelöscht und `main` auf den v2.9.0-Commit zurückgesetzt. Laufende VMs, die vorher eine dieser Versionen hatten, können mit `git pull` nicht mehr auf den aktuellen main kommen (die History wurde umgeschrieben). Stattdessen `git fetch origin && git reset --hard origin/main` — siehe Upgrade-Paste-Block in den Release Notes.

## v2.9.0 (2026-04-14)

### Übersetzungen für alle v2.7.x/v2.8.x Features + HTTPS-Autohide + README

- **60 neue Übersetzungskeys** in allen 6 Sprachen (de/en/fr/es/it/nl) — deckt den Setup-Wizard (`wiz.*`), die Login-Seite (`login.*`) und den Zugangsschutz-Block in den Settings (`set.auth_*`) ab. Damit sind alle neuen Features aus v2.7.0–v2.8.1 vollständig lokalisiert.
- **Setup-Wizard (`templates/setup.html`)** nutzt jetzt `t()` statt hardkodiertem Deutsch — Title, Welcome, beide Wizard-Schritte, Done-Screen, Fehlermeldungen und Button-Texte.
- **Login-Seite (`templates/login.html`)** ist vollständig übersetzt inkl. Footer-Text.
- **Zugangsschutz-Block in Settings** übersetzt inkl. Fehlermeldungen und Disable-Confirm-Dialog.
- **HTTPS-Autohide**: Wenn der Request aus dem Tailscale-CGNAT-Bereich (`100.64.0.0/10`) kommt, blendet `/settings` die komplette HTTPS-Card aus. Tailscale verschlüsselt den Transport schon — ein self-signed-Zertifikat obendrauf ist dann nur Rauschen. Direkter LAN- oder Localhost-Zugriff sieht die Card weiterhin wie gehabt.
- **README aktualisiert** mit Abschnitten zu Web-UI-Login, First-Run-Setup-Wizard, VM-Deployment-Flow und der systemd-Awareness des In-App-Updaters. String-Count auf ~540 pro Locale aktualisiert.

## v2.8.1 (2026-04-14)

- **Dashboard: Durchschnittslinie im SOH-Plot** — Der SOH-Chart in der Vehicle-History bekommt eine horizontale graue gestrichelte Linie mit dem Mittelwert aller angezeigten Messpunkte. Macht Drift/Trends auf einen Blick erkennbar. Der Mittelwert wird in der Legende unter dem Chart als `Ø xx.x%` angezeigt. Nur aktiv wenn ≥3 Datenpunkte vorhanden sind. Andere Charts bleiben unverändert.

## v2.8.0 (2026-04-14)

### Optional: Web-UI Login als Vorschaltseite

Tailscale schützt den Netzwerkzugriff — aber wer den Share-Link kennt und im Tailnet ist, landet ohne weitere Hürde im Dashboard. Dieses Release bringt eine eingebaute Passwort-Vorschaltseite als Defense-in-Depth:

- **Optional**: Standardmäßig aus. Wer sie will, schaltet sie in Settings → „Zugangsschutz" ein. Bestehende Installs sind nach dem Update unverändert, niemand wird aus seiner eigenen App gesperrt.
- **Integriert**: Teil der App, nicht vor die App geschoben. Updates vom GitHub-Repo rollen normal durch und brechen die Auth nicht.
- **Session-Cookies**: Flask-Sessions mit einem pro-Install generierten, in AppConfig persistierten 32-Byte-Secret (siehe `services/auth_service.py:get_or_create_session_secret`). 30 Tage Lifetime.
- **Password-Hashing**: Werkzeug `generate_password_hash` / `check_password_hash` (bcrypt-kompatibel). Klartext landet nie auf Disk.
- **Einfache UX**: Simpler Username+Password-Login, keine E-Mail, kein Account-Management. Einziger Flow für den Ein-Personen-Fall.

Neue Endpunkte: `/login`, `/logout`, `/api/auth/enable`, `/api/auth/disable`, `/api/auth/change_password`. Guard läuft als `before_request`-Hook parallel zum Setup-Wizard-Guard — Setup hat Vorrang, damit ein frisch provisionierter Nutzer erstmal durch den Wizard kann ohne schon auth-konfiguriert zu sein.

Voraussetzung für echte Sicherheit ist nach wie vor, dass die VM nur über Tailscale erreichbar ist (UFW nur auf `tailscale0`). Der App-Login ist die zweite Schicht nach dem VPN.

## v2.7.4 (2026-04-14)

- **Setup-Wizard: LUKS-Device-Detection ohne Root-Privilegien** — `get_luks_device()` rief vorher `cryptsetup status evdata` auf, das aber `/dev/mapper/evdata` öffnen muss, und das gehört auf Debian `root:disk 660`. Der App-User `ev-tracker` ist nicht in der `disk`-Gruppe, deshalb schlug der Aufruf mit Permission denied fehl. Folge: Das Wizard-Footer zeigte „LUKS-Device: (unknown)" und — viel gravierender — der tatsächliche Passphrase-Change brach mit „LUKS-Device nicht gefunden" ab. Jetzt wird der Pfad per **Sysfs** aufgelöst: `/dev/mapper/evdata` → `dm-N` → `/sys/block/dm-N/slaves/` → Parent-Block-Device. Sysfs ist world-readable, also braucht's dafür kein sudo und keine Gruppenmitgliedschaft.

## v2.7.3 (2026-04-14)

- **Setup-Wizard: Browser-Redirect zuverlässig machen** — Der `before_request`-Hook prüfte den `Accept`-Header, um Browser-Zugriffe von API-Calls zu unterscheiden. Das war zu zerbrechlich: je nach Browser/Accept-Header landete der Nutzer auf der JSON-Antwort `{"error":"setup_pending",...}` statt auf dem Wizard. Jetzt einfach: alle GET-Requests werden während des Setups auf `/setup` umgeleitet, nur Nicht-GET (POST/PUT/DELETE) bekommen weiter die JSON-503-Antwort für API-Clients.

## v2.7.2 (2026-04-14)

- **Setup-Wizard explizit auf Linux beschränken** — `is_setup_pending()` gibt auf macOS und Windows jetzt hart `False` zurück, ohne überhaupt den Marker-Pfad zu prüfen. Praktisch war das schon vorher der Fall (der Pfad `/srv/ev-data/.setup_pending` existiert auf Nicht-Linux-Hosts sowieso nicht), aber jetzt ist's auch im Code klar dokumentiert, dass der Wizard VM-spezifisch ist. Schützt zusätzlich vor dem Randfall, dass jemand versehentlich eine Datei unter dem Pfad anlegt und damit den Wizard triggert, obwohl die nötigen `sudo cryptsetup`/`chpasswd`-Kommandos gar nicht existieren.

## v2.7.1 (2026-04-14)

- **Setup-Wizard: zweiter Schritt für das SSH-Login-Passwort** — Der First-Run-Wizard nimmt jetzt neben der LUKS-Passphrase auch ein neues Login-Passwort für den `ev-tracker`-User entgegen. Ruft unter der Haube `sudo chpasswd` auf (braucht einen zusätzlichen NOPASSWD-sudoers-Eintrag für `/usr/sbin/chpasswd`). Wizard-Fortschritt wird in `/srv/ev-data/.setup_state.json` getrackt, sodass ein Mid-Wizard-Reload den Nutzer nahtlos an den nächsten offenen Schritt stellt statt LUKS nochmal abzufragen. Erst wenn beide Schritte durch sind, wird der Marker gelöscht und das Dashboard freigegeben. Damit kann der Admin nach Provisioning beide Temp-Credentials vergessen — der Nutzer ist vollständig autark.

## v2.7.0 (2026-04-14)

### First-Run Setup-Wizard für VM-Deployments

Bisher musste der End-Nutzer einer frisch provisionierten VM per SSH reinloggen und `sudo cryptsetup luksChangeKey /dev/sdb` manuell ausführen, um die temporäre LUKS-Passphrase zu ersetzen. Das war für nicht-technische Nutzer eine dicke Hürde. Jetzt erscheint beim ersten Browser-Zugriff automatisch ein Setup-Wizard:

1. Die Provisioning-Pipeline (`ev-provision`) legt am Ende einen Marker `/srv/ev-data/.setup_pending` an.
2. Ein `before_request`-Hook leitet alle Nicht-Setup-Requests auf `/setup` um, solange der Marker existiert.
3. Der Wizard (eine einseitige HTML-Wizard-UI in `templates/setup.html`) fragt die temporäre und die neue Passphrase ab, ruft per `sudo cryptsetup luksChangeKey` das Device aus dem laufenden `cryptsetup status evdata` auf, und entfernt bei Erfolg den Marker.
4. Nach erfolgreichem Change ist der Nutzer „angekommen" — ab diesem Moment kennt niemand ausser ihm selbst die Passphrase, auch der Admin nicht.

Der Wizard ist Deutschland-only getextet (Setup ist ein einmaliger Flow und das Zielpublikum sind deutsche Nutzer), der Rest der App bleibt übersetzt wie gehabt. Nicht-VM-Hosts (z.B. Entwickler-Laptops) sind nicht betroffen, weil der Marker nie existiert.

**Voraussetzung für den Live-Betrieb**: `ev-provision` muss am Ende den Marker anlegen und die sudoers-Regel für `cryptsetup luksChangeKey` setzen. Beides ist in der Admin-Anleitung dokumentiert; für bestehende VMs einmalig nachziehen.

## v2.6.0 (2026-04-14)

### In-App Updater unter systemd reparieren

Auf Linux-Installationen mit `ev-tracker` als systemd-Service hatte der Update-Button über die App-UI faktisch nichts getan: Klick → kurze Anzeige „Update wird installiert" → nach Refresh immer noch alte Version. Root cause: Der Updater spawnt einen detached `updater_helper.py`-Prozess, der nach dem Exit des Flask-Prozesses den File-Swap erledigen soll. Unter systemd landet der Helper aber **im gleichen cgroup** wie der Service — und wenn systemd den Service zum Neustart kill't, wird der Helper mitgerissen, **bevor er die Dateien getauscht hat**. Ergebnis: Service startet neu, nichts hat sich geändert.

Fix: systemd wird jetzt erkannt (via `INVOCATION_ID` oder `/run/systemd/system`), und in dem Fall läuft der File-Swap **inline im Flask-Prozess**, bevor dieser sich beendet. Python-Bytecode liegt schon im RAM, das Überschreiben der `.py`-Dateien auf der Disk ist sicher. `pip install -r requirements.txt` wird synchron durchgeführt, dann `os._exit(0)` — und `Restart=always` in der systemd-Unit sorgt dafür, dass der Service mit dem neuen Code wieder hochkommt.

Für Standalone-Installationen (macOS, Windows, oder Linux ohne systemd) bleibt der bestehende Helper-Pfad unverändert.

## v2.5.9 (2026-04-13)

- **Kia/Hyundai Token-Fetch: Selenium-Flow für headless Linux-Umgebungen fit gemacht** — Auf VMs ohne DBus-Session (z.B. Server-Installationen mit Xvfb+noVNC für den Login-Flow) hat der Selenium-basierte Token-Fetch gleich mehrfach gestolpert: (1) Chromium crashte mit „DevToolsActivePort file doesn't exist" wegen fehlender `--no-sandbox` / `--disable-dev-shm-usage` Flags, (2) `webdriver-manager` holte eine veraltete ChromeDriver-Version (max 114) die zu modernem Chromium 147 nicht passte, (3) Debian's Chromium liegt unter `/usr/bin/chromium` statt `/usr/bin/chrome`, was Selenium nicht automatisch fand.
- Fix: `webdriver-manager` komplett rausgeworfen zugunsten des eingebauten **Selenium Manager** (ab Selenium 4.11), der den passenden ChromeDriver automatisch zieht. Chromium-Binary-Pfad wird jetzt aus `/usr/bin/chromium|chromium-browser|google-chrome` automatisch erkannt. Sandbox- und Shared-Memory-Flags werden immer gesetzt. Requirement wird bei Bedarf auf `selenium>=4.11` hochgeschoben.

## v2.5.8 (2026-04-12)

- **Fahrtenbuch: Rekup-Spalte war immer leer** — Bei jeder Bewegungserkennung sind `prev.departed_at` und `curr.arrived_at` derselbe Sync-Zeitstempel (der Moment, in dem die Bewegung erkannt wurde), wodurch das kumulative Regen-Delta immer 0 war. Die Abfahrt ankert jetzt auf `prev.last_seen_at` (letzter bestätigter Sync am alten Spot vor Abfahrt), die Ankunft bleibt `curr.arrived_at` — damit liegt die Delta-Berechnung über zwei verschiedene Syncs.

## v2.5.7 (2026-04-11)

- **Lade- und Rekup-Zyklen als ganze Zahlen** — `charge_cycles` und `recup_cycles` in `get_summary_stats` runden jetzt auf ganze Zyklen statt eine Nachkommastelle. Fraktions-Zyklen ergeben keinen intuitiven Sinn; ein ganzer Zyklus ist die Maßeinheit.

## v2.5.6 (2026-04-11)

### Hybrid recuperation: keep the km × 0.086 estimate, layer measured on top

v2.5.4/5 replaced the full lifetime recuperation with the tiny measured cumulative (6.92 kWh for the first 92 km of tracking), which threw away years of historical km where the old `km × 0.086` estimate was the best number available.

This release uses a hybrid:

- **km before the first vehicle sync** → `first_sync_odometer × static_rate` (default 0.086 kWh/km, still configurable in Settings)
- **km from that point on** → real measured `regen_cumulative_kwh` from the vehicle API

Result on a real Kia Niro dataset: `82217 × 0.086 + 6.92 = 7077.6 kWh` — matches the pre-v2.5.4 lifetime total, and from here on grows only via measured values as the car drives.

- The Recuperation KPI card now shows `7.071 + 6.9 (kumuliert)` to make the split obvious.
- The measured rate (0.075 kWh/km from the last 90d) is still shown with the broadcast icon — that's the *current driving efficiency*, separate from the historical baseline.
- The "Gemessene Rekuperation" card remains unchanged: it only shows real per-period measurements and never touches the historical km × 0.086 portion.

## v2.5.5 (2026-04-11)

### Regen scale hotfix: raw is Wh, not hundredths of kWh

v2.5.4 divided the raw Kia/Hyundai `total_power_regenerated` by 100, which still left values 10× too high. The actual unit is **Wh** (watt-hours) for a rolling 3-month window — the correct divisor is **1000**. On a real Kia Niro EV dataset the v2.5.4 values showed a regen rate of 0.75 kWh/km (physically impossible); after this fix the rate settles at ~0.075 kWh/km (matches the car's spec).

- **`_build_vehicle_sync`** now divides by 1000.0 instead of 100.0.
- **New migration `regen_scale_fix_v2`** applies a second `/10` pass on `total_regenerated_kwh` — so pre-v2.5.4 rows (already `/10`'d by v1) land on `/100` total, and v2.5.4 rows land on `/10`. Both converge on the correct `raw/1000` kWh scale.
- **`regen_cumulative_kwh` is wiped and recomputed** after the v2 migration so the monotonic series matches the corrected inputs.
- Live vehicle widget and dashboard "Gemessene Rekuperation" card now show realistic numbers.

## v2.5.4 (2026-04-11)

### Rekuperation: korrekt interpretiert, kumuliert, pro Fahrt

The Kia/Hyundai API returns `total_power_regenerated` as **hundredths of kWh for a rolling 3-month window** — not lifetime, not tenths. Every stat that touched that value was previously off by a factor of 10 and mistook the rolling window for a cumulative total. This release fixes the interpretation and builds real per-period / per-trip statistics on top of it.

#### Data fix
- **Divisor corrected** in `_build_vehicle_sync` ([app.py](app.py)): raw value is divided by **100** (not 10). A raw reading of `21534` now stores the correct `215.34 kWh` instead of `2153.4 kWh`.
- **One-time migration** on startup divides every existing `vehicle_syncs.total_regenerated_kwh` by 10 to retroactively fix rows written under the old scale. Gated by `regen_scale_fix_v1` in AppConfig so it only runs once.
- **New column `regen_cumulative_kwh`** on `vehicle_syncs` — monotonically increasing "measured regen since first sync". Built from delta-walking the raw series: positive deltas add up, rollovers (new raw < previous raw, meaning a month fell off the 3-month window) contribute 0. Backfilled for existing rows automatically on first boot after upgrade.

#### Dynamic recuperation rate
- **`kWh/km` recuperation rate is now measured from the last 90 days of vehicle syncs** (cumulative regen delta / odometer delta) instead of the hardcoded `0.086`. Falls back to the configured static value when there's no vehicle data. Settings page shows a green "automatisch" badge + the measured rate when in use.
- `get_summary_stats` now prefers the real measured lifetime cumulative over the extrapolated `total_km * recup_rate` estimate whenever vehicle history is available.

#### New `get_regen_stats()`
- Returns measured recuperation aggregated by: **today, this week, this month, last 30d, last 90d, this year, lifetime**, plus `km_equivalent` (lifetime regen converted to km at the car's actual consumption).
- Uses `bisect` lookups against a single sorted pull of the cumulative series — O(log n) per query.

#### Per-trip recuperation
- Each trip in `get_trips()` gets a `regen_kwh` field via cumulative-at-timestamp lookups at `departed_at` and `arrived_at`.
- Trip summary (`get_trip_summary`) adds `total_regen_kwh` and `regen_per_km` across the visible window.
- New **Rekup** column in the `/trips` table and in the PDF Fahrtenbuch table (80 most recent trips).

#### Dashboard
- New **"Gemessene Rekuperation"** card directly under the KPI grid: 6 period cards + km-equivalent, only shown when vehicle sync data exists.
- **Recuperation KPI card** now shows the measured `kWh/km` rate instead of the configured one, plus a `bi-broadcast` icon when the rate is being pulled live from the car.
- **Vehicle-history Regen chart** switched from the rolling 3-month raw value (which fluctuates month-to-month) to the monotonic cumulative, so the line actually grows instead of wiggling.
- **Live vehicle widget** label updated to `Rekuperiert (3 Mon.)` and the double `/10` bug fixed — the widget now shows the correct kWh value.

#### PDF report
- New page **"Gemessene Rekuperation"** with an 8-cell KPI table (today / week / month / 30d / 90d / year / lifetime / km-equivalent) + the auto-detected rate.
- Fahrtenbuch table gets a **Rekup** column (column widths adjusted).
- Vehicle-history "Rekuperation gesamt" chart title updated to "Rekuperation (gemessen, kumuliert)".
- `regen_delta` summary line on the vehicle-history page is now labelled "Rekup. kumuliert".

#### Translations
- 13 new keys × 6 languages for the regen period cards, the settings badge, and the trips column.

## v2.5.3 (2026-04-10)

### Cross-platform polish

- **Windows: startup banner & emoji log lines** — `app.py` reconfigures stdout/stderr to UTF-8 with `errors='replace'` at import time, so `python app.py` in a legacy cmd code page no longer raises `UnicodeEncodeError` on the "⚡ EV Charge Tracker" banner. `start.bat` already set `chcp 65001` for its own window, but manual launches from an unconfigured shell now survive too.
- **Linux: IP discovery in `start.sh`** — now tries `ip -4 -o addr show scope global` first (modern distros), then `hostname -I` (glibc), then `ifconfig` (BSD / macOS / older). Each branch is tolerant of missing binaries. Previously Alpine/BusyBox machines saw an empty "Smartphone-URL" line for no good reason.
- **Updater: restore exec bit after update** — GitHub source zips strip the POSIX exec bit, so after an in-app update `./start.sh` was no longer directly executable on Linux/macOS. [`updater_helper.py`](updater_helper.py) now `chmod +x`'s `start.sh` and `start.command` right after the file swap on non-Windows platforms.
- **`datetime.utcnow()` → timezone-aware** — `services/ssl_service.py` replaces the deprecated call with `datetime.now(timezone.utc)` for cert generation. `get_cert_info()` also handles both `not_valid_before`/`after` (cryptography <42) and `not_valid_before_utc`/`after_utc` (>=42) so it works across versions without a DeprecationWarning.

## v2.5.2 (2026-04-10)

### Unified vehicle sync log line
- **Every vehicle sync now logs the same structured one-liner** regardless of which code path triggered it:
  ```
  Vehicle sync [smart->force, src=bg-loop]: SoC=73%, odo=14283km, GPS=yes, charging=False, api=34/200
  ```
- **mode** reflects the actual API mode that was used:
  - `cached` / `force` for the straight modes
  - `smart->cached` (smart mode ran cached because GPS fresh or car charging)
  - `smart->force` (smart mode escalated to force because GPS stale and not charging)
- **src** reflects the caller, so you can tell which trigger caused the call:
  - `bg-loop` — background sync service (the 10-min smart cadence)
  - `trips-auto` — auto-fresh on `/trips` page load (background thread)
  - `manual` — "Jetzt synchronisieren" button on the trips page
  - `settings` — "Sync (Cached)" / "Sync (Live)" buttons in Settings
  - `dashboard` — the cached/live refresh on the dashboard widget
- **GPS=yes/no** — whether the response carried a location (important for the Fahrtenbuch; Kia cached mode usually returns `no`).
- **api=N/200** — current daily API counter right after the call, so you can see budget burn in real time in the `/logs` feed.
- New helper `log_sync_result()` in [services/vehicle/sync_service.py](services/vehicle/sync_service.py) is the single source of truth — all five call sites now route through it.

## v2.5.1 (2026-04-10)

### Live log viewer
- **New `/logs` page** with its own nav entry. Shows whatever the app's Python loggers emit: vehicle sync activity, parking hook decisions, Nominatim reverse lookups, updater events, ENTSO-E calls, errors — everything that used to only be visible in the terminal.
- **In-memory ring buffer** (last 2000 records) via a custom `RingBufferHandler` attached to the root logger on startup. Thread-safe, zero disk I/O, zero config. New file: [services/log_service.py](services/log_service.py).
- **Live polling** every 2 s via `/api/logs?after=<last_id>` — only new records cross the wire, so the tab stays cheap even when it's sitting open all day. Delta-based, not a full re-fetch.
- **HTTP access logging is opt-in**, toggle in the toolbar. Off by default (keeps the feed clean); flip it on and every `GET /api/...` line from werkzeug shows up too. The preference is persisted in AppConfig (`log_show_requests`) so it survives restarts.
- **Toolbar controls**: auto-refresh on/off, auto-scroll on/off, level filter (DEBUG+ / INFO+ / WARNING+ / ERROR+), free-text filter (matches logger name + message), clear, download as `.log` file.
- **Color-coded by level** — DEBUG grey, WARNING amber, ERROR red, CRITICAL bold red — in both light and dark mode. Monospace font, timestamp with milliseconds.
- `POST /api/logs/clear` and `POST /api/logs/requests` round out the API.

### Translations
- 11 new keys × 6 languages.

## v2.5.0 (2026-04-10)

### Fahrtenbuch: honest numbers, smarter sync, real addresses

#### Dropped misleading trip duration / avg-speed
- **Trip duration, "Fahrzeit" KPI and "Ø km/h" column removed.** With any realistic polling cadence the "arrived_at" of the next parking event is off by up to the sample interval, so any duration/speed number was a fiction. What we report now is what we actually know: **km from the odometer** and **SoC used**.
- PDF "Fahrtenbuch" table drops min / km-h columns and widens From / To columns instead.
- Highlights page drops "Schnellste Fahrt"; "Längste Fahrt" shows km only.
- CSV export drops the dauer/km-h columns.

#### Smart-sync active window
- **New `smart_active_start_hour` / `smart_active_end_hour` / `smart_active_interval_min`** AppConfig keys (defaults 6 / 22 / 10). Fully configurable from Settings → Vehicle API (the new row appears when `Smart` mode is selected).
- Smart mode now runs **every 10 min between 06:00 and 22:00 by default** and **does not sync at all at night** — better granularity for catching real movement without burning the 190/200 daily Kia quota and without waking the car's 12V battery while you sleep.
- With the default 10 min × 16 h = ca. 96 cached calls/day plus the existing "force if GPS stale >6 h and not charging" logic for the Live upgrades. Settings hint shows the math next to the row.
- `_compute_sleep_secs()` in [services/vehicle/sync_service.py](services/vehicle/sync_service.py) handles both smart-window and the legacy hourly cadence for `cached`/`force` modes. Outside the window the loop sleeps until the window opens without firing any API calls.
- Interval options: 5 / 10 / 15 / 20 / 30 / 45 / 60 min. Minimum hardcoded to 5 min.

#### Unknown locations are always resolved to an address / POI
- **No more raw `53.12, 10.45` coordinates** in the Fahrtenbuch. Every parking event gets its `address` field populated via Nominatim reverse-geocoding. POIs (shops, restaurants, parking lots) are captured too because Nominatim's `display_name` leads with the POI name when one exists.
- **Background worker** fires on every `/trips` page load and fills addresses for any parking event that doesn't have one yet (up to 50 per run, 1 req/s per Nominatim ToS, permanent DB cache → after the first full pass it's a no-op).
- **New `POST /api/trips/geocode_missing`** for manual re-trigger.
- Trips table now shows: 🏠 Zuhause / 💼 Arbeit / ⭐ Favorit / full street address — never raw coordinates. While a new event is waiting to be resolved, the row shows "Adresse wird ermittelt…".
- New `geocode_missing_events()` helper in `services/trips_service.py`.

### Translations
- 8 new keys × 6 languages (`trips.home`, `trips.work`, `trips.resolving`, `set.api_smart_window_label`, `set.api_smart_from`, `set.api_smart_to`, `set.api_smart_every`, `set.api_smart_hint`).

## v2.4.3 (2026-04-09)

### Trips page is fast again
- **Background auto-fresh** — the live vehicle sync that runs when you open `/trips` no longer blocks page rendering. It now runs in a daemon thread, so the page renders in ~12 ms instead of 5-10 s waiting for Kia to wake the car. The page will show whatever GPS data we already have; the background sync drops in updated data that will appear on the next reload.
- **Threshold raised** from 30 minutes to 2 hours. With smart-mode enabled and the background-fresh debounce, the API counter doesn't get burned on every visit during the day.
- **5-minute debounce** so two `/trips` visits in quick succession only kick off one background sync (and the second one isn't told a stale "in flight" sync was a fresh sync).
- **GPS freshness indicator** in the toolbar: "GPS vor 12 min", "GPS vor 3 h", "GPS vor 2 Tagen". You can see at a glance how stale the map data is and decide whether to hit "Jetzt synchronisieren" manually.

### Translations
- 3 new keys × 6 languages.

## v2.4.2 (2026-04-09)

### Fahrtenbuch — actually working with sparse Kia polling

This release fixes a stack of subtle bugs that prevented parking events from being created on a real-world database with the Kia/Hyundai cached-mode sync.

#### Root cause fix
- **Parking hook now runs on EVERY save**, not only when `differs_from(last)` returns True. Previously, a force-refresh that delivered the *same* GPS coordinates as the existing latest row (because the car hadn't moved) would skip the hook entirely — so no parking event was ever created. Fixed in `_save_vehicle_sync` ([app.py](app.py)).

#### Backfill
- New `backfill_parking_events()` in `services/trips_service.py` replays every existing `VehicleSync` row chronologically through the parking hook. This catches up databases populated before v2.3.0 (no hook) or after weeks of cached polling where the hook only fired occasionally.
- **Auto-runs on startup** if the parking_events table is empty AND there is at least one VehicleSync row with GPS data.
- **`POST /api/trips/backfill`** for manual triggering. New "Aus Historie nachbauen" button on the `/trips` page.

#### Smart sync mode
- New `'smart'` option in Settings → Vehicle API alongside `cached` and `force`.
- Smart mode runs cached by default but **upgrades to a force-refresh when the latest GPS-bearing sync is older than 6 h** (configurable via `smart_force_max_hours`) and the car is not currently charging. This catches movement without burning the 12V battery on every cycle.
- Tracks `last_force_refresh_at` so the smart-mode decision logic has something to compare against.

#### Tighter trip durations
- New `last_seen_at` column on `parking_events` is updated on every sync that confirms the same position. The trip-duration calculation now uses `last_seen_at` of the previous event as the lower bound instead of `arrived_at`, which would have overstated the trip duration by the entire parking spell.
- Auto-migrated on startup.

#### Trips page auto-fresh
- Opening `/trips` automatically triggers a force vehicle sync if all of these are true: a brand is configured, auto-sync is enabled, the brand supports GPS (per the feature matrix), the latest GPS sync is >30 min old, and the daily API counter is below 180/200. Skipped silently otherwise. Means the map is current the moment you open the page.

#### Manual sync button
- **"Jetzt synchronisieren"** button on `/trips` triggers an immediate force refresh and reports back whether GPS came through or not.

#### Settings UX
- **Warning banner** when sync mode is `cached` and brand is Kia/Hyundai: "GPS für Fahrtenbuch erfordert Smart oder Live, oder manueller Sync (Live)".
- Last-sync line in Vehicle API card now shows a 📍 icon when the most recent row has GPS data.

### Translations
- 13 new translation keys × 6 languages.

## v2.4.1 (2026-04-09)

### Restart button
- **New "App neustarten" button** in Settings → App-Info, plus an inline "Jetzt neustarten" button that appears after saving HTTPS settings or generating a new certificate. No more manual `start.command` after switching HTTPS mode.
- **Restart-only mode** for `updater_helper.py`: `--staging-dir` is now optional. Without it the helper skips the file swap and the pip install, just waits for the parent PID and spawns a fresh `venv/bin/python app.py` with the same nohup-wrap, env-strip, and health check as the update flow.
- **`POST /api/restart`** triggers the same delayed-shutdown pattern as `/api/update/install`, and the Settings page polls until the app is back online and reloads the browser.

## v2.4.0 (2026-04-09)

### HTTPS / TLS support
- **Self-signed certificate** auto-generation via the `cryptography` library (preferred) or `openssl` CLI (fallback). Cert is stored in `data/ssl/server.{crt,key}` and reused across restarts. SAN entries cover `localhost`, `127.0.0.1`, and the LAN IP, so the same cert works on desktop AND smartphone.
- **Three modes** in Settings → "HTTPS / Sicherheit": `off` (HTTP), `auto` (self-signed), `custom` (paths to your own Let's Encrypt cert).
- **Cert metadata viewer** — subject, valid-until date, SHA256 fingerprint shown in the UI. Parsing falls back from `cryptography` to `openssl x509 -text` so it works without the library.
- **"Cert herunterladen"** button serves the public cert as a `.crt` download — install it on your iPhone/Android via Profile to get rid of browser warnings permanently.
- **HTTP/insecure warning** banner in Settings if the user accesses the app over HTTP from a non-localhost address (Geolocation API and PWA features won't work over plain HTTP).

### Brand feature matrix
- New `services/vehicle/feature_matrix.py` with hand-curated capabilities for all 14 brands across 10 features (SoC, GPS, 12V battery, SoH, recuperation, 30-day consumption, doors/locks, climate, tire pressure, live status).
- **`/api/vehicle/features/<brand>`** returns the matrix for the selected brand.
- **Settings → Vehicle API** shows a 10-item grid with green/yellow/red indicators when a brand is picked. No more "wait, why isn't my Polestar showing recuperation data" surprises.

### Tesla connector expansion
- **Tire pressure warnings** computed from `tpms_pressure_*` vs `tpms_rcp_*_value` recommended pressures.
- **Climate detail**: defrost, rear window heater, steering wheel heater.
- **Software update detection** via `vehicle_state.software_update.status`.
- **Charging session detail**: `minutes_to_full_charge`, `charge_energy_added`, charger voltage and current — exposed in `raw_data`.
- **Sentry mode state** for the security-conscious.

### Manual location for charges
- **Charge form** ([templates/input.html](templates/input.html)) gets a new "Standort der Ladestation" section with:
  - Free-text location name (e.g. "Aldi Berlin Mitte", "Ionity A2")
  - Lat/lon fields
  - **"Mein Standort"** button uses the browser's Geolocation API (works on smartphones over HTTPS or on localhost)
  - **"Zuhause"** / **"Arbeit"** quick-fill from your saved Settings locations
  - Reverse-geocoding via Nominatim auto-fills the name field if you didn't type one
  - **Clear** button
- Captured charges feed the existing `Charge.location_lat/lon/name` columns, which the **charging stations memory** in the highlights service already groups by location for "cheapest stations on my regular routes".

### Database
- New `AppConfig` keys: `ssl_mode`, `ssl_custom_cert`, `ssl_custom_key`. Auto-created on first save.

### Dependencies
- Added `cryptography>=42.0.0` to `requirements.txt` (previously optional via openssl CLI).

### i18n
- 47 new translation keys × 6 languages.

## v2.3.4 (2026-04-09)

### Favorites picker — visible feedback + diagnostics
- **Crosshair cursor + blue outline** on the map when in pick mode (home/work/favorite). Previously the user had no visual confirmation that the map was waiting for a click.
- **Console logging** of every step in the favorites flow (pickMode transitions, map clicks, POST results) so issues can be debugged from browser DevTools.
- **`e.preventDefault()` + `e.stopPropagation()`** on `btnAddFav` click — defensive against any parent form swallowing the event.
- **Auto-focus the name field** when "Bitte Name eingeben" warning fires.
- **Status message includes coordinates and name** after a favorite is saved, so the user sees concrete confirmation.
- **Refactored map click handler** into a named `handleMapClick` function with explicit early returns per branch — easier to reason about and reduces the chance of state bleed between branches.

## v2.3.3 (2026-04-09)

### The actual updater fix (root cause)
- **`debug=True` → `debug=False` in `app.py`** — the Werkzeug auto-reloader passes a listening socket to its child via the `WERKZEUG_SERVER_FD` environment variable. That env var was propagating from the dying Flask through `os._exit` → `updater.py` → `updater_helper.py` → freshly-spawned Flask, where `socket.fromfd(WERKZEUG_SERVER_FD)` then crashed with `OSError: [Errno 9] Bad file descriptor`. For a self-hosted app, debug mode is the wrong default anyway.
- **Helper strips `WERKZEUG_*` env vars** before spawning, as belt-and-suspenders for older `app.py` files that still have `debug=True`.

### Fixes
- **Favorites can now be set on the map** — the `btnAddFav` button was missing `type="button"` and used a brittle one-shot click handler. Both fixed: button now has explicit type, and the favorites flow uses the same `pickMode` pattern as home/work picking. Pressing Enter in the favorite name field now also triggers add-mode. Errors are logged to status with the actual response code.

## v2.3.2 (2026-04-09)

### Updater fixes (the actual restart problem)
- **`nohup`-wrap the new Python process**, not just the bash launcher. macOS Terminal.app sends SIGHUP to *every* process in its session when the window closes, even processes that called `setsid`. The v2.3.1 fix bypassed `start.sh` correctly but the bare `python app.py` was still vulnerable. v2.3.2 wraps it in `/usr/bin/nohup` which sets `SIG_IGN` for SIGHUP — survives terminal close.
- **Health check after spawn** — the helper now waits 4 seconds, verifies the spawned process is still alive (`p.poll() is None`), and probes port 7654 with a TCP socket. Failure is logged with the exit code instead of being silent.
- **More verbose logging** — every step in `_restart_app` is timestamped so future failures are debuggable from `updates/restart.log` alone.

## v2.3.1 (2026-04-09)

### Updater fixes
- **Helper restarts the app reliably** — `updater_helper.py` now spawns `venv/bin/python app.py` directly instead of going through `start.command` → `start.sh`. This bypasses the redundant pip install loop and the `set -e` shell pitfalls, dropping restart latency from ~15 s to ~3 s.
- **Port-release race fix** — wait 2 s after the parent Flask process dies before binding the port again, so we can't hit `EADDRINUSE`.
- **Restart log** — every restart attempt is logged to `updates/restart.log` with timestamps and stdout/stderr of the spawned process. Previously failures were silent because output went to `/dev/null`.
- **`_spawn_helper` prefers the staging helper** — `updater.py` now launches `staging/.../updater_helper.py` (the new release) instead of the in-place helper, so future updater bugfixes take effect on the very first update that ships them.

## v2.3.0 (2026-04-09)

### New Features

#### Driving log / Fahrtenbuch
- **Auto-detected parking events** — every vehicle sync hooks into a new `ParkingEvent` log. The car's location is checked against the last open event; >100 m means "moved", a new event is opened, the previous one is closed with arrival/departure odometer + SoC.
- **Home / Work / Favorites picker** — click on a Leaflet/OpenStreetMap card in Settings to set your home and work coordinates (drag-to-fine-tune supported). Optional named favorites for parents, vacation home, etc. All parking events are auto-classified as `home`/`work`/`favorite`/`other` with a 200 m radius. Reclassification runs whenever you change a location.
- **Trips page** at `/trips` — KPI cards (count, total km, drive time, commute km), Leaflet map with marker clustering colored by location label, full trips table with from/to/km/duration/avg-speed/SoC.
- **CSV + GPX export** — `/api/trips/export.csv` for the tax advisor, `/api/trips/export.gpx` for Google Earth / Komoot / OsmAnd.
- **PDF report** gets a new "Fahrtenbuch" section with the last 80 trips and a header showing home↔work km (relevant for German Pendlerpauschale).

#### Maintenance log / Wartungs-Logbuch
- **New `/maintenance` page** — track inspections, tires, brakes, wipers, 12V battery, cabin filter, MOT/TÜV with date, odometer, cost and free-text notes.
- **Smart reminders** — every entry can have a `next_due_km` and/or `next_due_date`. The page surfaces a "due soon / overdue" banner; the form auto-fills sensible defaults (e.g. inspection = 12 months / 30 000 km).
- **PDF report** gets a "Wartungs-Logbuch" section with the full history and total cost.

#### Charging stations memory
- **Lat/lon/name on `Charge`** — the input form now optionally captures the location of a charge.
- **`/api/highlights` returns charging stations** grouped by rounded coordinates with cheapest €/kWh, total kWh, count and last-used date — for finding the cheapest stations within your usual routes.

#### Range calculator
- **Realistic range estimate** at `/api/range` — uses live SoC, the configured battery capacity, the 30-day average consumption from the API (or fallback to lifetime average), and the current outdoor temperature from Open-Meteo at your home location. Applies a temperature penalty (1.30× below 0°C, 1.18× < 10°C, 1.06× < 20°C, 1.10× > 30°C). Shown as a dashboard card.

#### Weather correlation
- **Open-Meteo integration** — `services/weather_service.py` fetches daily mean temperatures for your home location with DB caching (no API key, no rate-limit issues for normal usage).
- **Dashboard chart** — bar (kWh/month) + line (avg outdoor °C) showing exactly why winter is more expensive.

#### Highlights / fun facts
- **Dashboard "Highlights" card** — cheapest charge, most expensive charge, biggest single charge, longest trip (km), fastest trip (avg km/h), longest park (days). Also rendered on a dedicated page in the PDF report.

#### Reverse geocoding
- **Nominatim integration** — `services/geocode_service.py` resolves coordinates to street addresses, with a permanent DB cache and a 1-second rate-limiter (Nominatim ToS). Used by parking events on demand.

#### THG quota reminder
- **Banner** between January 1 and March 31 if no THG quota is logged for the previous year — direct link to Settings.

### Database
- New tables: `parking_events`, `maintenance_log`, `geocode_cache`, `weather_cache` — auto-created on startup.
- New columns on `charges`: `location_lat`, `location_lon`, `location_name` — auto-migrated.

### i18n
- **83 new translation keys** in all 6 languages (DE, EN, FR, ES, IT, NL) — every new page, banner, button and tooltip is fully localized.

## v2.2.0 (2026-04-09)

### New Features
- **Real in-app updater** — the "Update verfügbar" button in Settings now actually rolls out the update on the user's machine instead of opening the GitHub release page in a browser. Click → confirm → the app downloads the new release zip, stages it, hands off to a detached `updater_helper.py` process, gracefully shuts itself down, the helper swaps files (preserving `venv/`, `data/`, `.git/`), runs `pip install -r requirements.txt` and re-launches the app via the platform start script. The settings page polls until the app comes back online and reloads the browser automatically.
- **`POST /api/update/install`** and **`GET /api/update/check`** routes drive the new flow.

### How it works
The trick is the detour through a standalone `updater_helper.py` script: the running Flask process cannot safely overwrite its own `app.py` and templates while still serving requests, so the helper runs in a separate detached subprocess that waits on the parent PID, then performs the file swap. Pattern adapted from `shelly-energy-analyzer`.

## v2.1.1 (2026-04-09)

### Fixes
- **Updater** — version comparison now uses semver tuples instead of plain string inequality. A user on a later dev version no longer sees an "update available" pointing at an older release, and `2.10.0` correctly sorts above `2.9.0`.

## v2.1.0 (2026-04-09)

### New Features
- **Vehicle history tracking** — every vehicle sync now persists battery (SoC), range, odometer, 12V battery, calculated SoH, total recuperated kWh, 30-day kWh/100km consumption, and GPS location. New rows are only stored when at least one tracked value has changed (compact, audit-friendly history).
- **Dashboard vehicle history widget** — 7 compact time-series mini-charts (SoC, range, odometer, 12V, SoH, recuperation, consumption) showing the evolution of all tracked metrics.
- **Vehicle location map** — small Leaflet/OpenStreetMap card on the dashboard showing where the car was last seen, with marker and zoom.
- **PDF report extended** — new "Fahrzeug-Historie" section with all 7 time-series charts, summary KPIs (km driven, SoH delta, recuperation delta) and the last known GPS position.

### Database
- New columns on `vehicle_syncs`: `battery_12v_percent`, `battery_soh_percent`, `total_regenerated_kwh`, `consumption_30d_kwh_per_100km`, `location_lat`, `location_lon` (auto-migrated on startup).

## v2.0.0 (2026-04-09)

### New Features
- **Multi-language support** — Deutsch, English, Français, Español, Italiano, Nederlands. Switchable in Settings → Sprache. 286 strings per locale, JSON-based fallback to German.
- **Marketing-ready README** — badges, screenshots section, problem/solution table, "why this app" pitch, GitHub topics for discoverability (electric-vehicle, ev-charging, kia, hyundai, tesla, …).

### Improvements
- Lightweight i18n service (`services/i18n.py`) with `t()` global, per-request language selection, format-string support.

## v1.9.0 (2026-04-09)

### New Features
- **6 additional vehicle brands** via API connectors:
  - **Tesla** (`teslapy`, OAuth refresh-token, miles → km auto-convert)
  - **Renault** & **Dacia** (`renault-api`, async)
  - **Polestar** (`pypolestar`, async)
  - **MG / SAIC** (`saic-ismart-client-ng`)
  - **Smart #1/#3** (`pySmartHashtag`)
  - **Porsche** (`pyporscheconnectapi`)
- Modular connector architecture preserved — Kia/Hyundai integration untouched, no token loss.
- All packages installable from Settings → Vehicle API UI (no terminal needed).

### Improvements
- **Dark / Light mode** toggle in navbar, inline boot script avoids flash, synced across browser tabs via `localStorage` storage event.
- **Local timestamps** — `datetime.utcnow` replaced with `datetime.now` everywhere; "Letzte Sync" no longer shows UTC.
- **Repo cleanup** — `.DS_Store`, `.claude/`, `*.command` added to `.gitignore` and untracked.
- **Dynamic copyright year** — footer no longer hardcoded to 2025.

## v1.8.4 (2026-04-08)

### Fixes
- Reverted experimental client-side OAuth wizard — Selenium-based token fetch (v1.5.4) is back as the only reliable approach for headed environments.

## v1.8.3 (2026-04-08)

### Fixes
- **SoH fallback** — when the EU API does not populate `BatteryManagement.SoH.Ratio` (most non-Kona vehicles), SoH is computed from `total_consumed_kwh / battery_kwh` and shown in the dashboard widget.

## v1.8.2 (2026-04-08)

### Fixes
- **Kia API unit conversion** — `totalPwrCsp` and `regenPwr` empirically use 0.1 kWh units (not Wh as the upstream library docs claim). Recuperation now matches dashboard expectations (~7.072 kWh, not 21.011).

## v1.8.1 (2026-04-08)

### Fixes
- **PDF "Gesamtübersicht" layout** — replaced overlapping manual y-positioning with a clean bordered KPI table.
- **Dashboard auto-refresh** — vehicle widget now actually fetches fresh cached data on page load (was only restoring from localStorage cache).
- **SoH on dashboard** — added new "SoH %" tile to the live vehicle widget.

## v1.8.0 (2026-04-08)

### New Features
- **PDF Report** — new "Report" button in navigation, generates multi-page PDF with:
  - KPI overview (costs, kWh, CO2, savings, consumption, recuperation)
  - 10 colorful charts (monthly costs/kWh/CO2 with averages, cumulative cost/kWh, CO2 break-even, price trend, charge count, AC/DC/PV pie charts, yearly comparison)
  - Detailed tables (AC/DC/PV statistics, yearly overview, monthly breakdown)
  - Auto-generated filename with car model and date

## v1.7.0 (2026-04-08)

### New Features
- **Start/Stop charge tracking** — buttons on input page trigger force-refresh from vehicle, auto-fill date/time/SoC/odometer
- **Live charge timer** — shows elapsed time, estimates kWh from time × AC power
- **Auto-stop** — polls every 10 min during charging, auto-stops when SoC reaches charge limit or car stops charging
- **CO2 from time range** — calculates weighted average CO2 from ENTSO-E for the charge period (start to end hour)
- **API rate limiter** — tracks daily Kia API calls (190/200 limit), counter shown on dashboard, auto-reset at midnight
- **Session persistence** — charge session survives tab switches and page reloads via localStorage

### Improvements
- Charge poll interval: 10 min (was 5 min) to respect Kia EU 200 calls/day limit
- Auto-sync minimum interval: 1 hour (was 30 min)
- Sync service respects daily API limit
- Settings: vehicleCredentials and syncSection render server-side when brand configured

## v1.6.0 (2026-04-08)
- **Cached vs Live refresh** — two buttons on dashboard: "Cached" reads server cache, "Live" wakes the car for fresh data
- **Force refresh fallback** — if Live returns null values (odometer, range, 12V), last known values are preserved
- **Settings sync modes** — "Sync (Cached)" and "Sync (Live)" buttons, auto-sync mode selector (Cached/Live)
- **Input force refresh** — vehicle fetch button in "Neue Ladung" always wakes the car
- **localStorage cache** — vehicle data persists across tab switches, no re-fetch needed
- **Hyundai token support** — token fetch now works for both Kia and Hyundai with brand-specific OAuth URLs

## v1.5.5 (2026-04-07)
- **Full vehicle live dashboard** — all available data from Kia/Hyundai displayed in 3-row widget
- **New data points** — doors/trunk/hood status, tire pressure warnings, 30-day consumption, Schuko charge time, registration date, Google Maps location link
- **Extended API** — `/api/vehicle/status` returns all vehicle data

## v1.5.4 (2026-04-07)
- **One-click Kia/Hyundai token fetch** — opens Chrome with mobile user-agent, user logs in + solves reCAPTCHA, token is auto-captured and saved
- **Working OAuth flow** — uses `peukiaidm-online-sales` client for initial login, then exchanges for CCSP refresh token
- **Clean settings UI** — brand selection, install buttons, delete/reset, manual token entry as fallback

## v1.5.1 (2026-04-07)
- **One-click package install** — install vehicle API packages directly from settings UI (no terminal needed)

## v1.5.0 (2026-04-07)

### New Features
- **Vehicle API integration** — connect your car to auto-fetch SoC, odometer, charging status
- **Supported brands** — Kia (UVO), Hyundai (Bluelink), VW (WeConnect), Skoda (MySkoda), Seat (MyCar), Cupra (MyCupra), Audi (myAudi)
- **Auto-fill on input** — "Von Fahrzeug abrufen" button fills SoC and odometer from vehicle API
- **Background sync service** — periodic vehicle status polling (configurable 1h–12h interval)
- **Vehicle sync history** — all synced data points stored in database
- **Settings UI** — Fahrzeug-API card with brand selection, credentials, connection test, manual sync, auto-sync toggle
- **Modular connector architecture** — plugin-based design, new brands can be added easily
- **Optional dependencies** — vehicle API packages only needed when used (graceful degradation)

## v1.4.4 (2026-04-04)
- **Average lines in all monthly charts** — dashed Ø lines for costs, kWh, and CO2

## v1.4.3 (2026-04-04)
- **Average line in monthly cost chart** — dashed line showing Ø cost per month

## v1.4.0 (2026-04-04)
- **Auto CO2 backfill** — missing CO2 values are automatically fetched from ENTSO-E after CSV import
- **Manual backfill button** — "CO₂ nachladen" in ENTSO-E settings with live progress
- **Background processing** — rate-limit aware with automatic retries

## v1.3.1 (2026-04-04)
- Fix uniform chart heights across all dashboard rows

## v1.3.0 (2026-04-04)

### New Features
- **PV charging** — third charge type "PV (Solar)" alongside AC/DC
- **PV system configuration** — kWp, annual yield, lifetime, production CO2 in settings
- **Auto-calculated PV CO2** — from system specs (e.g. 10kWp → ~42 g/kWh)
- **PV auto-fill** — selecting PV pre-fills CO2 and price fields
- **AC/DC/PV comparison** — dashboard table includes PV column when data exists
- **PV filter** — history filterable by PV charge type
- **Mobile-friendly charts** — responsive sizing, fewer ticks, smaller fonts, shorter legends on small screens

## v1.2.1 (2026-04-04)
- **CSV import via web UI** — upload Google Sheet CSV directly in settings (no CLI needed)
- Refactored import logic into reusable `import_csv_data()` function

## v1.2.0 (2026-04-04)

### New Features
- **Vehicle configuration** — car model, battery capacity, max AC power editable in settings
- **THG quota management** — add/delete yearly CO2 bonus payouts, deducted from total costs
- **Odometer tracking** — km field per charge, inline editing in history view
- **Charging hour** — select hour (00-23) for hour-specific ENTSO-E CO2 data
- **Recuperation tracking** — configurable kWh/km rate, total energy recovered, extra km, recuperation cycles
- **CO2 break-even chart** — cumulative CO2 savings vs. battery production with break-even line
- **Well-to-wheel CO2** — configurable fossil car WTW emissions (default 164 g/km DE average)
- **Auto-calculated charging losses** — from SoC difference and battery capacity when not manually entered
- **New dashboard KPIs** — net costs (after THG), consumption kWh/100km, cost per 100km, charge cycles, recuperation stats
- **CO2 charts** — monthly CO2 emissions bar chart, cumulative CO2 savings line chart
- **Improved dashboard layout** — AC/DC and yearly tables separated, full-width cost chart

### Fixes
- Fix ENTSO-E connection test button (hidden input override)
- Fix GitHub username in settings template and update checker
- Auto-migrate database schema (adds columns without data loss)

## v1.1.0 (2026-04-04)
- Vehicle configuration in settings
- THG quota tracking

## v1.0.2 (2026-04-04)
- Fix GitHub username in update checker and settings link

## v1.0.1 (2026-04-04)
- Fix ENTSO-E connection test button

## v1.0.0 (2026-04-04)
- Initial release
