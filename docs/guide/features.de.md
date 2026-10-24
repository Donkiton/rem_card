<p align="center">
  <a href="features.en.md"><kbd>English</kbd></a>
  <a href="features.ru.md"><kbd>Русский</kbd></a>
  <a href="features.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="features.ja.md"><kbd>日本語</kbd></a>
  <a href="features.de.md"><kbd><strong>Deutsch</strong> ✓</kbd></a>
</p>

<p align="center">
  <a href="../../README.de.md">← README</a> ·
  <a href="setup.de.md">Start</a> ·
  <a href="architecture.de.md">Architektur</a>
</p>

# Funktionen und Arbeitsbereiche

## Rollen in einem gemeinsamen Arbeitsablauf

- **Ärztlicher Arbeitsplatz** — Aufnahmen und Betten, Patientenkurve nach Schichten, Anordnungen und Vorlagen, Ernährungsplanung, Eingriffe, Behandlungsausgänge, Archiv, Berichte und Rechner.
- **Pflegearbeitsplatz** — anstehende Anordnungen, Durchführungsnachweise, tatsächlich aufgenommene Nahrung, Verlauf während der Schicht, Druck und Arbeitsstatistik.
- **OP** — Notfall- und elektiver OP, Schnellanordnungen, Medikamente, Operationsphasen und Ereignisse, Verlegung zwischen Intensivstation (РАО) und OP, Fallabschluss und Berichte.
- **Administration** — zentrale Einstellungen, Zustand der aktuellen und archivierten Datenbanken, Sicherungen, Stammdaten, Druck, Darstellung und Diagnose.

## Funktionen der aktuellen Version

### Gemeinsame Anwendung 5.1

Ärztlicher Arbeitsplatz, Pflegearbeitsplatz, elektiver und Notfall-OP werden aus einem gemeinsamen Fenster geöffnet. Beim Rollenwechsel schließt die Anwendung die bereits angenommenen Schreibvorgänge ab und gibt die Ressourcen der vorherigen Sitzung frei. Die Datenbankprüfung und die Vorbereitung des Arbeitsplatzes zeigen ihren jeweiligen Status an; der Einstieg kann abgebrochen werden. Die Startdiagnose hilft, Verzögerungen beim Öffnen der Anwendung und der jeweiligen Rolle zu untersuchen.

Die Auswahl des Datenordners ist in die Anwendung integriert. Ein gemeinsamer Wartungsmodus koordiniert den Datenbankzugriff der Arbeitsplätze. Ist die zentrale Datenbank nicht erreichbar, prüft der Notfallzugang, ob eine lokale Kopie vorhanden, verwendbar und ausreichend aktuell ist. Die Rückkehr zum Netzwerkbetrieb und die Übertragung der Änderungen erfolgen in einem gesonderten Ablauf.

### Patientenkurve und Schichtarbeit

Die Patientenkurve umfasst Anordnungen und deren Durchführung, Vitalparameter und Diagramme, Flüssigkeitseinfuhr und -ausfuhr, Beatmung, Eingriffe, Laboruntersuchungen und klinische Ereignisse. Der Zugriff auf die aktuelle Kurve, die Kurve des Vortags und die vorausgeplante Kurve ist getrennt; die Patientenhistorie ist über das Archiv zugänglich. Für Anordnungen stehen Stammdaten für Medikamente, Lösungsmittel und Applikationswege sowie Vorlagen und ein Schichtzeitplan zur Verfügung.

Auf der Registerkarte **„Ernährung“ («Диета»)** legt das ärztliche Personal den Ernährungsplan fest und wendet bearbeitbare Vorlagen an; die Pflege dokumentiert die tatsächliche Aufnahme. Eine verweigerte Zufuhr lässt sich als **0 ml** speichern. Dokumentierte Ernährungsmengen fließen in die Flüssigkeitsbilanz ein; eigene Änderungen können schrittweise rückgängig gemacht werden. Eine Prüfung der Datensatzversion verhindert, dass Änderungen anderer Benutzer überschrieben werden.

