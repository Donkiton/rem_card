<p align="center">
  <a href="README.md"><kbd>English</kbd></a>
  <a href="README.ru.md"><kbd>Русский</kbd></a>
  <a href="README.zh-CN.md"><kbd><strong>中文（简体）</strong> ✓</kbd></a>
  <a href="README.ja.md"><kbd>日本語</kbd></a>
  <a href="README.de.md"><kbd>Deutsch</kbd></a>
</p>

<p align="center">
  <img src="docs/assets/readme/remcard-hero.zh-CN.svg" width="100%" alt="RemCard ICU — 面向重症监护室与手术室的电子重症监护记录系统">
</p>

<h1 align="center">RemCard ICU — 电子重症监护记录系统</h1>

<p align="center">
  <strong>RemCard 是一款 Windows 应用，用于重症监护室与手术室的患者记录和交接班协作。</strong><br>
  将医嘱、生命体征、液体平衡、机械通气、医疗操作、检验、事件、报告和归档整合到同一工作流程中。
</p>

<p align="center">
  <code>版本 5.1.10</code> · <code>Windows</code> · <code>Python 3.11</code> · <code>PySide6</code> · <code>SQLite</code> · <code>RemCard Proprietary Source License</code> · <code>实验性项目</code>
</p>

<p align="center">
  <a href="https://cliniciansthatcode.com/directory/#p-vadim-bityutsky"><img src="docs/assets/readme/clinicians-that-code-verified.svg" width="260" height="28" alt="Clinicians That Code：已认证开发者 — RemCard 作者的目录条目"></a><br>
  <sub>RemCard 作者已收录于 Clinicians That Code 目录；此徽章仅表明作者的目录身份，不代表软件已通过临床验证。</sub>
</p>

<p align="center">
  <a href="#系统运行展示">界面</a> ·
  <a href="#当前版本的功能">功能</a> ·
  <a href="#协同工作架构">架构</a> ·
  <a href="#从源代码快速启动">从源码启动</a> ·
  <a href="#安装版本与更新">安装与更新</a> ·
  <a href="docs/README.md">文档</a> ·
  <a href="CHANGELOG.md">更新记录</a> ·
  <a href="LICENSE">许可证</a>
</p>

本页介绍 **RemCard 5.1.10**，更新于 **2026 年 10 月 22 日**。统一的 `RemCard.exe` 提供四种角色入口；独立的 `RemCardUpdater.exe` 用于安装完整更新。源码版本号见 [`VERSION`](VERSION)，具体构建的组成见其清单。

RemCard 由麻醉与重症医学医师 Vadim Bityutsky 开发。应用界面和链接中的详细文档目前主要为俄语。切换 README 语言仅切换本页说明，不会翻译应用界面或截图。

