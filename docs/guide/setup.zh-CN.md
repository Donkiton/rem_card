<p align="center">
  <a href="setup.en.md"><kbd>English</kbd></a>
  <a href="setup.ru.md"><kbd>Русский</kbd></a>
  <a href="setup.zh-CN.md"><kbd><strong>中文（简体）</strong> ✓</kbd></a>
  <a href="setup.ja.md"><kbd>日本語</kbd></a>
  <a href="setup.de.md"><kbd>Deutsch</kbd></a>
</p>

<p align="center">
  <a href="../../README.zh-CN.md">← README</a> ·
  <a href="features.zh-CN.md">功能</a> ·
  <a href="architecture.zh-CN.md">架构</a>
</p>

# 启动、更新与参考

## 从源代码快速启动

项目在 <strong>Windows 与 Python 3.11</strong> 环境下开发和验证。需要 Git 和 Python，无需 `.env` 文件。

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

启动 <strong>`RemCard.exe`</strong>，首次运行时选择数据文件夹，然后选择所需角色。请保留包含 `_internal` 的完整分发文件夹，仅有 EXE 文件不足以运行。`remcard_data_path.json` 保存在程序旁；更新从 `<папка данных>\UPD\releases` 查找，其中 `<папка данных>` 表示所选数据文件夹。

独立的 `RemCardDoctor.exe`、`RemCardNurse.exe`、手术室 EXE 和 `RemCardPathSetup.exe` 属于旧的分发方案。5.1 版本采用统一入口。项目仅分发<strong>完整更新</strong>：SemVer 中的 `PATCH` 表示版本级别，而非单独的软件包格式。

<details>
<summary><strong>测试构建与安全的生产发布</strong></summary>