Dokumente für Eingriffe, darunter zentralvenöse Katheter, Lumbalpunktionen und Bluttransfusionen, sowie PDF-Berichte zur Patientenkurve und zum OP-Fall stehen zur Verfügung.

### Rechner und Überwachung der Flüssigkeitstherapie bei Verbrennungen

Das Menü **„Berechnung“ («Расчёт»)** in Sektor 8 vereint Rechner für Infusionen, Elektrolyte und Verbrennungen. Der Verbrennungsrechner ist für ärztliches und pflegerisches Personal aus der Patientenkurve zugänglich, wenn eine entsprechende ICD-Diagnose vorliegt.

Er berücksichtigt Alter, Gewicht, verbrannte Körperoberfläche und Verletzungszeitpunkt und unterstützt den ersten, zweiten und dritten Tag sowie die Zeit nach Überwindung des Schocks. Tatsächliche Flüssigkeitszufuhr und Urinausscheidung werden aus den gespeicherten Kurvendaten geladen; bei den pädiatrischen Schemata für den ersten sowie den zweiten und dritten Tag wird auch die tatsächliche enterale Zufuhr berücksichtigt. Das Fenster zeigt Berechnungszeitraum, Überwachungszeitpunkt, Restvolumen und Abweichung vom berechneten Zeitplan. Der Tag der Patientenkurve und der seit der Verletzung vergangene Tag werden getrennt erfasst.

Die Daten werden beim Öffnen des Rechners geladen. Der Befehl **„Aus Kurve aktualisieren“ («Обновить из карты»)** liest sie erneut ein und behält dabei die als manuell markierten Werte bei. Ein Lesefehler wird nicht als Nullvolumen behandelt; eine Änderung der Eingabedaten verwirft das vorherige Ergebnis. Die Berechnung lässt sich mit Erläuterungen kopieren; Anordnungen werden nicht automatisch angelegt. Ausführliche Regeln und Einschränkungen enthält die [Dokumentation des Rechners](../burn_infusion_calculator.md).

### Gemeinsames Archiv und Analysen

Die Archivzentrale steht ärztlichem Personal, Pflege und Administration entsprechend ihren Rollen zur Verfügung. Sie vereint das Patientenarchiv der Intensivstation, das Archiv der OP-Fälle, Analysen, Diagramme und statistische Berichte. Zum Anzeigen einer Kurve aus einer Archivdatenbank wird ein eigener Lesedienst verwendet.

Für Analysen können Zeitraum und Patientengruppe gewählt, Filter angewendet, Ansichten gespeichert sowie Zeiträume oder Gruppen verglichen werden. Zu jeder Kennzahl sind das Ergebnis, die Berechnungsmethode und die Liste der eingeschlossenen Fälle verfügbar. Ein gemeinsames Kennzahlenregister legt Einheiten, Quellen und Einschlusskriterien fest; die Datenqualitätsprüfung erkennt Duplikate und widersprüchliche Werte. Fehlende Vergleichsdaten werden ausdrücklich gekennzeichnet. Berechnungen und Diagrammerstellung laufen im Hintergrund; Berichte und Diagramme verwenden einen konsistenten Analysesnapshot.

Definitionen der Kennzahlen und Grenzen der technischen Prüfung sind im [Analysevertrag](../analytics_clinical_contract.md) und in der [Implementierungsmatrix](../analytics_implementation_matrix.md) beschrieben.

### OP, Einstellungen und Diagnose

Die Arbeitsplätze für Notfall- und elektive Operationen dokumentieren den OP-Fall: Team, Anästhesieverfahren, Phasen, Messwerte, Medikamente und Schnellanordnungen. Die Übergabe von der Intensivstation ist mit der Patientenverlegung und der Aufnahme im OP verknüpft. Bei Netzwerkproblemen steht ein gesonderter, eingeschränkter lokaler OP-Modus zur Verfügung; seine Daten werden nach Wiederherstellung der Verbindung in einem speziellen Verfahren übertragen.

