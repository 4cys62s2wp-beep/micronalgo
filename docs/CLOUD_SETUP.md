# Ohne Mac betreiben

Der Bot braucht eine Maschine, die um 15:45 und 09:30 New Yorker Zeit wach ist.
Ein Handy kann das nicht sein -- iOS und Android beenden Hintergrundprozesse,
und ein Python-Daemon laeuft dort nicht durch. Was vom Handy aus geht: alles
**einrichten, ueberwachen und steuern**. Hier sind die drei Wege, ehrlich
sortiert.

| Weg | Einrichtung vom Handy | Zuverlaessigkeit | Kosten |
|---|---|---|---|
| Container (Fly.io / Railway) | fast vollstaendig | hoch -- laeuft durch | ~0-5 $/Monat |
| GitHub Actions Cron | **vollstaendig** | mittel -- Cron ist unpuenktlich | 0 $ (oeffentliches Repo) |
| Mac mit launchd | nein, ein Terminalbefehl | hoch, solange er wach ist | 0 $ |

---

## Weg 1: Container (empfohlen)

Der Bot laeuft durchgehend, schlaeft selbst bis zum naechsten
Entscheidungszeitpunkt, nutzt die Websocket-Streams, und der Zustand liegt auf
einem Volume. Das ist die Form, fuer die er gebaut ist.

### Fly.io, Schritt fuer Schritt

```bash
# 0. Einmalig: CLI installieren und anmelden
brew install flyctl && fly auth login

# 1. App anlegen, noch nicht deployen
cd ~/micronalgo
fly launch --no-deploy --copy-config --dockerfile deploy/Dockerfile

# 2. Volume fuer den Zustand (1 GB reicht auf Jahre)
fly volumes create micronalgo_data --size 1 --region ewr

# 3. Schluessel als Secrets -- NICHT ins Image, nicht in fly.toml
fly secrets set ALPACA_API_KEY_ID=PK... ALPACA_API_SECRET_KEY=...

# 4. Deployen
fly deploy

# 5. Nachsehen, dass genau EINE Maschine laeuft
fly status
fly logs
```

Bei Schritt 1 schlaegt `fly launch` womoeglich einen anderen App-Namen vor,
weil `micronalgo` global schon vergeben ist. Das ist normal; nimm den
Vorschlag an.

Bei Schritt 2 muss die Region dieselbe sein wie `primary_region` in
`fly.toml` (`ewr`, Newark), sonst findet die Maschine ihr Volume nicht.

**Warum genau eine Maschine:** Zwei Instanzen wuerden sich um dieselbe
Position streiten. Der Instanz-Lock im Code schuetzt nur *innerhalb* einer
Maschine. Was es hier verhindert, ist das Volume -- ein Fly-Volume haengt immer
nur an einer Maschine. Deshalb: `fly status` muss genau eine Zeile zeigen, und
`fly scale count 2` waere der eine Befehl, den Du nie eingeben darfst.

**Kosten:** eine `shared-cpu-1x`-Maschine mit 512 MB und 1 GB Volume liegt bei
ungefaehr 2-5 $ im Monat. Fly verlangt inzwischen eine hinterlegte Karte.

### Steuerung, auch vom Handy

```bash
fly logs                                   # was er gerade tut
fly secrets set MICRONALGO_DRY_RUN=false   # echte Paper-Orders scharfschalten
fly ssh console -C "micronalgo status"     # was er glaubt
fly ssh console -C "micronalgo kill"       # Not-Aus, Position bleibt
fly scale count 0                          # haerterer Not-Aus: Maschine aus
```

Die `fly`-Befehle brauchen einmalig einen Rechner **oder** die Web-Konsole auf
fly.io, die im Handy-Browser funktioniert. Danach reicht die Weboberflaeche.

### Ein Halt verhaelt sich hier anders als auf dem Mac

Auf dem Mac bleibt ein gehaltener Bot unten, weil launchd einen Exit-Code 0
nicht neu startet. Ein Container-Host entscheidet das nach seiner eigenen
Restart-Policy: steht sie auf `always`, startet der Container nach dem Halt neu,
der Start-Guard erkennt den gespeicherten Halt und beendet sich sofort wieder --
in einer Schleife.

