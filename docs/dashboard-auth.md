# Dashboard-Zugang: Anmeldung, CSRF und Operatoridentität

Betriebsdokumentation zu WP-8A. Ergänzt `docs/avatar-dashboard.md` (Bedienung)
und `deploy/README.md` (Installation).

Bis WP-8A war das Dashboard durch nichts als die Adresszeile geschützt. Wer
`127.0.0.1:8091` erreichte — ein zweiter Dienst auf dem Pi, eine zu weit
gefasste Proxy-Regel, ein beim Debuggen weitergeleiteter Port — konnte Reviews
freigeben, den Circuit Breaker zurücksetzen, das Avatarbudget ausgeben und jeden
Episodenpfad auf der Platte lesen. Die dokumentierte Caddy-Basicauth half dabei
nicht: sie steht in einer Datei, die dieses Repository nicht kontrolliert, und
für den Port, den Gunicorn bindet, existiert sie überhaupt nicht.

## 1. Drei getrennte Mechanismen

| Mechanismus | Frage | Komponente |
| --- | --- | --- |
| Authentifizierung | Wer ist das? | Flask-Login |
| CSRF | Wollte dieser Betreiber diese Anfrage? | Flask-WTF (`CSRFProtect`) |
| Operatoridentität | Wessen Name steht im Audit? | Session, sonst nichts |

Sie werden bewusst nicht zusammengefasst. Angemeldet zu sein ist keine
Einwilligung: eine fremde Seite kann einen authentifizierten Browser zu einem
POST bringen. Umgekehrt ist ein gültiges Token kein Ausweis. Die
Origin-Prüfung aus WP-7 bleibt als zweite Schicht bestehen — ein Token, das
abfließt, ist ein Token, das funktioniert.

Passwörter werden mit `werkzeug.security` gehasht (scrypt, Salt pro Passwort).
Es wird hier keine Kryptografie geschrieben.

## 2. Konfiguration

Alles über Umgebungsvariablen, nichts in der Datenbank, nichts im Repository.

| Variable | Bedeutung |
| --- | --- |
| `WEB_AUTH_ENABLED` | Standard `true`. `false` nur für Loopback-Entwicklung. |
| `WEB_BIND_HOST` | Was der WSGI-Server tatsächlich bindet. |
| `WEB_SESSION_SECRET` | Mindestens 32 Zeichen, einmalig erzeugt. |
| `WEB_OPERATOR_USERNAME` | Der Name, der im Audit landet. |
| `WEB_OPERATOR_PASSWORD_HASH` | Nur der Hash, niemals das Passwort. |
| `WEB_OPERATOR_DISPLAY_NAME` | Rein kosmetisch, nie die Auditidentität. |
| `WEB_SESSION_LIFETIME_MINUTES` | Standard 720. |
| `WEB_COOKIE_SECURE` | Standard `true`; nur für lokales http auf `false`. |
| `WEB_LOGIN_MAX_ATTEMPTS` / `_COOLDOWN_SECONDS` / `_COOLDOWN_MAX_SECONDS` | Sperre nach Fehlversuchen. |

### Fail-closed

Der Dienst startet **nicht**, wenn:

* Authentifizierung aktiv ist, aber Benutzername, Hash oder Session-Secret
  fehlen — eine halb konfigurierte Anmeldung ist der klassische Weg, sie „für
  jetzt“ ganz abzuschalten;
* Authentifizierung aus ist und `WEB_BIND_HOST` **nicht** Loopback ist — das
  wäre das Dashboard ohne Passwort im Netz.

Beides ist ein Startfehler, kein Fehler zur Laufzeit: ein ungeschütztes
Dashboard darf nicht erst eine Anfrage beantworten, bevor es jemandem auffällt.
Die Fehlermeldung nennt nur Variablennamen — sie wird bei jedem Neustartversuch
ins Journal geschrieben.

## 3. Einrichtung

```bash
# 1. Session-Secret einmalig erzeugen
python -c "import secrets; print(secrets.token_urlsafe(48))"

# 2. Passwort-Hash interaktiv erzeugen (verdeckte Eingabe, mit Bestätigung)
btcedu generate-password-hash

# 3. Beide Werte in .env eintragen, dazu Benutzername und WEB_AUTH_ENABLED=true
# 4. Dienst neu starten
sudo systemctl restart btcedu-web
```