在 [5.1.10 版本](https://github.com/Donkiton/rem_card/releases/tag/v5.1.10)中，即使历史记录较多，少量变更也可以通过增量方式传送至本地副本。通过网络获取患者状态的操作已移出护士界面线程；延迟到达的响应不会覆盖较新的数据。此外，改进了初始状态保存的安全重试，以及共享数据库锁释放后的夜间备份加载。医生与护士协作的检查结果见[同步验证报告](docs/sync_responsiveness_validation.md)；副本规则与测量范围见[副本契约](docs/source_of_truth_and_sync_contract.md)和[测量方法](docs/replica_delta_benchmark.md)。

已构建的 5.1.10 软件包禁止迁移共享数据库，也禁止提高最低客户端版本。对于兼容的工作站，这是一次**可选更新**，无需在所有计算机上同时安装。首次完整快照的耗时仍取决于网络速度和数据库大小。

> [!WARNING]
> **RemCard 不是医疗器械，不能替代临床规范、临床决策或专业人员的责任监督。** 实际使用前，必须进行独立的技术与临床验证、目标环境验收，并准备备用工作流程。

## 系统运行展示

以下为 **RemCard 5.1.0** 使用演示患者的界面截图；当前版本为 **5.1.10**。点击图片可查看原始尺寸。

### 统一入口与角色选择

[![RemCard 5.1.0：医生、护士、择期手术室与急诊手术室](docs/assets/readme/remcard-roles-5-1.jpg)](docs/assets/readme/remcard-roles-5-1.jpg)

一个应用提供四种工作界面。点击**“角色”（«Роли»）**可返回角色选择；登录界面支持浅色与深色主题。

### 患者列表

[![RemCard 5.1.0 患者列表：住院信息与患者记录入口](docs/assets/readme/remcard-patients-5-1.jpg)](docs/assets/readme/remcard-patients-5-1.jpg)

按床位展示在院患者、最新指标，并可快速打开患者记录与报告。

<details>
<summary><strong>床位管理与新增患者</strong></summary>

### 患者管理

[![已占用与空闲床位，以及所选患者的信息卡](docs/assets/readme/remcard-beds-5-1.jpg)](docs/assets/readme/remcard-beds-5-1.jpg)

**“新增患者”（«Добавить пациента»）**按钮打开床位管理。选择空闲床位后，点击“占用床位”（«ЗАНЯТЬ КОЙКУ»）办理入院；对于已占用床位，可以打开患者信息卡。

</details>

### 医嘱单

[![RemCard 5.1.0 每日医嘱单与执行标记](docs/assets/readme/remcard-orders-5-1.jpg)](docs/assets/readme/remcard-orders-5-1.jpg)

药物、给药途径、执行计划和完成标记按班次时间轴展示，时间范围为当天 08:00 至次日 08:00。

### 液体平衡

[![RemCard 5.1.0 液体入量、出量与每小时平衡表](docs/assets/readme/remcard-balance-5-1.jpg)](docs/assets/readme/remcard-balance-5-1.jpg)

输液、药物、血液、血浆和经口摄入量与排出量进行对照；同时可查看每小时明细与平衡汇总。

<!-- 5.1.0 版本生命体征图表的新截图将另行加入本图库。 -->

## 同一工作流程中的角色

- **医生** — 入院与床位管理、按班次组织的患者记录、医嘱与模板、营养计划、医疗操作、转归、归档、报告与计算器。
- **护士** — 即将执行的医嘱、执行记录、实际营养摄入、班次内动态、打印与工作统计。
- **手术室** — 急诊与择期手术室、快捷医嘱、药物、手术阶段与事件、重症监护病区（РАО）和手术室之间的患者转交、病例结束与报告。
- **管理员** — 统一设置中心、当前与归档数据库状态、备份、基础数据目录、打印、外观与诊断。

## 当前版本的功能

### 统一应用 5.1

医生、护士、择期手术室和急诊手术室均从同一窗口进入。切换角色时，应用会完成已接收的写入任务，并释放上一会话的资源。数据库检查和工作站准备过程会显示状态，用户可取消登录。启动诊断有助于分析应用或角色界面打开缓慢的原因。

数据文件夹设置已集成到应用中。统一维护模式协调各工作站对数据库的访问。中央数据库不可用时，应急登录会检查本地副本是否存在、是否可用以及生成时间；恢复联网模式与迁移变更采用单独的流程。

### 患者记录与班次工作

患者记录整合医嘱及执行情况、生命体征与图表、液体入量和出量、机械通气、医疗操作、实验室检验及临床事件。当天、前一天和预先计划的记录使用独立入口；患者历史记录可通过归档查看。医嘱使用药物、溶媒和给药途径目录，以及模板和班次执行计划。

在**“饮食”（«Диета»）**选项卡中，医生制定营养计划并应用可编辑模板，护士记录实际摄入。拒绝摄入可保存为 **0 毫升**。实际营养摄入计入液体平衡；用户可以依次撤销自己的修改。记录版本检查可防止覆盖其他用户的变更。

支持中心静脉置管、腰椎穿刺、输血等操作文书，并可生成患者记录和手术病例的 PDF 报告。

### 计算器与烧伤补液监测

第 8 区的**“计算”（«Расчёт»）**菜单汇集输液、电解质和烧伤计算器。对于具有相应 ICD 诊断的患者，医生和护士均可从患者记录打开烧伤计算器。

计算器考虑年龄、体重、烧伤面积和受伤时间，支持第 1、2、3 个 24 小时阶段及休克纠正后的阶段。从已保存的患者记录中读取实际液体入量和尿量；在儿童第 1 天和第 2–3 天模式下，还会计入实际肠内入量。窗口显示计算时段、监测时间、剩余液体量以及相对于计划进度的偏差。记录所对应的日期与自受伤起经过的天数分别计算。

打开计算器时会加载数据；**“从记录更新”（«Обновить из карты»）**命令会重新读取数据，并保留已勾选的手动输入值。读取错误不会被当作零入量，修改输入数据则会清除之前的计算结果。可复制附有说明的计算结果，但不会自动生成医嘱。详细规则和限制见[计算器文档](docs/burn_infusion_calculator.md)。

### 统一归档与分析

医生、护士和管理员可根据角色权限访问归档中心。该中心整合重症监护病区患者归档、手术病例归档、分析、图表和统计报告。查看归档数据库中的患者记录时，使用独立的读取服务。

分析功能支持选择时段和患者组、应用筛选、保存视图，以及比较不同时段或患者组。每项指标均提供结果、计算方法和纳入病例列表。统一指标注册表定义单位、数据来源和纳入标准；数据质量检查可发现重复与不一致的数值。比较数据缺失时会明确显示。计算与图表生成在后台执行；报告与图表使用一致的分析快照。

指标定义和技术验证范围见[分析契约](docs/analytics_clinical_contract.md)与[实现矩阵](docs/analytics_implementation_matrix.md)。

### 手术室、设置与诊断

急诊和择期手术工作界面记录整个手术病例，包括团队、麻醉处理、阶段、指标、药物和快捷医嘱。从重症监护病区转交患者的流程与患者流转、手术室接收相衔接。发生网络故障时，可使用独立且受限的手术室本地模式；恢复连接后，其数据需通过专门的迁移流程转入。

设置中心整合基础数据目录、手术室参数、营养模板、检验项目目录、打印、主题、背景和数据库维护。用户反馈可附带诊断日志。技术日志设有轮换、保留期限和容量预算；高频常规指标合并为每分钟摘要，重复错误则压缩为带上下文的简要记录。医疗变更审计与技术日志、同步通知分别存储。

## 协同工作架构

<p align="center">
  <a href="https://donkiton.github.io/rem_card/codemap/codemap.html"><img src="docs/assets/readme/remcard-workflow.zh-CN.svg" width="100%" alt="医生、护士和手术室通过共享数据文件夹协作，使用医疗数据库、设置与备份"></a>
</p>

RemCard 是基于 **PySide6** 的桌面应用，使用共享数据文件夹。工作站常规使用不需要单独的 Web 服务器。主要数据流为**界面 → 服务 → DAO → SQLite**。

1. **保存。** 界面将变更提交到写入队列。控制器取得文件锁并执行事务；只有中央数据库完成 `COMMIT` 后，才会确认保存成功。草稿或排队本身并不意味着数据已保存。
2. **更新其他工作站。** 触发器在同一事务中将通知写入 `change_log`。`DataUpdateMonitor` 检测变更，`SyncCoordinator` 使过期数据失效，随后工作站重新读取所需快照。计算机之间不传输缓存文件。
3. **快速读取。** `ReadCoordinator`、快照和本地缓存加快显示。首次本地副本通过 SQLite Backup API 创建，之后将少量变更应用于另一个本地候选副本。候选副本通过检查后，以原子方式替换旧副本；超出限制或不兼容时，执行完整复制。副本仅用于读取。如果其版本低于所需版本，读取将切换至中央数据库。中央数据库完成 `COMMIT` 后的记录始终是临床数据的权威来源。
4. **响应速度。** 快照加载、网络写入、分析和 PDF 生成均使用后台任务。延迟返回的结果在应用于当前打开的患者或所选时段前会经过检查。

### 实现位置

| 目录 | 职责 |
| --- | --- |
| [`ui/`](ui/) | 医生与护士工作界面、手术室、患者记录各区、归档、医疗操作和界面设置。 |
| [`services/`](services/) | 应用操作、计算、液体平衡、营养，以及读取与同步协调。 |
| [`services/analytics/`](services/analytics/) | 指标注册表、数据选取、队列、比较、统计与图表数据。 |
| [`data/`](data/) | DAO、模型、基础数据目录和独立设置数据库。 |
| [`app/`](app/) | 启动与角色、路径、SQLite 基础设施、副本、备份、应急模式、日志与更新。 |
| [`scripts/`](scripts/), [`tests/`](tests/) | 构建与发布、CI、质量检查、自动化测试和安全性场景。 |

详细的[权威数据源与同步契约](docs/source_of_truth_and_sync_contract.md)说明了事务边界、副本时效和缓存规则。

## 交互式架构图

<p align="center">
  <a href="https://donkiton.github.io/rem_card/codemap/codemap.html"><strong>打开 RemCard 交互式架构图 →</strong></a><br>
  <sub>24 个主要模块 · 48 条已验证的关系 · 6 条端到端流程</sub>
</p>

架构图于 **2026 年 9 月 23 日**更新，反映统一应用架构：`run_remcard.py` 启动、角色选择、数据库准入、后台检查、会话结束、读取副本和完整更新。它概览主要组件，并非列出每个文件；对应的源码提交标注在图中。点击模块可查看其传入和传出关系、相关测试、源码路径以及作为依据的代码符号。还可以选择端到端场景、搜索组件、筛选模块类型、缩放与拖动图形。

架构图完全在浏览器中运行，不向外部服务器发送数据。可核验的原始数据见 [`codemap.json`](docs/codemap/codemap.json)，独立 HTML 版本见 [`codemap.html`](docs/codemap/codemap.html)。

## 从源代码快速启动

项目在 **Windows 与 Python 3.11** 环境下开发和验证。需要 Git 和 Python，无需 `.env` 文件。

```powershell
git clone https://github.com/Donkiton/rem_card.git
cd rem_card
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python run_remcard.py
```

`run_remcard.py` 打开带角色选择的统一应用。

> [!CAUTION]
> 开发和测试应使用独立的数据文件夹，避免使用生产数据库。锁机制和访问检查不能替代测试环境隔离。统一应用外壳使用 `SessionLease` 进行会话准入控制。

<details>
<summary><strong>开发数据库：选择与防止意外替换</strong></summary>

源码运行模式使用本地开发数据文件夹；首次运行时可能会创建缺失的目录结构。已选文件夹列表和当前路径分别保存在各 checkout 的 `.remcard/dev_database_paths.json` 中。

打开所选的已有数据库时会进行检查：应用不能在原数据库丢失后悄然创建一个空数据库来替代它。旧路径 `%LOCALAPPDATA%\RemCard\dev_database_paths.json` 仅用于一次性迁移该设置。

</details>

## 安装版本与更新

Windows 正式版本以完整的 PyInstaller 文件夹形式分发，而非 MSI 安装包或 Docker 镜像。

| 文件 | 用途 |
| --- | --- |
| `RemCard.exe` | 统一应用：角色选择、工作界面与数据文件夹设置。 |
| `RemCardUpdater.exe` | 安装完整更新的辅助程序。 |

启动 **`RemCard.exe`**，首次运行时选择数据文件夹，然后选择所需角色。请保留包含 `_internal` 的完整分发文件夹，仅有 EXE 文件不足以运行。`remcard_data_path.json` 保存在程序旁；更新从 `<папка данных>\UPD\releases` 查找，其中 `<папка данных>` 表示所选数据文件夹。

独立的 `RemCardDoctor.exe`、`RemCardNurse.exe`、手术室 EXE 和 `RemCardPathSetup.exe` 属于旧的分发方案。5.1 版本采用统一入口。项目仅分发**完整更新**：SemVer 中的 `PATCH` 表示版本级别，而非单独的软件包格式。

<details>
<summary><strong>测试构建与安全的生产发布</strong></summary>

构建当前工作树但不发布：

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py --test-worktree
```

该命令运行必要检查、PyInstaller 和两个 EXE 的冒烟测试，但不会修改 `VERSION`、`CHANGELOG.md` 或 `app/release_info.json`，不会创建提交、执行 `git push` 或发布软件包。结果带有 `TEST_WORKTREE_ONLY.txt` 标记，以防意外发布到生产环境。

正式发布前，版本和准确的提交必须先进入 GitHub `main`；随后由发布负责人使用 `--expected-version` 和 `--expected-commit` 参数运行 `scripts\build_release.py`。在隔离测试数据库中验收后，通过独立命令 `scripts\publish_full_update.py` 传送软件包。`ready.ok` 最后创建：它出现后，该数据库的客户端即可立即获得此版本。详情见[构建与发布](docs/how_to_build_and_update.md)和[自动更新](docs/auto_update.md)。

</details>

## 共享 SQLite 数据库与数据安全

医疗数据库位于 `archiv/rao_journal.db`，基础数据目录和通用设置（包括主题与背景参数）位于 `settings/remcard_settings.db`。用户自定义背景图保存在 `settings/backgrounds`。这些路径均相对于所选的数据文件夹。仓库中的基础数据 JSON 文件用于初始数据和兼容层，并非安装版本运行时的权威数据源。详情见[设置数据库说明](docs/settings_db.md)。

共享网络数据库遵循以下规则：

- 使用 `journal_mode=DELETE`、`synchronous=EXTRA`、`mmap_size=0`；共享数据库禁止使用 WAL；
- 写入通过控制器、文件锁和事务执行；
- 运行中的数据库仅通过 SQLite Backup API 备份，不能直接复制已打开的文件；
- 迁移、恢复和轮换使用经过验证的备份，以及 `quick_check` 和 `integrity_check`。

这些措施降低了已知风险，但并不意味着 SQLite 放在任意网络环境中都会自动变得安全。访问权限、延迟、备份和恢复仍须经过单独的运行验收。参见[数据库安全](docs/db_safety_contract.md)和[权威数据源与同步契约](docs/source_of_truth_and_sync_contract.md)。

## 开发者检查

本地检查和 GitHub Actions 使用统一入口。安装依赖并激活环境后运行：

```powershell
python scripts\run_ci_checks.py core
python scripts\run_ci_checks.py ui
python scripts\run_ci_checks.py quality
python scripts\run_ci_checks.py regression
git diff --check
```

`core` 检查逻辑与数据，`ui` 检查 Qt 界面，`quality` 检查代码质量与架构约束，`regression` 执行完整的安全性场景清单。CI 中各组独立运行；最终的 `tests` 状态检查各组是否成功、报告是否完整。pytest 模块在 [`tests/groups.json`](tests/groups.json) 中分组，没有遗漏或重复。

`python scripts\run_ci_checks.py all` 在一个进程中运行全部 pytest，以检查整体隔离性；它不能替代独立的 `quality` 和 `regression` 检查。运行器为数据库和 QSettings 分配临时路径，并将报告保存在 `tmp/ci-reports/<группа>`，其中 `<группа>` 表示检查组。也可使用常规命令 `python -m pytest tests -q` 进行针对性验证。详情见[测试与 CI](docs/testing.md)。

网络测试环境、恢复演练、应急模式和离线模式会读取或修改所选数据文件夹。请仅在明确指定的测试数据库上，按照[运行验收规程](docs/operational_acceptance.md)和[备份恢复演练说明](docs/backup_restore_drill.md)执行。

## 文档、状态与联系

- [文档索引](docs/README.md) — 当前规程与历史资料。
- [交互式架构图](https://donkiton.github.io/rem_card/codemap/codemap.html) — 关联源码的模块、依赖、测试与端到端流程。
- [更新记录](CHANGELOG.md)与 [`VERSION`](VERSION) — 变更与当前版本。
- [测试与 CI](docs/testing.md) — 测试分组、隔离、完整性检查与报告。
- [烧伤计算器](docs/burn_infusion_calculator.md)与[分析契约](docs/analytics_clinical_contract.md) — 计算、数据来源与限制。
- [版本管理](docs/versioning.md)、[自动更新](docs/auto_update.md)与[构建](docs/how_to_build_and_update.md) — 完整版本发布流程。
- [数据库安全契约](docs/db_safety_contract.md)、[权威数据源与同步](docs/source_of_truth_and_sync_contract.md)、[运行验收](docs/operational_acceptance.md)和[应急手册](docs/emergency_runbook.md) — 安全运行。
- [项目网站](https://donkiton.github.io/rem_site/#english)与[发布版本](https://github.com/Donkiton/rem_card/releases)。

这是一个持续演进的实验性项目。测试、静态检查、备份和保护机制不能替代独立的技术、信息安全与临床审计。

如需报告问题，或提出有关架构、性能和安全运行的建议，请联系 [menfise@mail.ru](mailto:menfise@mail.ru)，邮件主题为 `РЕМ КАРТА`。

## 许可证与责任

RemCard 的理念、架构和原创源代码作者及开发者为 Vadim Alekseevich Bityutsky（Битюцкий Вадим Алексеевич；GitHub：[DonKiton](https://github.com/DonKiton)）。

RemCard 依据 [RemCard Proprietary Source License 1.1](LICENSE) 分发，允许自然人进行非商业使用。个人用户或开发者可以免费查看、下载、复制、运行、研究和修改代码，也可以分享原版或修改版，但不得收取费用或获取其他商业利益。分发时必须保留许可证和作者信息，注明修改内容，提供源代码，并明确标识修改版为非官方版本。

任何组织使用，或为雇主、客户、企业、诊所及机构利益进行的使用，均须事先取得权利人的书面许可。在没有单独协议的情况下，禁止销售与转售、收费部署、安装、集成、定制开发、维护、培训、咨询、托管、SaaS、订阅、广告及其他变现行为。这些限制适用于所有衍生版本：修改字体、界面、功能、名称或程序的其他部分，并不会赋予从中获利的权利。

在 GitHub 上公开托管或创建 fork 不授予商业权利。程序按“现状”提供，用户自行承担使用责任。第三方库和依赖仍遵循各自的许可证。根据 OSI 的分类，由于商业使用受到限制，本许可证属于源码可用（source-available）许可证，而非开源（open-source）许可证。如本摘要与完整文本存在差异，以 [LICENSE](LICENSE) 文件为准。
