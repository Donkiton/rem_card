[CmdletBinding()]
param(
    [ValidateSet("Menu", "Connect", "Disconnect", "Status", "Cleanup", "RemCard", "Doctor", "Nurse")]
    [string]$Action = "Menu",
    [switch]$Elevated
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$script:Purpose = "RemCard emergency sandbox"
$script:Context = $null

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\", "/")
}

function Test-PathInside {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Candidate
    )

    $normalizedRoot = Get-NormalizedPath $Root
    $normalizedCandidate = Get-NormalizedPath $Candidate
    if ($normalizedCandidate.Equals($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $normalizedCandidate.StartsWith(
        $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NotReparsePoint {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label не должна быть ссылкой или точкой повторной обработки: $Path"
    }
}

function Get-TestShareName {
    param([Parameter(Mandatory = $true)][string]$BundleRoot)

    $identity = (Get-NormalizedPath $BundleRoot).TrimEnd("\").ToLowerInvariant()
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($identity)
        $hash = $sha.ComputeHash($bytes)
    }
    finally {
        $sha.Dispose()
    }
    $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
    return "RemCardTest_" + $hex.Substring(0, 12)
}

function Get-SandboxContext {
    $bundleRoot = Get-NormalizedPath $PSScriptRoot
    if ($bundleRoot.StartsWith("\\")) {
        throw "Распакуйте тестовую сборку в обычную папку на локальном диске."
    }

    $rootItem = Get-Item -LiteralPath $bundleRoot -Force
    if ($rootItem.PSProvider.Name -ne "FileSystem") {
        throw "Тестовая сборка должна находиться в локальной файловой системе."
    }
    $driveRoot = [System.IO.Path]::GetPathRoot($bundleRoot)
    $driveInfo = New-Object System.IO.DriveInfo($driveRoot)
    if ($driveInfo.DriveType -eq [System.IO.DriveType]::Network) {
        throw "Тестовую сборку нельзя запускать с сетевого диска."
    }

    $markerPath = Join-Path $bundleRoot "TEST_SANDBOX.json"
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw "Нет TEST_SANDBOX.json. Запуск вне изолированной тестовой сборки запрещён."
    }
    try {
        $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Не удалось прочитать TEST_SANDBOX.json: $($_.Exception.Message)"
    }
    if ([int]$marker.schema_version -ne 1 -or [string]$marker.purpose -cne $script:Purpose) {
        throw "Повреждён маркер изолированной тестовой сборки."
    }

    $databaseRoot = Get-NormalizedPath (Join-Path $bundleRoot "Database")
    $stateRoot = Get-NormalizedPath (Join-Path $bundleRoot "State")
    if (-not (Test-PathInside $bundleRoot $databaseRoot) -or
        -not (Test-PathInside $bundleRoot $stateRoot)) {
        throw "Папки Database и State должны находиться внутри тестовой сборки."
    }
    Assert-NotReparsePoint $bundleRoot "Папка тестовой сборки"
    Assert-NotReparsePoint $databaseRoot "Папка Database"
    Assert-NotReparsePoint $stateRoot "Папка State"

    $shareName = Get-TestShareName $bundleRoot
    return [PSCustomObject]@{
        BundleRoot = $bundleRoot
        DatabaseRoot = $databaseRoot
        StateRoot = $stateRoot
        ProgRoot = Join-Path $bundleRoot "Prog"
        MarkerPath = $markerPath
        ShareName = $shareName
        UncPath = "\\localhost\$shareName"
        ActionLog = Join-Path $stateRoot "Test-RemCard.last-action.txt"
    }
}

function Test-IsAdministrator {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-SmbCommandsAvailable {
    foreach ($name in @("Get-SmbShare", "New-SmbShare", "Remove-SmbShare")) {
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
            throw "Команда $name недоступна. Нужна Windows с поддержкой общего доступа SMB."
        }
    }
}

function Get-ExistingTestShare {
    Assert-SmbCommandsAvailable
    return Get-SmbShare -Name $script:Context.ShareName -ErrorAction SilentlyContinue
}

function Assert-ShareTargetsDatabase {
    param([Parameter(Mandatory = $true)]$Share)

    $actual = Get-NormalizedPath ([string]$Share.Path)
    if (-not $actual.Equals($script:Context.DatabaseRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw (
            "Обнаружена коллизия имени общего ресурса {0}. Ожидалась папка {1}, указана {2}. " +
            "Операция остановлена без изменений."
        ) -f $script:Context.ShareName, $script:Context.DatabaseRoot, $actual
    }
}

function Write-ActionLog {
    param(
        [Parameter(Mandatory = $true)][string]$Result,
        [Parameter(Mandatory = $true)][string]$Message
    )

    try {
        if (-not (Test-Path -LiteralPath $script:Context.StateRoot -PathType Container)) {
            New-Item -ItemType Directory -Path $script:Context.StateRoot -Force | Out-Null
        }
        $text = "{0}`r`n{1}`r`n{2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Result, $Message
        Set-Content -LiteralPath $script:Context.ActionLog -Value $text -Encoding UTF8
    }
    catch {
        # Журнал удобен, но его отказ не должен менять результат безопасной операции с ресурсом.
    }
}

function Connect-TestShare {
    if (-not (Test-IsAdministrator)) {
        throw "Для подключения тестового ресурса нужны права администратора."
    }
    if (-not (Test-Path -LiteralPath $script:Context.DatabaseRoot -PathType Container)) {
        throw "Нет папки тестовой базы: $($script:Context.DatabaseRoot)"
    }
    Assert-NotReparsePoint $script:Context.DatabaseRoot "Папка Database"

    $existing = Get-ExistingTestShare
    if ($null -ne $existing) {
        Assert-ShareTargetsDatabase $existing
        return "Тестовый сетевой ресурс уже подключён: $($script:Context.UncPath)"
    }

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    New-SmbShare `
        -Name $script:Context.ShareName `
        -Path $script:Context.DatabaseRoot `
        -FullAccess $currentUser `
        -Description "RemCard isolated emergency test database" `
        -ErrorAction Stop | Out-Null

    $created = Get-ExistingTestShare
    if ($null -eq $created) {
        throw "Windows не вернула созданный тестовый ресурс."
    }
    Assert-ShareTargetsDatabase $created
    return "Тестовая база подключена: $($script:Context.UncPath)"
}

function Close-TestDatabaseOpenFiles {
    if (-not (Get-Command "Get-SmbOpenFile" -ErrorAction SilentlyContinue) -or
        -not (Get-Command "Close-SmbOpenFile" -ErrorAction SilentlyContinue)) {
        return 0
    }

    $closed = 0
    foreach ($openFile in @(Get-SmbOpenFile -ErrorAction Stop)) {
        $openPath = [string]$openFile.Path
        if ([string]::IsNullOrWhiteSpace($openPath)) {
            continue
        }
        try {
            if (-not (Test-PathInside $script:Context.DatabaseRoot $openPath)) {
                continue
            }
        }
        catch {
            continue
        }
        Close-SmbOpenFile -FileId $openFile.FileId -Force -Confirm:$false -ErrorAction Stop
        $closed++
    }
    return $closed
}

function Disconnect-TestShare {
    if (-not (Test-IsAdministrator)) {
        throw "Для отключения тестового ресурса нужны права администратора."
    }

    $existing = Get-ExistingTestShare
    if ($null -eq $existing) {
        return "Тестовый сетевой ресурс уже отключён. Данные не удалялись."
    }
    Assert-ShareTargetsDatabase $existing

    try {
        Remove-SmbShare -Name $script:Context.ShareName -Force -Confirm:$false -ErrorAction Stop
    }
    catch {
        $firstError = $_.Exception.Message
        $closed = Close-TestDatabaseOpenFiles
        if ($closed -le 0) {
            throw "Не удалось отключить тестовый ресурс: $firstError"
        }
        $retry = Get-ExistingTestShare
        if ($null -ne $retry) {
            Assert-ShareTargetsDatabase $retry
            Remove-SmbShare -Name $script:Context.ShareName -Force -Confirm:$false -ErrorAction Stop
        }
    }

    if ($null -ne (Get-ExistingTestShare)) {
        throw "Тестовый ресурс остался подключён."
    }
    return "Тестовый сетевой ресурс отключён. Папки Database и State сохранены."
}

function Invoke-ElevatedShareAction {
    param([ValidateSet("Connect", "Disconnect", "Cleanup")][string]$RequestedAction)

    if (Test-IsAdministrator) {
        if ($RequestedAction -eq "Connect") {
            $message = Connect-TestShare
        }
        else {
            $message = Disconnect-TestShare
        }
        Write-ActionLog "OK" $message
        Write-Host $message -ForegroundColor Green
        return
    }

    $escapedScript = $PSCommandPath.Replace("'", "''")
    $command = "& '$escapedScript' -Action '$RequestedAction' -Elevated"
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($command))
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded" `
        -Verb RunAs `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        $detail = ""
        if (Test-Path -LiteralPath $script:Context.ActionLog -PathType Leaf) {
            $detail = (Get-Content -LiteralPath $script:Context.ActionLog -Raw -Encoding UTF8).Trim()
        }
        if ($detail) {
            throw "Операция с тестовым ресурсом не выполнена.`n$detail"
        }
        throw "Операция с тестовым ресурсом не выполнена (код $($process.ExitCode))."
    }
    if (Test-Path -LiteralPath $script:Context.ActionLog -PathType Leaf) {
        Write-Host ((Get-Content -LiteralPath $script:Context.ActionLog -Raw -Encoding UTF8).Trim())
    }
}

