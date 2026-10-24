<p align="center">
  <a href="setup.en.md"><kbd>English</kbd></a>
  <a href="setup.ru.md"><kbd>Русский</kbd></a>
  <a href="setup.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="setup.ja.md"><kbd>日本語</kbd></a>
  <a href="setup.de.md"><kbd><strong>Deutsch</strong> ✓</kbd></a>
</p>

<p align="center">
  <a href="../../README.de.md">← README</a> ·
  <a href="features.de.md">Funktionen</a> ·
  <a href="architecture.de.md">Architektur</a>
</p>

# Start, Updates und Referenz

## Schnellstart aus dem Quellcode

Das Projekt wird unter **Windows mit Python 3.11** entwickelt und getestet. Git und Python werden benötigt; eine `.env`-Datei ist nicht erforderlich.

```powershell
git clone https://github.com/Donkiton/rem_card.git
cd rem_card
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python run_remcard.py
```

`run_remcard.py` öffnet die gemeinsame Anwendung mit Rollenauswahl.

> [!CAUTION]
> Verwenden Sie für Entwicklung und Tests einen separaten Datenordner statt der produktiven Datenbank. Sperren und Zugriffsprüfungen ersetzen keine isolierte Testumgebung. Die gemeinsame Anwendung verwendet die Sitzungszulassung `SessionLease`.

<details>
<summary><strong>Entwicklungsdatenbank: Auswahl und Schutz vor unbeabsichtigtem Ersatz</strong></summary>

Die aus dem Quellcode gestartete Version verwendet einen lokalen Datenordner für die Entwicklung; beim ersten Start kann die fehlende Verzeichnisstruktur angelegt werden. Die Liste ausgewählter Ordner und der aktive Pfad werden für jeden Checkout separat in `.remcard/dev_database_paths.json` gespeichert.

Eine ausgewählte vorhandene Datenbank wird mit einer Prüfung geöffnet: Die Anwendung darf nicht unbemerkt eine leere Datenbank anstelle einer verschwundenen anlegen. Der alte Pfad `%LOCALAPPDATA%\RemCard\dev_database_paths.json` wird nur zur einmaligen Übernahme der Einstellung verwendet.

</details>

## Installierte Version und Updates

Die Windows-Version für den Betrieb wird als vollständiger PyInstaller-Ordner verteilt, nicht als MSI oder Docker-Image.

| Datei | Zweck |
| --- | --- |
| `RemCard.exe` | Gemeinsame Anwendung: Rollenauswahl, Arbeitsplätze und Auswahl des Datenordners. |
| `RemCardUpdater.exe` | Hilfsprogramm zur Installation eines vollständigen Updates. |

Starten Sie **`RemCard.exe`**, wählen Sie beim ersten Start den Datenordner und anschließend die gewünschte Rolle. Bewahren Sie den vollständigen Programmordner einschließlich `_internal` auf: Die EXE allein reicht nicht aus. Neben der Anwendung wird `remcard_data_path.json` gespeichert; Updates werden in `<папка данных>\UPD\releases` gesucht, wobei `<папка данных>` für den gewählten Datenordner steht.

Die separaten Programme `RemCardDoctor.exe`, `RemCardNurse.exe`, die OP-EXE-Dateien und `RemCardPathSetup.exe` gehören zum früheren Auslieferungsmodell. Version 5.1 verwendet einen gemeinsamen Einstieg. Das Projekt verteilt ausschließlich **vollständige Updates**: `PATCH` in SemVer bezeichnet die Versionsstufe und kein separates Paketformat.

<details>
<summary><strong>Test-Build und sichere Veröffentlichung für den Produktivbetrieb</strong></summary>

