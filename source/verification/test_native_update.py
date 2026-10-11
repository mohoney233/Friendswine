"""Offline synthetic-PE regression tests. Never opens or injects a game process."""
import copy
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / 'dist' / 'Friendswine-1.3.0' / 'bin' / 'fuxiang-inject.exe'
# Set FW_TEST_INJECTOR when testing a different rebuilt output.
import os
EXE = Path(os.environ.get('FW_TEST_INJECTOR', str(EXE)))
NAMES = ['PyGILState_Ensure', 'PyGILState_Release', 'PyRun_SimpleString', 'PyRun_SimpleStringFlags']


def fixture(shift=0):
    """Build a small, structurally valid x64 PE; code is only data for the reader."""
    b = bytearray(0x1200)
    def w16(o, v): struct.pack_into('<H', b, o, v)
    def w32(o, v): struct.pack_into('<I', b, o, v)
    b[:2] = b'MZ'; w32(0x3c, 0x80); b[0x80:0x84] = b'PE\0\0'
    w16(0x84, 0x8664); w16(0x86, 4); w16(0x94, 240); w16(0x96, 0x2022)
    opt = 0x98
    w16(opt, 0x20b)
    struct.pack_into('<Q', b, opt + 24, 0x180000000)
    w32(opt + 32, 0x1000); w32(opt + 36, 0x200)
    w32(opt + 56, 0x5000 + shift); w32(opt + 60, 0x400)
    w32(opt + 108, 16)
    w32(opt + 112, 0x2000 + shift); w32(opt + 116, 0x200)
    w32(opt + 136, 0x3000 + shift); w32(opt + 140, 36)
    for i, (name, vs, va, rs, raw, flags) in enumerate([
        (b'.text', 0x400, 0x1000, 0x400, 0x400, 0x60000020),
        (b'.rdata', 0x600, 0x2000, 0x600, 0x800, 0x40000040),
        (b'.pdata', 0x200, 0x3000, 0x200, 0xe00, 0x40000040),
        (b'.reloc', 0x200, 0x4000, 0x200, 0x1000, 0x42000040),
    ]):
        struct.pack_into('<8sIIIIIIHHI', b, opt + 240 + i * 40,
                         name, vs, va + shift, rs, raw, 0, 0, 0, 0, flags)
    b[0x400:0x800] = b'\xcc' * 0x400
    for off in (0, 0x40, 0xa0): b[0x400 + off:0x410 + off] = bytes(range(16))
    b[0x480:0x487] = b'\x33\xd2\xe9' + struct.pack('<i', 0xa0 - 0x80 - 7)
    e = 0x800
    w32(e + 20, 4); w32(e + 24, 4)
    w32(e + 28, 0x2080 + shift); w32(e + 32, 0x20a0 + shift); w32(e + 36, 0x20c0 + shift)
    name_offset = 0xe0
    for i, (name, rva) in enumerate(zip(NAMES, (0x1000, 0x1040, 0x1080, 0x10a0))):
        w32(0x880 + i * 4, rva + shift)
        w32(0x8a0 + i * 4, 0x2000 + name_offset + shift)
        w16(0x8c0 + i * 2, i)
        value = name.encode() + b'\0'
        b[0x800 + name_offset:0x800 + name_offset + len(value)] = value
        name_offset += len(value)
    b[0xb00:0xb04] = b'\x01\x00\x00\x00'
    for i, off in enumerate((0, 0x40, 0xa0)):
        struct.pack_into('<III', b, 0xe00 + i * 12, 0x1000 + off + shift, 0x1010 + off + shift, 0x2300 + shift)
    b[0xc00:0xc10] = b'resource-data-v1'
    return b