Die Einstellungszentrale vereint Stammdaten, OP-Parameter, Ernährungsvorlagen, Laborkatalog, Druck, Designs, Hintergründe und Datenbankwartung. Benutzeranfragen können Diagnoseprotokolle enthalten. Für technische Protokolle gelten Rotation, Aufbewahrungsfristen und ein Größenbudget; häufige reguläre Messwerte werden zu Minutenzusammenfassungen verdichtet, wiederholte Fehler zu kompakten Einträgen mit Kontext. Das medizinische Änderungsprotokoll wird getrennt von technischen Protokollen und Synchronisierungsmeldungen gespeichert.


<details>
<summary><strong>Screenshots und Hinweise zur Bedienung</strong></summary>

## Die Anwendung im Einsatz

Screenshots von **RemCard 5.1.0** mit Demonstrationspatienten; die aktuelle Version ist **5.1.10**. Ein Klick auf ein Bild öffnet es in voller Größe.

### Gemeinsamer Einstieg und Rollenauswahl

[![RemCard 5.1.0: ärztlicher Arbeitsplatz, Pflegearbeitsplatz sowie elektiver und Notfall-OP](../assets/readme/remcard-roles-5-1.jpg)](../assets/readme/remcard-roles-5-1.jpg)

Eine Anwendung für vier Arbeitsplätze. Die Schaltfläche **„Rollen“ («Роли»)** führt zurück zur Rollenauswahl; auf dem Anmeldebildschirm stehen ein helles und ein dunkles Design zur Verfügung.

### Patientenliste

[![Patientenliste in RemCard 5.1.0: Aufnahmedaten und Zugang zur Patientenkurve](../assets/readme/remcard-patients-5-1.jpg)](../assets/readme/remcard-patients-5-1.jpg)

Aktuelle Patienten nach Betten, die letzten Messwerte und direkter Zugriff auf Patientenkurve und Berichte.

<details>
<summary><strong>Bettenverwaltung und Patientenaufnahme</strong></summary>

### Patientenverwaltung

[![Belegte und freie Betten mit den Angaben zum ausgewählten Patienten](../assets/readme/remcard-beds-5-1.jpg)](../assets/readme/remcard-beds-5-1.jpg)

Die Schaltfläche **„Patient hinzufügen“ («Добавить пациента»)** öffnet die Bettenverwaltung. Wählen Sie ein freies Bett und klicken Sie auf „BETT BELEGEN“ («ЗАНЯТЬ КОЙКУ»), um die Aufnahme zu erfassen. Bei einem belegten Bett können die Patientendaten geöffnet werden.

</details>

### Anordnungsblatt

[![Tägliches Anordnungsblatt in RemCard 5.1.0 mit dokumentierter Durchführung](../assets/readme/remcard-orders-5-1.jpg)](../assets/readme/remcard-orders-5-1.jpg)

Medikamente, Applikationswege, Zeitplan und Durchführungsnachweise auf der Schichtachse von 08:00 Uhr bis 08:00 Uhr des Folgetags.

### Flüssigkeitsbilanz

[![Einfuhr, Ausfuhr und stündliche Flüssigkeitsbilanz in RemCard 5.1.0](../assets/readme/remcard-balance-5-1.jpg)](../assets/readme/remcard-balance-5-1.jpg)

Infusionen, Medikamente, Blut, Plasma und orale Zufuhr werden der Ausfuhr gegenübergestellt; eine Stundentabelle und die Gesamtbilanz sind daneben verfügbar.

<!-- Ein neuer Screenshot der Vitalparameterkurven aus Version 5.1.0 wird dieser Galerie gesondert hinzugefügt. -->

</details>
