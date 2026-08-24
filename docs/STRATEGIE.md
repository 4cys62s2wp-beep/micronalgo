# Die Strategie, und was gegen sie spricht

## Was der Effekt ist

Die Rendite einer Aktie zerfaellt lueckenlos in zwei disjunkte Fenster:

```
r_on = O_t / C_(t-1) - 1     ca. 17,5 Stunden pro Werktag, Boerse geschlossen
r_id = C_t / O_t     - 1     6,5 Stunden, Boerse offen
```

Empirisch faellt bei vielen Aktien und Indizes praktisch die gesamte langfristige
Rendite in das erste Fenster, waehrend das zweite null oder negativ beitraegt.
Dokumentiert unter anderem bei Cooper, Cliff & Gulen (2008), Lachance (*Night
Trading*), Bogousslavsky (*The Cross-Section of Intraday and Overnight Returns*)
und Knuteson. MU ist ein besonders extremes Beispiel, weil die Aktie hohes Beta
und hohe idiosynkratische Volatilitaet hat.

## Warum es existieren koennte

Vier Erklaerungen, die ernst genommen werden:

1. **Risikoprämie fuer Gap-Risiko.** Wer ueber Nacht haelt, traegt ein Risiko, das
   sich nicht absichern laesst - die Boerse ist zu, es gibt keinen Stop-Loss.
   Dafuer will man bezahlt werden. Gegen diese Erklaerung spricht, dass die Praemie
   nicht mit der Laenge des Fensters skaliert: die Montagsnacht umfasst drei
   Kalendertage und zahlt trotzdem nicht das Dreifache.
2. **Informationsfluss.** Unternehmensmeldungen erscheinen fast alle nach
   Boersenschluss. Wer die Nacht haelt, haelt die Nachrichten.
3. **Nachfragedruck.** Marktorders von Privatanlegern sammeln sich ueber Nacht
   und werden in der Eroeffnungsauktion abgearbeitet, was die Eroeffnung nach oben
   drueckt.
4. **Positionsmanagement von Intermediaeren.** Wer intraday Liquiditaet stellt,
   moechte flach in die Nacht - und verkauft dafuer gegen Ende des Tages.

## Warum es trotzdem nicht handelbar sein koennte

In der Reihenfolge, in der ich sie fuer gefaehrlich halte:

**1. Transaktionskosten.** 252 Round-Trips pro Jahr. Bereits 10 bps pro Round-Trip
kosten 22 % jaehrlich. Das ist der Grund, warum die Auktionsausfuehrung nicht
optional ist, sondern die gesamte These traegt.

**2. Bid-Ask-Bounce.** Wenn der Schlusskurs systematisch auf dem Geldkurs und der
Eroeffnungskurs auf dem Briefkurs druckt, entsteht

```
r_on = (1 + s/2) / (1 - s/2) - 1  ~=  s
```

jede einzelne Sitzung, mit exakt spiegelbildlichem Abzug intraday - **ohne dass
irgendeine Oekonomie dahintersteht**. Der Bericht schaetzt den effektiven Spread
nach Corwin-Schultz aus Hoch/Tief und vergleicht die Kante damit.

Wichtig und deshalb ausdruecklich gesagt: Tagesdaten enthalten keine Quotes. Diese
Pruefung kann das Artefakt **nicht ausschliessen**, sie kann nur sagen, ob es gross
genug waere, um die Kante zu erklaeren. Endgueltig klaeren laesst sich das nur mit
Quote- oder Minutendaten.

**3. Die Aera.** Vor der Dezimalisierung im April 2001 betrug die minimale
Kursstufe 1/16, davor 1/8 Dollar. Auf einer Aktie zu 10 $ ist das ein Spread von
125 bps. Ein Effekt, der dort gemessen wird, ist von Mikrostruktur nicht zu
trennen. Zusaetzlich: Nasdaqs Closing Cross startete erst um 2004 - davor gab es
fuer ein Nasdaq-Papier wie MU gar keine Schlussauktion, in der eine MOC-Order
haette ausgefuehrt werden koennen. Alles vor diesem Datum ist historisch
interessant und operativ bedeutungslos.

**4. Zerfall.** Der Effekt ist seit 2008 publiziert. Publizierte Anomalien werden
typischerweise schwaecher. Die Zeile *letzte 5 Jahre* im Bericht ist deshalb der
wichtigste Einzeltest.

**5. Das Risikoprofil.** Man haelt jede Earnings-Luecke, vier pro Jahr, ohne
Ausstiegsmoeglichkeit. MU hatte wiederholt zweistellige Nachtluecken. Mit Hebel
beendet eine einzige davon das Konto.

**6. Kapitaleffizienz.** Man ist nur ~17,5 von 168 Wochenstunden im Markt. Der
Sharpe kann hervorragend aussehen, waehrend die absolute Rendite mager bleibt -
und wer das mit Hebel loest, holt sich genau das Gap-Risiko zurueck, das eben
noch das Argument fuer die Praemie war.

**7. Konzentration.** Wenn zehn von achttausend Sitzungen den Grossteil des
Ergebnisses tragen, ist die Erwartung fuer die Zukunft eine Frage darueber, ob
solche Tage wiederkommen. Der Bericht misst das direkt.

**8. Ein Papier, eine Strategie.** Kein Diversifikationseffekt, dafuer das volle
idiosynkratische Risiko - ein Werksbrand, eine Uebernahme, ein Speicherzyklus.

## Wie dieses Repository damit umgeht

Es faellt kein Urteil im Voraus. Es rechnet jeden dieser Punkte aus, stellt eine
Schwelle daneben und sagt PASS, WARN oder FAIL. Das Gesamturteil ist das
schlechteste Einzelurteil, und der Bericht setzt es **vor** die Ertragskurve.

Was das Repository *nicht* leistet: es kann nicht entscheiden, ob die
Nachtprämie eine echte Risikoprämie oder ein Druckseiten-Artefakt ist. Diese
Grenze steht im Bericht, statt weggelassen zu werden.
