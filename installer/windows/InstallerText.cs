using System;
using System.Collections.Generic;
using System.Linq;

namespace RemCard.Setup
{
    // Source strings remain canonical diagnostics. Only presentation is translated;
    // paths, the original LICENSE, JSON keys and diagnostic codes are never rewritten.
    public static class InstallerText
    {
        private static volatile string _language = "ru";
        public static string Language { get { return _language; } }
        public static void SetLanguage(string value)
        {
            if (value != "ru" && value != "en") throw new ArgumentException("Language must be ru or en.");
            _language = value;
        }

        private static readonly Dictionary<string, string> English = new Dictionary<string, string>(StringComparer.Ordinal)
        {
            { "Знакомство", "Welcome" }, { "Условия", "License" }, { "Размещение", "Location" },
            { "Всё готово", "Review" }, { "Установка", "Installation" }, { "Завершение", "Finish" },
            { "Удаление Ремкарты", "Uninstall RemCard" }, { "Установка Ремкарты ", "Install RemCard " },
            { " — проверка интерфейса", " — interface preview" },
            { "Для тех, кто рядом\nв самые важные минуты.", "For those who care\nwhen it matters most." },
            { "© 2026 Ремкарта", "© 2026 RemCard" }, { "Сайт Ремкарты ↗", "RemCard website ↗" }, { "О проекте ↗", "About the project ↗" },
            { "РЕМКАРТА", "REMCARD" }, { "Удаление программы", "Uninstall application" }, { "Мастер установки", "Setup wizard" },
            { "Переключить светлую и тёмную тему", "Switch light and dark theme" }, { "Свернуть установщик", "Minimize setup" }, { "Закрыть установщик", "Close setup" },
            { "ВАШЕ РАБОЧЕЕ\nМЕСТО", "YOUR\nWORKSPACE" }, { "Подтверждение", "Confirmation" }, { "Удаление", "Uninstall" },
            { "УДАЛЕНИЕ ПРОГРАММЫ", "UNINSTALL APPLICATION" }, { "ЗАВЕРШЕНО", "COMPLETED" }, { "ВАШЕ РАБОЧЕЕ МЕСТО", "YOUR WORKSPACE" }, { "ШАГ ", "STEP " },
            { "Больше времени\nдля пациента.", "More time\nfor your patient." },
            { "Ремкарта — рабочее пространство команды реанимации. Назначения, наблюдение и документация в одной системе.", "RemCard is the intensive care team's workspace. Orders, observations and documentation in one system." },
            { "АНЕСТЕЗИОЛОГИЯ И РЕАНИМАТОЛОГИЯ", "ANAESTHESIOLOGY AND INTENSIVE CARE" },
            { "Реанимационная карта", "Intensive care chart" }, { "Наблюдение · Назначения · Баланс", "Observations · Orders · Fluid balance" },
            { "Ремкарты", "Care charts" }, { "Работа команды", "Teamwork" }, { "Оперблок", "Operating theatre" },
            { "Настроим программу на этом компьютере. Папку данных можно выбрать при первом запуске.", "Set up the application on this computer. Choose your data folder on first launch." },
            { "Освободим рабочее место.", "Remove this application." },
            { "Будут удалены файлы Ремкарты и созданные установщиком ярлыки. Настройки и ваши данные сохранятся.", "RemCard program files and setup-created shortcuts will be removed. Your data and settings will be kept." },
            { "Установленная программа", "Installed application" },
            { "Сетевая база, медицинские документы, локальные пользовательские данные и настройка пути к базе не удаляются.", "The shared database, medical documents, local user data and database connection settings will be kept." },
            { "Договоримся об условиях.", "Review the license terms." },
            { "Перед установкой ознакомьтесь с лицензией Ремкарты.", "Please read the RemCard license before installing." },
            { "Личное некоммерческое использование", "Personal non-commercial use" }, { "Разрешено на условиях лицензии проекта.", "Permitted under the project's license terms." },
            { "Работа в организации", "Use in an organization" },
            { "Использование в клинике или другой организации требует письменного разрешения автора и отдельного соглашения.", "Use in a clinic or another organization requires the author's written permission and a separate agreement." },
            { "Авторские права", "Copyright" }, { "Битюцкий Вадим Алексеевич · 2026", "Vadim Alekseevich Bityutskiy · 2026" },
            { "Прочитать полный текст лицензии", "Read the full license" },
            { "Я ознакомился и принимаю условия лицензионного соглашения.", "I have read and accept the license agreement." },
            { "Где разместим Ремкарту?", "Where should RemCard go?" },
            { "Выберите локальную папку программы. Общая база данных подключается отдельно.", "Choose a local application folder. Connect the shared database separately." },
            { "Рекомендуемая папка", "Recommended folder" }, { "Для текущего пользователя · без прав администратора", "Current user · no administrator rights required" },
            { "Другая папка", "Another folder" }, { "Например, D:\\RemCard", "For example, D:\\RemCard" }, { "Папка установки", "Installation folder" },
            { "Обзор  ▱", "Browse  ▱" }, { "Ярлык на рабочем столе", "Desktop shortcut" }, { "В меню «Пуск»", "Start menu shortcut" },
            { "Папка программы должна быть доступна для записи: в неё будут устанавливаться последующие обновления.", "The application folder must be writable so that future updates can be installed there." },
            { "Всё готово к установке.", "Ready to install." },
            { "Проверьте параметры. Дальше мы подготовим ваше рабочее пространство.", "Review your choices. Next, we will prepare your workspace." },
            { "Приложение", "Application" }, { "Ремкарта ", "RemCard " }, { "Для текущего пользователя", "Current user" },
            { "Рабочий стол", "Desktop" }, { "Меню «Пуск»", "Start menu" }, { "Ярлыки", "Shortcuts" }, { "Без ярлыков", "No shortcuts" },
            { "Папка данных", "Data folder" }, { "При первом запуске", "On first launch" }, { "Программа будет установлена в", "The application will be installed in" },
            { "Существующая сетевая база и документы в установку не входят.", "The existing shared database and documents are separate from this installation." },
            { "Удаляем программу.", "Removing the application." }, { "Готовим рабочее место.", "Preparing your workspace." },
            { "Данные и настройки сохраняются.", "Keeping your data and settings." }, { "Ещё немного — и можно приступать к работе.", "Just a moment. Your workspace will be ready soon." },
            { "Подготовка", "Preparing" }, { "Ход установки", "Installation progress" }, { "Проверяем готовность пакета", "Checking the installation package" },
            { "ЖУРНАЛ УДАЛЕНИЯ", "UNINSTALL LOG" }, { "ЖУРНАЛ УСТАНОВКИ", "INSTALLATION LOG" }, { "Скрыть", "Hide" }, { "Показать", "Show" },
            { "Ремкарта удалена.", "RemCard has been removed." }, { "Готово к новой смене.", "Ready for your next shift." },
            { "Файлы программы удалены. Пользовательские данные и настройки сохранены.", "Program files have been removed. Your data and settings have been kept." },
            { "Ремкарта установлена. Спасибо, что выбираете инструменты, созданные для работы рядом с пациентом.", "RemCard is installed. Thank you for choosing tools built for patient care." },
            { "Ваше рабочее пространство", "Your workspace" }, { "✓  Файлы подготовлены       ✓  Обновления готовы", "✓  Files ready       ✓  Updates ready" },
            { "Запустить Ремкарту после завершения", "Launch RemCard when setup finishes" },
            { "При первом запуске укажите общую папку данных вашего отделения.", "On first launch, select your department's shared data folder." },
            { "Программу можно установить снова в ту же папку. Настройка подключения к базе будет доступна при следующей установке.", "You can reinstall into the same folder. Database connection settings will remain available." },
            { "Удаление остановлено", "Uninstall stopped" }, { "Установка остановлена", "Installation stopped" }, { "Пожалуйста, дождитесь завершения", "Please wait for completion" },
            { "Повторить  ↻", "Retry  ↻" }, { "Отмена", "Cancel" }, { "Хорошей и спокойной смены!", "Wishing you a calm shift!" }, { "Завершить  ✓", "Finish  ✓" },
            { "Пользовательские данные сохранятся", "Your data will be kept" }, { "Удалить Ремкарту", "Uninstall RemCard" },
            { "Настройка за несколько шагов", "Set up in a few steps" }, { "Пожалуйста, ознакомьтесь с условиями", "Please review the license terms" },
            { "Папка программы на этом компьютере", "Application folder on this computer" }, { "Можно вернуться и изменить параметры", "Go back to change your choices" },
            { "Назад", "Back" }, { "Установить  ↓", "Install  ↓" }, { "Далее  →", "Next  →" },
            { "Операция отменена.", "Operation cancelled." }, { "Можно изменить параметры или попробовать снова.", "You can change your choices or try again." },
            { "Удаление прервано.", "Uninstall interrupted." }, { "Установка прервана.", "Installation interrupted." }, { "Показать журнал", "Show log" },
            { "Удаление остановлено.", "Uninstall stopped." }, { "Установка остановлена.", "Installation stopped." },
            { "Посмотрите причину ошибки и повторите попытку.", "Review the error and try again." }, { "Удаление прервано", "Uninstall interrupted" }, { "Установка прервана", "Installation interrupted" },
            { "Общие компоненты", "Shared components" }, { "Проверка экрана ошибки без записи на диск", "Error screen preview; no installation is performed" },
            { "Пакет Ремкарты ", "RemCard package " }, { " подготовлен к проверке интерфейса", " is ready for interface preview" }, { "Проверка оформления сообщения об ошибке", "Previewing error message layout" },
            { "Нет доступа к записи в папку приложения. Проверьте права на выбранную папку или выберите другой каталог.", "The application folder is not writable. Check its permissions or choose another folder." },
            { "Понятно", "OK" }, { "Лицензия Ремкарты", "RemCard license" }, { "Полный текст действующей лицензии проекта.", "The complete, original project license." }, { "К условиям", "Back to terms" },
            { "Для программы нужна локальная папка.", "Choose a local folder for the application." }, { "Эта папка будет создана при установке.", "This folder will be created during installation." },
            { "Выберите папку", "Choose a folder" }, { "Укажите место для программы. Общая папка базы данных выбирается после установки.", "Choose an application folder. Select the shared database folder after installation." },
            { "Путь к папке программы", "Application folder path" }, { "Личная папка", "User folder" }, { "На уровень выше", "Up one level" }, { "Выбрать папку", "Select folder" },
            { "Остановить операцию?", "Stop this operation?" }, { "Дождитесь остановки и завершения восстановления файлов. Не закрывайте установщик принудительно.", "Wait for the operation to stop and files to be restored. Do not force-close setup." },
            { "Остановить", "Stop" }, { "Остановка операции…", "Stopping operation…" }, { "Продолжить", "Continue" },
            { "Закрыть установщик?", "Close setup?" }, { "Вы можете вернуться к настройке в любой момент.", "You can return to setup at any time." }, { "Выйти", "Exit" }, { "Остаться", "Stay" },
            { "Не удалось открыть Ремкарту.", "Could not launch RemCard." }, { "Готово", "Done" }, { "Не удалось открыть ссылку.", "Could not open the link." },
            { "Предыдущий процесс удаления ещё завершает работу. Повторите запуск.", "The previous uninstaller is still finishing. Please try again." },
            { "Тестовая установка требует явный --target.", "Test installation requires an explicit --target." }, { "Тестовое удаление требует явный --target.", "Test uninstall requires an explicit --target." },
            { "Не удалось открыть установщик", "Could not open setup" }, { "Ремкарта — ошибка установщика", "RemCard — setup error" }, { "Закрыть", "Close" }, { "Не найден ресурс: ", "Missing resource: " },
            { "Декоративная ЭКГ: комплексы P–QRS–T с бегущей развёрткой", "Decorative ECG: scrolling P–QRS–T complexes" },
            { "ЭКГ после ошибки: прежние комплексы сохраняются, дальше рисуется изолиния", "ECG after an error: previous complexes remain, followed by a flat line" },
            { "Укажите папку установки.", "Enter an installation folder." }, { "Выберите папку на локальном диске, например D:\\RemCard.", "Choose a local disk folder, for example D:\\RemCard." },
            { "В пути к папке установки есть недопустимые символы.", "The installation path contains invalid characters." },
            { "Нельзя устанавливать программу прямо в корень диска. Выберите отдельную папку.", "The application cannot be installed in a drive root. Choose a dedicated folder." },
            { "В пути к папке установки есть недопустимый компонент.", "The installation path contains an invalid component." },
            { "В пути использовано зарезервированное Windows имя папки.", "The path contains a reserved Windows folder name." },
            { "Папка программы должна находиться на доступном локальном диске.", "The application folder must be on an available local disk." }, { "Не удалось проверить выбранный диск.", "Could not check the selected drive." },
            { "Эта папка предназначена для системных или пользовательских данных. Выберите отдельную папку программы.", "This folder is reserved for system or user data. Choose a dedicated application folder." },
            { "Выберите отдельную папку для программы, а не корневую папку профиля или документов.", "Choose a dedicated application folder within your profile or documents, not their root." },
            { "На месте выбранной папки уже существует файл.", "A file already exists at the selected folder path." },
            { "Выбрана папка исходников или базы данных. Для приложения нужна отдельная папка.", "This is a source code or database folder. Choose a dedicated application folder." },
            { "В папке уже есть одноимённые файлы, но она не распознана как установка Ремкарты. Выберите другую папку.", "This folder contains conflicting files and is not a recognized RemCard installation. Choose another folder." },
            { "В установщике отсутствует пакет приложения.", "The application package is missing from setup." }, { "В установщике отсутствует программа удаления.", "The uninstaller is missing from setup." },
            { "Версия приложения не совпадает с версией установщика.", "The application and setup versions do not match." },
            { "Проверка пакета", "Checking package" }, { "Проверяем целостность полного релиза ", "Verifying full release " },
            { "Размер файла в пакете больше ожидаемого: ", "A package file is larger than expected: " }, { "Проверка файла не пройдена: ", "File verification failed: " }, { ". Текущая установка не изменялась.", ". The existing installation was not changed." },
            { "Проверочный сбой после подготовки файлов. Рабочая установка сохранена.", "Simulated failure after file preparation. The existing installation was preserved." },
            { "Подготовка файлов", "Preparing files" }, { "Резервирование", "Creating backup" }, { "Сохраняем предыдущую версию перед заменой файлов.", "Backing up the previous version before replacing files." },
            { "Установка приложения", "Installing application" }, { "Переносим проверенную сборку в рабочую папку.", "Moving the verified release into the application folder." },
            { "Проверочный сбой при замене файлов. Выполняется возврат предыдущей версии.", "Simulated failure during file replacement. Restoring the previous version." },
            { "Ярлыки и регистрация", "Shortcuts and registration" }, { "Добавляем Ремкарту в список установленных приложений.", "Adding RemCard to installed applications." },
            { "удаление новой части", "removing new files" }, { "возврат ", "restoring " },
            { "Не удалось полностью вернуть предыдущую установку. Резервная папка: ", "The previous installation could not be fully restored. Backup folder: " },
            { "Не удалось записать файлы приложения. Проверьте права на выбранную папку и закройте Ремкарту. Предыдущая установка сохранена.", "Could not write application files. Check folder permissions and close RemCard. The previous installation was preserved." },
            { " установлена. Пользовательские настройки сохранены.", " is installed. User settings were preserved." },
            { "Папка не распознана как установленная этим мастером Ремкарта. Удаление остановлено.", "This folder is not recognized as a RemCard installation created by this setup. Uninstall stopped." },
            { "Подготовка удаления", "Preparing to uninstall" }, { "Сохраняем пользовательские настройки и проверяем файлы программы.", "Preserving user settings and checking application files." },
            { "Удаление не завершено, восстановление файлов неполное. Резервная папка: ", "Uninstall did not finish and files were not fully restored. Backup folder: " },
            { "Не удалось удалить программу. Закройте Ремкарту и повторите попытку. Файлы возвращены на место.", "Could not uninstall. Close RemCard and try again. Files were restored." },
            { "Очистка", "Cleaning up" }, { "Удаляем только файлы приложения. База, настройки и журналы сохраняются.", "Removing application files only. The database, settings and logs are kept." },
            { "Ремкарта удалена. Пользовательские данные и настройки сохранены.", "RemCard was removed. User data and settings were preserved." },
            { "Пакет приложения пуст.", "The application package is empty." }, { "Пакет содержит неизвестный файл приложения: ", "The package contains an unknown application file: " },
            { "Некорректный список файлов пакета: ", "Invalid package file inventory: " }, { "В пакете отсутствуют обязательные файлы полной сборки.", "The package is missing required full release files." },
            { "В списке файлов указан пустой путь.", "The file inventory contains an empty path." }, { "В пакете указан абсолютный путь.", "The package contains an absolute path." }, { "В пакете указан небезопасный путь.", "The package contains an unsafe path." },
            { "Файл пакета выходит за пределы папки установки.", "A package file is outside the installation folder." },
            { "Путь установки содержит символическую ссылку или точку подключения. Выберите обычную локальную папку.", "The installation path contains a symbolic link or junction. Choose a regular local folder." },
            { "В папке программы обнаружена ссылка. Операция остановлена для сохранности файлов.", "A link was found in the application folder. The operation stopped to protect your files." },
            { "На выбранном диске недостаточно свободного места для подготовки полной сборки и безопасной замены файлов.", "The selected drive does not have enough free space to prepare the full release and safely replace files." },
            { "Не удалось получить доступ к папке. Возможно, её уже использует другой установщик.", "Could not access the folder. Another installer may be using it." },
            { "В этой папке уже начато обновление Ремкарты. Дождитесь его завершения и повторите попытку. Существующая блокировка не изменялась.", "A RemCard update has started in this folder. Wait for it to finish and try again. The existing lock was not changed." },
            { "Не удалось заблокировать запуск обновления. Дождитесь завершения других операций с Ремкартой и повторите попытку.", "Could not prevent an update from starting. Wait for other RemCard operations to finish and try again." },
            { "Обновление Ремкарты ещё работает. Дождитесь завершения обновления и повторите попытку.", "RemCard is still updating. Wait for the update to finish and try again." },
            { "Закройте Ремкарту, её обновление и программу удаления перед продолжением. Не удалось проверить один из процессов.", "Close RemCard, its updater and uninstaller before continuing. One process could not be checked." },
            { "Ремкарта или её обновление ещё работают из выбранной папки. Закройте программу и повторите попытку.", "RemCard or its updater is still running from the selected folder. Close it and try again." },
            { "Очистка остановлена: обнаружена ссылка.", "Cleanup stopped: a link was detected." }, { "Внутренняя проверка пути очистки не пройдена.", "The internal cleanup path check failed." },
            { "Предупреждение: временная папка сохранена для ручной проверки: ", "Warning: a temporary folder was kept for manual inspection: " },
            { "Закройте все окна Ремкарты и дождитесь завершения сохранения данных. Установщик пока не может заблокировать новый запуск программы.", "Close all RemCard windows and wait for data to finish saving. Setup cannot yet prevent a new application launch." },
            { "Потеряна блокировка запуска Ремкарты. Операция остановлена для сохранности установки.", "The RemCard launch lock was lost. The operation stopped to protect the installation." },
            { "Ремкарта уже зарегистрирована в другой папке: ", "RemCard is already registered in another folder: " }, { ". Выберите эту папку для переустановки.", ". Choose that folder to reinstall." },
            { "Ярлык «Ремкарта» уже указывает на другую программу или установку. Сохраните или переименуйте его перед установкой либо отключите создание этого ярлыка.", "The RemCard shortcut points to another application or installation. Keep or rename it before installing, or disable this shortcut." },
            { "Ярлык «Ремкарта» изменился во время установки. Чужой ярлык не заменялся.", "The RemCard shortcut changed during installation. The other shortcut was not replaced." },
            { "Ремкарта — рабочее место команды реанимации", "RemCard — the intensive care team's workspace" },
            { "восстановление ярлыка", "restoring shortcut" }, { "восстановление регистрации", "restoring registration" },
            { "Не найден пакет приложения.", "The application package was not found." }, { "Нарушена структура установочного пакета.", "The setup package structure is invalid." },
            { "Повреждены сведения о составе пакета.", "The package inventory metadata is damaged." }, { "Не поддерживается формат установочного пакета.", "This setup package format is not supported." },
            { "Не определена версия приложения.", "The application version is invalid." }, { "В пакете повторяется имя файла: ", "Duplicate package file name: " },
            { "В пакете обнаружена ссылка вместо файла.", "The package contains a link instead of a file." }, { "Некорректный список файлов приложения.", "The application file inventory is invalid." },
            { "В пакете отсутствует файл: ", "Missing package file: " }, { "В пакете обнаружен незаявленный файл: ", "Unlisted package file: " },
            { "В пакете отсутствуют обязательные файлы Ремкарты.", "Required RemCard files are missing from the package." }, { "Установочный файл неожиданно заканчивается.", "The setup file ends unexpectedly." },
            { "Проверяем целостность Ремкарты ", "Verifying RemCard " },
            { "Установочный пакет повреждён. Получите полную копию установщика и повторите попытку.", "The setup package is damaged. Obtain a complete copy of setup and try again." },
            { "Не совпадает контрольная сумма: ", "Checksum mismatch: " }, { "Проверка файлов", "Checking files" }, { "Пустое имя файла в пакете.", "The package contains an empty file name." },
            { "Недопустимый путь в пакете.", "The package contains an invalid path." }, { "Недопустимое смещение архива.", "Invalid archive offset." }
        };
        private static readonly string[] Fragments = English.Keys.OrderByDescending(value => value.Length).ToArray();

        public static string Get(string source)
        {
            if (source == null || _language == "ru") return source;
            string translated;
            if (English.TryGetValue(source, out translated)) return translated;
            int diagnosticSeparator = source.IndexOf(" · ", StringComparison.Ordinal);
            if (source.StartsWith("RC-", StringComparison.Ordinal) && diagnosticSeparator > 0)
                return source.Substring(0, diagnosticSeparator + 3) + Get(source.Substring(diagnosticSeparator + 3));
            // Dynamic diagnostics consist of a known leading sentence plus an opaque
            // path/version and, sometimes, a known trailing sentence. Do not replace
            // words inside paths or license text.
            foreach (string key in Fragments)
            {
                if (source.StartsWith(key, StringComparison.Ordinal))
                    return English[key] + TranslateSuffix(source.Substring(key.Length));
            }
            return TranslateSuffix(source);
        }
        private static string TranslateSuffix(string source)
        {
            foreach (string key in Fragments)
                if (source.EndsWith(key, StringComparison.Ordinal))
                    return source.Substring(0, source.Length - key.Length) + English[key];
            return source;
        }
    }
}
