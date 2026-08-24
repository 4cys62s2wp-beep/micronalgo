# micronalgo

Forschung und vollautomatisches Paper-Trading fuer den Overnight-/Intraday-Effekt
bei Micron Technology (MU).

Die Ausgangsfrage: *Waere man bei jedem Boersenschluss eingestiegen und bei jeder
Eroeffnung ausgestiegen, haette man angeblich rund +140.000 % gemacht - andersherum
fast -100 %.* Dieses Repository baut die Maschinerie, mit der sich das
nachrechnen laesst, und die Maschinerie, die es handeln kann. Und es baut
genauso sorgfaeltig die Pruefungen, die zeigen, ob davon etwas uebrig bleibt.

---

## Die Kernrechnung

Fuer jede Sitzung `t` mit Eroeffnung `O` und Schluss `C`:

```
overnight   r_on = O_t / C_(t-1) - 1      (gehalten, waehrend die Boerse zu ist)
intraday    r_id = C_t / O_t     - 1      (gehalten, waehrend sie offen ist)
ganzer Tag  r_cc = C_t / C_(t-1) - 1      (was Buy & Hold verdient)

(1 + r_cc) = (1 + r_on) * (1 + r_id)      exakt - O_t kuerzt sich weg
```

Die beiden Fenster sind disjunkt und lueckenlos. Der gesamte Ertrag einer Aktie
verteilt sich auf sie. Die Behauptung ist, dass bei MU praktisch alles auf das
Nacht-Fenster faellt.

**Das ist keine Meinungsfrage, sondern nachrechenbar.** Genau dafuer ist das
Repository da.

---

## Was Du sofort ausprobieren kannst

```bash
pip install -e ".[all]"
micronalgo demo            # laeuft komplett offline, braucht kein Netz
```

`demo` erzeugt eine synthetische Kursreihe mit MU-aehnlichen Eigenschaften und
laesst die komplette Analyse darueber laufen. Damit siehst Du in einer Minute,
was das Werkzeug ausgibt - **aber die Zahlen sagen nichts ueber MU**, sie kommen
aus Zufallsdaten. Fuer die echte Antwort:

```bash
micronalgo study                                  # laedt MU und rechnet alles
micronalgo study --provider csv:/pfad/zu/MU.csv   # falls kein Provider erreichbar ist
```

Das schreibt `reports/mu_overnight_<datum>.{txt,html,json}`.

> **Hinweis zur Entstehung:** Diese Umgebung hatte eine strikte Egress-Policy -
> Stooq, Yahoo, Alpaca, Tiingo, Polygon und Nasdaq waren alle mit 403 gesperrt.
> Die echten MU-Zahlen konnte ich hier deshalb **nicht** ziehen. Stattdessen ist
> die gesamte Engine gegen synthetische Reihen mit analytisch bekanntem Ergebnis
> verifiziert (Abweichung 2e-15), und jeder Netzwerk-Parser wird gegen
> aufgezeichnete Antworten getestet. Der Befehl oben liefert Dir die echten
> Zahlen auf Deinem Rechner.

---

## Was der Bericht Dir sagt - und warum er zuerst die schlechten Nachrichten bringt

Der Bericht stellt den **Realitaets-Check vor** die Ertragskurve. Das ist Absicht:
die Schlagzeilenzahl ist der Teil, der am leichtesten in die Irre fuehrt.

Jedes Kriterium hat eine ausformulierte Schwelle und ein Urteil:

| Kriterium | Warum es drin ist |
|---|---|
| Datenvalidierung | Eine falsch angepasste Kursreihe erzeugt eine wunderschoene und voellig erfundene Kurve. Faellt diese Zeile durch, bedeutet nichts darunter etwas. |
| CAGR nach Kosten | 252 Round-Trips pro Jahr. Die Kostenannahme *ist* das Ergebnis. |
| CAGR bei 5 bps Slippage | Was passiert, wenn die Auktionsausfuehrung nicht klappt. |
| Break-even-Kosten | **Geometrisch** gerechnet. Der arithmetische Mittelwert ueberschaetzt sie jede Sitzung um etwa sigma²/2. |
| Kante vs. effektiver Spread | Wenn Schlusskurse auf dem Geld- und Eroeffnungskurse auf dem Briefkurs drucken, entsteht ein voller Spread "Nachtrendite" pro Sitzung - **ganz ohne Oekonomie dahinter**. |
| Bootstrap-Konfidenzintervall | Ein t-Test ueberschaetzt die Signifikanz bei fetten Raendern und Autokorrelation massiv. |
| Letzte 5 Jahre | Der wichtigste Test. Viel vom historischen Effekt liegt vor der Dezimalisierung 2001. |
| Anteil der 10 besten Tage | Wenn ein Jahrzehnt Kante aus 10 Sitzungen besteht, ist es ein Lotterielos. |
| Deflated Sharpe | Korrigiert dafuer, wie viele Varianten man ausprobiert hat. |
| Max Drawdown / schlimmste Nacht | Du haeltst jede Earnings-Luecke ueber Nacht. Es gibt keinen Stop-Loss, waehrend die Boerse zu ist. |