`btcedu generate-password-hash` fragt über `getpass`, schreibt **keine**
Konfigurationsdatei und gibt nur die fertige Zeile aus. Das Passwort erreicht
weder die Shell-History noch die Prozessliste noch den Terminalpuffer. Ein
Helfer, der selbst in `.env` schreibt, ist genau der Weg, auf dem ein Passwort
im Git landet.

### Zugang zurücksetzen

Wenn niemand mehr hineinkommt:

```bash
btcedu generate-password-hash          # neuen Hash erzeugen
# WEB_OPERATOR_PASSWORD_HASH in .env ersetzen
sudo systemctl restart btcedu-web      # Neustart verwirft auch die Sperre
```

Der Neustart löscht zugleich die Fehlversuchszähler, weil sie im Speicher
liegen. Das ist die dokumentierte Grenze dieser Lösung, keine Nebenwirkung.

## 4. Login und Session

* Login nur per `POST /login`, Logout nur per `POST /logout` — ein per GET
  erreichbarer Logout ist ein Link, den jeder platzieren kann.
* Eine Ablehnung nennt nie, welche Hälfte falsch war. „Unbekannter Benutzer“ ist
  kostenlose Aufklärung für einen Angreifer.
* Session Fixation: die Anmeldung **leert** die Session, bevor sie sie neu
  aufbaut. Sonst überlebte ein vorher untergeschobener Wert — einschließlich
  eines vom Angreifer gewählten CSRF-Tokens — in die authentifizierte hinein.
* Cookie: `HttpOnly`, `SameSite=Strict`, in Produktion `Secure`.
* `?next=` wird nur akzeptiert, wenn es ein relativer Pfad innerhalb dieser
  Anwendung ist. Alles mit Schema, Host, Backslash oder führendem `//` wird
  kommentarlos verworfen.
* Weder Benutzername noch Passwort erreichen ein Log oder einen Queryparameter.

### Sperre nach Fehlversuchen

Nach `WEB_LOGIN_MAX_ATTEMPTS` Fehlversuchen greift ein Cooldown, der sich pro
weiterem Versuch verdoppelt und bei `WEB_LOGIN_COOLDOWN_MAX_SECONDS` gedeckelt
ist. Kein blockierender Sleep, sondern eine Sperrzeit.

Der Zähler hängt am **Benutzernamen**, nicht an der Client-Adresse. Diese
Adresse ist ein Header, den der Reverse Proxy schreibt und den ein Angreifer
vorschlagen kann; eine Sicherheitsentscheidung auf `X-Forwarded-For` zu stützen
heißt, sie durch Variieren eines Headers umgehbar zu machen — schlimmer als
keine Sperre, weil es wie eine aussieht.

Der Preis wird offen benannt: bei einem einzigen Betreiber kann jemand, der den
Benutzernamen kennt, ihn für die Dauer des Cooldowns aussperren. Deshalb
beginnt der Cooldown bei einer Minute und nicht bei einer Stunde, und deshalb
ist der Ausweg ein CLI-Kommando, das diesen Pfad nie berührt.

## 5. Was geschützt ist

Geschützt ist **alles** außer einer kurzen, ausdrücklichen Liste. Ein neuer
Endpunkt ist damit in dem Moment abgesichert, in dem er geschrieben wird.

Öffentlich bleiben:

| Endpunkt | Warum |
| --- | --- |
| `GET/POST /login` | Sonst könnte sich niemand anmelden. |
| `POST /logout` | Muss auch auf einer abgelaufenen Session funktionieren. |
| `/static/*` | Stylesheet und Skript, keinerlei dynamische Daten. |
| `GET /api/health` | Damit systemd und der Proxy den Prozess prüfen können. |

Der Healthcheck ist absichtlich fast leer: `{"status": "ok", "time": ...}`.
Version und Commit verrieten einem anonymen Aufrufer, welchen Code diese
Maschine fährt und welche Sicherheitshinweise damit auf sie zutreffen; beide
gibt es jetzt nur noch mit Session.

Verhalten ohne Anmeldung:

* Browserseite → Weiterleitung auf `/login`
* `/api/...` → **401** als JSON (`{"error": ..., "code": "unauthenticated"}`),
  niemals ein HTML-Redirect, den ein Aufrufer als Daten parst
* Eine existierende und eine erfundene Episode antworten identisch — vor der
  Anmeldung wird nicht verraten, was es hier gibt.