**Gehandelt wird dabei nicht.** Der Guard laeuft vor jeder Handelslogik, ein
gehaltener Bot gibt keine Order ab. Was Du bekommst, ist Lograuschen, kein
Risiko. Sichtbar wird es als wiederkehrende `halted`-Zeile in `fly logs`; die
Behandlung ist dieselbe wie ueberall:

```bash
fly ssh console -C "micronalgo resume --clear-halt"
```

Vorher aber nachsehen, **warum** er gehalten hat -- ein Halt ist das Ergebnis
einer Risiko-Wache, nicht eines Fehlers.

### Railway / Render / eigener Server

`deploy/Dockerfile` funktioniert ueberall. Wichtig, egal wo:

* **Genau eine Instanz.** Zwei Bots wuerden sich um dieselbe Position streiten.
  Der Instanz-Lock im Code schuetzt nur innerhalb einer Maschine.
* **Ein Volume auf `/data`**, sonst vergisst ein Neustart die offene Position.
  (Die Reconciliation gleicht dann zwar gegen den Broker ab und haelt im
  Zweifel an -- richtig, aber unnoetig unbequem.)
* **Kein Rolling-Update.** Erst die alte Instanz stoppen, dann die neue starten.

---

## Weg 2: GitHub Actions (der reine Handy-Weg)

Komplett im Handy-Browser oder in der GitHub-App einrichtbar, ohne Terminal.

### Schritt 0, ohne den nichts passiert: nach `main` zusammenfuehren

GitHub startet zeitgesteuerte Ablaeufe (`schedule`) und zeigt den *Run
workflow*-Knopf (`workflow_dispatch`) **ausschliesslich** fuer Workflow-Dateien
auf dem **Standardzweig**. Liegt der Code nur im Entwicklungszweig, passiert
gar nichts -- und es erscheint auch kein Knopf. Keine Fehlermeldung, einfach
Stille. Das ist GitHub-Verhalten, kein Fehler im Bot.

Einmal zusammenfuehren, geht vom Handy:

```
https://github.com/4cys62s2wp-beep/micronalgo/compare/main...claude/micron-trading-algo-fy4q2o
```

*Create pull request* -> *Merge pull request*. Erst danach greifen die Schritte
unten.

### Danach

1. **Repo-Secrets setzen** -- Settings -> Secrets and variables -> Actions ->
   New repository secret:
   * `ALPACA_API_KEY_ID`
   * `ALPACA_API_SECRET_KEY`

   Fehlt einer, bricht der Lauf mit einer Klartextmeldung ab, die den fehlenden
   Namen nennt -- nicht mit einem HTTP 403, das nach Netzproblem aussieht.
2. **Workflow aktivieren** -- Actions-Tab oeffnen, "paper trading" auswaehlen,
   "Enable workflow".
3. **Testlauf** -- "Run workflow" antippen. Laeuft im Trockenlauf, sendet also
   nichts; die Zusammenfassung zeigt, was er getan haette.
4. **Scharfschalten**, wenn der Testlauf sauber war -- Settings -> Secrets and
   variables -> Actions -> Variables -> New repository variable:
   `MICRONALGO_DRY_RUN` = `false`

Ab dann laeuft er nach Zeitplan. Jeder Lauf steht im Actions-Tab, den es auch
als GitHub-App gibt.

### Die Einschraenkung, die Du kennen musst

GitHub-Cron ist **nicht puenktlich**. GitHub sagt selbst, dass Laeufe bei hoher
Last um 10 Minuten und mehr verspaetet starten. Ein 5-Minuten-Auktionsfenster
waere damit unzuverlaessig.

Der Workflow faengt das ab, indem er das Einstiegsfenster auf `close-40` bis
`close-10` verbreitert -- 30 Minuten statt 5 -- und alle 10 Minuten laeuft.
Selbst 25 Minuten Verspaetung landen dann noch im Fenster. Der Preis: die Order
geht frueher raus, mit einem etwas aelteren Referenzpreis fuer die Stueckzahl.
Bei Privatanlegergroesse ist das belanglos (`capital_fraction` laesst 5 % Luft).

Verpasst er das Fenster doch, **ueberspringt** er die Sitzung sicher -- er jagt
der Auktion nie hinterher. Du verlierst dann eine Gelegenheit, kein Geld.

