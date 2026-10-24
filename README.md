<p align="center">
  <a href="README.md"><kbd><strong>English</strong> ✓</kbd></a>
  <a href="README.ru.md"><kbd>Русский</kbd></a>
  <a href="README.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="README.ja.md"><kbd>日本語</kbd></a>
  <a href="README.de.md"><kbd>Deutsch</kbd></a>
</p>

<p align="center">
  <img src="docs/assets/readme/remcard-hero.en.svg" width="100%" alt="RemCard ICU — one patient record across the ICU and operating room, from 08:00 to 08:00">
</p>

<h1 align="center">RemCard ICU — intensive care charting</h1>

<p align="center">
  <strong>A Windows application for patient records and shared shift work in the ICU and operating room.</strong><br>
  Orders, vital signs, fluid balance, ventilation, procedures, laboratory results, events, reports and archives in one workflow.
</p>

<p align="center">
  <code>Version 5.1.10</code> · <code>Windows</code> · <code>Python 3.11</code> · <code>PySide6</code> · <code>SQLite</code> · <code>RemCard Proprietary Source License</code> · <code>Experimental project</code>
</p>

<p align="center">
  <a href="https://cliniciansthatcode.com/directory/#p-vadim-bityutsky"><img src="docs/assets/readme/clinicians-that-code-verified.svg" width="260" height="28" alt="Clinicians That Code: verified builder — RemCard author's directory listing"></a><br>
  <sub>RemCard's author is listed in the Clinicians That Code directory. This does not establish clinical validation of the software.</sub>
</p>

<p align="center">
  <a href="#see-it-in-use">Screenshots</a> ·
  <a href="#current-features">Features</a> ·
  <a href="#how-the-shared-workflow-works">Architecture</a> ·
  <a href="#quick-start-from-source">Run from source</a> ·
  <a href="#installed-version-and-updates">Installation and updates</a> ·
  <a href="docs/README.md">Documentation</a> ·
  <a href="CHANGELOG.md">Changelog</a> ·
  <a href="LICENSE">License</a>
</p>

**README language:** use the links above for a complete translated README. The application interface, screenshots and linked technical documentation are primarily in Russian.

Overview of **RemCard 5.1.10**, updated **October 22, 2026**. A single `RemCard.exe` opens a choice of four roles; the separate `RemCardUpdater.exe` installs full updates. The source version is recorded in [`VERSION`](VERSION), and each build's contents are recorded in its manifest.