function Start-TestRemCard {
    $fileName = "RemCard.exe"
    $exePath = Join-Path $script:Context.ProgRoot $fileName
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Не найден тестовый EXE: $exePath"
    }

    $process = Start-Process `
        -FilePath $exePath `
        -WorkingDirectory $script:Context.ProgRoot `
        -WindowStyle Hidden `
        -PassThru
    Write-Host "Запущена единая тестовая RemCard (PID $($process.Id)). Выберите роль в программе." -ForegroundColor Green
}

function Write-StatusValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value,
        [ConsoleColor]$Color = [ConsoleColor]::Gray
    )

    Write-Host ("{0,-25} {1}" -f ($Name + ":"), $Value) -ForegroundColor $Color
}

function Read-JsonStatusFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Show-TestStatus {
    Write-Host ""
    Write-Host "Состояние изолированного стенда" -ForegroundColor Cyan
    Write-StatusValue "Папка" $script:Context.BundleRoot
    Write-StatusValue "Маркер стенда" "исправен" Green

    $medicalDb = Join-Path $script:Context.DatabaseRoot "archiv\rao_journal.db"
    $settingsDb = Join-Path $script:Context.DatabaseRoot "settings\remcard_settings.db"
    Write-StatusValue "Тестовая медицинская БД" $(if (Test-Path -LiteralPath $medicalDb -PathType Leaf) { "есть" } else { "НЕ НАЙДЕНА" }) $(if (Test-Path -LiteralPath $medicalDb -PathType Leaf) { "Green" } else { "Red" })
    Write-StatusValue "Тестовая БД настроек" $(if (Test-Path -LiteralPath $settingsDb -PathType Leaf) { "есть" } else { "НЕ НАЙДЕНА" }) $(if (Test-Path -LiteralPath $settingsDb -PathType Leaf) { "Green" } else { "Red" })

    try {
        $share = Get-ExistingTestShare
        if ($null -eq $share) {
            Write-StatusValue "Сеть тестовой базы" "ОТКЛЮЧЕНА" Yellow
        }
        else {
            $actual = Get-NormalizedPath ([string]$share.Path)
            if ($actual.Equals($script:Context.DatabaseRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                $uncDatabase = Join-Path $script:Context.UncPath "archiv\rao_journal.db"
                if (Test-Path -LiteralPath $uncDatabase -PathType Leaf) {
                    Write-StatusValue "Сеть тестовой базы" "доступна: $($script:Context.UncPath)" Green
                }
                else {
                    Write-StatusValue "Сеть тестовой базы" "ресурс создан, но БД по UNC недоступна" Red
                }
            }
            else {
                Write-StatusValue "Сеть тестовой базы" "КОЛЛИЗИЯ: $actual" Red
            }
        }
    }
    catch {
        Write-StatusValue "Сеть тестовой базы" "не удалось проверить: $($_.Exception.Message)" Red
    }

    $standbyMetadataPath = Join-Path $script:Context.StateRoot "Emergency\standby\standby_metadata.json"
    if (-not (Test-Path -LiteralPath $standbyMetadataPath -PathType Leaf)) {
        Write-StatusValue "Резерв для аварии" "ещё не создан" Yellow
    }
    else {
        $standby = Read-JsonStatusFile $standbyMetadataPath
        if ($null -eq $standby) {
            Write-StatusValue "Резерв для аварии" "Не готов: служебный файл повреждён" Red
        }
        else {
            $medicalSnapshot = [string]$standby.medical_db_path
            $settingsSnapshot = [string]$standby.settings_db_path
            $emergencyRoot = Join-Path $script:Context.StateRoot "Emergency"
            $snapshotsExist = $false
            try {
                $snapshotsExist = (
                    -not [string]::IsNullOrWhiteSpace($medicalSnapshot) -and
                    -not [string]::IsNullOrWhiteSpace($settingsSnapshot) -and
                    (Test-PathInside $emergencyRoot $medicalSnapshot) -and
                    (Test-PathInside $emergencyRoot $settingsSnapshot) -and
                    (Test-Path -LiteralPath $medicalSnapshot -PathType Leaf) -and
                    (Test-Path -LiteralPath $settingsSnapshot -PathType Leaf)
                )
            }
            catch {
                $snapshotsExist = $false
            }
            if ([string]$standby.validation_status -eq "valid" -and
                [string]$standby.quick_check_status -eq "ok" -and
                $snapshotsExist) {
                $updatedText = [string]$standby.updated_at
                try {
                    $updatedText = ([datetimeoffset]::Parse($updatedText)).ToLocalTime().ToString("dd.MM.yyyy HH:mm:ss")
                }
                catch {
                    # Если формат времени новый, показываем исходное значение без потери статуса.
                }
                Write-StatusValue "Резерв для аварии" "Готов; обновлён $updatedText" Green
            }
            elseif (-not $snapshotsExist) {
                Write-StatusValue "Резерв для аварии" "Не готов: нет обоих файлов резерва" Red
            }
            else {
                Write-StatusValue "Резерв для аварии" "Не готов: внутренняя проверка копии не пройдена" Red
            }
        }
    }

    $activeRoot = Join-Path $script:Context.StateRoot "Emergency\active"
    $sessions = @()
    if (Test-Path -LiteralPath $activeRoot -PathType Container) {
        $sessions = @(Get-ChildItem -LiteralPath $activeRoot -Directory -Force -ErrorAction SilentlyContinue)
    }
    if ($sessions.Count -eq 0) {
        Write-StatusValue "Аварийная сессия" "нет"
    }
    else {
        foreach ($sessionDir in $sessions) {
            $metadataPath = Join-Path $sessionDir.FullName "emergency_session.json"
            $metadata = if (Test-Path -LiteralPath $metadataPath -PathType Leaf) { Read-JsonStatusFile $metadataPath } else { $null }
            if ($null -eq $metadata) {
                Write-StatusValue "Аварийная сессия" "$($sessionDir.Name): служебный файл отсутствует/повреждён" Red
            }
            else {
                Write-StatusValue "Аварийная сессия" ("{0}: статус {1}, роль {2}" -f $sessionDir.Name, $metadata.status, $metadata.source_role) Yellow
            }
        }
    }
    Write-StatusValue "Логи" (Join-Path $script:Context.StateRoot "Logs")
    Write-StatusValue "Локальное состояние" (Join-Path $script:Context.StateRoot "LocalAppData\RemCard")
    Write-Host ""
}

function Show-Menu {
    while ($true) {
        Clear-Host
        Write-Host "RemCard — изолированная проверка аварийного режима" -ForegroundColor Cyan
        Write-Host "Только синтетические данные из этой папки."
        Write-Host "Сначала выберите 1, затем запускайте RemCard. Сеть сама не восстанавливается."
        Show-TestStatus
        Write-Host "1  Подключить / восстановить тестовую сеть"
        Write-Host "2  Запустить единую тестовую RemCard"
        Write-Host "3  Отключить тестовую сеть (имитация сбоя)"
        Write-Host "4  Обновить состояние"
        Write-Host "0  Завершить и убрать только тестовый сетевой ресурс"
        Write-Host ""
        $choice = Read-Host "Выберите действие"
        try {
            switch ($choice) {
                "1" { Invoke-ElevatedShareAction "Connect" }
                "2" { Start-TestRemCard }
                "3" { Invoke-ElevatedShareAction "Disconnect" }
                "4" { }
                "0" {
                    Invoke-ElevatedShareAction "Cleanup"
                    Write-Host "Работа меню завершена. Тестовые данные сохранены." -ForegroundColor Green
                    return
                }
                default { Write-Host "Неизвестный пункт меню." -ForegroundColor Yellow }
            }
        }
        catch {
            Write-Host "Ошибка: $($_.Exception.Message)" -ForegroundColor Red
        }
        Write-Host ""
        [void](Read-Host "Нажмите Enter для продолжения")
    }
}

try {
    $script:Context = Get-SandboxContext
    switch ($Action) {
        "Menu" { Show-Menu }
        "Connect" { Invoke-ElevatedShareAction "Connect" }
        "Disconnect" { Invoke-ElevatedShareAction "Disconnect" }
        "Cleanup" { Invoke-ElevatedShareAction "Cleanup" }
        "Status" { Show-TestStatus }
        "RemCard" { Start-TestRemCard }
        # Старые команды автоматизации остаются безопасными алиасами единого входа.
        "Doctor" { Start-TestRemCard }
        "Nurse" { Start-TestRemCard }
    }
}
catch {
    $message = $_.Exception.Message
    if ($null -ne $script:Context -and $Elevated) {
        Write-ActionLog "ERROR" $message
    }
    Write-Error $message
    exit 1
}
