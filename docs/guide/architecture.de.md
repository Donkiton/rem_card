<p align="center">
  <a href="architecture.ru.md"><kbd>Русский</kbd></a>
  <a href="architecture.en.md"><kbd>English</kbd></a>
  <a href="architecture.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="architecture.ja.md"><kbd>日本語</kbd></a>
  <a href="architecture.de.md"><kbd><strong>Deutsch</strong> ✓</kbd></a>
</p>

<p align="center">
  <a href="../../README.de.md">← README</a> ·
  <a href="features.de.md">Funktionen</a> ·
  <a href="setup.de.md">Start</a>
</p>

# Architektur und Synchronisierung

## So funktioniert das gemeinsame System

<p align="center">
  <a href="https://donkiton.github.io/rem_card/codemap/codemap.html"><img src="../assets/readme/remcard-workflow.de.svg" width="100%" alt="Ärztlicher Arbeitsplatz, Pflege und OP nutzen einen gemeinsamen Datenordner mit medizinischer Datenbank, Einstellungen und Sicherungen"></a>
</p>

RemCard ist eine **PySide6**-Desktopanwendung mit einem gemeinsamen Datenordner. Die gemeinsame SQLite-Datenbank liegt im lokalen Netzwerk der Organisation. Für den regulären Betrieb der Arbeitsplätze wird kein separater Webserver benötigt. Der Hauptpfad lautet **Oberfläche → Dienste → DAO → SQLite**.

1. **Speichern.** Die Oberfläche übergibt eine Änderung an die Schreibwarteschlange. Der Controller erwirbt eine Dateisperre und führt eine Transaktion aus; erfolgreiches Speichern wird erst nach `COMMIT` in der zentralen Datenbank bestätigt. Ein Entwurf oder ein Eintrag in der Warteschlange bedeutet für sich genommen noch nicht, dass die Daten gespeichert sind.
2. **Aktualisierung anderer Arbeitsplätze.** Trigger schreiben in derselben Transaktion Benachrichtigungen in `change_log`. `DataUpdateMonitor` erkennt Änderungen, `SyncCoordinator` verwirft veraltete Daten, und der Arbeitsplatz liest den benötigten Snapshot erneut ein. Cache-Dateien werden nicht zwischen Rechnern übertragen.
3. **Schneller Lesezugriff.** `ReadCoordinator`, Snapshots und ein lokaler Cache beschleunigen die Anzeige. Die erste lokale Replik entsteht über die SQLite Backup API; danach werden kleine Änderungen auf eine separate lokale Kandidatenkopie angewendet. Diese wird geprüft und ersetzt die bisherige Kopie atomar; bei Überschreitung von Grenzwerten oder Inkompatibilität erfolgt eine vollständige Kopie. Die Replik dient ausschließlich dem Lesen. Liegt ihre Version hinter dem erforderlichen Stand zurück, wird aus der zentralen Datenbank gelesen. Maßgeblich für klinische Daten bleibt der zentrale Datensatz nach `COMMIT`.
4. **Reaktionsfähigkeit.** Das Laden von Snapshots, Schreibvorgänge über das Netzwerk, Analysen und PDF-Erstellung nutzen Hintergrundaufgaben. Verspätete Ergebnisse werden geprüft, bevor sie auf den geöffneten Patienten oder den ausgewählten Zeitraum angewendet werden.

### Wo sich die Implementierung befindet

| Verzeichnis | Zuständigkeit |
| --- | --- |
| [`ui/`](../../ui/) | Ärztliche und pflegerische Arbeitsplätze, OP, Bereiche der Patientenkurve, Archiv, Eingriffe und Oberflächeneinstellungen. |
| [`services/`](../../services/) | Anwendungsoperationen, Berechnungen, Bilanz, Ernährung sowie Koordination von Lesezugriffen und Synchronisierung. |
| [`services/analytics/`](../../services/analytics/) | Kennzahlenregister, Abfragen, Kohorten, Vergleiche, Statistiken und Diagrammdaten. |
| [`data/`](../../data/) | DAO, Modelle, Stammdaten und separate Einstellungsdatenbank. |
| [`app/`](../../app/) | Start und Rollen, Pfade, SQLite-Infrastruktur, Replik, Sicherungen, Notfallmodi, Protokolle und Updates. |
| [`scripts/`](../../scripts/), [`tests/`](../../tests/) | Build und Veröffentlichung, CI, Qualitätsprüfungen, automatisierte Tests und Sicherheitsszenarien. |

Der ausführliche [Vertrag zur maßgeblichen Datenquelle und Synchronisierung](../source_of_truth_and_sync_contract.md) beschreibt Transaktionsgrenzen, Aktualität der Replik und Cache-Regeln.

## Interaktive Architekturkarte

<p align="center">
  <a href="https://donkiton.github.io/rem_card/codemap/codemap.html"><strong>Interaktive RemCard-Karte öffnen →</strong></a><br>
  <sub>24 Hauptmodule · 48 belegte Verbindungen · 6 durchgängige Abläufe</sub>
</p>

Die Karte wurde am **23. September 2026** für die Architektur der gemeinsamen Anwendung aktualisiert: Start über `run_remcard.py`, Rollenauswahl, Datenbankzulassung, Hintergrundprüfung, Sitzungsende, Lesereplik und vollständiges Update. Sie zeigt die Hauptkomponenten und ist kein Verzeichnis sämtlicher Dateien; der zugrunde liegende Commit ist auf der Karte angegeben. Ein Klick auf ein Modul zeigt eingehende und ausgehende Verbindungen, zugehörige Tests, Quellpfade und die als Belege dienenden Symbole. Außerdem lassen sich durchgängige Abläufe auswählen, Komponenten suchen, Modultypen filtern sowie die Darstellung vergrößern, verkleinern und verschieben.

Die Karte läuft vollständig im Browser und sendet keine Daten an einen externen Server. Die überprüfbaren Ausgangsdaten stehen in [`codemap.json`](../codemap/codemap.json), die eigenständige HTML-Version in [`codemap.html`](../codemap/codemap.html).
