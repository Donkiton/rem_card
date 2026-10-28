using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace RemCard.Setup
{
    public sealed class PayloadArchive : IDisposable
    {
        private const int FooterSize = 104;
        private readonly string executable;
        private readonly long offset;
        private readonly long length;
        private readonly byte[] expectedHash;
        private readonly FileStream source;
        private readonly SegmentStream segment;
        private readonly ZipArchive zip;
        private readonly Dictionary<string, ZipArchiveEntry> entries;
        private readonly List<PayloadFile> files;
        public string Version { get; private set; }
        public string SourceCommit { get; private set; }
        public long TotalUnpackedBytes { get; private set; }
        public IList<PayloadFile> Files { get { return files.AsReadOnly(); } }
        public string PackageSha256 { get { return Hex(expectedHash); } }

        private PayloadArchive(string path)
        {
            executable = path;
            source = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
            try
            {
                if (source.Length < FooterSize) throw Invalid("В установщике отсутствует пакет приложения.");
                source.Position = source.Length - FooterSize;
                BinaryReader reader = new BinaryReader(source, Encoding.UTF8, true);
                if (Encoding.ASCII.GetString(reader.ReadBytes(16)) != "RCSETUP_PAYLOAD1") throw Invalid("Не найден пакет приложения.");
                offset = reader.ReadInt64(); length = reader.ReadInt64(); long metadataLength = reader.ReadInt64();
                expectedHash = reader.ReadBytes(32); byte[] metadataHash = reader.ReadBytes(32);
                if (offset < 1024 || length < 1 || metadataLength < 2 || metadataLength > 16 * 1024 * 1024 ||
                    offset > source.Length || length > source.Length - offset ||
                    offset + length + metadataLength != source.Length - FooterSize)
                    throw Invalid("Нарушена структура установочного пакета.");
                source.Position = offset + length;
                byte[] metadataBytes = reader.ReadBytes((int)metadataLength);
                using (SHA256 sha = SHA256.Create())
                    if (!Equal(sha.ComputeHash(metadataBytes), metadataHash)) throw Invalid("Повреждены сведения о составе пакета.");
                JavaScriptSerializer serializer = new JavaScriptSerializer(); serializer.MaxJsonLength = 16 * 1024 * 1024;
                PayloadMetadata metadata = serializer.Deserialize<PayloadMetadata>(Encoding.UTF8.GetString(metadataBytes));
                if (metadata == null || metadata.format != 1 || metadata.app != "RemCard" || metadata.files == null || metadata.files.Length < 3)
                    throw Invalid("Не поддерживается формат установочного пакета.");
                Version parsedVersion;
                if (!System.Version.TryParse(metadata.version, out parsedVersion)) throw Invalid("Не определена версия приложения.");
                Version = metadata.version; SourceCommit = metadata.source_commit;
                segment = new SegmentStream(source, offset, length);
                zip = new ZipArchive(segment, ZipArchiveMode.Read, true, Encoding.UTF8);
                entries = new Dictionary<string, ZipArchiveEntry>(StringComparer.OrdinalIgnoreCase);
                foreach (ZipArchiveEntry entry in zip.Entries)
                {
                    if (String.IsNullOrEmpty(entry.Name)) continue;
                    string relative = ValidateRelative(entry.FullName);
                    if (entries.ContainsKey(relative)) throw Invalid("В пакете повторяется имя файла: " + relative);
                    if (((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000) throw Invalid("В пакете обнаружена ссылка вместо файла.");
                    entries.Add(relative, entry);
                }
                files = new List<PayloadFile>();
                HashSet<string> listed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (PayloadMetadataFile item in metadata.files)
                {
                    string relative = ValidateRelative(item.path);
                    if (!listed.Add(relative) || item.size < 0 || item.sha256 == null || item.sha256.Length != 64)
                        throw Invalid("Некорректный список файлов приложения.");
                    ZipArchiveEntry entry;
                    if (!entries.TryGetValue(relative, out entry) || entry.Length != item.size) throw Invalid("В пакете отсутствует файл: " + relative);
                    files.Add(new PayloadFile { Path = relative, Size = item.size, Sha256 = item.sha256.ToLowerInvariant() });
                    TotalUnpackedBytes = checked(TotalUnpackedBytes + item.size);
                }
                foreach (string relative in entries.Keys)
                    if (!listed.Contains(relative) && !relative.Equals("ready.ok", StringComparison.OrdinalIgnoreCase))
                        throw Invalid("В пакете обнаружен незаявленный файл: " + relative);
                if (!listed.Contains("RemCard.exe") || !listed.Contains("RemCardUpdater.exe") || !listed.Contains("manifest.json"))
                    throw Invalid("В пакете отсутствуют обязательные файлы Ремкарты.");
            }
            catch { source.Dispose(); throw; }
        }

        public static PayloadArchive Open(string executablePath) { return new PayloadArchive(executablePath); }
        public Stream OpenFile(PayloadFile file) { return entries[ValidateRelative(file.Path)].Open(); }

        public void VerifyPackage(Action<InstallProgress> report, CancellationToken cancellation)
        {
            using (FileStream input = new FileStream(executable, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (SHA256 sha = SHA256.Create())
            {
                input.Position = offset; long remaining = length, completed = 0; int lastPercent = -1;
                byte[] buffer = new byte[1024 * 1024];
                while (remaining > 0)
                {
                    cancellation.ThrowIfCancellationRequested();
                    int count = input.Read(buffer, 0, (int)Math.Min(buffer.Length, remaining));
                    if (count == 0) throw Invalid("Установочный файл неожиданно заканчивается.");
                    sha.TransformBlock(buffer, 0, count, null, 0); remaining -= count; completed += count;
                    int percent = 2 + (int)(completed * 10 / length);
                    if (percent != lastPercent && report != null) { report(new InstallProgress(percent, "Проверка пакета", "Проверяем целостность Ремкарты " + Version)); lastPercent = percent; }
                }
                sha.TransformFinalBlock(new byte[0], 0, 0);
                if (!Equal(sha.Hash, expectedHash)) throw Invalid("Установочный пакет повреждён. Получите полную копию установщика и повторите попытку.");
            }
        }

        public void VerifyAll(Action<InstallProgress> report, CancellationToken cancellation)
        {
            VerifyPackage(report, cancellation); int completed = 0;
            foreach (PayloadFile file in files)
            {
                cancellation.ThrowIfCancellationRequested();
                using (Stream input = OpenFile(file))
                using (SHA256 sha = SHA256.Create())
                    if (!Hex(sha.ComputeHash(input)).Equals(file.Sha256, StringComparison.OrdinalIgnoreCase)) throw Invalid("Не совпадает контрольная сумма: " + file.Path);
                completed++;
                if (report != null && (completed % 100 == 0 || completed == files.Count))
                    report(new InstallProgress(12 + completed * 88 / files.Count, "Проверка файлов", file.Path));
            }
        }

        public void Dispose() { if (zip != null) zip.Dispose(); if (source != null) source.Dispose(); }
        private static InstallFailureException Invalid(string message) { return new InstallFailureException("RC-PACKAGE-001", message); }
        public static string Hex(byte[] value) { return BitConverter.ToString(value).Replace("-", "").ToLowerInvariant(); }
        private static bool Equal(byte[] first, byte[] second)
        { if (first.Length != second.Length) return false; int result = 0; for (int i = 0; i < first.Length; i++) result |= first[i] ^ second[i]; return result == 0; }
        public static string ValidateRelative(string path)
        {
            if (String.IsNullOrWhiteSpace(path)) throw Invalid("Пустое имя файла в пакете.");
            string normalized = path.Replace('\\', '/');
            if (normalized.StartsWith("/") || normalized.Contains(":") || normalized.IndexOf('\0') >= 0 ||
                normalized.Split('/').Any(part => part == ".." || part == "." || part.Length == 0)) throw Invalid("Недопустимый путь в пакете.");
            return normalized;
        }

        public sealed class PayloadMetadata
        {
            public int format { get; set; }
            public string app { get; set; }
            public string version { get; set; }
            public string source_commit { get; set; }
            public PayloadMetadataFile[] files { get; set; }
        }
        public sealed class PayloadMetadataFile
        {
            public string path { get; set; }
            public long size { get; set; }
            public string sha256 { get; set; }
        }
        private sealed class SegmentStream : Stream
        {
            private readonly Stream stream; private readonly long origin, size; private long position;
            public SegmentStream(Stream input, long start, long length) { stream = input; origin = start; size = length; }
            public override bool CanRead { get { return true; } }
            public override bool CanSeek { get { return true; } }
            public override bool CanWrite { get { return false; } }
            public override long Length { get { return size; } }
            public override long Position { get { return position; } set { Seek(value, SeekOrigin.Begin); } }
            public override int Read(byte[] buffer, int offset, int count)
            { stream.Position = origin + position; int read = stream.Read(buffer, offset, (int)Math.Min(count, size - position)); position += read; return read; }
            public override long Seek(long value, SeekOrigin seek)
            { long next = seek == SeekOrigin.Begin ? value : seek == SeekOrigin.Current ? position + value : size + value; if (next < 0 || next > size) throw new IOException("Недопустимое смещение архива."); position = next; return position; }
            public override void Flush() { }
            public override void SetLength(long value) { throw new NotSupportedException(); }
            public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
        }
    }
}
