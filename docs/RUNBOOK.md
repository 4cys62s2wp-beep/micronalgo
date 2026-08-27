# Runbook

Was tun, wenn etwas passiert. Geschrieben fuer den Moment, in dem man es
tatsaechlich braucht - also kurz.

## Sofortmassnahmen

```bash
micronalgo kill --reason "grund"    # keine neuen Einstiege; Ausstiege laufen weiter
micronalgo status                   # was der Bot glaubt
tail -f logs/audit.jsonl            # jede Entscheidung, mit Begruendung
micronalgo resume --clear-halt      # weiter, inkl. Loeschen eines Halts
```

Der Not-Aus ist eine Datei. Das ist Absicht: er funktioniert, wenn die Config
kaputt ist, die API nicht antwortet und die Person, die danach greift, in Panik
ist. `touch state/KILL` reicht.

## Der Bot hat angehalten

`micronalgo status` zeigt `halted: true` und einen Grund.

**"broker holds N MU that this bot did not open"**
: Beim Broker liegen Stuecke, die der Bot sich nicht zuordnen kann - manueller
  Trade, eine zweite Instanz, oder ein verlorener Zustand. Der Bot handelt
  bewusst nicht darauf weiter. Position beim Broker ansehen, entscheiden, ob sie
  bleiben oder weg soll, dann `micronalgo resume --clear-halt`.

**"API error budget exhausted"**
: Zu viele Broker-Fehler in kurzer Zeit. Ein Broker, der Fehler wirft, ist ein
  Broker, dessen Positionsangaben man nicht trauen kann. Status der Plattform
  pruefen, dann fortsetzen.

**"calendar unresolved"**
: Weder Broker noch `exchange_calendars` konnten das Datum aufloesen, und der
  Bot faellt bei Kalenderzweifeln geschlossen aus. Meist ein zu altes
  `exchange_calendars`. `pip install -U exchange_calendars`.

## Es wird noch eine Position gehalten, obwohl die Boerse offen ist

Das ist das Szenario mit der schlechtesten Erwartung - das Intraday-Fenster ist
genau der Teil, den die Strategie meiden will.

1. `micronalgo tick` ausfuehren. Bei `on_missed_exit=market_at_open` (Standard)
   schickt der Bot dann eine Market-Order.
2. Passiert nichts, im Log nach `exit_not_confirmed` oder `exit_escalated`
   suchen.
3. Im Zweifel manuell beim Broker schliessen. Der Abgleich beim naechsten Start
   erkennt das und schliesst den Trade sauber ab.

## Die Einstiegs-Order ist in der Schlussauktion nicht gefuellt worden

Im Log steht `entry order did not fill`. Kein Notfall: es gibt keine Position,
also gibt es nichts auszusteigen. Der Bot markiert die Sitzung und macht am
naechsten Tag normal weiter. Haeuft sich das, ist der wahrscheinlichste Grund,
dass die Order zu spaet ankommt - `entry_submit_offset_min` erhoehen.

## Zwei Instanzen

Die zweite beendet sich mit Exit-Code 2 und nennt die PID der ersten. Wenn diese
PID nicht mehr existiert (harter Crash), ist der Lock schon frei - einfach neu
starten.

## Der Zustand ist beschaedigt

Der Bot laedt automatisch `state.json.bak`. Sind beide kaputt, startet er leer,
schreibt eine `RuntimeWarning` und sichert die kaputte Datei als
`state.json.corrupt`. Der Abgleich gegen den Broker rekonstruiert die Wahrheit.
Falls dabei eine nicht zuordenbare Position auftaucht, haelt er an - siehe oben.

## Regelmaessige Kontrolle

Woechentlich:
```bash
micronalgo status
grep -c '"event":"entry_submitted"' logs/audit.jsonl
grep '"event":"trade_closed"' logs/audit.jsonl | tail -20
```

Monatlich: `micronalgo study` mit aktuellen Daten neu laufen lassen und mit dem
letzten Bericht vergleichen. Kippt die Zeile *letzte 5 Jahre* auf FAIL, ist das
das Signal, dass der Effekt aufgehoert hat zu funktionieren - nicht ein Grund,
die Schwelle zu senken.

## Betrieb per cron

`tick` ist idempotent und dafuer gebaut:

```cron
*/5 13-21 * * 1-5  cd /pfad/zu/micronalgo && /pfad/zu/venv/bin/micronalgo tick --live >> logs/cron.log 2>&1
```

Das UTC-Fenster ist absichtlich grosszuegig, damit es Sommer- wie Winterzeit
abdeckt; welche Sitzungszeiten wirklich gelten, entscheidet der Kalender im
Programm, nicht der cron-Ausdruck.
