<p align="center">
  <a href="features.en.md"><kbd><strong>English</strong> ✓</kbd></a>
  <a href="features.ru.md"><kbd>Русский</kbd></a>
  <a href="features.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="features.ja.md"><kbd>日本語</kbd></a>
  <a href="features.de.md"><kbd>Deutsch</kbd></a>
</p>

<p align="center">
  <a href="../../README.md">← README</a> ·
  <a href="setup.en.md">Setup</a> ·
  <a href="architecture.en.md">Architecture</a>
</p>

# Features and workspaces

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

Data loads when the calculator opens; **Refresh from chart** («Обновить из карты») reloads it while preserving marked manual values. A read failure is not treated as zero volume, and changing inputs clears the previous result. The calculation can be copied with explanations; it does not create orders automatically. Detailed rules and limits are in the [calculator documentation](../burn_infusion_calculator.md).

### Unified archive and analytics

The archive center is available to doctors, nurses and administrators according to their role permissions. It combines ICU patient archives, surgical case archives, analytics, charts and statistical reports. Opening a chart from an archived database uses a separate read service.

Analytics supports selecting a period and patient group, applying filters, saving views and comparing periods or groups. Each metric provides its result, calculation method and list of included cases. A shared metric registry defines units, sources and inclusion criteria; data-quality checks identify duplicates and inconsistent values. Missing comparison data is shown explicitly. Calculations and chart rendering run in the background; reports and charts use a consistent analytics snapshot.

Metric definitions and the scope of technical verification are described in the [analytics contract](../analytics_clinical_contract.md) and [implementation matrix](../analytics_implementation_matrix.md).

### Operating rooms, settings and diagnostics

Emergency and planned OR workspaces record the surgical team, anesthesia, stages, observations, medications and quick orders. Transfer from the ICU is linked to patient movement and OR admission. A separate, limited local OR mode is available during network problems; its data follows a dedicated transfer procedure when connectivity returns.

The settings center combines reference catalogs, OR parameters, nutrition templates, the laboratory test catalog, printing, themes, backgrounds and database maintenance. User support requests can include diagnostic logs. Technical logs have rotation, retention periods and a storage budget; frequent routine metrics become minute summaries, while repeated errors become compact contextual records. The medical change audit is stored separately from technical logs and synchronization notifications.


<details>
<summary><strong>Screenshots and workflow notes</strong></summary>

## See it in use

Screenshots show **RemCard 5.1.0** with demonstration patients; the current release is **5.1.10**. Click an image to open it at full size. The interface shown is in Russian.

### One entry point and role selection

[![RemCard 5.1.0: doctor, nurse, planned and emergency operating rooms](../assets/readme/remcard-roles-5-1.jpg)](../assets/readme/remcard-roles-5-1.jpg)

One application for four workspaces. The **Roles** («Роли») button returns to role selection; light and dark themes are available on the entry screen.

### Patient list

[![RemCard 5.1.0 patient list: admission information and links to patient charts](../assets/readme/remcard-patients-5-1.jpg)](../assets/readme/remcard-patients-5-1.jpg)

Active patients by bed, recent observations, and quick access to charts and reports.

<details>
<summary><strong>Bed management and patient admission</strong></summary>

### Patient management

[![Occupied and available beds with the selected patient's details](../assets/readme/remcard-beds-5-1.jpg)](../assets/readme/remcard-beds-5-1.jpg)

The **Add patient** («Добавить пациента») button opens bed management. Select a free bed and click **Occupy bed** («ЗАНЯТЬ КОЙКУ») to record an admission. An occupied bed provides access to the patient's details.

</details>

### Medication orders

[![RemCard 5.1.0 daily medication orders with administration tracking](../assets/readme/remcard-orders-5-1.jpg)](../assets/readme/remcard-orders-5-1.jpg)

Medications, administration routes, schedules and completion marks on a shift timeline from 08:00 to 08:00 the following day.

### Fluid balance

[![RemCard 5.1.0 fluid intake, output and hourly balance table](../assets/readme/remcard-balance-5-1.jpg)](../assets/readme/remcard-balance-5-1.jpg)

Infusions, medications, blood, plasma and oral intake are compared with output, alongside an hourly table and total balance.

<!-- A new screenshot of the 5.1.0 vital-sign charts will be added to this gallery separately. -->

</details>
