<p align="center">
  <a href="setup.ru.md"><kbd>Русский</kbd></a>
  <a href="setup.en.md"><kbd>English</kbd></a>
  <a href="setup.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="setup.ja.md"><kbd><strong>日本語</strong> ✓</kbd></a>
  <a href="setup.de.md"><kbd>Deutsch</kbd></a>
</p>

<p align="center">
  <a href="../../README.ja.md">← README</a> ·
  <a href="features.ja.md">機能</a> ·
  <a href="architecture.ja.md">アーキテクチャ</a>
</p>

# 起動・更新・参考情報

## ソースからのクイックスタート

開発と検証は、<strong>WindowsとPython 3.11</strong>で行っています。GitとPythonが必要です。`.env`ファイルは不要です。

```powershell
git clone https://github.com/Donkiton/rem_card.git
cd rem_card
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python run_remcard.py
```

`run_remcard.py`は、役割選択を備えた統合アプリケーションを開きます。

> [!CAUTION]
> 開発と試験には、本番データベースではなく、独立したデータフォルダーを使用してください。ロックやアクセス確認は、テスト環境の分離に代わるものではありません。統合シェルは、セッションへのアクセス許可に`SessionLease`を使用します。

<details>
<summary><strong>開発用データベースの選択と意図しない置き換えの防止</strong></summary>

ソース版はローカルの開発用データフォルダーを使用します。初回起動時には、不足している構造が作成される場合があります。選択したフォルダーの一覧と現在のパスは、チェックアウトごとに`.remcard/dev_database_paths.json`へ個別に保存されます。

選択した既存のデータベースは、検証した上で開かれます。見つからなくなったデータベースの代わりに、アプリケーションが通知なく空のデータベースを作成することは認められていません。旧パス`%LOCALAPPDATA%\RemCard\dev_database_paths.json`は、設定を一度だけ移行するために使用されます。

</details>

## 配布版のインストールと更新

Windows向けの運用版は、MSIやDockerイメージではなく、PyInstallerで作成したフォルダー一式として配布されます。

| ファイル | 用途 |
| --- | --- |
| `RemCard.exe` | 統合アプリケーション：役割選択、業務画面、データフォルダーの設定。 |
| `RemCardUpdater.exe` | 完全更新パッケージをインストールする補助プログラム。 |

<strong>`RemCard.exe`</strong>を起動し、初回起動時にデータフォルダーを選択してから、必要な役割を選びます。`_internal`を含む配布フォルダー全体を保持してください。EXEファイル単体では動作しません。プログラムと同じ場所に`remcard_data_path.json`が保存され、更新は`<папка данных>\UPD\releases`（選択したデータフォルダー内）から検索されます。

個別の`RemCardDoctor.exe`、`RemCardNurse.exe`、手術室用EXE、`RemCardPathSetup.exe`は旧配布方式のものです。バージョン5.1では統合ログインを使用します。このプロジェクトは<strong>完全更新パッケージのみ</strong>を配布します。SemVerの`PATCH`はバージョンの区分であり、独立したパッケージ形式を意味しません。

<details>
<summary><strong>テストビルドと安全な本番配布</strong></summary>