Das Gesamturteil ist das schlechteste Einzelurteil. `micronalgo study` gibt bei
`FAIL` den Exit-Code 2 zurueck - das laesst sich in CI verdrahten.

---

## Der eine Grund, warum das ueberhaupt handelbar sein koennte

Die natuerliche Ausfuehrung ist ein **Market-on-Close**-Kauf und ein
**Market-on-Open**-Verkauf. Beide werden zum offiziellen Auktionspreis
ausgefuehrt - und genau diese Preise verwendet der Backtest als `close` und
`open`. Eine Order in Privatanlegergroesse **ueberquert also keinen Spread**.

Der Unterschied ist alles:

| Ausfuehrung | Kosten/Round-Trip | Jahresbelastung |
|---|---|---|
| Auktion, gebuehrenfrei (Alpaca) | **0,28 bps** | **-0,7 %** |
| Auktion + 0,5 bps Slippage/Seite | 1,3 bps | -3,2 % |
| Spread ueberqueren (1 Cent auf 100 $) | 10 bps | **-22 %** |
| Achtel-Tick-Aera (1994-1997) | ~175 bps | **-100 %** |

Die letzte Zeile ist der Grund, warum die fruehe Historie diese Frage gar nicht
beantworten kann. Das ist zugleich die Annahme, die am meisten Angriff verdient -
deshalb rechnet der Bericht jedes Szenario einzeln durch.

---

## Der Bot

```bash
micronalgo preflight --probe-orders   # prueft ALLES gegen Dein echtes Paper-Konto
micronalgo paper                      # laeuft (dry_run ist standardmaessig AN)
micronalgo paper --live               # sendet Orders ans Paper-Konto
micronalgo status                     # was der Bot gerade glaubt
micronalgo kill                       # Not-Aus
```

### Der Handelstag, in Ortszeit der Boerse

```
Eroeffnung -60m   Market-on-Open-VERKAUF fuer die ueber Nacht gehaltene Position
Eroeffnung +5m    pruefen, dass das Konto flach ist
Schluss    -15m   Market-on-Close-KAUF
Schluss    -10m   harter Cutoff; danach wird die Sitzung ausgelassen
Schluss    +5m    Ausfuehrung pruefen und protokollieren
```

Alle vier sind **Versaetze zur jeweiligen Sitzung**, nie feste Uhrzeiten. An
einem 13:00-Halbtag verschiebt sich die Schlussauktions-Arbeit automatisch auf
12:45/12:50/13:05. Eine feste `15:45`-Planung wuerde dort die Auktion verpassen
und eine ungewollte Intraday-Position hinterlassen - also genau das Risiko, das
die Strategie vermeiden soll.

### Warum ein Neustart nichts kaputtmacht

`tick()` ist **idempotent**. Ob es einmal pro Minute, einmal pro Stunde oder
fuenfzigmal hintereinander aufgerufen wird, aendert nichts: jede Aktion fragt
zuerst den persistierten Zustand, ob sie schon erledigt ist. Dazu kommt eine
deterministische `client_order_id` pro (Symbol, Handelstag, Leg, Versuch), deren
Eindeutigkeit die Boerse selbst erzwingt.

Der entscheidende Teil: **ein Timeout erhoeht den Versuchszaehler nicht.** Nach
einem Timeout kann die Order sehr wohl angekommen sein; eine neue ID waere genau
der Weg, aus einer gewollten Position zwei zu machen. Stattdessen wird die Order
per `client_order_id` gesucht und uebernommen.

