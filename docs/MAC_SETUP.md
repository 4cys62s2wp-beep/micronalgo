# micronalgo auf dem Mac

Komplette Einrichtung als dauerhaft laufende Software: Installation, Autostart
per launchd, Echtzeit-Anbindung, Ueberwachung, und die eine Mac-Eigenheit, die
man kennen muss (Schlafmodus).

## 1. Installation

```bash
git clone <repo-url> micronalgo && cd micronalgo
sh deploy/install_mac.sh
```

Das Skript prueft Python (>= 3.10), legt `.venv` an, installiert alles inkl.
Echtzeit-Abhaengigkeit (`websocket-client`), kopiert `.env.example` nach `.env`
und laesst einen Offline-Selbsttest laufen. Kein `sudo`, nichts landet
ausserhalb des Repo-Ordners und `~/Library/LaunchAgents`.

Fehlt `python3`: entweder `xcode-select --install` (Apple Command Line Tools)
oder `brew install python@3.12`.

## 2. Schluessel eintragen

`.env` oeffnen und die beiden Alpaca-**Paper**-Schluessel eintragen
(app.alpaca.markets -> Paper Trading -> API Keys). Sonst nichts noetig; alle
Voreinstellungen sind die sicheren (dry-run an, Paper-Endpunkt, Not-Aus-Pfad).

## 3. Alles gegen das echte Paper-Konto pruefen

```bash
.venv/bin/micronalgo preflight --probe-orders
```

Muss komplett auf PASS stehen, insbesondere `order_type_cls` und
`order_type_opg` -- die Auktionsorders sind die Annahme, an der die ganze
Strategie haengt. Der Probe-Modus schickt eine 1-Stueck-Order und storniert sie
sofort; er verweigert sich selbst, wenn die Schlussauktion zu nah ist.

## 4. Dauerbetrieb per launchd

```bash
sh deploy/install_mac.sh --launchd
```

Richtet `~/Library/LaunchAgents/com.micronalgo.paper.plist` ein: Start beim
Login und automatischer Neustart nach einem Absturz. Ein bewusster **Halt**
bleibt trotzdem unten: launchd startet den Prozess zwar nach dem Exit-Code 2
einmal neu, aber der Start-Guard erkennt den persistierten Halt und beendet
sich sauber mit Code 0 -- den launchd nicht neu startet. Der Bot wartet dann,
bis ein Mensch `micronalgo resume --clear-halt` ausfuehrt.

Warum ein Dauerprozess statt launchd-Kalendereintraegen: der Bot kennt die
Boersensitzungen selbst, inklusive 13:00-Halbtagen, und sein `tick()` ist
idempotent. Ein Prozess, der immer laeuft und intelligent bis zum naechsten
Entscheidungszeitpunkt schlaeft, ist robuster als Kalendereintraege, die
Halbtage nicht kennen.

Steuerung:

```bash
tail -f logs/launchd.err.log                                  # was er tut
launchctl kickstart -k gui/$(id -u)/com.micronalgo.paper      # Neustart erzwingen
launchctl unload ~/Library/LaunchAgents/com.micronalgo.paper.plist   # stoppen
```

## 5. Echtzeit-Anbindung

`micronalgo paper` verbindet sich automatisch mit zwei Alpaca-Websocket-Streams:

* **Live-Trades** fuer MU: haelt den Referenzpreis fuer die Stueckzahl-Berechnung
  sekundenfrisch statt einen REST-Roundtrip alt.
* **Order-Events**: eine Ausfuehrung, Ablehnung oder Stornierung weckt den Bot
  **sofort**, statt bis zum naechsten Poll zu warten. Ein toter Auktions-Exit
  wird dadurch in Sekunden eskaliert statt in Minuten -- das ist der Teil von
  "schnellster Entry / bester Exit", der real existiert.

Ehrlich gesagt, damit keine falsche Erwartung entsteht: **auf die Fill-Preise
selbst hat Geschwindigkeit keinen Einfluss.** Beide Legs fuellen im
Auktions-Print, und der ist fuer alle Teilnehmer identisch -- eine um 15:45:00
eingereichte MOC-Order und eine um 15:49:00 eingereichte fuellen zum selben
Kurs. Streaming beschleunigt die *Fehlerbehandlung*, nicht den Markt.

Faellt ein Stream aus, laeuft alles unveraendert ueber REST-Polling weiter
(sichtbar im Log); die Streams sind Beschleuniger, nie Abhaengigkeiten.
Abschalten: `micronalgo paper --no-stream`.

## 6. Ueberwachen

```bash
.venv/bin/micronalgo status --watch    # Live-Ansicht im Terminal
.venv/bin/micronalgo status            # einmalig als JSON
tail -f logs/audit.jsonl               # jede Entscheidung mit Begruendung
```

## 7. Der Schlafmodus (wichtig)

Ein Mac im Ruhezustand fuehrt keine Prozesse aus. Verschlaeft er das
Einstiegsfenster (15:45-15:50 ET), **ueberspringt** der Bot die Sitzung sicher --
er jagt der Auktion nie hinterher --, aber er handelt eben auch nicht.
Verschlaeft er das Ausstiegsfenster, schickt er nach dem Aufwachen sofort eine
Market-Order (Standard-Policy `market_at_open`).

Optionen, von einfach nach solide:

1. **Systemeinstellungen -> Displays -> Erweitert -> "Automatisches Aktivieren
   des Ruhezustands verhindern, wenn das Display ausgeschaltet ist"** (bei
   Laptops: am Netzteil). Reicht fuer einen Mac, der zuhause am Strom haengt.
2. **Geplantes Aufwachen**: `sudo pmset repeat wakeorpoweron MTWRF 15:30:00`
   (Beispiel; die Zeit in DEINER Zeitzone waehlen, die 15:40 ET abdeckt --
   Sommer-/Winterzeit verschiebt das!). Der idempotente Tick erledigt den Rest.
3. **Ein always-on-Geraet** (Mac mini, kleiner Server): die eigentliche Loesung,
   wenn das ernsthaft laufen soll.

Zeitumrechnung als Anker: 15:45 New York ist 21:45 in Deutschland im Winter und
21:45 im Sommer ebenfalls -- beide Zonen wechseln, aber an verschiedenen Daten;
in den zwei Wochen um die Umstellungen weicht es um eine Stunde ab. Der Bot
selbst rechnet immer in Boersen-Ortszeit und ist davon nicht betroffen; nur ein
pmset-Weckplan in lokaler Zeit muss das beruecksichtigen.

## 8. Deinstallieren

```bash
launchctl unload ~/Library/LaunchAgents/com.micronalgo.paper.plist 2>/dev/null
rm -f ~/Library/LaunchAgents/com.micronalgo.paper.plist
rm -rf /pfad/zu/micronalgo
```

Mehr als das gibt es nicht -- keine versteckten Daemons, keine Systemdateien.