Die Zusicherung, dass nichts vergessen wurde, steht nicht in dieser Datei,
sondern in `tests/test_web_auth.py::test_the_whole_route_table_is_covered`: der
Test läuft über jede registrierte Route und besteht darauf, dass sie entweder
geschützt oder eine bewusste Zeile in `PUBLIC_ENDPOINTS` ist.

## 6. CSRF

Jede zustandsändernde Methode (POST, PUT, PATCH, DELETE) braucht ein an die
Session gebundenes Token. Das Dashboard rendert es einmal in ein
`<meta name="csrf-token">` und sendet es über den Header `X-CSRFToken`; das
Loginformular sendet es als verstecktes Feld — Login-CSRF meldet ein Opfer sonst
im Konto des Angreifers an.

* Fehlendes oder falsches Token → **403** mit `{"code": "csrf_failed"}`, ohne
  Tokenwert, ohne Session-ID, ohne Hinweis darauf, wie ein gültiges aussieht.
* Token und Session gehören zusammen: ein Token aus einer anderen Sitzung wird
  abgelehnt.
* Tokens erscheinen in keinem Log und in keiner URL.
* Die Doppelklick- und Idempotenzmechanik aus WP-7 bleibt zusätzlich bestehen.
* CSRF ist an die Authentifizierung gekoppelt (`WTF_CSRF_ENABLED`): ohne Session
  gibt es nichts mitzureiten. Das ist **eine** Entscheidung („dies ist
  ungeschützte Loopback-Entwicklung“) statt zweier, die auseinanderdriften und
  Produktion halb abgedeckt zurücklassen könnten.
* CLI und Hintergrundjobs sprechen kein HTTP mit dieser App und sind von
  alldem unberührt.

## 7. Operatoridentität

`operator_ref` kommt ausschließlich aus der authentifizierten Session. Ein im
Body mitgeschicktes `operator_ref` wird ignoriert; ein `X-Forwarded-User`-Header
gilt nicht als Ausweis, weil ein Header nur so vertrauenswürdig ist wie jeder
Hop, der ihn hätte schreiben können.

Jede Referenz trägt ihre Herkunft:

| Präfix | Bedeutung |
| --- | --- |
| `web:` | angemeldeter Dashboardbetreiber |
| `cli:` | Mensch an der Kommandozeile dieser Maschine |
| `system:` | automatische Entscheidung ohne Menschen |
| kein Präfix | Altzeile vor WP-8A — Herkunft unbekannt |

Alte Zeilen werden **nicht** nachträglich eingeordnet: so zu tun, als kenne man
die Herkunft einer alten Entscheidung, wäre schlechter als sie als unbekannt
auszuweisen.

Der Doppelpunkt ist kein Zufall. Die Grammatik der Rechtenachweise in
`core/anchor_rights.py` lehnt ihn ab — eine Rechtefreigabe ist eine rechtliche
Aussage über eine benannte Person und darf niemals durch eine Dashboardsitzung
erfüllbar sein.

Der Anzeigename (`WEB_OPERATOR_DISPLAY_NAME`) ist davon getrennt und erscheint
nur in der Oberfläche.

## 8. Deployment

* Cookies nur über HTTPS: `WEB_COOKIE_SECURE=true` und Caddy davor.
* Der Gunicorn-Port bleibt auf `127.0.0.1`; `WEB_BIND_HOST` in der Unit muss dem
  `-b`-Flag entsprechen, weil der Prozess sonst nicht wissen kann, worüber er
  erreichbar ist.
* Die Caddy-Basicauth darf bestehen bleiben — sie ist jetzt eine zweite Tür,
  nicht die einzige. Keine Secrets in der Caddyfile.
* Proxy-Header werden für Sicherheitsentscheidungen nicht ausgewertet.
* `.env` bleibt außerhalb von Git; die systemd-`EnvironmentFile` zeigt darauf.

## 9. Verbleibende Risiken

* **Ein Konto.** Kein personenbezogenes Audit über mehrere Menschen hinweg. Die
  Schnittstelle (`Operator.id`, `operator_ref`) ist die, die eine Benutzertabelle
  hätte, damit die spätere Erweiterung lokal bleibt.
* **Sperrzähler im Speicher.** Ein Neustart löscht sie.
* **Kein zweiter Faktor.** Für einen Einzelbetreiber hinter HTTPS mit
  Caddy-Basicauth davor bewusst nicht eingeführt.
* **Studio-Plates und Themenmedien** sind weiterhin nicht bytehashgebunden
  (WP-8B).