class NativeInspection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='friendswine-native-')
        cls.index = 0
        cls.baseline = cls.inspect(fixture())

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    @classmethod
    def inspect(cls, blob, supported=True):
        cls.index += 1
        path = Path(cls.temp.name) / f'fixture-{cls.index}.dll'
        path.write_bytes(blob)
        completed = subprocess.run([str(EXE), 'inspect-engine', str(path)], capture_output=True,
                                   encoding='utf-8-sig', timeout=20)
        report = json.loads(completed.stdout)
        if bool(report.get('supported')) != supported or completed.returncode != (0 if supported else 1):
            raise AssertionError((completed.returncode, report, completed.stderr))
        if supported:
            assert report['engine_sha256'] == hashlib.sha256(blob).hexdigest()
            assert len(report['native_fingerprint']) == 64
        return report

    def refused(self, blob, text):
        result = self.inspect(blob, False)
        self.assertIn(text, result['error'])

    def verify_header(self, loaded, expected=True, actual_base=0x7ff900000000):
        type(self).index += 1
        dll = Path(self.temp.name) / f'header-source-{self.index}.dll'
        header = Path(self.temp.name) / f'loaded-headers-{self.index}.bin'
        dll.write_bytes(fixture()); header.write_bytes(loaded)
        completed = subprocess.run([str(EXE), 'verify-loaded-header', str(dll), str(header), hex(actual_base)],
                                   capture_output=True, encoding='utf-8-sig', timeout=20)
        result = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0 if expected else 1)
        self.assertEqual(bool(result.get('verified')), expected)
        return result

    def test_loaded_headers_preferred_imagebase_is_valid(self):
        self.verify_header(fixture()[:0x400])

    def test_loaded_headers_actual_aslr_imagebase_is_valid(self):
        loaded = fixture()[:0x400]
        struct.pack_into('<Q', loaded, 0x98 + 24, 0x7ff900000000)
        self.verify_header(loaded)

    def test_loaded_headers_fake_imagebase_is_refused(self):
        loaded = fixture()[:0x400]
        struct.pack_into('<Q', loaded, 0x98 + 24, 0x7ff900100000)
        result = self.verify_header(loaded, False)
        self.assertIn('ImageBase', result['error'])

    def test_loaded_headers_aslr_does_not_mask_other_field_change(self):
        loaded = fixture()[:0x400]
        struct.pack_into('<Q', loaded, 0x98 + 24, 0x7ff900000000)
        loaded[0x98 + 32] ^= 1
        result = self.verify_header(loaded, False)
        self.assertIn('another changed field', result['error'])

    def test_current_fixture_all_exports_and_thunk_body(self):
        self.assertEqual(set(self.baseline['exports']), set(NAMES[:3]))
        thunk = self.baseline['exports']['PyRun_SimpleString']
        self.assertEqual(thunk['size'], 7)
        self.assertEqual(thunk['target']['size'], 16)
        self.assertEqual(self.baseline['executable_sections'][0]['size'], 0x400)

    def test_whole_image_rva_move_preserves_native_fingerprint(self):
        result = self.inspect(fixture(0x1000))
        self.assertNotEqual(result['engine_sha256'], self.baseline['engine_sha256'])
        self.assertNotEqual(result['exports'][NAMES[0]]['rva'], self.baseline['exports'][NAMES[0]]['rva'])
        self.assertEqual(result['native_fingerprint'], self.baseline['native_fingerprint'])

    def test_resource_only_change_preserves_native_fingerprint(self):
        data = fixture(); data[0xc00] ^= 1
        result = self.inspect(data)
        self.assertNotEqual(result['engine_sha256'], self.baseline['engine_sha256'])
        self.assertEqual(result['native_fingerprint'], self.baseline['native_fingerprint'])

    def test_noncritical_executable_byte_change_invalidates_fingerprint(self):
        data = fixture(); data[0x7f0] ^= 1
        result = self.inspect(data)
        self.assertEqual(result['exports'], self.baseline['exports'])
        self.assertNotEqual(result['native_fingerprint'], self.baseline['native_fingerprint'])

    def test_thunk_destination_body_change_invalidates_fingerprint(self):
        data = fixture(); data[0x4a0] ^= 1
        result = self.inspect(data)
        self.assertEqual(result['exports']['PyRun_SimpleString']['code_sha256'], self.baseline['exports']['PyRun_SimpleString']['code_sha256'])
        self.assertNotEqual(result['native_fingerprint'], self.baseline['native_fingerprint'])

    def test_malformed_dos_header(self): self.refused(b'MZ', 'missing DOS signature')
    def test_malformed_pe_offset(self):
        data = fixture(); struct.pack_into('<I', data, 0x3c, 0xfffffff0)
        self.refused(data, 'file range outside image')
    def test_non_x64_machine(self):
        data = fixture(); struct.pack_into('<H', data, 0x84, 0x14c)
        self.refused(data, 'only x64')
    def test_missing_export(self):
        data = fixture(); index = data.index(b'PyGILState_Ensure'); data[index] = ord('X')
        self.refused(data, 'missing export')
    def test_forwarded_export(self):
        data = fixture(); struct.pack_into('<I', data, 0x880, 0x21c0)
        self.refused(data, 'forwarded export')
    def test_export_in_non_executable_data(self):
        data = fixture(); struct.pack_into('<I', data, 0x888, 0x2400)
        self.refused(data, 'executable section')
    def test_export_ordinal_out_of_bounds(self):
        data = fixture(); struct.pack_into('<H', data, 0x8c0, 65535)
        self.refused(data, 'ordinal outside')
    def test_export_pointer_out_of_image(self):
        data = fixture(); struct.pack_into('<I', data, 0x820, 0xfffffff0)
        self.refused(data, 'RVA outside')
    def test_exception_directory_truncated(self):
        data = fixture(); struct.pack_into('<I', data, 0x98 + 140, 35)
        self.refused(data, 'exception directory')
    def test_exception_overlap(self):
        data = fixture(); struct.pack_into('<I', data, 0xe0c, 0x1008)
        self.refused(data, 'overlapping')
    def test_unknown_leaf_no_guessed_length(self):
        data = fixture(); struct.pack_into('<I', data, 0x880, 0x1020)
        self.refused(data, 'unbounded leaf')
    def test_unrecognized_thunk_instruction(self):
        data = fixture(); data[0x481] = 0xc9
        self.refused(data, 'thunk grammar')
    def test_thunk_target_name_mismatch(self):
        data = fixture(); struct.pack_into('<i', data, 0x483, 0x1a)
        self.refused(data, 'target does not match')
    def test_thunk_target_no_unwind_start(self):
        data = fixture(); struct.pack_into('<i', data, 0x483, 0x1a); struct.pack_into('<I', data, 0x88c, 0x10a1)
        self.refused(data, 'no unique unwind boundary')
    def test_chained_unwind_refused(self):
        data = fixture(); data[0xb00] = 0x21
        self.refused(data, 'chained/unknown')
    def test_executable_relocation_refused(self):
        data = fixture(); struct.pack_into('<II', data, 0x98 + 152, 0x4000, 12)
        struct.pack_into('<IIHH', data, 0x1000, 0x1000, 12, 0xa000, 0)
        self.refused(data, 'relocation in executable code')
    def test_relocation_block_out_of_bounds(self):
        data = fixture(); struct.pack_into('<II', data, 0x98 + 152, 0x4000, 12)
        struct.pack_into('<IIHH', data, 0x1000, 0x1000, 100, 0xa000, 0)
        self.refused(data, 'relocation block size')
    def test_overlapping_sections(self):
        data = fixture(); struct.pack_into('<I', data, 0x98 + 240 + 40 + 12, 0x1100)
        self.refused(data, 'overlapping virtual')


if __name__ == '__main__':
    started = time.time()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeInspection)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'schema': 1, 'offline_only': True, 'tests_run': result.testsRun,
              'failures': len(result.failures), 'errors': len(result.errors),
              'passed': result.wasSuccessful(), 'duration_seconds': round(time.time() - started, 3),
              'fixture_scope': ['dynamic exports', 'whole-image RVA shift', 'resource-only update',
                                'noncritical executable code change', 'bounded thunk and target',
                                'malformed PE', 'forwarder', 'missing export', 'relocations'],
              'failures_detail': [str(v) for v in result.failures + result.errors]}
    (ROOT / 'verification' / 'native-update-tests.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