### Was der Bot bewusst nicht tut

- **Er jagt keine verpasste Schlussauktion** mit einer Market-Order hinterher.
  Der Auktionspreis ist der Grund, warum die Kante die Kosten ueberlebt; eine
  verpasste Sitzung kostet nur eine Gelegenheit, ein schlechter Fill kostet Geld
  und entkoppelt still die Live-Ergebnisse vom Backtest.
- **Er handelt nicht auf einer Position, die er sich nicht zuordnen kann.** Findet
  der Abgleich beim Broker Stuecke, die dieser Bot nicht eroeffnet hat, haelt er
  an und ruft einen Menschen. Automatisch fremde Positionen glattzustellen waere
  der schlimmere Fehler.
- **Der Not-Aus blockiert niemals Ausstiege.** Er verhindert neue Einstiege. Ein
  Not-Aus, der auch Ausstiege sperrt, wuerde eine Position im Intraday-Fenster
  stranden lassen - dem Fenster mit negativer Erwartung.

### Risiko-Wachen

Jede ist ein **Veto, keine ist ein Ausloeser**: ein Fehler in einem Veto kostet
einen entgangenen Trade, ein Fehler in einem Ausloeser kostet eine Position, die
niemand wollte.

Not-Aus-Datei, Drawdown-Stopp, Tagesverlustgrenze, Serie von Verlusten,
Preis-Plausibilitaet (faengt eine kaputte Quote *und* einen nicht angewandten
Split), Datenalter, Kaufkraft, Notional- und Stueckzahl-Deckel, Handelbarkeit
des Papiers, und ein Circuit Breaker fuer API-Fehler.

---

## TradingView

Zwei Skripte in `pine/`:

**`overnight_vs_intraday.pine`** (Indikator, Tageschart) - das eigentliche
Deliverable. Er rechnet die Zerlegung direkt und ist damit **exakt**, ohne jede
Fill-Annahme. Nutze ihn als visuellen Beweis und als unabhaengige Gegenprobe zum
Python-Backtest; die Zahlen muessen uebereinstimmen.

**`micron_overnight_strategy.pine`** (Strategie, Intraday-Chart 1m-15m).

Die ehrliche Einschraenkung, die ich beim Bauen pruefen musste: **auf einem
Tageschart kann Pine das nicht.** Mit `process_orders_on_close = true` fuellt eine
Market-Order zum *Schluss* des Bars, mit `false` zur *Eroeffnung des naechsten*.
Die Strategie braucht von jedem eins. Keine einzelne Einstellung liefert beides,
also wuerde man auf einem Tageschart stillschweigend Close-zu-Close testen. Auf
einem 1-Minuten-Chart sind es verschiedene Bars, dort geht es - mit etwa einer
Minute Abweichung zum echten Auktionspreis, die im Skriptkopf steht.

TradingViews eingebauter Paper-Broker kann ueberhaupt keine Auktionsorders. Wer
die echten MOC/MOO-Orders will, nimmt den Python-Bot gegen Alpaca Paper - dafuer
ist er da.

---

## Aufbau

```
src/micronalgo/
  calendar_nyse.py     NYSE-Kalender mit Fail-Closed-Quellenhierarchie
  config.py            Einstellungen; alle Zeiten als Versatz zur Sitzung
  data/
    schema.py          kanonisches Bar-Schema (angepasste UND unangepasste Preise)
    providers/         Stooq, Yahoo, Tiingo, Alpaca, lokale CSV
    loader.py          Provider-Kette, Parquet-Cache, Provenienz
    validate.py        die Pruefungen, die eine kaputte Reihe wirklich finden
    synthetic.py       Reihen mit analytisch bekanntem Ergebnis
  research/
    returns.py         die Zerlegung
    costs.py           Kostenmodell mit datierten Gebuehrensaetzen
    engine.py          Backtest mit echter Kassenbuchfuehrung
    metrics.py         inkl. Probabilistic und Deflated Sharpe
    robustness.py      Bootstrap, Permutation, Regime, Corwin-Schultz-Spread
    filters.py         Overlays, strukturell lag-sicher
    study.py           die Studie inkl. Realitaets-Check
    report.py          Konsole und eigenstaendiges HTML
  live/
    broker.py          Protocol + idempotente Order-IDs
    alpaca.py          REST-Adapter, reine Parser
    simbroker.py       Simulator mit virtueller Uhr
    runner.py          der idempotente Zustandsautomat
    risk.py            die Vetos
    state.py           atomare Persistenz, Instanz-Lock
    reconcile.py       der Broker hat immer recht
    preflight.py       prueft zur Laufzeit, was der Build nicht konnte
    scheduler.py       Poll-Schleife
```

