<p align="center">
  <a href="README.md"><kbd>Русский</kbd></a>
  <a href="README.en.md"><kbd>English</kbd></a>
  <a href="README.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="README.ja.md"><kbd>日本語</kbd></a>
  <a href="README.de.md"><kbd><strong>Deutsch</strong> ✓</kbd></a>
</p>

<p align="center">
  <img src="docs/assets/readme/remcard-hero.de.svg" width="100%" alt="RemCard ICU — eine gemeinsame Patientenkurve für Intensivstation und OP, von 08:00 bis 08:00 Uhr">
</p>

<p align="center">
  <strong>Patientendokumentation und gemeinsame Schichtarbeit auf der Intensivstation und im OP.</strong><br>
  Eine Windows-Anwendung für ärztliches Personal, Pflege sowie elektive und Notfall-OP-Teams.
</p>

<p align="center">
  <a href="VERSION">v5.1.10</a> · Windows · Python 3.11 · PySide6 · SQLite<br>
  <a href="https://github.com/Donkiton/rem_card/releases">Veröffentlichungen</a> ·
  <a href="#weiterführende-dokumentation">Dokumentation</a> ·
  <a href="https://donkiton.github.io/rem_site/#english">Website</a> ·
  <a href="LICENSE">Lizenz</a>
</p>

> [!WARNING]
> **Experimentelle Software, kein Medizinprodukt.** Der klinische Einsatz erfordert eine unabhängige technische und klinische Prüfung, die Abnahme in der vorgesehenen Umgebung und einen Ersatzarbeitsablauf.

## Einblick in die Oberfläche

Demonstrationspatienten · Screenshots aus **5.1.0** · aktuelle Version **5.1.10** · Oberfläche auf Russisch. Klappen Sie eine Gruppe auf, um die Bilder anzuzeigen.

<details>
<summary><strong>Arbeitsplätze und Rollenauswahl</strong></summary>

[![Ärztlicher Arbeitsplatz, Pflegearbeitsplatz sowie elektiver und Notfall-OP](docs/assets/readme/remcard-roles-5-1.jpg)](docs/assets/readme/remcard-roles-5-1.jpg)

Vier Arbeitsplätze mit gemeinsamem Einstieg sowie hellem und dunklem Design.

</details>

<details>
<summary><strong>Patienten, Betten und Aufnahmen</strong></summary>

[![Aktuelle Patienten und Zugang zu ihren Kurven](docs/assets/readme/remcard-patients-5-1.jpg)](docs/assets/readme/remcard-patients-5-1.jpg)

[![Belegte und freie Betten mit Patientendaten](docs/assets/readme/remcard-beds-5-1.jpg)](docs/assets/readme/remcard-beds-5-1.jpg)

Bettenstatus, Aufnahmen und schneller Zugriff auf die Patientenkurve.

</details>

<details>
<summary><strong>Medikamentenanordnungen und Flüssigkeitsbilanz</strong></summary>

[![Tägliche Medikamentenanordnungen und Durchführungsnachweise](docs/assets/readme/remcard-orders-5-1.jpg)](docs/assets/readme/remcard-orders-5-1.jpg)

[![Flüssigkeitseinfuhr, Ausfuhr und stündliche Bilanz](docs/assets/readme/remcard-balance-5-1.jpg)](docs/assets/readme/remcard-balance-5-1.jpg)

Anordnungen, Durchführungsnachweise und Flüssigkeitsbilanz über die Schicht von 08:00 bis 08:00 Uhr.

</details>

## Auf einen Blick

- **Patientenkurve** — Anordnungen und Durchführung, Vitalparameter, Flüssigkeitsbilanz, Beatmung, Ernährung, Eingriffe und Laborwerte.
- **Zusammenarbeit im Team** — ärztliche und pflegerische Arbeitsplätze, elektiver und Notfall-OP sowie Übergabe zwischen Intensivstation und OP.
- **Klinische Werkzeuge** — Infusions-, Elektrolyt- und Verbrennungsrechner sowie PDF-Berichte.
- **Archiv und Analysen** — Patienten- und OP-Fallhistorie, Filter, Vergleiche und Diagramme.

## Erste Schritte

Starten Sie bei einer installierten Version **`RemCard.exe`** aus dem vollständigen Programmordner einschließlich `_internal`.

Den Start aus dem Quellcode, die Auswahl des Datenordners und Updates beschreibt die [Startanleitung](docs/guide/setup.de.md). Entwicklung und Tests erfordern eine separate Testdatenbank.

## Weiterführende Dokumentation

| Anleitung | Inhalt |
| --- | --- |
| [Funktionen](docs/guide/features.de.md) | Arbeitsplätze, Patientenkurve, Ernährung, Rechner und Analysen. |
| [Start und Wartung](docs/guide/setup.de.md) | Start aus dem Quellcode, Installation, Updates, Datensicherheit und Entwicklerprüfungen. |
| [Architektur](docs/guide/architecture.de.md) | Gemeinsame Daten, Synchronisierung und interaktive Architekturkarte. |

Ausführliche technische Verfahren sind im [Dokumentationsverzeichnis](docs/README.md) aufgeführt, überwiegend auf Russisch.

<p align="center">
  <a href="https://cliniciansthatcode.com/directory/#p-vadim-bityutsky"><img src="docs/assets/readme/clinicians-that-code-verified.svg" width="260" height="28" alt="Eintrag des RemCard-Autors im Verzeichnis Clinicians That Code"></a><br>
  <sub>Entwickelt vom Anästhesisten und Intensivmediziner Vadim Bityutsky · <a href="mailto:menfise@mail.ru">Kontakt</a></sub>
</p>
