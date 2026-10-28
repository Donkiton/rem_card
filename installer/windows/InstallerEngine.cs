using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;

namespace RemCard.Setup
{
    /// <summary>Per-user installation of an immutable, verified full RemCard release.</summary>
    public static class InstallerEngine
    {
        public const string UninstallerName = "RemCardUninstall.exe";
        public const string InstallationMarkerName = "remcard_installation.json";
        public const string UninstallRegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\RemCard";
        private const string SetupLockName = ".remcard_install.lock";
        private const string UpdateStartingLockName = ".remcard_update_starting.lock";
        private static readonly string[] ApplicationFiles = {
            "RemCard.exe", "RemCardUpdater.exe", "RemCardDoctor.exe", "RemCardNurse.exe",
            "RemCardOperBlock.exe", "RemCardOperBlockEmergency.exe", "RemCardOperBlockPlanned.exe",
            "RemCardPathSetup.exe", "VERSION", "CHANGELOG.md", "manifest.json"
        };
        private static readonly string[] OwnedFiles = ApplicationFiles.Concat(new[] { UninstallerName, InstallationMarkerName }).ToArray();
        private static readonly string[] OwnedDirectories = { "_internal" };
        private static readonly HashSet<string> AllowedPayloadRootFiles = new HashSet<string>(ApplicationFiles, StringComparer.OrdinalIgnoreCase);
        private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false);