### Zwei Rechenwege, ein Ergebnis

`simulate()` ist die explizite, pruefbare Schleife. `net_return_series()` ist der
vektorisierte schnelle Pfad fuer den Bootstrap, der Tausende Durchlaeufe braucht.
Ein Test haelt sie aneinander - der schnelle Pfad kann nicht stillschweigend
abdriften.

Ebenso teilen Backtest und Bot dieselbe Sizing-Funktion, und ein Test faehrt den
Zustandsautomaten gegen den Simulator und vergleicht das Endkapital mit dem
Backtest. Uebrig bleiben 0,003 % - reine Ganzstueck-Rundung.

---

## Tests

```bash
pytest -q          # 162 Tests, kein Netzwerkzugriff
ruff check src tests
```

Kein Test braucht ein Netz. Jede Kursreihe ist entweder synthetisch mit bekanntem
Ergebnis oder eine aufgezeichnete Provider-Antwort. Darunter:

- die Zerlegung gegen geschlossene Formeln (Abweichung < 1e-12);
- ein Beweis, dass Kosten genau einmal pro Leg anfallen;
- ein Beweis, dass Filter tatsaechlich verschoben sind (Un-Verschieben aendert das Ergebnis);
- ein voller 36-Jahres-Abgleich der Kalender-Regel-Engine gegen `exchange_calendars`;
- reiner Bid-Ask-Bounce ohne echte Kante **muss** geflaggt werden, eine echte Kante **darf nicht**;
- Neustart mitten am Tag, verpasster Cutoff, abgelehnte Order, toter Auktionsauftrag, Not-Aus waehrend eine Position offen ist;
- eine Golden-Regression, die die Backtest-Zahlen einfriert.

---

## Ehrliche Einordnung

Ich habe beim Bauen mehrere eigene Fehler gefunden und korrigiert. Zwei sind der
Erwaehnung wert, weil sie zeigen, wo die Fallen liegen:

1. **Ich hatte angenommen, die Identitaetspruefung `(1+r_cc) = (1+r_on)(1+r_id)`
   wuerde falsche Kursanpassungen finden.** Sie tut es nicht - `O_t` kuerzt sich
   weg, die Identitaet gilt algebraisch immer. Der echte Detektor ist ein
   anderer, und die Doku sagt das jetzt ausdruecklich.

2. **Ich hatte zuerst Roll's Spread-Schaetzer fuer den Bid-Ask-Bounce-Test
   genommen.** Mein eigener Testfall hat gezeigt, dass er genau diesen Artefakt
   *nicht* sehen kann: liegen alle Schlusskurse auf derselben Quote-Seite,
   enthaelt die Close-zu-Close-Reihe keinerlei Bounce. Jetzt ist Corwin-Schultz
   ueber Hoch/Tief der primaere Schaetzer.

Was auch nach allen Pruefungen offen bleibt: Tagesdaten enthalten keine Quotes.
Ob die gemessene Nachtprämie eine echte Risikoprämie ist oder daher kommt, auf
welcher Seite des Spreads gedruckt wird, laesst sich damit **nicht endgueltig
entscheiden**. Dafuer braucht es Quote- oder Minutendaten. Die Regime-Tabelle ist
die andere Haelfte der Antwort: der Artefakt schrumpft mit der Tickgroesse.

---

## Bevor Du echtes Geld anfasst

`docs/GO_LIVE_CHECKLIST.md`. Echtes Geld ist zusaetzlich durch eine explizite
Umgebungsvariable gesperrt; ein Tippfehler in einer URL darf dafuer nie reichen.

---

## Haftungsausschluss

Forschungssoftware, keine Anlageberatung. Das vergangene Verhalten einer
Kursreihe ist keine Prognose. Eine Einzelaktien-Strategie traegt idiosynkratisches
Risiko, das Diversifikation entfernen wuerde. Jede Zahl haengt an den Daten und
Kostenannahmen, die im Bericht stehen.

MIT-Lizenz.