現在の作業ツリーから、配布を行わずにビルドするには次を実行します。

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py --test-worktree
```

このコマンドは必須チェック、PyInstaller、両EXEのスモークテストを実行しますが、`VERSION`、`CHANGELOG.md`、`app/release_info.json`を変更せず、コミット作成、`git push`、パッケージの配布も行いません。生成物には`TEST_WORKTREE_ONLY.txt`の印が付き、本番環境へ誤って配布されないようになっています。

運用向けリリースでは、まずバージョンと対象の正確なコミットがGitHubの`main`に存在している必要があります。その後、リリース担当者が`--expected-version`と`--expected-commit`を指定して`scripts\build_release.py`を実行します。独立したテストデータベースでの受け入れ確認後、別のコマンド`scripts\publish_full_update.py`でパッケージを移送します。`ready.ok`は最後に作成され、このファイルが現れると、そのデータベースを使用するクライアントは直ちに当該バージョンを利用できるようになります。詳細は[ビルドと配布](../how_to_build_and_update.md)および[自動更新](../auto_update.md)を参照してください。

</details>

## 共有SQLiteデータベースとデータの安全性

医療データベースは`archiv/rao_journal.db`にあり、テーマや背景の設定を含むマスターデータと共通設定は`settings/remcard_settings.db`に保存されます。利用者が追加した背景画像は`settings/backgrounds`に保存されます。これらのパスは、選択したデータフォルダーからの相対パスです。リポジトリ内のマスターデータ用JSONファイルは、初期データと互換性のための層として使用され、インストール済み版の運用データの正本ではありません。詳細は[設定データベースの説明](../settings_db.md)を参照してください。

共有ネットワークデータベースには、次の規則が定められています。

- `journal_mode=DELETE`、`synchronous=EXTRA`、`mmap_size=0`を使用し、共有データベースでのWALは禁止しています。
- 書き込みはコントローラー、ファイルロック、トランザクションを通じて行います。
- 稼働中のデータベースのバックアップにはSQLite Backup APIのみを使用し、開いているファイルを単純にコピーしません。
- マイグレーション、復元、ローテーションには、検証済みバックアップ、`quick_check`、`integrity_check`を使用します。

これらの対策は既知のリスクを低減しますが、SQLiteを任意のネットワーク環境に配置しても自動的に安全になるわけではありません。アクセス権、遅延、バックアップ、復元については、運用環境での個別の受け入れ確認が必要です。[データベースの安全性](../db_safety_contract.md)と[正本と同期の契約](../source_of_truth_and_sync_contract.md)を参照してください。

## 開発者向けのチェック

ローカルのチェックとGitHub Actionsは、共通のエントリーポイントを使用します。依存関係をインストールし、仮想環境を有効にした状態で実行します。

```powershell
python scripts\run_ci_checks.py core
python scripts\run_ci_checks.py ui
python scripts\run_ci_checks.py quality
python scripts\run_ci_checks.py regression
git diff --check
```

`core`はロジックとデータ、`ui`はQtインターフェース、`quality`はコード品質とアーキテクチャ上の制約、`regression`は安全性シナリオの全登録項目を検証します。CIでは各グループが独立して実行され、最終ステータス`tests`が各グループの成功とレポートの完全性を確認します。pytestモジュールは、漏れや重複なく[`tests/groups.json`](../../tests/groups.json)に割り当てられています。

`python scripts\run_ci_checks.py all`は、すべてのpytestを単一プロセスで実行して全体の分離性を確認します。個別の`quality`と`regression`を置き換えるものではありません。ランナーはデータベースとQSettingsに一時パスを割り当て、レポートを`tmp/ci-reports/<группа>`に保存します。個別の確認には、通常の`python -m pytest tests -q`も使用できます。詳細は[テストとCI](../testing.md)を参照してください。

ネットワーク検証環境、復元訓練、緊急モード、オフラインモードは、選択したデータフォルダーを読み取るか変更します。[運用受け入れ手順](../operational_acceptance.md)と[バックアップ検証手順](../backup_restore_drill.md)に従い、意図的に指定したテストデータベースでのみ実行してください。


## 現行リリースについて

<strong>RemCard 5.1.10</strong>の概要。最終更新：<strong>2026年10月22日</strong>。統合された`RemCard.exe`から4つの役割を選択でき、別プログラムの`RemCardUpdater.exe`が完全更新パッケージをインストールします。ソースのバージョン番号は[`VERSION`](../../VERSION)、各ビルドの構成はそのマニフェストに記載されています。

開発者は麻酔科・集中治療医のVadim Bityutskyです。現在のユーザーインターフェースとリンク先の詳細ドキュメントは主にロシア語です。READMEの翻訳は、アプリの画面やスクリーンショットが翻訳されていることを意味しません。

[リリース5.1.10](https://github.com/Donkiton/rem_card/releases/tag/v5.1.10)では、変更履歴が大きい場合でも、小規模な変更を差分としてローカルレプリカに転送できるようになりました。患者状態のネットワーク経由での取得は看護師画面の処理から分離され、遅れて届いた応答が新しいデータを上書きしないよう保護されています。初期状態の保存を安全に再試行する処理と、共有データベースのロック解除後に夜間バックアップを読み込む処理も改善されました。医師と看護師の共同作業の検証については[同期検証レポート](../sync_responsiveness_validation.md)、レプリカの規則と計測範囲については[レプリカの契約](../source_of_truth_and_sync_contract.md)と[計測手順](../replica_delta_benchmark.md)を参照してください。

配布パッケージ5.1.10は、共有データベースのマイグレーションと最低クライアントバージョンの引き上げを禁止する設定でビルドされています。互換性のある端末に対する<strong>任意の更新</strong>であり、すべてのコンピューターへ同時にインストールする必要はありません。初回の完全スナップショットにかかる時間は、引き続きネットワーク速度とデータベースのサイズに依存します。

> [!WARNING]
> <strong>RemCardは医療機器ではなく、臨床プロトコル、臨床判断、専門職による責任ある確認に代わるものではありません。</strong> 実際の運用には、独立した技術・情報セキュリティ・臨床検証、使用環境での受け入れ試験、および代替の業務手順が必要です。

## ドキュメント、開発状況、連絡先

- [ドキュメント一覧](../README.md) — 現行の手順と過去の資料。
- [インタラクティブなアーキテクチャマップ](https://donkiton.github.io/rem_card/codemap/codemap.html) — ソースにひも付いたモジュール、依存関係、テスト、横断的な処理フロー。
- [変更履歴](../../CHANGELOG.md)と[`VERSION`](../../VERSION) — 変更点と現在のバージョン。
- [テストとCI](../testing.md) — テストグループ、分離、網羅性の確認、レポート。
- [熱傷計算ツール](../burn_infusion_calculator.md)と[分析の臨床契約](../analytics_clinical_contract.md) — 計算、データソース、制限。
- [バージョニング](../versioning.md)、[自動更新](../auto_update.md)、[ビルド](../how_to_build_and_update.md) — 完全リリースのプロセス。
- [データベースの安全性契約](../db_safety_contract.md)、[正本と同期](../source_of_truth_and_sync_contract.md)、[運用受け入れ](../operational_acceptance.md)、[緊急時の手順](../emergency_runbook.md) — 安全な運用。
- [プロジェクトWebサイト](https://donkiton.github.io/rem_site/#english)と[リリース](https://github.com/Donkiton/rem_card/releases) — プロジェクトの紹介と配布版。

このプロジェクトは実験的なもので、活発に変更されています。テスト、静的チェック、バックアップ、保護機構は、独立した技術監査、情報セキュリティ監査、臨床監査に代わるものではありません。

不具合報告、およびアーキテクチャ、性能、安全な運用に関する提案は、[menfise@mail.ru](mailto:menfise@mail.ru)へお送りください。メールの件名は`РЕМ КАРТА`としてください。

## ライセンスと責任

RemCardの構想、アーキテクチャ、オリジナルのソースコードの著作者および開発者は、Vadim Alekseevich Bityutsky（Битюцкий Вадим Алексеевич、GitHub：[DonKiton](https://github.com/DonKiton)）です。

RemCardは[RemCard Proprietary Source License 1.1](../../LICENSE)に基づいて配布され、個人による非商用利用が認められています。個人の利用者または開発者は、金銭その他の商業的利益を得ることなく、無料でコードを閲覧、ダウンロード、コピー、実行、研究、変更し、原版または変更版を共有できます。再配布する場合は、ライセンスと著作者情報を保持し、変更内容を明示し、ソースコードを提供し、変更版が非公式であることを明確に表示する必要があります。

組織による利用、または雇用主、顧客、事業、診療所、施設のための利用には、いかなる場合も権利者の事前の書面による許可が必要です。別途合意がない限り、販売・再販売、有償の導入、インストール、統合、改修、保守、研修、コンサルティング、ホスティング、SaaS、サブスクリプション、広告、その他の収益化は禁止されています。これらの制限はすべての派生版にも適用されます。フォント、インターフェース、機能、名称、その他プログラムの一部を変更しても、収益を得る権利が付与されることはありません。

GitHubでの公開やforkは、商用利用の権利を付与しません。本プログラムは「現状のまま」提供され、利用者の責任で使用されます。サードパーティーのライブラリと依存関係には、それぞれのライセンスが適用されます。商用利用が制限されているため、OSIの分類ではオープンソースではなく、ソース公開型（source-available）のライセンスです。この概要とライセンス全文に相違がある場合は、[LICENSE](../../LICENSE)ファイルが優先されます。
