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

### Fly.io

```bash
fly launch --no-deploy --copy-config --dockerfile deploy/Dockerfile
fly volumes create micronalgo_data --size 1
fly secrets set ALPACA_API_KEY_ID=xxx ALPACA_API_SECRET_KEY=yyy
fly deploy
```

Danach vom Handy aus steuerbar:

```bash
fly logs                              # was er gerade tut
fly secrets set MICRONALGO_DRY_RUN=false   # echte Paper-Orders scharfschalten
fly scale count 0                     # Not-Aus: Maschine anhalten
```

Die `fly`-Befehle brauchen einmalig einen Rechner **oder** die Web-Konsole auf
fly.io, die im Handy-Browser funktioniert. Danach reicht die Weboberflaeche.

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

1. **Repo-Secrets setzen** -- Settings -> Secrets and variables -> Actions ->
   New repository secret:
   * `ALPACA_API_KEY_ID`
   * `ALPACA_API_SECRET_KEY`
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

### Wo der Zustand lebt

In einem eigenen Branch `bot-state`, damit Code-Historie und Bot-Historie sich
nie in die Quere kommen. Jeder Lauf holt ihn, arbeitet damit, schreibt ihn
zurueck -- ein vollstaendiges, nachlesbares Protokoll jeder Entscheidung.

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
