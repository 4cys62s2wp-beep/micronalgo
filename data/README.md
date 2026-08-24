# Kursdaten

Dieses Verzeichnis ist der Cache. Die Rohdaten kommen aus einem der Provider in
`src/micronalgo/data/providers/`.

## Reihenfolge der Empfehlung

| Provider | Historie | Schluessel | Anmerkung |
|---|---|---|---|
| `stooq`  | vollstaendig | nein | OHLC **einheitlich** angepasst - genau das, was die Zerlegung braucht |
| `yahoo`  | vollstaendig | nein | OHLC split-angepasst plus separates `Adj Close`; der Loader rechnet das Verhaeltnis auf alle vier Spalten |
| `tiingo` | vollstaendig | ja | liefert angepasste *und* unangepasste Felder getrennt - die sauberste Quelle |
| `alpaca` | ab ~2016 | ja | zu kurz fuer die historische Frage, aber die richtige Quelle fuer den Live-Betrieb |
| `csv:`   | beliebig | nein | der Ausweg, der immer funktioniert |

## Wenn kein Provider erreichbar ist

Beliebige CSV mit `Date,Open,High,Low,Close[,Adj Close][,Volume]` exportieren
(TradingView, Yahoo, der eigene Broker) und dann:

```bash
micronalgo study --provider csv:/pfad/zu/MU.csv
```

Enthaelt die Datei eine `Adj Close`-Spalte, wird das Verhaeltnis
`Adj Close / Close` **einheitlich auf alle vier OHLC-Spalten** angewendet. Das
ist die einzige Anpassung, die die Overnight/Intraday-Zerlegung ueberlebt.

## Earnings-Termine

`data/earnings_mu.csv` mit einer Spalte `date` (ISO `YYYY-MM-DD`), die die
Sitzungen enthaelt, an deren **Schluss** eine Position wegen eines danach
gemeldeten Quartalsergebnisses nicht eroeffnet werden soll.

Dieses Projekt liefert bewusst **keine** vorgefertigte Liste mit: ein veralteter
oder erfundener Earnings-Kalender ist schlechter als gar keiner, weil er
stillschweigend die falschen Sitzungen ausschliesst und den Backtest sauberer
aussehen laesst als die Realitaet. Die verbindliche Quelle ist der
Investor-Relations-Kalender des Unternehmens.

`micronalgo` kann Kandidaten aus den Daten schaetzen
(`filters.infer_earnings_dates`), markiert sie im Bericht aber ausdruecklich als
geschaetzt.
