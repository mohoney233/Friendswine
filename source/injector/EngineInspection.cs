using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Fuxiang
{
    // This is an offline PE reader. It never loads the DLL or resolves through the
    // Windows loader. Unknown layout/ABI evidence is an explicit refusal.
    internal sealed class EngineInspection : IDisposable
    {
        internal static readonly string[] Required = { "PyGILState_Ensure", "PyGILState_Release", "PyRun_SimpleString" };
        internal sealed class Section
        {
            internal string Name;
            internal uint Rva, VirtualSize, RawSize, RawOffset, Characteristics;
            internal bool Executable => (Characteristics & 0x20000000) != 0;
            internal string Hash;
        }
        internal sealed class Procedure
        {
            internal string Name, Hash, Boundary;
            internal uint Rva, Size;
            internal Procedure Target;
            internal object Json() => new {
                rva = Rva, size = Size, code_sha256 = Hash, boundary_kind = Boundary,
                target = Target == null ? null : Target.Json()
            };
            internal string Fingerprint() => Name + ":" + Size + ":" + Hash + ":" + Boundary
                + (Target == null ? "" : ":target:" + Target.Size + ":" + Target.Hash);
        }
        readonly FileStream stream;
        readonly List<Section> sections = new();
        readonly Dictionary<uint, (uint End, uint Unwind)> functions = new();
        readonly Dictionary<string, uint> exports = new(StringComparer.Ordinal);
        internal readonly Dictionary<string, Procedure> Procedures = new(StringComparer.Ordinal);
        internal readonly string PathName;
        internal uint ImageSize, HeaderSize;
        internal int ImageBaseFieldOffset;
        internal ushort Machine;
        internal string EngineHash, Fingerprint;
        internal uint ExportRva, ExportSize, ExceptionRva, ExceptionSize;
        uint relocRva, relocSize;
        internal IReadOnlyList<Section> Sections => sections;

        static InvalidDataException Bad(string detail) => new InvalidDataException("unsupported_engine: " + detail);
        static string Hex(byte[] b) => Convert.ToHexString(b).ToLowerInvariant();
        internal EngineInspection(string path)
        {
            PathName = System.IO.Path.GetFullPath(path);
            // Deny replacement/writes for the whole inspection and live verification.
            stream = new FileStream(PathName, FileMode.Open, FileAccess.Read, FileShare.Read);
            try { Parse(); }
            catch { stream.Dispose(); throw; }
        }
        public void Dispose() => stream.Dispose();

        void Range(long offset, long count)
        {
            if (offset < 0 || count < 0 || offset > stream.Length || count > stream.Length - offset)
                throw Bad("file range outside image");
        }
        internal byte[] FileBytes(long offset, int count)
        {
            Range(offset, count);
            byte[] b = new byte[count];
            stream.Position = offset;
            stream.ReadExactly(b);
            return b;
        }
        ushort U16(long at) => BitConverter.ToUInt16(FileBytes(at, 2));
        uint U32(long at) => BitConverter.ToUInt32(FileBytes(at, 4));
        internal long Offset(uint rva, uint count)
        {
            if ((ulong)rva + count > ImageSize) throw Bad("RVA outside image");
            if (rva < HeaderSize && (ulong)rva + count <= HeaderSize) { Range(rva, count); return rva; }
            Section found = null;
            foreach (var s in sections)
            {
                if (rva >= s.Rva && (ulong)rva + count <= (ulong)s.Rva + s.RawSize)
                {
                    if (found != null) throw Bad("ambiguous RVA mapping");
                    found = s;
                }
            }
            if (found == null) throw Bad("RVA is not wholly backed by file data");
            long off = (long)found.RawOffset + rva - found.Rva;
            Range(off, count);
            return off;
        }
        internal byte[] RvaBytes(uint rva, int count) => FileBytes(Offset(rva, checked((uint)count)), count);
        Section CodeSection(uint rva, uint count)
        {
            var candidates = sections.Where(s => s.Executable && rva >= s.Rva
                && (ulong)rva + count <= (ulong)s.Rva + s.VirtualSize
                && (ulong)rva + count <= (ulong)s.Rva + s.RawSize).ToList();
            if (candidates.Count != 1 || count == 0) throw Bad("function is not in one file-backed executable section");
            return candidates[0];
        }
        string StringAt(uint rva)
        {
            var s = sections.SingleOrDefault(s => rva >= s.Rva && rva < (ulong)s.Rva + s.RawSize);
            if (s == null) throw Bad("export name outside section");
            int length = (int)Math.Min(4096UL, (ulong)s.Rva + s.RawSize - rva);
            var bytes = RvaBytes(rva, length);
            int end = Array.IndexOf(bytes, (byte)0);
            if (end < 1 || bytes.Take(end).Any(b => b > 127)) throw Bad("invalid or unterminated export name");
            return Encoding.ASCII.GetString(bytes, 0, end);
        }
        string HashRange(long offset, long count)
        {
            Range(offset, count);
            using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            byte[] chunk = new byte[1024 * 1024];
            stream.Position = offset;
            while (count > 0)
            {
                int size = (int)Math.Min(chunk.Length, count);
                stream.ReadExactly(chunk.AsSpan(0, size));
                hash.AppendData(chunk, 0, size);
                count -= size;
            }
            return Hex(hash.GetHashAndReset());
        }
        void Parse()
        {
            if (stream.Length < 64 || U16(0) != 0x5a4d) throw Bad("missing DOS signature");
            uint pe = U32(0x3c);
            Range(pe, 24);
            if (U32(pe) != 0x00004550) throw Bad("missing PE signature");
            Machine = U16(pe + 4);
            if (Machine != 0x8664) throw Bad("only x64 PE is supported");
            ushort count = U16(pe + 6), optionalSize = U16(pe + 20);
            if (count == 0 || count > 96 || optionalSize < 160) throw Bad("invalid PE header sizes");
            long opt = (long)pe + 24;
            Range(opt, optionalSize);
            if (U16(opt) != 0x20b) throw Bad("only PE32+ is supported");
            ImageBaseFieldOffset = checked((int)(opt + 24));
            ImageSize = U32(opt + 56); HeaderSize = U32(opt + 60);
            uint directoryCount = U32(opt + 108);
            if (directoryCount < 6 || directoryCount > 16 || optionalSize < 112 + directoryCount * 8)
                throw Bad("missing or truncated data directories");
            if (ImageSize == 0 || HeaderSize == 0 || HeaderSize > ImageSize || HeaderSize > stream.Length)
                throw Bad("invalid image/header size");
            long sec = opt + optionalSize;
            Range(sec, count * 40L);
            if (sec + count * 40L > HeaderSize) throw Bad("section table extends beyond headers");
            for (int i = 0; i < count; i++)
            {
                long at = sec + i * 40L;
                var s = new Section {
                    Name = Encoding.ASCII.GetString(FileBytes(at, 8)).TrimEnd('\0'),
                    VirtualSize = U32(at + 8), Rva = U32(at + 12), RawSize = U32(at + 16),
                    RawOffset = U32(at + 20), Characteristics = U32(at + 36)
                };
                ulong span = Math.Max(s.VirtualSize, s.RawSize);
                if ((ulong)s.Rva + span > ImageSize || (span > 0 && s.Rva < HeaderSize))
                    throw Bad("section outside image or overlapping headers");
                Range(s.RawOffset, s.RawSize);
                if (s.RawSize > 0 && s.RawOffset < HeaderSize) throw Bad("section overlaps file headers");
                foreach (var previous in sections)
                {
                    ulong previousSpan = Math.Max(previous.VirtualSize, previous.RawSize);
                    if (span > 0 && previousSpan > 0 && s.Rva < (ulong)previous.Rva + previousSpan
                        && previous.Rva < (ulong)s.Rva + span) throw Bad("overlapping virtual sections");
                    if (s.RawSize > 0 && previous.RawSize > 0 && s.RawOffset < (ulong)previous.RawOffset + previous.RawSize
                        && previous.RawOffset < (ulong)s.RawOffset + s.RawSize) throw Bad("overlapping raw sections");
                }
                if (s.Executable && (s.VirtualSize == 0 || s.VirtualSize > s.RawSize))
                    throw Bad("uninitialized executable section is unsupported");
                sections.Add(s);
            }
            if (!sections.Any(s => s.Executable)) throw Bad("no executable sections");
            ExportRva = U32(opt + 112); ExportSize = U32(opt + 116);
            ExceptionRva = U32(opt + 136); ExceptionSize = U32(opt + 140);
            relocRva = U32(opt + 152); relocSize = U32(opt + 156);
            ParseExports();
            ParseFunctions();
            RejectCodeRelocations();
            foreach (var name in Required)
            {
                if (!exports.TryGetValue(name, out uint rva) || rva == 0) throw Bad("missing export: " + name);
                if (rva >= ExportRva && rva < (ulong)ExportRva + ExportSize) throw Bad("forwarded export: " + name);
                Procedures.Add(name, ParseProcedure(name, rva));
            }
            foreach (var section in sections.Where(s => s.Executable)) section.Hash = HashRange(section.RawOffset, section.RawSize);
            EngineHash = HashRange(0, stream.Length);
            var text = new StringBuilder("friendswine-native-v1\nx64\n");
            foreach (var name in Required) text.Append(Procedures[name].Fingerprint()).Append('\n');
            foreach (var s in sections.Where(s => s.Executable).OrderBy(s => s.Name, StringComparer.Ordinal))
                text.Append("section:").Append(s.Name).Append(':').Append(s.VirtualSize).Append(':').Append(s.RawSize).Append(':').Append(s.Hash).Append('\n');
            Fingerprint = Hex(SHA256.HashData(Encoding.UTF8.GetBytes(text.ToString())));
        }
        void ParseExports()
        {
            if (ExportRva == 0 || ExportSize < 40) throw Bad("missing export directory");
            Offset(ExportRva, ExportSize);
            byte[] d = RvaBytes(ExportRva, 40);
            uint numberFunctions = BitConverter.ToUInt32(d, 20), names = BitConverter.ToUInt32(d, 24);
            if (numberFunctions == 0 || names == 0 || names > numberFunctions || numberFunctions > 1000000)
                throw Bad("invalid export counts");
            byte[] f = RvaBytes(BitConverter.ToUInt32(d, 28), checked((int)numberFunctions * 4));
            byte[] n = RvaBytes(BitConverter.ToUInt32(d, 32), checked((int)names * 4));
            byte[] o = RvaBytes(BitConverter.ToUInt32(d, 36), checked((int)names * 2));
            for (int i = 0; i < names; i++)
            {
                ushort ordinal = BitConverter.ToUInt16(o, i * 2);
                if (ordinal >= numberFunctions) throw Bad("export ordinal outside address table");
                string name = StringAt(BitConverter.ToUInt32(n, i * 4));
                if (!exports.TryAdd(name, BitConverter.ToUInt32(f, ordinal * 4))) throw Bad("duplicate export name");
            }
        }
        void ParseFunctions()
        {
            if (ExceptionRva == 0 || ExceptionSize == 0 || ExceptionSize % 12 != 0)
                throw Bad("missing/truncated x64 exception directory");
            long at = Offset(ExceptionRva, ExceptionSize);
            uint previousEnd = 0;
            for (uint i = 0; i < ExceptionSize; i += 12)
            {
                byte[] entry = FileBytes(at + i, 12);
                uint begin = BitConverter.ToUInt32(entry, 0), end = BitConverter.ToUInt32(entry, 4), unwind = BitConverter.ToUInt32(entry, 8);
                if (begin == 0 || end <= begin || begin < previousEnd || end > ImageSize || unwind == 0 || !functions.TryAdd(begin, (end, unwind)))
                    throw Bad("invalid, overlapping, or unordered runtime function table");
                Offset(unwind, 4);
                previousEnd = end;
            }
        }
        void RejectCodeRelocations()
        {
            if (relocRva == 0 && relocSize == 0) return;
            if (relocRva == 0 || relocSize < 8) throw Bad("invalid relocation directory");
            long start = Offset(relocRva, relocSize);
            uint pos = 0;
            while (pos < relocSize)
            {
                if (relocSize - pos < 8) throw Bad("truncated relocation block");
                uint page = U32(start + pos), size = U32(start + pos + 4);
                if (size < 8 || size % 2 != 0 || size > relocSize - pos) throw Bad("invalid relocation block size");
                var block = FileBytes(start + pos + 8, (int)size - 8);
                for (int i = 0; i < block.Length; i += 2)
                {
                    ushort entry = BitConverter.ToUInt16(block, i);
                    int type = entry >> 12;
                    if (type == 0) continue;
                    if (type != 10) throw Bad("unsupported x64 relocation type " + type);
                    ulong target = (ulong)page + (uint)(entry & 4095);
                    if (target + 8 > ImageSize) throw Bad("relocation target outside image");
                    if (sections.Any(s => s.Executable && target < (ulong)s.Rva + Math.Max(s.VirtualSize, s.RawSize) && target + 8 > s.Rva))
                        throw Bad("relocation in executable code requires a new reviewed normalization policy");
                }
                pos += size;
            }
        }
        Procedure BoundedProcedure(string name, uint rva)
        {
            if (!functions.TryGetValue(rva, out var f)) throw Bad("no unique unwind boundary: " + name);
            CodeSection(rva, f.End - rva);
            byte flagsVersion = RvaBytes(f.Unwind, 1)[0];
            if ((flagsVersion & 7) != 1 || (flagsVersion >> 3 & 4) != 0)
                throw Bad("chained/unknown unwind layout requires review: " + name);
            return new Procedure { Name = name, Rva = rva, Size = f.End - rva,
                Hash = HashRange(Offset(rva, f.End - rva), f.End - rva), Boundary = "x64-unwind" };
        }
        Procedure ParseProcedure(string name, uint rva)
        {
            if (functions.ContainsKey(rva)) return BoundedProcedure(name, rva);
            // The one explicitly supported leaf grammar is the CPython one-argument
            // wrapper: xor edx,edx; jmp rel32. Its complete instruction length is 7.
            // The jump must name PyRun_SimpleStringFlags and have a proven .pdata body.
            // No arbitrary prefix hash, RET scan, or guessed instruction decoding.
            if (name != "PyRun_SimpleString") throw Bad("unbounded leaf function: " + name);
            CodeSection(rva, 7);
            byte[] code = RvaBytes(rva, 7);
            if (code[0] != 0x33 || code[1] != 0xd2 || code[2] != 0xe9)
                throw Bad("unsupported leaf thunk grammar: " + name);
            long target = (long)rva + 7 + BitConverter.ToInt32(code, 3);
            if (target <= 0 || target > uint.MaxValue || !exports.TryGetValue("PyRun_SimpleStringFlags", out uint namedTarget) || namedTarget != target)
                throw Bad("leaf thunk target does not match PyRun_SimpleStringFlags");
            if (functions.Any(f => rva >= f.Key && rva < f.Value.End)) throw Bad("thunk is interior to another function");
            return new Procedure { Name = name, Rva = rva, Size = 7, Hash = Hex(SHA256.HashData(code)),
                Boundary = "xor-edx-jmp-rel32-to-unwind", Target = BoundedProcedure("PyRun_SimpleStringFlags", (uint)target) };
        }
        internal object Json() => new {
            schema = 1, supported = true, machine = Machine, architecture = "x64", image_size = ImageSize,
            engine_sha256 = EngineHash,
            exports = Procedures.ToDictionary(kv => kv.Key, kv => kv.Value.Json(), StringComparer.Ordinal),
            executable_sections = sections.Where(s => s.Executable).Select(s => new { name = s.Name, size = s.RawSize, virtual_size = s.VirtualSize, sha256 = s.Hash }).ToArray(),
            native_fingerprint = Fingerprint,
            fingerprint_policy = "x64-unwind-or-reviewed-thunk-plus-all-executable-sections-v1"
        };
        internal string ToJson() => JsonSerializer.Serialize(Json());

        // Some loaders update OptionalHeader.ImageBase to the actual mapped base.
        // Accept exactly that documented address value or the disk preferred base;
        // this is a value check, never an unconditional mask of arbitrary bytes.
        internal byte[] NormalizeLoadedHeaders(byte[] loaded, ulong actualBase)
        {
            if (loaded.Length != HeaderSize) throw Bad("loaded header length mismatch");
            byte[] disk = FileBytes(0, loaded.Length);
            ulong preferred = BitConverter.ToUInt64(disk, ImageBaseFieldOffset);
            ulong observed = BitConverter.ToUInt64(loaded, ImageBaseFieldOffset);
            if (actualBase == 0 || (observed != preferred && observed != actualBase))
                throw Bad("loaded ImageBase is neither the preferred nor actual module address");
            byte[] normalized = (byte[])loaded.Clone();
            Array.Copy(disk, ImageBaseFieldOffset, normalized, ImageBaseFieldOffset, 8);
            return normalized;
        }

        internal void VerifyLoadedHeaders(byte[] loaded, ulong actualBase)
        {
            byte[] normalized = NormalizeLoadedHeaders(loaded, actualBase);
            byte[] disk = FileBytes(0, normalized.Length);
            if (!disk.AsSpan().SequenceEqual(normalized))
                throw Bad("loaded header contains another changed field");
        }
    }
}