### Nicht-US-Konten: Alpaca Europe

Der Standard-Endpunkt ist `https://paper-api.alpaca.markets`. Alpaca Europe
benutzt einen anderen Hostnamen, und die Sicherung im Code erkennt ihn dann
nicht als Papierkonto. Beides sind Repository-*Variablen* (nicht Secrets):

* `MICRONALGO_ALPACA_BASE_URL` = Dein Paper-Endpunkt
* `MICRONALGO_ALPACA_PAPER` = `true`

Die zweite Variable ist die ausdrueckliche Erklaerung "das ist ein Papierkonto".
Sie existiert getrennt, damit die Sicherung nie ueber eine Namensaehnlichkeit
entscheidet.

### Wo der Zustand lebt

In einem eigenen Branch `bot-state`, damit Code-Historie und Bot-Historie sich
nie in die Quere kommen. Jeder Lauf holt ihn, arbeitet damit, schreibt ihn
zurueck -- ein vollstaendiges, nachlesbares Protokoll jeder Entscheidung.

Die Pfade beim Holen und beim Zurueckschreiben muessen exakt uebereinstimmen.
Sie taten es einmal nicht, und weil git seinen Fehler nach `/dev/null` schrieb,
meldete der Schritt trotzdem Erfolg -- der Bot startete jedes Mal mit leerem
Zustand, waehrend das Protokoll das Gegenteil behauptete. Der Schritt **zaehlt**
deshalb heute die wiederhergestellten Dateien und gibt die Zahl aus, statt
Erfolg zu behaupten.

### Die Untersuchung ohne Rechner

Zweiter Ablauf im selben Repo: **Untersuchung**. `micronalgo study` braucht
Kursdaten aus dem Netz -- genau das, was ein Handy nicht kann, ein Runner aber
schon.

*Actions* -> **Untersuchung** -> *Run workflow*. Vorgaben (`study`, ab
`2007-01-01`) einfach bestaetigen. Nach ein paar Minuten steht der vollstaendige
Bericht in der Zusammenfassung des Laufs; zusaetzlich haengt er als Artefakt
(`.txt`, `.html`, `.json`) am Lauf.

Ein **FAIL**-Urteil laesst den Lauf bewusst **gruen**: es ist ein Befund ueber
die Strategie, kein Defekt der Software. Rot wird nur ein echter Absturz.

### Not-Aus vom Handy

Actions-Tab -> "paper trading" -> "..." -> **Disable workflow**. Damit werden
keine neuen Positionen eroeffnet. Achtung: eine bereits offene Position wird
dann auch nicht mehr verkauft -- vor dem Abschalten also pruefen, ob Du flach
bist (Alpaca-App, siehe unten), oder die Position dort von Hand schliessen.

---

## Weg 3: Mac (schon gebaut)

`sh deploy/install_mac.sh --launchd`, Details in `docs/MAC_SETUP.md`. Ein
einziger Terminalbefehl, danach laeuft es. Der Haken bleibt der Schlafmodus.

---

## Vom Handy aus zuschauen

**Die Alpaca-App** (iOS und Android) zeigt Dein Paper-Konto: Positionen, jede
Order, jede Ausfuehrung, den Kontostand. Das ist die ehrlichste Kontrolle, die
Du hast -- sie zeigt, was der Broker wirklich gemacht hat, nicht was der Bot
glaubt. Bei Abweichung hat immer der Broker recht; genau so ist auch die
Reconciliation im Code gebaut.

**GitHub-App**: Actions-Tab fuer jeden Lauf, `bot-state`-Branch fuer den
Zustand und das Audit-Log.

**Fly.io / Railway**: Logs in der Weboberflaeche.

---

## Bevor Du irgendwo scharfschaltest

`docs/GO_LIVE_CHECKLIST.md`. Der wichtigste Punkt, der sich nicht umgehen
laesst: `micronalgo preflight --probe-orders` muss einmal gegen Dein echtes
Paper-Konto sauber durchlaufen -- besonders `order_type_cls` und
`order_type_opg`. Ohne Auktionsorders ueberquerst Du 252-mal im Jahr einen
Spread, und die Kante ist weg.

Der Actions-Workflow laeuft den Preflight vor **jedem** Tick und handelt nicht,
wenn er durchfaellt.
