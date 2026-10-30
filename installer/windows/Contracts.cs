using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Windows.Media.Imaging;

namespace RemCard.Setup
{
    public sealed class InstallerContext : IDisposable
    {
        public string Version;
        public string SourceCommit;
        public PayloadArchive Payload;
        public byte[] UninstallerBytes;
        public string ExistingInstallDirectory;
        public bool UninstallMode;
        public bool SuppressLaunch;
        public bool NoRegistration;
        public string AutomationTargetDirectory;
        public string AutomationPreview;
        public string Language = "ru";
        public int SimulateFailureAfterFiles = -1;
        public void Dispose() { if (Payload != null) Payload.Dispose(); }
    }

    public sealed class InstallOptions
    {
        public string TargetDirectory;
        public bool DesktopShortcut = true;
        public bool StartMenuShortcut = true;
        public bool NoRegistration;
        public int SimulateFailureAfterFiles = -1;
    }

    public sealed class InstallProgress
    {
        public int Percent;
        public string Phase;
        public string Message;
        public InstallProgress(int percent, string phase, string message)
        { Percent = percent; Phase = phase; Message = message; }
    }

    public sealed class InstallResult
    {
        public string InstallDirectory;
        public string ExecutablePath;
    }

    public sealed class InstallFailureException : Exception
    {
        public string Code { get; private set; }
        public InstallFailureException(string code, string message) : base(message) { Code = code; }
        public InstallFailureException(string code, string message, Exception inner) : base(message, inner) { Code = code; }
    }

    public sealed class PayloadFile
    {
        public string Path;
        public long Size;
        public string Sha256;
    }

    // Main agent supplies PayloadArchive: Version, SourceCommit, Files,
    // TotalUnpackedBytes, OpenFile(PayloadFile), VerifyPackage(report, token), Dispose.
    // Engine supplies InstallerEngine.Install(context, options, report, token),
    // InstallerEngine.ValidateTarget(path), and
    // InstallerEngine.Uninstall(context, target, report, noRegistration, token).
    // UI supplies InstallerWindow(InstallerContext), deriving Window.
    public static class InstallerResources
    {
        public static string ReadText(string name)
        {
            using (Stream stream = typeof(InstallerResources).Assembly.GetManifestResourceStream(name))
            {
                if (stream == null) throw new InvalidOperationException("Не найден ресурс: " + name);
                using (StreamReader reader = new StreamReader(stream, System.Text.Encoding.UTF8)) return reader.ReadToEnd();
            }
        }

        public static byte[] ReadBytes(string name)
        {
            using (Stream stream = typeof(InstallerResources).Assembly.GetManifestResourceStream(name))
            {
                if (stream == null) return null;
                using (MemoryStream buffer = new MemoryStream()) { stream.CopyTo(buffer); return buffer.ToArray(); }
            }
        }

        public static BitmapImage Image(string name)
        {
            byte[] bytes = ReadBytes(name);
            if (bytes == null) return null;
            using (MemoryStream stream = new MemoryStream(bytes))
            {
                BitmapImage image = new BitmapImage(); image.BeginInit();
                image.CacheOption = BitmapCacheOption.OnLoad; image.StreamSource = stream;
                image.EndInit(); image.Freeze(); return image;
            }
        }
    }
}