In [release 5.1.10](https://github.com/Donkiton/rem_card/releases/tag/v5.1.10), small changes can reach the local replica as deltas even when the change history is large. Network reads of patient status run outside the nurse interface; late responses cannot replace newer data. The release also refines safe retries when saving initial status and loading the nightly backup after the shared database lock is released. Doctor–nurse collaboration checks are documented in the [synchronization validation report](docs/sync_responsiveness_validation.md); replica rules and measurement limits are described in the [replica contract](docs/source_of_truth_and_sync_contract.md) and [benchmark methodology](docs/replica_delta_benchmark.md).

The packaged 5.1.10 build disables both shared-database migrations and any increase in the minimum required client version. It is an **optional update** for compatible workstations: there is no need to update every computer at the same time. The initial full snapshot still depends on network speed and database size.

> [!WARNING]
> **RemCard is not a medical device and does not replace clinical protocols, clinical decisions or responsible professional oversight.** Real-world use requires independent technical and clinical review, acceptance testing in the intended environment, and a backup workflow.

## See it in use

Screenshots show **RemCard 5.1.0** with demonstration patients; the current release is **5.1.10**. Click an image to open it at full size. The interface shown is in Russian.

### One entry point and role selection

[![RemCard 5.1.0: doctor, nurse, planned and emergency operating rooms](docs/assets/readme/remcard-roles-5-1.jpg)](docs/assets/readme/remcard-roles-5-1.jpg)

One application for four workspaces. The **Roles** («Роли») button returns to role selection; light and dark themes are available on the entry screen.

### Patient list

[![RemCard 5.1.0 patient list: admission information and links to patient charts](docs/assets/readme/remcard-patients-5-1.jpg)](docs/assets/readme/remcard-patients-5-1.jpg)

Active patients by bed, recent observations, and quick access to charts and reports.

<details>
<summary><strong>Bed management and patient admission</strong></summary>

### Patient management

[![Occupied and available beds with the selected patient's details](docs/assets/readme/remcard-beds-5-1.jpg)](docs/assets/readme/remcard-beds-5-1.jpg)

The **Add patient** («Добавить пациента») button opens bed management. Select a free bed and click **Occupy bed** («ЗАНЯТЬ КОЙКУ») to record an admission. An occupied bed provides access to the patient's details.

</details>

### Medication orders

[![RemCard 5.1.0 daily medication orders with administration tracking](docs/assets/readme/remcard-orders-5-1.jpg)](docs/assets/readme/remcard-orders-5-1.jpg)

Medications, administration routes, schedules and completion marks on a shift timeline from 08:00 to 08:00 the following day.

### Fluid balance

[![RemCard 5.1.0 fluid intake, output and hourly balance table](docs/assets/readme/remcard-balance-5-1.jpg)](docs/assets/readme/remcard-balance-5-1.jpg)

Infusions, medications, blood, plasma and oral intake are compared with output, alongside an hourly table and total balance.

<!-- A new screenshot of the 5.1.0 vital-sign charts will be added to this gallery separately. -->

## Roles in one workflow

- **Doctor** — admissions and beds, shift-based patient charts, orders and templates, diet prescriptions, procedures, outcomes, archives, reports and calculators.
- **Nurse** — upcoming orders, completion records, actual nutritional intake, shift trends, printing and work statistics.
- **Operating room** — emergency and planned workspaces, quick orders, medications, surgical stages and events, ICU–OR patient handover, case completion and reports.
- **Administrator** — a unified settings center, current and archived database status, backups, reference catalogs, printing, appearance and diagnostics.

## Current features

### Unified 5.1 application

Doctor, nurse, planned OR and emergency OR workspaces open from one window. When switching roles, the application finishes accepted writes and releases the previous session's resources. Database checks and workstation preparation show their progress; entry can be cancelled. Startup diagnostics help investigate delays when opening the application or a role.

Data-folder setup is built into the application. Shared maintenance mode coordinates workstation access to the database. If the central database is unavailable, emergency entry checks whether a local copy exists, is usable and is recent enough; returning to network mode and transferring changes follow a separate workflow.

### Patient charts and shift work

The chart combines orders and completion records, vital signs and graphs, fluid intake and output, ventilation, procedures, laboratory tests and clinical events. Current, previous-day and planned charts have separate entry points; patient history is available in the archive. Medication, diluent and administration-route catalogs, templates and shift schedules support prescribing.

In the **Diet** («Диета») tab, the doctor sets the nutrition plan and applies editable templates, while the nurse records actual consumption. Refusal can be recorded as **0 ml**. Nutritional intake contributes to fluid balance; users can undo their own changes in sequence. Record-version checks prevent overwriting another user's changes.

Procedure documents cover central venous catheterization, lumbar puncture and blood transfusion, alongside PDF reports for patient charts and surgical cases.

### Calculators and burn fluid monitoring

The **Calculation** («Расчёт») menu in sector 8 brings together infusion, electrolyte and burn calculators. The burn calculator is available to doctors and nurses from the chart of a patient with a corresponding ICD diagnosis.

It accounts for age, body weight, burn surface area and injury time, supporting days one, two and three and the period after recovery from shock. Actual intake and urine output are loaded from saved chart data; pediatric modes for day one and days two–three also include actual enteral intake. The window shows the calculation period, monitoring time, remaining volume and deviation from the calculated schedule. Chart days and days since injury are tracked separately.

Data loads when the calculator opens; **Refresh from chart** («Обновить из карты») reloads it while preserving marked manual values. A read failure is not treated as zero volume, and changing inputs clears the previous result. The calculation can be copied with explanations; it does not create orders automatically. Detailed rules and limits are in the [calculator documentation](docs/burn_infusion_calculator.md).

### Unified archive and analytics

The archive center is available to doctors, nurses and administrators according to their role permissions. It combines ICU patient archives, surgical case archives, analytics, charts and statistical reports. Opening a chart from an archived database uses a separate read service.

Analytics supports selecting a period and patient group, applying filters, saving views and comparing periods or groups. Each metric provides its result, calculation method and list of included cases. A shared metric registry defines units, sources and inclusion criteria; data-quality checks identify duplicates and inconsistent values. Missing comparison data is shown explicitly. Calculations and chart rendering run in the background; reports and charts use a consistent analytics snapshot.

Metric definitions and the scope of technical verification are described in the [analytics contract](docs/analytics_clinical_contract.md) and [implementation matrix](docs/analytics_implementation_matrix.md).

### Operating rooms, settings and diagnostics

Emergency and planned OR workspaces record the surgical team, anesthesia, stages, observations, medications and quick orders. Transfer from the ICU is linked to patient movement and OR admission. A separate, limited local OR mode is available during network problems; its data follows a dedicated transfer procedure when connectivity returns.

The settings center combines reference catalogs, OR parameters, nutrition templates, the laboratory test catalog, printing, themes, backgrounds and database maintenance. User support requests can include diagnostic logs. Technical logs have rotation, retention periods and a storage budget; frequent routine metrics become minute summaries, while repeated errors become compact contextual records. The medical change audit is stored separately from technical logs and synchronization notifications.

## How the shared workflow works

<p align="center">
  <a href="https://donkiton.github.io/rem_card/codemap/codemap.html"><img src="docs/assets/readme/remcard-workflow.en.svg" width="100%" alt="A doctor, nurse, planned OR and emergency OR share the medical database, settings and backups through one data folder"></a>
</p>

RemCard is a **PySide6** desktop application with a shared data folder. Routine workstation use does not require a separate web server. The main flow is **UI → services → DAO → SQLite**.

1. **Saving.** The interface submits changes to a write queue. The controller obtains a file lock and executes a transaction; a successful save is confirmed after `COMMIT` in the central database. A draft or queue entry alone does not mean the data is saved.
2. **Updating another workstation.** Triggers write notifications to `change_log` in the same transaction. `DataUpdateMonitor` detects changes, `SyncCoordinator` invalidates stale data, and the workstation rereads the required snapshot. Cache files are not transferred between computers.
3. **Fast reads.** `ReadCoordinator`, snapshots and local caching speed up display. The initial local replica is created through the SQLite Backup API, then small changes are applied to a separate local candidate. The candidate is checked and atomically replaces the previous copy; exceeding limits or encountering incompatibility triggers a full copy. The replica is read-only. If its version is behind the required version, reads switch to the central database. The central record after `COMMIT` remains the clinical source of truth.
4. **Responsiveness.** Snapshot loading, network writes, analytics and PDF generation use background tasks. Late results are checked before being applied to the open patient or selected period.

### Where the implementation lives

| Directory | Responsibility |
| --- | --- |
| [`ui/`](ui/) | Doctor and nurse workspaces, operating rooms, chart sectors, archives, procedures and UI settings. |
| [`services/`](services/) | Application operations, calculations, fluid balance, nutrition, read coordination and synchronization. |
| [`services/analytics/`](services/analytics/) | Metric registry, selections, cohorts, comparisons, statistics and chart data. |
| [`data/`](data/) | DAOs, models, reference catalogs and the separate settings database. |
| [`app/`](app/) | Startup and roles, paths, SQLite infrastructure, replicas, backups, emergency modes, logs and updates. |
| [`scripts/`](scripts/), [`tests/`](tests/) | Build and publication tools, CI, quality checks, automated tests and safety scenarios. |

The detailed [source-of-truth and synchronization contract](docs/source_of_truth_and_sync_contract.md) defines transaction boundaries, replica freshness and cache rules.

## Interactive architecture map

<p align="center">
  <a href="https://donkiton.github.io/rem_card/codemap/codemap.html"><strong>Open the interactive RemCard map →</strong></a><br>
  <sub>24 main modules · 48 verified connections · 6 end-to-end flows</sub>
</p>

The map was updated on **September 23, 2026** for the unified application architecture: `run_remcard.py` startup, role selection, database admission, background checks, session shutdown, the read replica and full updates. It is an overview of main components, rather than a list of every file; the source commit is shown on the map. Click a module to inspect its incoming and outgoing connections, related tests, source paths and supporting symbols. You can also select end-to-end scenarios, search components, filter module types, zoom and pan.

The map runs entirely in the browser and sends no data to an external server. Its verifiable source data is in [`codemap.json`](docs/codemap/codemap.json), with a standalone HTML version in [`codemap.html`](docs/codemap/codemap.html).

## Quick start from source

The project is developed and tested on **Windows with Python 3.11**. Git and Python are required; no `.env` file is needed.

```powershell
git clone https://github.com/Donkiton/rem_card.git
cd rem_card
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python run_remcard.py
```

`run_remcard.py` opens the unified application with role selection.

> [!CAUTION]
> Use a separate data folder for development and testing, rather than the production database. Locks and access checks do not replace test-environment isolation. The unified shell uses a `SessionLease` for session admission.

<details>
<summary><strong>Development database: selection and protection against accidental replacement</strong></summary>

The source version uses a local development data folder; missing structure may be created on first launch. The selected-folder list and active path are stored separately for each checkout in `.remcard/dev_database_paths.json`.

An existing selected database is opened with checks: the application must not silently create an empty database in place of a missing one. The legacy `%LOCALAPPDATA%\RemCard\dev_database_paths.json` path is used only for a one-time settings migration.

</details>

## Installed version and updates

The Windows distribution is a complete PyInstaller folder, rather than an MSI package or Docker image.

| File | Purpose |
| --- | --- |
| `RemCard.exe` | Unified application: role selection, workspaces and data-folder setup. |
| `RemCardUpdater.exe` | Utility for installing a full update. |

Run **`RemCard.exe`**, select the data folder on first launch, then choose a role. Keep the entire distribution folder, including `_internal`: the EXE alone is insufficient. `remcard_data_path.json` is stored beside the application; updates are discovered in `<data folder>\UPD\releases`.

Separate `RemCardDoctor.exe`, `RemCardNurse.exe`, OR executables and `RemCardPathSetup.exe` belong to the previous distribution model. Version 5.1 uses one entry point. The project distributes **full updates only**: `PATCH` in SemVer denotes the version level, not a separate package format.

<details>
<summary><strong>Test builds and safe production publication</strong></summary>

Build the current working tree without publishing:

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py --test-worktree
```

This runs mandatory checks, PyInstaller and smoke tests for both executables. It does not change `VERSION`, `CHANGELOG.md` or `app/release_info.json`, create a commit, run `git push` or publish a package. The output is marked `TEST_WORKTREE_ONLY.txt` so it cannot accidentally be published to production.

For a production release, the version and exact commit must first be in GitHub `main`; the release manager then runs `scripts\build_release.py` with `--expected-version` and `--expected-commit`. After acceptance testing on an isolated test database, the package is transferred using the separate `scripts\publish_full_update.py` command. `ready.ok` is created last: once it appears, the version is immediately available to clients of that database. See [build and publication](docs/how_to_build_and_update.md) and [automatic updates](docs/auto_update.md).

</details>

## Shared SQLite database and data safety

The medical database is at `archiv/rao_journal.db`; reference catalogs and shared settings, including theme and background parameters, are in `settings/remcard_settings.db`. Custom background images are in `settings/backgrounds`. These paths are relative to the selected data folder. Repository JSON catalogs provide seed data and a compatibility layer, but are not the installed version's operational source of truth. See the [settings database documentation](docs/settings_db.md).

The shared network database follows these rules:

- `journal_mode=DELETE`, `synchronous=EXTRA`, `mmap_size=0`; WAL is prohibited for the shared database.
- Writes go through the controller, a file lock and transactions.
- A live database is backed up only through the SQLite Backup API, not by copying an open file.
- Migrations, restoration and rotation use verified backups, `quick_check` and `integrity_check`.

These measures reduce known risks; they do not automatically make an arbitrary SQLite network deployment safe. Access permissions, latency, backup and restoration require separate operational acceptance testing. See [database safety](docs/db_safety_contract.md) and the [source-of-truth and synchronization contract](docs/source_of_truth_and_sync_contract.md).

## Developer checks

Local checks and GitHub Actions use the same entry point. After installing dependencies, run these in the activated environment:

```powershell
python scripts\run_ci_checks.py core
python scripts\run_ci_checks.py ui
python scripts\run_ci_checks.py quality
python scripts\run_ci_checks.py regression
git diff --check
```

`core` checks logic and data, `ui` checks the Qt interface, `quality` checks code quality and architectural constraints, and `regression` runs the full safety-scenario registry. In CI the groups run independently; the final `tests` status checks their success and report completeness. Pytest modules are assigned in [`tests/groups.json`](tests/groups.json) without omissions or duplicates.

`python scripts\run_ci_checks.py all` runs the entire pytest suite in one process to check shared isolation; it does not replace the separate `quality` and `regression` runs. The runner assigns temporary database and QSettings paths, saving reports in `tmp/ci-reports/<group>`. Standard `python -m pytest tests -q` is also available for direct testing. See [testing and CI](docs/testing.md).

Network test rigs, restore drills, emergency mode and offline mode read or modify the selected data folder. Run them only against a deliberately selected test database, following the [acceptance procedure](docs/operational_acceptance.md) and [backup verification guide](docs/backup_restore_drill.md).

## Documentation, status and contact

- [Documentation index](docs/README.md) — current procedures and historical materials, primarily in Russian.
- [Interactive architecture map](https://donkiton.github.io/rem_card/codemap/codemap.html) — modules, dependencies, tests and end-to-end flows linked to source.
- [Project website](https://donkiton.github.io/rem_site/#english) and [releases](https://github.com/Donkiton/rem_card/releases).
- [Changelog](CHANGELOG.md) and [`VERSION`](VERSION) — changes and the current version.
- [Testing and CI](docs/testing.md) — test groups, isolation, completeness checks and reports.
- [Burn calculator](docs/burn_infusion_calculator.md) and [analytics contract](docs/analytics_clinical_contract.md) — calculations, data sources and limitations.
- [Versioning](docs/versioning.md), [automatic updates](docs/auto_update.md) and [builds](docs/how_to_build_and_update.md) — the full-release process.
- [Database safety contract](docs/db_safety_contract.md), [source of truth and synchronization](docs/source_of_truth_and_sync_contract.md), [operational acceptance](docs/operational_acceptance.md) and [emergency runbook](docs/emergency_runbook.md) — safe operation.

RemCard is an experimental project under active development, built by anaesthesiologist-intensivist Vadim Bityutsky. Tests, static checks, backups and protective mechanisms do not replace independent technical, information-security and clinical audits.

For bug reports and suggestions about architecture, performance and safe operation, contact [menfise@mail.ru](mailto:menfise@mail.ru) with the subject `РЕМ КАРТА`.

## License and responsibility

RemCard's concept, architecture and original source code were created by its developer, Vadim Alekseevich Bityutsky (Битюцкий Вадим Алексеевич; GitHub: [DonKiton](https://github.com/DonKiton)).

RemCard is distributed under the [RemCard Proprietary Source License 1.1](LICENSE), which permits non-commercial use by individuals. An individual user or developer may view, download, copy, run, study and modify the code for free, and share original or modified versions without receiving money or other commercial benefit. Distribution must retain the license and author attribution, identify changes, provide source code and clearly mark modified versions as unofficial.

Any use by an organization or on behalf of an employer, client, business, clinic or institution requires the copyright holder's prior written permission. Without a separate agreement, sale and resale, paid deployment, installation, integration, modification, support, training, consulting, hosting, SaaS, subscriptions, advertising and other monetization are prohibited. These restrictions apply to all derivative versions: changing the font, interface, features, name or any other part of the application does not grant the right to earn money from it.

Public availability and forks on GitHub do not grant commercial rights. The software is provided “as is” and used at the user's own responsibility. Third-party libraries and dependencies retain their own licenses. Under the OSI classification this is a source-available license, rather than an open-source license, because commercial use is restricted. If this summary differs from the full license text, [LICENSE](LICENSE) takes precedence.