        public static string ValidateTarget(string path)
        {
            if (String.IsNullOrWhiteSpace(path)) throw Failure("PATH", "Укажите папку установки.");
            string expanded = Environment.ExpandEnvironmentVariables(path.Trim().Trim('"')).Replace('/', '\\');
            if (expanded.StartsWith(@"\\", StringComparison.Ordinal) || expanded.Length < 4 ||
                expanded[1] != ':' || expanded[2] != '\\' || !Char.IsLetter(expanded[0]))
                throw Failure("PATH", "Выберите папку на локальном диске, например D:\\RemCard.");
            string full;
            try { full = Path.GetFullPath(expanded).TrimEnd('\\'); }
            catch (Exception error) { throw Failure("PATH", "В пути к папке установки есть недопустимые символы.", error); }
            string root = Path.GetPathRoot(full);
            if (String.Equals(full.TrimEnd('\\'), root.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                throw Failure("PATH", "Нельзя устанавливать программу прямо в корень диска. Выберите отдельную папку.");
            foreach (string part in full.Substring(root.Length).Split('\\'))
            {
                if (String.IsNullOrEmpty(part) || part.EndsWith(".", StringComparison.Ordinal) || part.EndsWith(" ", StringComparison.Ordinal) ||
                    part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                    throw Failure("PATH", "В пути к папке установки есть недопустимый компонент.");
                string stem = part.Split('.')[0].ToUpperInvariant();
                if (stem == "CON" || stem == "PRN" || stem == "AUX" || stem == "NUL" ||
                    (stem.Length == 4 && (stem.StartsWith("COM", StringComparison.Ordinal) || stem.StartsWith("LPT", StringComparison.Ordinal)) && stem[3] >= '1' && stem[3] <= '9'))
                    throw Failure("PATH", "В пути использовано зарезервированное Windows имя папки.");
            }
            try
            {
                DriveInfo drive = new DriveInfo(root);
                if (!drive.IsReady || drive.DriveType == DriveType.Network)
                    throw Failure("PATH", "Папка программы должна находиться на доступном локальном диске.");
            }
            catch (InstallFailureException) { throw; }
            catch (Exception error) { throw Failure("PATH", "Не удалось проверить выбранный диск.", error); }
            string[] protectedTrees = {
                Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RemCard"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "rem_card")
            };
            foreach (string protectedTree in protectedTrees)
                if (!String.IsNullOrEmpty(protectedTree) && IsWithin(full, protectedTree))
                    throw Failure("PATH", "Эта папка предназначена для системных или пользовательских данных. Выберите отдельную папку программы.");
            string[] protectedRoots = {
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments)
            };
            if (protectedRoots.Any(item => !String.IsNullOrEmpty(item) && SamePath(full, item)))
                throw Failure("PATH", "Выберите отдельную папку для программы, а не корневую папку профиля или документов.");
            EnsureNoReparseAncestors(full);
            if (File.Exists(full)) throw Failure("PATH", "На месте выбранной папки уже существует файл.");
            if (Directory.Exists(full))
            {
                if (Directory.Exists(Path.Combine(full, ".git")) || File.Exists(Path.Combine(full, ".git")) ||
                    File.Exists(Path.Combine(full, "RemCard.spec")) || Directory.Exists(Path.Combine(full, "archiv")) ||
                    File.Exists(Path.Combine(full, "settings", "remcard_settings.db")) ||
                    File.Exists(Path.Combine(full, "rao_journal.db")))
                    throw Failure("PATH", "Выбрана папка исходников или базы данных. Для приложения нужна отдельная папка.");
                bool managedPresent = OwnedFiles.Any(name => File.Exists(Path.Combine(full, name))) || Directory.Exists(Path.Combine(full, "_internal"));
                if (managedPresent && !IsExistingInstallation(full))
                    throw Failure("PATH", "В папке уже есть одноимённые файлы, но она не распознана как установка Ремкарты. Выберите другую папку.");
            }
            return full;
        }

        public static InstallResult Install(InstallerContext context, InstallOptions options, Action<InstallProgress> report, CancellationToken token)
        {
            if (context == null || context.Payload == null || options == null)
                throw Failure("PAYLOAD", "В установщике отсутствует пакет приложения.");
            string target = ValidateTarget(options.TargetDirectory);
            ValidatePayload(context.Payload.Files);
            if (context.UninstallerBytes == null || context.UninstallerBytes.Length < 1024)
                throw Failure("PAYLOAD", "В установщике отсутствует программа удаления.");
            if (!String.Equals(context.Version, context.Payload.Version, StringComparison.Ordinal))
                throw Failure("PAYLOAD", "Версия приложения не совпадает с версией установщика.");
            Report(report, 1, "Проверка пакета", "Проверяем целостность полного релиза " + context.Version + ".");
            context.Payload.VerifyPackage(report, token);
            token.ThrowIfCancellationRequested();
            EnsureApplicationsClosed(target);
            RejectStartingUpdate(target);
            RegistrationPlan registration = options.NoRegistration ? null : new RegistrationPlan(target);
            if (registration != null) registration.ValidateChoices(options);
            bool targetCreated = !Directory.Exists(target);
            string suffix = Guid.NewGuid().ToString("N");
            string staging = Path.Combine(target, "__setup_new_" + suffix);
            string backup = Path.Combine(target, "__setup_old_" + suffix);
            List<string> backedUp = new List<string>();
            List<string> installed = new List<string>();
            bool committed = false;
            FileStream setupLock = null;
            FileStream updateStartingLock = null;
            ApplicationLaunchGuard launchGuard = null;
            try
            {
                CheckFreeSpace(target, context.Payload.TotalUnpackedBytes + context.UninstallerBytes.LongLength + 32L * 1024 * 1024);
                launchGuard = ApplicationLaunchGuard.Acquire();
                EnsureApplicationsClosed(target);
                Directory.CreateDirectory(target);
                EnsureNoReparseAncestors(target);
                setupLock = AcquireSetupLock(target);
                updateStartingLock = AcquireUpdateStartingLock(target);
                EnsureApplicationsClosed(target);
                Directory.CreateDirectory(staging);
                long copied = 0;
                int count = 0, lastPercent = -1;
                Stopwatch throttle = Stopwatch.StartNew();
                byte[] buffer = new byte[128 * 1024];
                foreach (PayloadFile file in context.Payload.Files)
                {
                    if (String.Equals(file.Path, "ready.ok", StringComparison.OrdinalIgnoreCase)) continue;
                    token.ThrowIfCancellationRequested();
                    string destination = SafeChild(staging, file.Path);
                    Directory.CreateDirectory(Path.GetDirectoryName(destination));
                    EnsureNoReparseAncestors(destination);
                    long fileBytes = 0;
                    string actualHash;
                    using (Stream input = context.Payload.OpenFile(file))
                    using (FileStream output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None, buffer.Length, FileOptions.SequentialScan))
                    using (SHA256 hash = SHA256.Create())
                    {
                        int read;
                        while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            token.ThrowIfCancellationRequested();
                            fileBytes += read;
                            if (fileBytes > file.Size) throw Failure("PAYLOAD", "Размер файла в пакете больше ожидаемого: " + file.Path);
                            output.Write(buffer, 0, read);
                            hash.TransformBlock(buffer, 0, read, buffer, 0);
                            copied += read;
                        }
                        hash.TransformFinalBlock(new byte[0], 0, 0);
                        actualHash = Hex(hash.Hash);
                        output.Flush(true);
                    }
                    if (fileBytes != file.Size || !String.Equals(actualHash, file.Sha256, StringComparison.OrdinalIgnoreCase))
                        throw Failure("PAYLOAD", "Проверка файла не пройдена: " + file.Path + ". Текущая установка не изменялась.");
                    count++;
                    if (options.SimulateFailureAfterFiles > 0 && count >= options.SimulateFailureAfterFiles)
                        throw Failure("TEST", "Проверочный сбой после подготовки файлов. Рабочая установка сохранена.");
                    int percent = 14 + (int)Math.Min(60, context.Payload.TotalUnpackedBytes == 0 ? 0 : copied * 60 / context.Payload.TotalUnpackedBytes);
                    if (percent != lastPercent || throttle.ElapsedMilliseconds >= 350)
                    {
                        Report(report, percent, "Подготовка файлов", file.Path);
                        lastPercent = percent; throttle.Restart();
                    }
                }
                File.WriteAllBytes(Path.Combine(staging, UninstallerName), context.UninstallerBytes);
                string installId = ExistingInstallId(target) ?? Guid.NewGuid().ToString("D");
                Dictionary<string, object> marker = new Dictionary<string, object> {
                    {"app", "RemCard"}, {"schema_version", 1}, {"install_id", installId},
                    {"install_location", target}, {"version", context.Version}, {"source_commit", context.SourceCommit ?? ""},
                    {"installed_at", DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture)},
                    {"registered", !options.NoRegistration}, {"desktop_shortcut", !options.NoRegistration && options.DesktopShortcut},
                    {"start_menu_shortcut", !options.NoRegistration && options.StartMenuShortcut}
                };
                File.WriteAllText(Path.Combine(staging, InstallationMarkerName), Serializer().Serialize(marker), Utf8);
                token.ThrowIfCancellationRequested();
                EnsureApplicationsClosed(target);
                EnsureNoReparseAncestors(target);
                foreach (string directory in OwnedDirectories)
                    if (Directory.Exists(Path.Combine(target, directory))) EnsureNoReparseTree(Path.Combine(target, directory));
                Directory.CreateDirectory(backup);
                Report(report, 77, "Резервирование", "Сохраняем предыдущую версию перед заменой файлов.");
                launchGuard.EnsureHeld();
                EnsureApplicationsClosed(target);
                foreach (string name in OwnedFiles.Concat(OwnedDirectories))
                {
                    string current = Path.Combine(target, name);
                    if (File.Exists(current) || Directory.Exists(current))
                    {
                        MovePath(current, Path.Combine(backup, name));
                        backedUp.Add(name);
                    }
                }
                token.ThrowIfCancellationRequested();
                Report(report, 84, "Установка приложения", "Переносим проверенную сборку в рабочую папку.");
                launchGuard.EnsureHeld();
                EnsureApplicationsClosed(target);
                string[] installOrder = OwnedDirectories.Concat(OwnedFiles.Where(name => name != "RemCard.exe")).Concat(new[] { "RemCard.exe" }).ToArray();
                foreach (string name in installOrder)
                {
                    string prepared = Path.Combine(staging, name);
                    if (!File.Exists(prepared) && !Directory.Exists(prepared)) continue;
                    token.ThrowIfCancellationRequested();
                    MovePath(prepared, Path.Combine(target, name));
                    installed.Add(name);
                    // Zero is reserved for the isolated acceptance test of a real commit rollback.
                    if (options.SimulateFailureAfterFiles == 0)
                        throw Failure("TEST", "Проверочный сбой при замене файлов. Выполняется возврат предыдущей версии.");
                }
                token.ThrowIfCancellationRequested();
                launchGuard.EnsureHeld();
                EnsureApplicationsClosed(target);
                if (registration != null)
                {
                    Report(report, 92, "Ярлыки и регистрация", "Добавляем Ремкарту в список установленных приложений.");
                    registration.Apply(context.Version, context.Payload.TotalUnpackedBytes, options);
                }
                committed = true;
            }
            catch (Exception error)
            {
                List<string> rollbackErrors = new List<string>();
                if (registration != null) registration.Rollback(rollbackErrors);
                for (int i = installed.Count - 1; i >= 0; i--)
                    TryAction(delegate { DeleteManagedPath(Path.Combine(target, installed[i])); }, "удаление новой части", rollbackErrors);
                foreach (string name in backedUp)
                {
                    string saved = Path.Combine(backup, name);
                    TryAction(delegate { if (File.Exists(saved) || Directory.Exists(saved)) MovePath(saved, Path.Combine(target, name)); }, "возврат " + name, rollbackErrors);
                }
                if (rollbackErrors.Count > 0)
                    throw Failure("ROLLBACK", "Не удалось полностью вернуть предыдущую установку. Резервная папка: " + backup + ". " + String.Join("; ", rollbackErrors), error);
                if (error is OperationCanceledException) throw;
                if (error is InstallFailureException) throw;
                throw Failure("005", "Не удалось записать файлы приложения. Проверьте права на выбранную папку и закройте Ремкарту. Предыдущая установка сохранена.", error);
            }
            finally
            {
                try
                {
                    CleanupOwnedTemporary(staging, target, report);
                    if (committed || IsMissingOrEmptyDirectory(backup)) CleanupOwnedTemporary(backup, target, report);
                }
                finally
                {
                    if (updateStartingLock != null) updateStartingLock.Dispose();
                    if (setupLock != null) setupLock.Dispose();
                    if (launchGuard != null) launchGuard.Dispose();
                    if (!committed && targetCreated) TryRemoveEmptyDirectory(target);
                }
            }
            Report(report, 100, "Готово", "Ремкарта " + context.Version + " установлена. Пользовательские настройки сохранены.");
            return new InstallResult { InstallDirectory = target, ExecutablePath = Path.Combine(target, "RemCard.exe") };
        }

        public static void Uninstall(InstallerContext context, string target, Action<InstallProgress> report, bool noRegistration, CancellationToken token)
        {
            target = ValidateTarget(target);
            Dictionary<string, object> marker = ReadMarker(target);
            if (marker == null || !ValidMarker(marker, target))
                throw Failure("IDENTITY", "Папка не распознана как установленная этим мастером Ремкарта. Удаление остановлено.");
            EnsureApplicationsClosed(target);
            RejectStartingUpdate(target);
            foreach (string directory in OwnedDirectories)
                if (Directory.Exists(Path.Combine(target, directory))) EnsureNoReparseTree(Path.Combine(target, directory));
            token.ThrowIfCancellationRequested();
            string backup = Path.Combine(target, "__setup_uninstall_" + Guid.NewGuid().ToString("N"));
            List<string> moved = new List<string>();
            bool skipRegistration = noRegistration || !GetBoolean(marker, "registered");
            RegistrationPlan registration = skipRegistration ? null : new RegistrationPlan(target, true);
            using (ApplicationLaunchGuard launchGuard = ApplicationLaunchGuard.Acquire())
            using (FileStream setupLock = AcquireSetupLock(target))
            using (FileStream updateStartingLock = AcquireUpdateStartingLock(target))
            {
                try
                {
                    Directory.CreateDirectory(backup);
                    Report(report, 15, "Подготовка удаления", "Сохраняем пользовательские настройки и проверяем файлы программы.");
                    launchGuard.EnsureHeld();
                    EnsureApplicationsClosed(target);
                    int count = 0;
                    foreach (string name in OwnedFiles.Concat(OwnedDirectories))
                    {
                        token.ThrowIfCancellationRequested();
                        string current = Path.Combine(target, name);
                        if (File.Exists(current) || Directory.Exists(current))
                        {
                            MovePath(current, Path.Combine(backup, name)); moved.Add(name);
                        }
                        Report(report, 20 + (++count * 55 / (OwnedFiles.Length + OwnedDirectories.Length)), "Удаление программы", name);
                    }
                    token.ThrowIfCancellationRequested();
                    launchGuard.EnsureHeld();
                    EnsureApplicationsClosed(target);
                    if (registration != null) registration.Remove(marker);
                }
                catch (Exception error)
                {
                    List<string> rollbackErrors = new List<string>();
                    if (registration != null) registration.Rollback(rollbackErrors);
                    foreach (string name in moved)
                    {
                        string saved = Path.Combine(backup, name);
                        TryAction(delegate { if (File.Exists(saved) || Directory.Exists(saved)) MovePath(saved, Path.Combine(target, name)); }, "возврат " + name, rollbackErrors);
                    }
                    if (rollbackErrors.Count > 0)
                        throw Failure("ROLLBACK", "Удаление не завершено, восстановление файлов неполное. Резервная папка: " + backup + ". " + String.Join("; ", rollbackErrors), error);
                    CleanupOwnedTemporary(backup, target, report);
                    if (error is OperationCanceledException || error is InstallFailureException) throw;
                    throw Failure("UNINSTALL", "Не удалось удалить программу. Закройте Ремкарту и повторите попытку. Файлы возвращены на место.", error);
                }
                // Beyond this point application files are intentionally removed. Never touch data outside target.
                Report(report, 90, "Очистка", "Удаляем только файлы приложения. База, настройки и журналы сохраняются.");
                CleanupOwnedTemporary(backup, target, report);
                if (!skipRegistration) TryRemoveEmptyDirectory(Path.GetDirectoryName(StartMenuShortcut()));
            }
            TryRemoveEmptyDirectory(target);
            Report(report, 100, "Готово", "Ремкарта удалена. Пользовательские данные и настройки сохранены.");
        }

        private static void ValidatePayload(IList<PayloadFile> files)
        {
            if (files == null || files.Count == 0) throw Failure("PAYLOAD", "Пакет приложения пуст.");
            HashSet<string> unique = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (PayloadFile file in files)
            {
                string path = NormalizeRelative(file.Path);
                if (String.Equals(path, "ready.ok", StringComparison.OrdinalIgnoreCase)) continue;
                string[] parts = path.Split('/');
                if ((parts.Length == 1 && !AllowedPayloadRootFiles.Contains(parts[0])) ||
                    (parts.Length > 1 && !String.Equals(parts[0], "_internal", StringComparison.OrdinalIgnoreCase)))
                    throw Failure("PAYLOAD", "Пакет содержит неизвестный файл приложения: " + path);
                if (!unique.Add(path) || file.Size < 0 || file.Sha256 == null || file.Sha256.Length != 64 || !file.Sha256.All(Uri.IsHexDigit))
                    throw Failure("PAYLOAD", "Некорректный список файлов пакета: " + path);
            }
            if (!unique.Contains("RemCard.exe") || !unique.Contains("RemCardUpdater.exe") || !unique.Contains("manifest.json") ||
                !unique.Any(name => name.StartsWith("_internal/", StringComparison.OrdinalIgnoreCase)))
                throw Failure("PAYLOAD", "В пакете отсутствуют обязательные файлы полной сборки.");
        }

        private static string NormalizeRelative(string path)
        {
            if (String.IsNullOrEmpty(path)) throw Failure("PAYLOAD", "В списке файлов указан пустой путь.");
            string normalized = path.Replace('\\', '/');
            if (normalized.StartsWith("/", StringComparison.Ordinal) || normalized.IndexOf(':') >= 0)
                throw Failure("PAYLOAD", "В пакете указан абсолютный путь.");
            foreach (string part in normalized.Split('/'))
                if (String.IsNullOrEmpty(part) || part == "." || part == ".." || part.EndsWith(".", StringComparison.Ordinal) ||
                    part.EndsWith(" ", StringComparison.Ordinal) || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                    throw Failure("PAYLOAD", "В пакете указан небезопасный путь.");
            return normalized;
        }

        private static string SafeChild(string root, string relative)
        {
            string child = Path.GetFullPath(Path.Combine(root, NormalizeRelative(relative).Replace('/', '\\')));
            if (!IsWithin(child, root) || SamePath(child, root)) throw Failure("PAYLOAD", "Файл пакета выходит за пределы папки установки.");
            return child;
        }

        private static void EnsureNoReparseAncestors(string path)
        {
            string current = Path.GetFullPath(path);
            while (!String.IsNullOrEmpty(current))
            {
                if (File.Exists(current) || Directory.Exists(current))
                {
                    if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                        throw Failure("PATH", "Путь установки содержит символическую ссылку или точку подключения. Выберите обычную локальную папку.");
                }
                DirectoryInfo parent = Directory.GetParent(current);
                current = parent == null ? null : parent.FullName;
            }
        }

        private static void EnsureNoReparseTree(string root)
        {
            EnsureNoReparseAncestors(root);
            foreach (string item in Directory.EnumerateFileSystemEntries(root))
            {
                FileAttributes attributes = File.GetAttributes(item);
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw Failure("PATH", "В папке программы обнаружена ссылка. Операция остановлена для сохранности файлов.");
                if ((attributes & FileAttributes.Directory) != 0) EnsureNoReparseTree(item);
            }
        }

        private static bool IsExistingInstallation(string target)
        {
            Dictionary<string, object> marker = ReadMarker(target);
            if (marker != null && ValidMarker(marker, target)) return true;
            string manifest = Path.Combine(target, "manifest.json");
            if (!File.Exists(Path.Combine(target, "RemCard.exe")) || !Directory.Exists(Path.Combine(target, "_internal")) || !File.Exists(manifest)) return false;
            try
            {
                Dictionary<string, object> data = Serializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(manifest, Encoding.UTF8));
                return GetString(data, "app") == "rem_card" && GetString(data, "package_type") == "full";
            }
            catch { return false; }
        }

        private static Dictionary<string, object> ReadMarker(string target)
        {
            string path = Path.Combine(target, InstallationMarkerName);
            if (!File.Exists(path)) return null;
            try { return Serializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8)); }
            catch { return null; }
        }

        private static bool ValidMarker(Dictionary<string, object> marker, string target)
        {
            Guid parsed;
            return marker != null && GetString(marker, "app") == "RemCard" &&
                Guid.TryParse(GetString(marker, "install_id"), out parsed) && SamePath(GetString(marker, "install_location"), target);
        }

        private static string ExistingInstallId(string target)
        { Dictionary<string, object> marker = ReadMarker(target); return ValidMarker(marker, target) ? GetString(marker, "install_id") : null; }

        private static void CheckFreeSpace(string target, long required)
        {
            DriveInfo drive = new DriveInfo(Path.GetPathRoot(target));
            if (drive.AvailableFreeSpace < required) throw Failure("SPACE", "На выбранном диске недостаточно свободного места для подготовки полной сборки и безопасной замены файлов.");
        }

        private static FileStream AcquireSetupLock(string target)
        {
            try { return new FileStream(Path.Combine(target, SetupLockName), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None, 1, FileOptions.DeleteOnClose); }
            catch (Exception error) { throw Failure("BUSY", "Не удалось получить доступ к папке. Возможно, её уже использует другой установщик.", error); }
        }

        private static void RejectStartingUpdate(string target)
        {
            string path = Path.Combine(target, UpdateStartingLockName);
            if (File.Exists(path) || Directory.Exists(path))
                throw Failure("UPDATE", "В этой папке уже начато обновление Ремкарты. Дождитесь его завершения и повторите попытку. Существующая блокировка не изменялась.");
        }

        private static FileStream AcquireUpdateStartingLock(string target)
        {
            string path = Path.Combine(target, UpdateStartingLockName);
            FileStream stream = null;
            try
            {
                EnsureNoReparseAncestors(path);
                // The accepted updater launcher reserves exactly this path with O_EXCL.
                // CreateNew closes the check/launch race; DeleteOnClose leaves no stale
                // marker even when the installer is terminated. Never open or remove
                // a marker created by an updater.
                stream = new FileStream(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None, 4096, FileOptions.DeleteOnClose);
                byte[] payload = Utf8.GetBytes(Serializer().Serialize(new Dictionary<string, object> {
                    {"state", "installer"}, {"pid", Process.GetCurrentProcess().Id},
                    {"target", target}, {"timestamp", (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds}
                }));
                stream.Write(payload, 0, payload.Length); stream.Flush(true);
                return stream;
            }
            catch (Exception error)
            {
                if (stream != null) stream.Dispose();
                throw Failure("UPDATE", "Не удалось заблокировать запуск обновления. Дождитесь завершения других операций с Ремкартой и повторите попытку.", error);
            }
        }

        private static void EnsureApplicationsClosed(string target)
        {
            HashSet<string> names = new HashSet<string>(OwnedFiles.Where(name => name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)).Select(Path.GetFileNameWithoutExtension), StringComparer.OrdinalIgnoreCase);
            foreach (Process process in Process.GetProcesses())
            {
                using (process)
                {
                    bool relevant;
                    try { relevant = names.Contains(process.ProcessName); } catch { continue; }
                    if (!relevant || process.Id == Process.GetCurrentProcess().Id) continue;
                    // Production updaters execute from an external UPD release or
                    // shadow directory, so target-directory filtering misses them.
                    if (String.Equals(process.ProcessName, "RemCardUpdater", StringComparison.OrdinalIgnoreCase))
                        throw Failure("UPDATE", "Обновление Ремкарты ещё работает. Дождитесь завершения обновления и повторите попытку.");
                    string executable;
                    try { executable = process.MainModule.FileName; }
                    catch (Exception error) { throw Failure("RUNNING", "Закройте Ремкарту, её обновление и программу удаления перед продолжением. Не удалось проверить один из процессов.", error); }
                    if (IsWithin(executable, target)) throw Failure("RUNNING", "Ремкарта или её обновление ещё работают из выбранной папки. Закройте программу и повторите попытку.");
                }
            }
        }

        private static void MovePath(string source, string destination)
        {
            EnsureNoReparseAncestors(source); EnsureNoReparseAncestors(destination);
            if (Directory.Exists(source)) Directory.Move(source, destination);
            else File.Move(source, destination);
        }

        private static void DeleteManagedPath(string path)
        {
            EnsureNoReparseAncestors(path);
            if (Directory.Exists(path)) { EnsureNoReparseTree(path); DeleteTreeWithoutLinks(path); }
            else if (File.Exists(path)) { File.SetAttributes(path, FileAttributes.Normal); File.Delete(path); }
        }

        private static void DeleteTreeWithoutLinks(string directory)
        {
            foreach (string item in Directory.EnumerateFileSystemEntries(directory))
            {
                FileAttributes attributes = File.GetAttributes(item);
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw Failure("PATH", "Очистка остановлена: обнаружена ссылка.");
                if ((attributes & FileAttributes.Directory) != 0) DeleteTreeWithoutLinks(item);
                else { File.SetAttributes(item, FileAttributes.Normal); File.Delete(item); }
            }
            Directory.Delete(directory, false);
        }

        private static void CleanupOwnedTemporary(string path, string target, Action<InstallProgress> report)
        {
            if (!Directory.Exists(path)) return;
            string full = Path.GetFullPath(path);
            if (!SamePath(Path.GetDirectoryName(full), target) || !Path.GetFileName(full).StartsWith("__setup_", StringComparison.Ordinal))
                throw Failure("PATH", "Внутренняя проверка пути очистки не пройдена.");
            try { EnsureNoReparseTree(full); DeleteTreeWithoutLinks(full); }
            catch (Exception error) { Report(report, 98, "Очистка", "Предупреждение: временная папка сохранена для ручной проверки: " + full + ". " + error.Message); }
        }

        private static void TryRemoveEmptyDirectory(string path)
        { try { if (Directory.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) == 0 && !Directory.EnumerateFileSystemEntries(path).Any()) Directory.Delete(path, false); } catch { } }

        private static bool IsMissingOrEmptyDirectory(string path)
        { try { return !Directory.Exists(path) || !Directory.EnumerateFileSystemEntries(path).Any(); } catch { return false; } }

        private static void TryAction(Action action, string description, List<string> errors)
        { try { action(); } catch (Exception error) { errors.Add(description + ": " + error.Message); } }

        private static bool SamePath(string a, string b)
        { if (String.IsNullOrEmpty(a) || String.IsNullOrEmpty(b)) return false; try { return String.Equals(Path.GetFullPath(a).TrimEnd('\\'), Path.GetFullPath(b).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase); } catch { return false; } }

        private static bool IsWithin(string child, string parent)
        { return SamePath(child, parent) || Path.GetFullPath(child).StartsWith(Path.GetFullPath(parent).TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase); }

        private static InstallFailureException Failure(string suffix, string message)
        { return new InstallFailureException("RC-INSTALL-" + suffix, message); }
        private static InstallFailureException Failure(string suffix, string message, Exception error)
        { return new InstallFailureException("RC-INSTALL-" + suffix, message, error); }
        private static void Report(Action<InstallProgress> report, int percent, string phase, string message)
        { if (report != null) report(new InstallProgress(Math.Max(0, Math.Min(100, percent)), phase, message)); }
        private static JavaScriptSerializer Serializer() { return new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 }; }
        private static string Hex(byte[] bytes) { return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant(); }
        private static string GetString(Dictionary<string, object> data, string name)
        { object value; return data != null && data.TryGetValue(name, out value) && value != null ? Convert.ToString(value, CultureInfo.InvariantCulture) : ""; }
        private static bool GetBoolean(Dictionary<string, object> data, string name)
        { object value; return data != null && data.TryGetValue(name, out value) && value is bool && (bool)value; }
        private static string DesktopShortcut() { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "Ремкарта.lnk"); }
        private static string StartMenuShortcut() { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Ремкарта", "Ремкарта.lnk"); }

        private sealed class RegistryValue
        { public object Data; public RegistryValueKind Kind; }

        /// <summary>
        /// Owns the same Windows named-pipe namespace as the accepted compiled
        /// client. The client sends SHOW and treats CLOSING as a reason to wait or
        /// exit before entering a clinical role. Keeping one maxInstances=1 pipe
        /// handle alive also prevents Qt from creating a competing listener.
        /// </summary>
        private sealed class ApplicationLaunchGuard : IDisposable
        {
            private readonly List<ClosingPipe> pipes = new List<ClosingPipe>();
            private bool disposed;

            public static ApplicationLaunchGuard Acquire()
            {
                ApplicationLaunchGuard guard = new ApplicationLaunchGuard();
                try
                {
                    guard.pipes.Add(new ClosingPipe("rem_card_unified"));
                    string testNamespace = Environment.GetEnvironmentVariable("REMCARD_TEST_INSTANCE_NAMESPACE");
                    if (!String.IsNullOrEmpty(testNamespace))
                    {
                        string suffix;
                        using (SHA256 hash = SHA256.Create()) suffix = Hex(hash.ComputeHash(Encoding.UTF8.GetBytes(testNamespace))).Substring(0, 16);
                        guard.pipes.Add(new ClosingPipe("rem_card_unified_test_" + suffix));
                    }
                    return guard;
                }
                catch (Exception error)
                {
                    guard.Dispose();
                    throw Failure("RUNNING", "Закройте все окна Ремкарты и дождитесь завершения сохранения данных. Установщик пока не может заблокировать новый запуск программы.", error);
                }
            }

            public void EnsureHeld()
            {
                if (disposed || pipes.Any(pipe => !pipe.IsHeld))
                    throw Failure("BUSY", "Потеряна блокировка запуска Ремкарты. Операция остановлена для сохранности установки.");
            }

            public void Dispose()
            {
                if (disposed) return;
                disposed = true;
                foreach (ClosingPipe pipe in pipes) pipe.Dispose();
            }

            private sealed class ClosingPipe : IDisposable
            {
                private readonly NamedPipeServerStream pipe;
                private readonly Thread worker;
                private volatile bool disposed;
                public bool IsHeld { get { return !disposed && !pipe.SafePipeHandle.IsClosed && !pipe.SafePipeHandle.IsInvalid; } }

                public ClosingPipe(string name)
                {
                    pipe = new NamedPipeServerStream(name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 4096, 4096);
                    worker = new Thread(Serve); worker.IsBackground = true; worker.Name = "RemCard setup launch guard";
                    try { worker.Start(); }
                    catch { pipe.Dispose(); throw; }
                }

                private void Serve()
                {
                    byte[] request = new byte[4];
                    byte[] response = Encoding.ASCII.GetBytes("CLOSING");
                    while (!disposed)
                    {
                        try
                        {
                            pipe.WaitForConnection();
                            int count = 0;
                            while (!disposed && count < request.Length)
                            {
                                int read = pipe.Read(request, count, request.Length - count);
                                if (read == 0) break;
                                count += read;
                            }
                            if (!disposed && count == 4 && Encoding.ASCII.GetString(request) == "SHOW")
                            {
                                pipe.Write(response, 0, response.Length); pipe.Flush(); pipe.WaitForPipeDrain();
                            }
                        }
                        catch (IOException) { if (disposed) break; }
                        catch (ObjectDisposedException) { break; }
                        finally
                        {
                            if (!disposed)
                            {
                                try { if (pipe.IsConnected) pipe.Disconnect(); }
                                catch (IOException) { }
                                catch (ObjectDisposedException) { }
                            }
                        }
                    }
                }

                public void Dispose()
                {
                    if (disposed) return;
                    disposed = true; pipe.Dispose();
                    if (Thread.CurrentThread != worker) worker.Join(1000);
                }
            }
        }

        private sealed class RegistrationPlan
        {
            private readonly string target;
            private readonly bool registryExisted;
            private readonly Dictionary<string, RegistryValue> registryValues = new Dictionary<string, RegistryValue>();
            private readonly Dictionary<string, byte[]> shortcutBefore = new Dictionary<string, byte[]>(StringComparer.OrdinalIgnoreCase);
            private readonly HashSet<string> touchedShortcuts = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            private bool touchedRegistry;

            public RegistrationPlan(string installTarget) : this(installTarget, false) { }
            public RegistrationPlan(string installTarget, bool removing)
            {
                target = installTarget;
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(UninstallRegistryPath, false))
                {
                    registryExisted = key != null;
                    if (key != null)
                    {
                        string location = Convert.ToString(key.GetValue("InstallLocation", ""), CultureInfo.InvariantCulture);
                        if (!removing && !String.IsNullOrEmpty(location) && !SamePath(location, target))
                            throw Failure("EXISTING", "Ремкарта уже зарегистрирована в другой папке: " + location + ". Выберите эту папку для переустановки.");
                        foreach (string name in key.GetValueNames()) registryValues[name] = new RegistryValue { Data = key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames), Kind = key.GetValueKind(name) };
                    }
                }
                foreach (string shortcut in new[] { DesktopShortcut(), StartMenuShortcut() })
                {
                    if (File.Exists(shortcut))
                    {
                        EnsureNoReparseAncestors(shortcut);
                        shortcutBefore[shortcut] = File.ReadAllBytes(shortcut);
                    }
                    else shortcutBefore[shortcut] = null;
                }
            }

            public void ValidateChoices(InstallOptions options)
            {
                foreach (string path in new[] { options.DesktopShortcut ? DesktopShortcut() : null, options.StartMenuShortcut ? StartMenuShortcut() : null })
                    if (path != null && File.Exists(path) && !ShortcutPointsTo(path, Path.Combine(target, "RemCard.exe")))
                        throw Failure("SHORTCUT", "Ярлык «Ремкарта» уже указывает на другую программу или установку. Сохраните или переименуйте его перед установкой либо отключите создание этого ярлыка.");
            }

            public void Apply(string version, long bytes, InstallOptions options)
            {
                touchedRegistry = true;
                using (RegistryKey key = Registry.CurrentUser.CreateSubKey(UninstallRegistryPath))
                {
                    key.SetValue("DisplayName", "Ремкарта", RegistryValueKind.String);
                    key.SetValue("DisplayVersion", version, RegistryValueKind.String);
                    key.SetValue("Publisher", "Битюцкий Вадим Алексеевич", RegistryValueKind.String);
                    key.SetValue("InstallLocation", target, RegistryValueKind.String);
                    key.SetValue("DisplayIcon", Path.Combine(target, "RemCard.exe"), RegistryValueKind.String);
                    key.SetValue("UninstallString", Quote(Path.Combine(target, UninstallerName)) + " --uninstall --target " + Quote(target), RegistryValueKind.String);
                    key.SetValue("URLInfoAbout", "https://donkiton.github.io/rem_site/", RegistryValueKind.String);
                    key.SetValue("InstallDate", DateTime.Now.ToString("yyyyMMdd", CultureInfo.InvariantCulture), RegistryValueKind.String);
                    key.SetValue("EstimatedSize", (int)Math.Min(Int32.MaxValue, (bytes + 1023) / 1024), RegistryValueKind.DWord);
                    key.SetValue("NoModify", 1, RegistryValueKind.DWord); key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                }
                SetShortcut(DesktopShortcut(), options.DesktopShortcut);
                SetShortcut(StartMenuShortcut(), options.StartMenuShortcut);
            }

            public void Remove(Dictionary<string, object> marker)
            {
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(UninstallRegistryPath, false))
                {
                    if (key != null && SamePath(Convert.ToString(key.GetValue("InstallLocation", ""), CultureInfo.InvariantCulture), target))
                        touchedRegistry = true;
                }
                if (touchedRegistry) Registry.CurrentUser.DeleteSubKeyTree(UninstallRegistryPath, false);
                if (GetBoolean(marker, "desktop_shortcut")) RemoveShortcut(DesktopShortcut());
                if (GetBoolean(marker, "start_menu_shortcut")) RemoveShortcut(StartMenuShortcut());
            }

            private void SetShortcut(string path, bool desired)
            {
                if (!desired) { RemoveShortcut(path); return; }
                if (File.Exists(path) && !ShortcutPointsTo(path, Path.Combine(target, "RemCard.exe")))
                    throw Failure("SHORTCUT", "Ярлык «Ремкарта» изменился во время установки. Чужой ярлык не заменялся.");
                touchedShortcuts.Add(path);
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                EnsureNoReparseAncestors(path);
                object shell = null, shortcut = null;
                try
                {
                    Type shellType = Type.GetTypeFromProgID("WScript.Shell", true);
                    shell = Activator.CreateInstance(shellType);
                    shortcut = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
                    Type shortcutType = shortcut.GetType();
                    shortcutType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { Path.Combine(target, "RemCard.exe") });
                    shortcutType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { target });
                    shortcutType.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "Ремкарта — рабочее место команды реанимации" });
                    shortcutType.InvokeMember("IconLocation", BindingFlags.SetProperty, null, shortcut, new object[] { Path.Combine(target, "RemCard.exe") + ",0" });
                    shortcutType.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, new object[0]);
                }
                finally { ReleaseCom(shortcut); ReleaseCom(shell); }
            }

            private void RemoveShortcut(string path)
            {
                if (!File.Exists(path) || !ShortcutPointsTo(path, Path.Combine(target, "RemCard.exe"))) return;
                touchedShortcuts.Add(path); File.Delete(path);
            }

            public void Rollback(List<string> errors)
            {
                foreach (string shortcut in touchedShortcuts)
                {
                    string path = shortcut;
                    TryAction(delegate
                    {
                        byte[] previous = shortcutBefore[path];
                        if (previous == null) { if (File.Exists(path)) File.Delete(path); }
                        else { Directory.CreateDirectory(Path.GetDirectoryName(path)); File.WriteAllBytes(path, previous); }
                    }, "восстановление ярлыка", errors);
                }
                if (!touchedRegistry) return;
                TryAction(delegate
                {
                    if (!registryExisted) Registry.CurrentUser.DeleteSubKeyTree(UninstallRegistryPath, false);
                    else using (RegistryKey key = Registry.CurrentUser.CreateSubKey(UninstallRegistryPath))
                    {
                        foreach (string name in key.GetValueNames()) key.DeleteValue(name, false);
                        foreach (KeyValuePair<string, RegistryValue> value in registryValues) key.SetValue(value.Key, value.Value.Data, value.Value.Kind);
                    }
                }, "восстановление регистрации", errors);
            }

            private static bool ShortcutPointsTo(string path, string executable)
            {
                object shell = null, shortcut = null;
                try
                {
                    Type type = Type.GetTypeFromProgID("WScript.Shell", true); shell = Activator.CreateInstance(type);
                    shortcut = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
                    object value = shortcut.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, shortcut, new object[0]);
                    return SamePath(Convert.ToString(value, CultureInfo.InvariantCulture), executable);
                }
                catch { return false; }
                finally { ReleaseCom(shortcut); ReleaseCom(shell); }
            }

            private static string Quote(string path) { return "\"" + path.Replace("\"", "") + "\""; }
            private static void ReleaseCom(object value) { if (value != null && Marshal.IsComObject(value)) Marshal.FinalReleaseComObject(value); }
        }
    }
}