构建当前工作树但不发布：

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py --test-worktree
```

该命令运行必要检查、PyInstaller 和两个 EXE 的冒烟测试，但不会修改 `VERSION`、`CHANGELOG.md` 或 `app/release_info.json`，不会创建提交、执行 `git push` 或发布软件包。结果带有 `TEST_WORKTREE_ONLY.txt` 标记，以防意外发布到生产环境。

正式发布前，版本和准确的提交必须先进入 GitHub `main`；随后由发布负责人使用 `--expected-version` 和 `--expected-commit` 参数运行 `scripts\build_release.py`。在隔离测试数据库中验收后，通过独立命令 `scripts\publish_full_update.py` 传送软件包。`ready.ok` 最后创建：它出现后，该数据库的客户端即可立即获得此版本。详情见[构建与发布](../how_to_build_and_update.md)和[自动更新](../auto_update.md)。

</details>

## 共享 SQLite 数据库与数据安全

医疗数据库位于 `archiv/rao_journal.db`，基础数据目录和通用设置（包括主题与背景参数）位于 `settings/remcard_settings.db`。用户自定义背景图保存在 `settings/backgrounds`。这些路径均相对于所选的数据文件夹。仓库中的基础数据 JSON 文件用于初始数据和兼容层，并非安装版本运行时的权威数据源。详情见[设置数据库说明](../settings_db.md)。

共享网络数据库遵循以下规则：

- 使用 `journal_mode=DELETE`、`synchronous=EXTRA`、`mmap_size=0`；共享数据库禁止使用 WAL；
- 写入通过控制器、文件锁和事务执行；
- 运行中的数据库仅通过 SQLite Backup API 备份，不能直接复制已打开的文件；
- 迁移、恢复和轮换使用经过验证的备份，以及 `quick_check` 和 `integrity_check`。

这些措施降低了已知风险，但并不意味着 SQLite 放在任意网络环境中都会自动变得安全。访问权限、延迟、备份和恢复仍须经过单独的运行验收。参见[数据库安全](../db_safety_contract.md)和[权威数据源与同步契约](../source_of_truth_and_sync_contract.md)。

## 开发者检查

本地检查和 GitHub Actions 使用统一入口。安装依赖并激活环境后运行：

```powershell
python scripts\run_ci_checks.py core
python scripts\run_ci_checks.py ui
python scripts\run_ci_checks.py quality
python scripts\run_ci_checks.py regression
git diff --check
```

`core` 检查逻辑与数据，`ui` 检查 Qt 界面，`quality` 检查代码质量与架构约束，`regression` 执行完整的安全性场景清单。CI 中各组独立运行；最终的 `tests` 状态检查各组是否成功、报告是否完整。pytest 模块在 [`tests/groups.json`](../../tests/groups.json) 中分组，没有遗漏或重复。

`python scripts\run_ci_checks.py all` 在一个进程中运行全部 pytest，以检查整体隔离性；它不能替代独立的 `quality` 和 `regression` 检查。运行器为数据库和 QSettings 分配临时路径，并将报告保存在 `tmp/ci-reports/<группа>`，其中 `<группа>` 表示检查组。也可使用常规命令 `python -m pytest tests -q` 进行针对性验证。详情见[测试与 CI](../testing.md)。

网络测试环境、恢复演练、应急模式和离线模式会读取或修改所选数据文件夹。请仅在明确指定的测试数据库上，按照[运行验收规程](../operational_acceptance.md)和[备份恢复演练说明](../backup_restore_drill.md)执行。


## 当前版本说明

本页介绍 <strong>RemCard 5.1.10</strong>，更新于 <strong>2026 年 10 月 22 日</strong>。统一的 `RemCard.exe` 提供四种角色入口；独立的 `RemCardUpdater.exe` 用于安装完整更新。源码版本号见 [`VERSION`](../../VERSION)，具体构建的组成见其清单。

RemCard 由麻醉与重症医学医师 Vadim Bityutsky 开发。应用界面和链接中的详细文档目前主要为俄语。本文档的语言切换仅改变说明文字，不会翻译应用界面或截图。

在 [5.1.10 版本](https://github.com/Donkiton/rem_card/releases/tag/v5.1.10)中，即使历史记录较多，少量变更也可以通过增量方式传送至本地副本。通过网络获取患者状态的操作已移出护士界面线程；延迟到达的响应不会覆盖较新的数据。此外，改进了初始状态保存的安全重试，以及共享数据库锁释放后的夜间备份加载。医生与护士协作的检查结果见[同步验证报告](../sync_responsiveness_validation.md)；副本规则与测量范围见[副本契约](../source_of_truth_and_sync_contract.md)和[测量方法](../replica_delta_benchmark.md)。

已构建的 5.1.10 软件包禁止迁移共享数据库，也禁止提高最低客户端版本。对于兼容的工作站，这是一次<strong>可选更新</strong>，无需在所有计算机上同时安装。首次完整快照的耗时仍取决于网络速度和数据库大小。

> [!WARNING]
> <strong>RemCard 不是医疗器械，不能替代临床规范、临床决策或专业人员的责任监督。</strong> 实际使用前，必须进行独立的技术与临床验证、目标环境验收，并准备备用工作流程。

## 文档、状态与联系

- [文档索引](../README.md) — 当前规程与历史资料。
- [交互式架构图](https://donkiton.github.io/rem_card/codemap/codemap.html) — 关联源码的模块、依赖、测试与端到端流程。
- [更新记录](../../CHANGELOG.md)与 [`VERSION`](../../VERSION) — 变更与当前版本。
- [测试与 CI](../testing.md) — 测试分组、隔离、完整性检查与报告。
- [烧伤计算器](../burn_infusion_calculator.md)与[分析契约](../analytics_clinical_contract.md) — 计算、数据来源与限制。
- [版本管理](../versioning.md)、[自动更新](../auto_update.md)与[构建](../how_to_build_and_update.md) — 完整版本发布流程。
- [数据库安全契约](../db_safety_contract.md)、[权威数据源与同步](../source_of_truth_and_sync_contract.md)、[运行验收](../operational_acceptance.md)和[应急手册](../emergency_runbook.md) — 安全运行。
- [项目网站](https://donkiton.github.io/rem_site/#english)与[发布版本](https://github.com/Donkiton/rem_card/releases)。

这是一个持续演进的实验性项目。测试、静态检查、备份和保护机制不能替代独立的技术、信息安全与临床审计。

如需报告问题，或提出有关架构、性能和安全运行的建议，请联系 [menfise@mail.ru](mailto:menfise@mail.ru)，邮件主题为 `РЕМ КАРТА`。

## 许可证与责任

RemCard 的理念、架构和原创源代码作者及开发者为 Vadim Alekseevich Bityutsky（Битюцкий Вадим Алексеевич；GitHub：[DonKiton](https://github.com/DonKiton)）。

RemCard 依据 [RemCard Proprietary Source License 1.1](../../LICENSE) 分发，允许自然人进行非商业使用。个人用户或开发者可以免费查看、下载、复制、运行、研究和修改代码，也可以分享原版或修改版，但不得收取费用或获取其他商业利益。分发时必须保留许可证和作者信息，注明修改内容，提供源代码，并明确标识修改版为非官方版本。

任何组织使用，或为雇主、客户、企业、诊所及机构利益进行的使用，均须事先取得权利人的书面许可。在没有单独协议的情况下，禁止销售与转售、收费部署、安装、集成、定制开发、维护、培训、咨询、托管、SaaS、订阅、广告及其他变现行为。这些限制适用于所有衍生版本：修改字体、界面、功能、名称或程序的其他部分，并不会赋予从中获利的权利。

在 GitHub 上公开托管或创建 fork 不授予商业权利。程序按“现状”提供，用户自行承担使用责任。第三方库和依赖仍遵循各自的许可证。根据 OSI 的分类，由于商业使用受到限制，本许可证属于源码可用（source-available）许可证，而非开源（open-source）许可证。如本摘要与完整文本存在差异，以 [LICENSE](../../LICENSE) 文件为准。