Ein Build des aktuellen Arbeitsverzeichnisses ohne Veröffentlichung:

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py --test-worktree
```

Dieser führt die vorgeschriebenen Prüfungen, PyInstaller und einen Smoke-Test beider EXE-Dateien aus. Dabei werden `VERSION`, `CHANGELOG.md` und `app/release_info.json` nicht geändert, kein Commit erstellt, kein `git push` ausgeführt und kein Paket veröffentlicht. Das Ergebnis erhält die Kennzeichnung `TEST_WORKTREE_ONLY.txt` und kann deshalb nicht versehentlich für den Produktivbetrieb veröffentlicht werden.

Für eine reguläre Veröffentlichung müssen Version und exakter Commit zunächst in GitHub `main` vorliegen; anschließend startet die für die Veröffentlichung verantwortliche Person `scripts\build_release.py` mit `--expected-version` und `--expected-commit`. Nach der Abnahme mit einer isolierten Testdatenbank wird das Paket mit dem separaten Befehl `scripts\publish_full_update.py` übertragen. `ready.ok` wird zuletzt angelegt: Sobald diese Datei vorhanden ist, steht die Version den Clients dieser Datenbank sofort zur Verfügung. Einzelheiten: [Build und Veröffentlichung](../how_to_build_and_update.md) und [automatische Updates](../auto_update.md).

</details>

## Gemeinsame SQLite-Datenbank und Datensicherheit

Die medizinische Datenbank befindet sich unter `archiv/rao_journal.db`, Stammdaten und gemeinsame Einstellungen einschließlich Designs und Hintergründen unter `settings/remcard_settings.db`. Benutzerdefinierte Hintergrundbilder liegen in `settings/backgrounds`. Alle Pfade sind relativ zum ausgewählten Datenordner angegeben. Die JSON-Stammdatendateien im Repository dienen als Ausgangsdaten und Kompatibilitätsschicht, sind aber nicht die maßgebliche Datenquelle der installierten Version. Einzelheiten enthält die [Beschreibung der Einstellungsdatenbank](../settings_db.md).

Für die gemeinsame Datenbank im Netzwerk gelten folgende Regeln:

- `journal_mode=DELETE`, `synchronous=EXTRA`, `mmap_size=0`; WAL ist für die gemeinsame Datenbank verboten.
- Schreibvorgänge erfolgen über Controller, Dateisperre und Transaktionen.
- Eine laufende Datenbank wird ausschließlich über die SQLite Backup API gesichert, nicht durch einfaches Kopieren der geöffneten Datei.
- Für Migrationen, Wiederherstellung und Rotation sind überprüfte Sicherungen sowie `quick_check` und `integrity_check` vorgesehen.

Diese Maßnahmen verringern bekannte Risiken, machen aber nicht jede beliebige Ablage von SQLite im Netzwerk automatisch sicher. Zugriffsrechte, Latenzen, Sicherung und Wiederherstellung erfordern eine gesonderte betriebliche Abnahme. Siehe [Datenbanksicherheit](../db_safety_contract.md) und [Vertrag zur maßgeblichen Datenquelle und Synchronisierung](../source_of_truth_and_sync_contract.md).

## Prüfungen für die Entwicklung

Lokale Prüfungen und GitHub Actions verwenden denselben Einstiegspunkt. Nach der Installation der Abhängigkeiten in der aktivierten Umgebung:

```powershell
python scripts\run_ci_checks.py core
python scripts\run_ci_checks.py ui
python scripts\run_ci_checks.py quality
python scripts\run_ci_checks.py regression
git diff --check
```

`core` prüft Logik und Daten, `ui` die Qt-Oberfläche, `quality` die Codequalität und Architekturvorgaben und `regression` das vollständige Register der Sicherheitsszenarien. In CI laufen die Gruppen unabhängig; der abschließende Status `tests` prüft ihren Erfolg und die Vollständigkeit der Berichte. Die pytest-Module sind in [`tests/groups.json`](../../tests/groups.json) ohne Auslassungen oder Dopplungen aufgeteilt.

Der Befehl `python scripts\run_ci_checks.py all` führt alle pytest-Tests in einem Prozess aus, um ihre gemeinsame Isolation zu prüfen; er ersetzt die separaten Gruppen `quality` und `regression` nicht. Der Runner weist temporäre Pfade für Datenbanken und QSettings zu und speichert Berichte unter `tmp/ci-reports/<группа>`, wobei `<группа>` die Prüfgruppe bezeichnet. Für gezielte Prüfungen ist auch der gewöhnliche Aufruf `python -m pytest tests -q` verfügbar. Weitere Informationen: [Tests und CI](../testing.md).

Netzwerktestumgebungen, Wiederherstellungsübungen sowie Notfall- und Offline-Modi lesen oder ändern den ausgewählten Datenordner. Führen Sie diese nur mit einer bewusst ausgewählten Testdatenbank gemäß der [Abnahmeregelung](../operational_acceptance.md) und der [Anleitung zur Prüfung von Sicherungen](../backup_restore_drill.md) aus.


## Zur aktuellen Version

Übersicht zu **RemCard 5.1.10**, aktualisiert am **22. Oktober 2026**. RemCard wurde vom Anästhesisten und Intensivmediziner Vadim Bityutsky entwickelt. Die gemeinsame Anwendung `RemCard.exe` bietet die Auswahl zwischen vier Rollen; das separate Programm `RemCardUpdater.exe` installiert vollständige Updates. Die Quellcodeversion steht in [`VERSION`](../../VERSION), die Zusammensetzung einer konkreten Ausgabe in deren Manifest.

**Die Sprachumschaltung betrifft die Projektdokumentation.** Die Anwendungsoberfläche, die Screenshots und der überwiegende Teil der verlinkten ausführlichen Dokumentation sind weiterhin auf Russisch.

In [Version 5.1.10](https://github.com/Donkiton/rem_card/releases/tag/v5.1.10) können kleine Änderungen auch bei einer umfangreichen Historie als Delta an die lokale Replik übertragen werden. Das Abrufen des Patientenstatus über das Netzwerk wurde aus dem Oberflächenthread des Pflegearbeitsplatzes ausgelagert; verspätete Antworten dürfen neuere Daten nicht mehr ersetzen. Sichere Wiederholungsversuche beim Speichern des Anfangsstatus und das Laden der nächtlichen Sicherung nach Freigabe der Sperre der gemeinsamen Datenbank wurden präzisiert. Prüfungen der Zusammenarbeit zwischen ärztlichem und pflegerischem Arbeitsplatz sind im [Prüfbericht zur Synchronisierung](../sync_responsiveness_validation.md) beschrieben; die Regeln für die Replik und die Grenzen der Messungen im [Replik-Vertrag](../source_of_truth_and_sync_contract.md) und in der [Messmethodik](../replica_delta_benchmark.md).

Das fertige Paket 5.1.10 wurde so erstellt, dass Migrationen der gemeinsamen Datenbank und eine Anhebung der minimal zulässigen Clientversion gesperrt sind. Für kompatible Arbeitsplätze ist dies ein **optionales Update**: Es muss nicht auf allen Rechnern gleichzeitig installiert werden. Der erste vollständige Snapshot hängt weiterhin von der Netzwerkgeschwindigkeit und der Datenbankgröße ab.

> [!WARNING]
> **RemCard ist kein Medizinprodukt und ersetzt weder klinische Protokolle noch klinische Entscheidungen oder die verantwortliche Kontrolle durch medizinisches Fachpersonal.** Für den tatsächlichen Einsatz sind eine unabhängige technische, sicherheitstechnische und klinische Prüfung, die Abnahme in der vorgesehenen Umgebung sowie ein Ersatzarbeitsablauf erforderlich.

## Dokumentation, Status und Kontakt

- [Dokumentationsübersicht](../README.md) — geltende Verfahren und historische Unterlagen.
- [Projektwebsite](https://donkiton.github.io/rem_site/#english) und [Veröffentlichungen](https://github.com/Donkiton/rem_card/releases) — Projektvorstellung und veröffentlichte Versionen.
- [Interaktive Architekturkarte](https://donkiton.github.io/rem_card/codemap/codemap.html) — Module, Abhängigkeiten, Tests und durchgängige Abläufe mit Verweisen auf den Quellcode.
- [Änderungsverlauf](../../CHANGELOG.md) und [`VERSION`](../../VERSION) — Änderungen und aktuelle Version.
- [Tests und CI](../testing.md) — Testgruppen, Isolation, Vollständigkeitskontrolle und Berichte.
- [Verbrennungsrechner](../burn_infusion_calculator.md) und [Analysevertrag](../analytics_clinical_contract.md) — Berechnungen, Datenquellen und Einschränkungen.
- [Versionierung](../versioning.md), [automatische Updates](../auto_update.md) und [Build](../how_to_build_and_update.md) — Verfahren für vollständige Veröffentlichungen.
- [Vertrag zur Datenbanksicherheit](../db_safety_contract.md), [maßgebliche Datenquelle und Synchronisierung](../source_of_truth_and_sync_contract.md), [betriebliche Abnahme](../operational_acceptance.md) und [Notfallanleitung](../emergency_runbook.md) — sicherer Betrieb.

Das Projekt ist experimentell und wird aktiv weiterentwickelt. Tests, statische Prüfungen, Sicherungen und Schutzmechanismen ersetzen keine unabhängige technische, informationssicherheitstechnische und klinische Prüfung.

Für Fehlermeldungen und Vorschläge zu Architektur, Leistung und sicherem Betrieb: [menfise@mail.ru](mailto:menfise@mail.ru), Betreff: `РЕМ КАРТА`.

## Lizenz und Verantwortung

Urheber des Konzepts, der Architektur und des ursprünglichen Quellcodes sowie Entwickler von RemCard ist Vadim Aleksejewitsch Bityutsky (Битюцкий Вадим Алексеевич; GitHub: [DonKiton](https://github.com/DonKiton)).

RemCard wird unter der [RemCard Proprietary Source License 1.1](../../LICENSE) bereitgestellt, die natürlichen Personen eine nichtkommerzielle Nutzung gestattet. Einzelne Benutzer oder Entwickler dürfen den Code kostenlos ansehen, herunterladen, kopieren, ausführen, untersuchen und ändern sowie die ursprüngliche oder eine veränderte Version weitergeben, sofern sie dafür weder Geld noch einen anderen kommerziellen Vorteil erhalten. Bei der Weitergabe müssen die Lizenz und die Urheberangaben erhalten bleiben, vorgenommene Änderungen angegeben, der Quellcode bereitgestellt und der inoffizielle Status einer Modifikation klar kenntlich gemacht werden.

Jede Nutzung durch eine Organisation oder im Interesse eines Arbeitgebers, Kunden, Unternehmens, einer Klinik oder Einrichtung erfordert die vorherige schriftliche Erlaubnis des Rechteinhabers. Ohne gesonderte Vereinbarung sind Verkauf und Weiterverkauf, entgeltliche Einführung, Installation, Integration, Weiterentwicklung, Betreuung, Schulung, Beratung, Hosting, SaaS, Abonnements, Werbung und jede andere Monetarisierung verboten. Diese Einschränkungen gelten für alle abgeleiteten Versionen: Eine Änderung von Schriftart, Oberfläche, Funktionen, Namen oder anderen Programmteilen verleiht kein Recht, damit Geld zu verdienen.

Die öffentliche Bereitstellung und ein Fork innerhalb von GitHub gewähren keine kommerziellen Rechte. Das Programm wird „wie besehen“ bereitgestellt; die Nutzung erfolgt in eigener Verantwortung. Bibliotheken und Abhängigkeiten Dritter behalten ihre eigenen Lizenzen. Nach der OSI-Einteilung handelt es sich um eine Source-available-Lizenz und nicht um eine Open-Source-Lizenz, da die kommerzielle Nutzung eingeschränkt ist. Bei Abweichungen zwischen dieser Zusammenfassung und dem vollständigen Lizenztext hat die Datei [LICENSE](../../LICENSE) Vorrang.
