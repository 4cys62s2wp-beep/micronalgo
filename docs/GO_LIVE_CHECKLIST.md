# Checkliste vor echtem Geld

Diese Liste ist bewusst unangenehm. Sie existiert, weil der Weg von "der
Backtest sieht gut aus" zu "ich habe echtes Geld verloren" kurz ist und
ausschliesslich aus uebersprungenen Schritten besteht.

Echtes Geld ist im Code zusaetzlich gesperrt: `micronalgo` startet nur dann gegen
einen Nicht-Paper-Endpunkt, wenn `MICRONALGO_LIVE_TRADING_ACK` exakt auf
`I UNDERSTAND THIS IS REAL MONEY` steht. Ein Tippfehler in einer URL darf nie
genuegen.

## 1. Die Daten stimmen

- [ ] `micronalgo validate --check-fresh` meldet **null Fehler**.
- [ ] `micronalgo validate --cross-provider stooq yahoo` zeigt eine Korrelation
      der Nachtrenditen > 0,99 und einen Median-Unterschied unter 1,5 bps.
      Weichen zwei unabhaengige Provider voneinander ab, ist mindestens einer
      falsch, und die Studie kann auf keinem von beiden weitergehen.
- [ ] Der Bericht nennt Provider, Anpassungsart und Abrufzeitpunkt. Ein Ergebnis,
      das Du keiner konkreten Datei von einem konkreten Provider an einem
      konkreten Tag zuordnen kannst, ist kein Ergebnis.

## 2. Die Studie haelt stand

- [ ] `micronalgo study` liefert Gesamturteil **PASS** oder ein WARN, dessen
      Begruendung Du gelesen und akzeptiert hast.
- [ ] Die Zeile *letzte 5 Jahre* ist positiv. Faellt sie durch, ist die
      Schlagzeilenzahl Geschichte, keine Strategie.
- [ ] Die Zeile *Kante vs. effektiver Spread* liegt ueber 2x. Darunter ist die
      Nachtprämie nicht von der Frage zu trennen, auf welcher Seite des Spreads
      gedruckt wird.
- [ ] Die Zeile *Break-even-Kosten* liegt deutlich ueber dem, was Du real zahlst.
      Rechne mit der geometrischen Zahl im Bericht, nicht mit dem arithmetischen
      Mittel.
- [ ] `--variants` entspricht der **ehrlichen** Zahl aller ausprobierten
      Konfigurationen, verworfene eingeschlossen. Sonst ist der Deflated Sharpe
      wertlos.
- [ ] Du hast die Regime-Tabelle angesehen und weisst, welcher Anteil des
      Ergebnisses aus der Zeit vor 2001 stammt.
- [ ] Du hast den maximalen Drawdown gesehen und ehrlich beantwortet, ob Du ihn
      ausgehalten haettest, ohne abzuschalten. Jeder Backtest unterstellt still,
      dass Du das tust.

## 3. Die Ausfuehrung ist verifiziert, nicht angenommen

- [ ] `micronalgo preflight --probe-orders` laeuft **sauber gegen Dein echtes
      Paper-Konto**. Das ist der Schritt, der prueft, was beim Bauen nicht
      geprueft werden konnte.
- [ ] `order_type_cls` und `order_type_opg` stehen auf PASS. Ohne Auktionsorders
      ueberquerst Du 252-mal im Jahr einen Spread, und die Kante ist weg.
- [ ] `calendar_agreement` steht auf PASS. Der lokale Kalender muss dem Broker
      auf jeden Halbtag der naechsten 90 Tage genau entsprechen.
- [ ] Du hast einen Halbtag im Logbuch gesehen und geprueft, dass die
      Einstiegs-Order dort um 12:45 statt 15:45 ging.

## 4. Paper-Betrieb, mindestens ein volles Quartal

- [ ] Der Bot lief **mindestens 60 Handelstage** ohne manuellen Eingriff.
- [ ] Du hast die tatsaechlichen Ausfuehrungspreise mit den offiziellen
      Auktionspreisen desselben Tages verglichen. Weichen sie systematisch ab,
      hast Du keine Auktionsausfuehrung, egal was die Order sagt.
- [ ] Der Live-Ertrag stimmt mit dem Backtest ueber denselben Zeitraum ueberein.
      Eine grosse Luecke bedeutet, dass eine Annahme falsch ist - finde welche,
      bevor Du weitergehst.
- [ ] Es gab mindestens einen Neustart mitten im Betrieb, und der Abgleich hat
      ihn sauber aufgeloest.
- [ ] Es gab mindestens einen Earnings-Termin, und Du hast gesehen, wie gross die
      Nachtluecke war.
- [ ] Das Audit-Log ist vollstaendig: zu jeder Sitzung gibt es entweder eine
      Order oder einen protokollierten Grund, warum nicht.

## 5. Der Betrieb steht

- [ ] Der Not-Aus ist getestet: Datei anlegen, pruefen dass kein neuer Einstieg
      erfolgt, pruefen dass ein **Ausstieg trotzdem laeuft**.
- [ ] Du weisst, wie Du eine Position manuell schliesst, wenn der Bot steht.
- [ ] Benachrichtigungen kommen an einem Ort an, den Du morgens tatsaechlich
      ansiehst.
- [ ] Die Positionsgroesse ist eine, deren Totalverlust Dich nicht trifft.
- [ ] `leverage` steht auf 1.0. Mit Hebel beendet eine einzige -25%-Nachtluecke
      das Konto, und die gibt es bei MU.

## 6. Du hast die Gegenseite verstanden

- [ ] Du kannst in eigenen Worten sagen, warum die Kante existieren *koennte* -
      und warum sie vielleicht nur ein Mikrostruktur-Artefakt ist.
- [ ] Dir ist klar, dass das eine **einzelne Aktie** ist: idiosynkratisches
      Risiko, das Diversifikation entfernen wuerde, ohne Gegenleistung.
- [ ] Dir ist klar, dass Du bei ~252 Round-Trips pro Jahr ausschliesslich
      kurzfristige Gewinne erzeugst, mit der entsprechenden Steuerlast.
- [ ] Du hast eine vorher festgelegte Abbruchbedingung aufgeschrieben: *bei
      welchem Drawdown oder welcher Verlustserie hoere ich auf?* Vorher, nicht
      mittendrin.

---

Wenn eine Zeile offen ist, ist die Antwort "noch nicht". Es gibt keinen Zeitdruck:
Der Effekt ist seit 2008 publiziert. Ein Quartal Paper-Trading mehr kostet nichts.
