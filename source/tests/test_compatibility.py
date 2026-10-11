import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

import compatibility as comp


def profile(profile_id='fixture', native='a' * 64):
    function = 'ClientWorlds.WorldArticle:WorldArticle.update'
    return {'schema': comp.PROFILE_SCHEMA, 'id': profile_id, 'revision': 1,
            'architecture': 'x64', 'native_fingerprint': native,
            'runtime': {'schema': 1, 'python_version': '3.11',
                        'functions': {function: 'b' * 64},
                        'capabilities': {'display': [function], 'training': []},
                        'native_interfaces': {
                            'game3d.frame_delay_exec': {'type': 'builtin_function_or_method',
                                                      'module': 'game3d', 'name': 'frame_delay_exec'},
                            'math3d.vector': {'type': 'type', 'module': 'math3d', 'name': 'vector'}}},
            'source': {'label': 'Synthetic test fixture', 'details': 'Never a game compatibility claim.'}}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='friendswine-profile-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.builtin = self.root / 'bundled'
        self.store = self.root / 'profiles'
        self.builtin.mkdir()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(comp.canonical_bytes(value))
        return path

    def test_address_and_whole_engine_hash_changes_do_not_change_verified_native_match(self):
        item = profile()
        first = {'architecture': 'x64', 'native_fingerprint': 'a' * 64,
                 'engine_sha256': 'c' * 64, 'exports': {'PyRun_StringFlags': 1000}}
        moved = dict(first, engine_sha256='d' * 64, exports={'PyRun_StringFlags': 2000})
        self.assertIs(comp.select_profile(first, [item]), item)
        self.assertIs(comp.select_profile(moved, [item]), item)

    def test_native_change_rejected_despite_identical_export_names(self):
        report = {'architecture': 'x64', 'native_fingerprint': 'c' * 64,
                  'exports': {'PyRun_StringFlags': 1000}}
        self.assertIsNone(comp.select_profile(report, [profile()]))

    def test_unknown_or_inconsistent_architecture_rejected(self):
        for report in ({'architecture': 'x86', 'machine': 34404}, {'machine': 332}, {}):
            self.assertIsNone(comp.select_profile(dict(report, native_fingerprint='a' * 64), [profile()]))
        self.assertEqual(comp.select_profile({'machine': 34404, 'native_fingerprint': 'a' * 64}, [profile()])['id'], 'fixture')

    def test_profile_id_disambiguates_runtime_variants(self):
        report = {'architecture': 'x64', 'native_fingerprint': 'a' * 64}
        with self.assertRaisesRegex(comp.CompatibilityError, 'AMBIGUOUS_PROFILE'):
            comp.select_profile(report, [profile('one'), profile('two')])
        self.assertEqual(comp.select_profile(report, [profile('one'), profile('two')], 'two')['id'], 'two')

    def test_json_import_is_pure_data_and_preserves_provenance(self):
        path = self.write('selected.json', profile())
        report = comp.import_profile_pack(path, self.store, self.builtin)
        self.assertFalse(report['signature_verified'])
        self.assertTrue(report['requires_runtime_probe'])
        values = comp.load_profiles(self.builtin, self.store)
        self.assertEqual([p['id'] for p in values], ['fixture'])
        self.assertEqual(values[0]['_origin'], 'imported')
        self.assertEqual(values[0]['_content_digest'], comp.profile_digest(profile()))

    def test_valid_zip_pack_import(self):
        path = self.root / 'data.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('pack.json', comp.canonical_bytes(comp.make_profile_pack([profile()])))
        self.assertEqual(comp.import_profile_pack(path, self.store, self.builtin)['ids'], ['fixture'])

    def test_zip_traversal_and_absolute_paths_are_rejected_without_extraction(self):
        for name in ('../escape.json', '/escape.json', 'C:/escape.json', r'..\escape.json'):
            with self.subTest(name=name):
                path = self.root / 'unsafe.zip'
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr(name, '{}')
                with self.assertRaises(comp.CompatibilityError):
                    comp.import_profile_pack(path, self.store, self.builtin)
                self.assertFalse(self.store.exists())
                self.assertFalse((self.root / 'escape.json').exists())

    def test_zip_python_and_unknown_entries_are_rejected(self):
        path = self.root / 'code.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('pack.json', comp.canonical_bytes(comp.make_profile_pack([profile()])))
            archive.writestr('run.py', 'raise RuntimeError("MUST NOT EXECUTE")')
        with self.assertRaisesRegex(comp.CompatibilityError, 'ONLY_PACK_JSON'):
            comp.import_profile_pack(path, self.store, self.builtin)

    def test_zip_duplicate_entry_rejected(self):
        path = self.root / 'duplicate.zip'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('pack.json', '{}')
                archive.writestr('pack.json', '{}')
        with self.assertRaisesRegex(comp.CompatibilityError, 'UNSAFE_ZIP_ENTRY'):
            comp.import_profile_pack(path, self.store, self.builtin)

    def test_zip_symlink_rejected(self):
        path = self.root / 'link.zip'
        entry = zipfile.ZipInfo('pack.json')
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(entry, '{}')
        with self.assertRaisesRegex(comp.CompatibilityError, 'UNSAFE_ZIP_ENTRY'):
            comp.import_profile_pack(path, self.store, self.builtin)

    def test_compressed_oversized_json_rejected(self):
        path = self.root / 'large.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('pack.json', b' ' * (comp.MAX_BYTES + 1))
        with self.assertRaisesRegex(comp.CompatibilityError, 'TOO_LARGE'):
            comp.import_profile_pack(path, self.store, self.builtin)

    def test_duplicate_json_key_rejected(self):
        path = self.root / 'bad.json'
        path.write_text('{"schema":"a","schema":"b"}', encoding='utf-8')
        with self.assertRaisesRegex(comp.CompatibilityError, 'DUPLICATE_JSON_KEY'):
            comp.import_profile_pack(path, self.store, self.builtin)

    def test_missing_unknown_or_executable_profile_fields_rejected(self):
        for variant in ('missing', 'unknown', 'schema', 'runtime'):
            value = profile()
            if variant == 'missing':
                del value['native_fingerprint']
            elif variant == 'unknown':
                value['code'] = 'print(1)'
            elif variant == 'schema':
                value['schema'] = 'future.unknown'
            else:
                del value['runtime']['capabilities']
            with self.subTest(variant=variant), self.assertRaises(comp.CompatibilityError):
                comp.validate_profile(value)

    def test_incomplete_training_restore_rejected(self):
        item = profile()
        item['runtime']['capabilities']['training'] = list(item['runtime']['functions'])
        with self.assertRaisesRegex(comp.CompatibilityError, 'TRAINING_RESTORE'):
            comp.validate_profile(item)

    def test_optional_volatile_flags_default_zero_and_reviewed_mask_accepted(self):
        item = profile()
        comp.validate_profile(item)
        for mask in (0, 0x10000000):
            item['runtime']['volatile_code_flags'] = mask
            self.assertIs(comp.validate_profile(item), item)

    def test_unknown_or_noninteger_volatile_flags_rejected(self):
        for mask in (True, False, None, 1, -1, 0x20000000, 0x10000001, '0x10000000', 0.0):
            item = profile()
            item['runtime']['volatile_code_flags'] = mask
            with self.subTest(mask=mask), self.assertRaisesRegex(comp.CompatibilityError, 'VOLATILE_CODE_FLAGS'):
                comp.validate_profile(item)

    def test_volatile_flags_nonzero_is_limited_to_reviewed_python_family(self):
        for version in ('3.10', '3.12', '3.14'):
            item = profile()
            item['runtime'].update(python_version=version, volatile_code_flags=0x10000000)
            with self.subTest(version=version), self.assertRaisesRegex(comp.CompatibilityError, 'VOLATILE_CODE_FLAGS'):
                comp.validate_profile(item)
            item['runtime']['volatile_code_flags'] = 0
            comp.validate_profile(item)

    def test_unknown_runtime_function_reference_rejected(self):
        item = profile()
        item['runtime']['capabilities']['display'].append('unknown:missing')
        with self.assertRaisesRegex(comp.CompatibilityError, 'CAPABILITY_REFERENCE'):
            comp.validate_profile(item)

    def test_pack_content_digest_is_integrity_only(self):
        pack = comp.make_profile_pack([profile()])
        pack['profiles'][0]['revision'] = 2
        with self.assertRaisesRegex(comp.CompatibilityError, 'INTEGRITY_MISMATCH'):
            comp.import_profile_pack(self.write('bad.json', pack), self.store, self.builtin)

    def test_duplicate_profile_ids_rejected(self):
        with self.assertRaisesRegex(comp.CompatibilityError, 'DUPLICATE_PROFILE_ID'):
            comp.make_profile_pack([profile(), profile()])

    def test_cannot_overwrite_builtin_or_existing_import(self):
        self.write('bundled/base.json', profile('base'))
        with self.assertRaisesRegex(comp.CompatibilityError, 'ALREADY_EXISTS'):
            comp.import_profile_pack(self.write('overwrite.json', profile('base')), self.store, self.builtin)
        selected = self.write('new.json', profile())
        comp.import_profile_pack(selected, self.store, self.builtin)
        with self.assertRaisesRegex(comp.CompatibilityError, 'ALREADY_EXISTS'):
            comp.import_profile_pack(selected, self.store, self.builtin)
        self.assertEqual(len(comp.load_profiles(self.builtin, self.store)), 2)

    def test_failed_pack_import_has_no_partial_profiles(self):
        pack = comp.make_profile_pack([profile('one'), profile('two')])
        pack['profiles'][1]['architecture'] = 'x86'
        with self.assertRaises(comp.CompatibilityError):
            comp.import_profile_pack(self.write('invalid.json', pack), self.store, self.builtin)
        self.assertEqual(comp.load_profiles(self.builtin, self.store), [])

    def test_remove_one_imported_profile_keeps_builtin_and_pack_peers(self):
        self.write('bundled/base.json', profile('base'))
        comp.import_profile_pack(self.write('two.json', comp.make_profile_pack([profile('one'), profile('two')])), self.store, self.builtin)
        self.assertTrue(comp.remove_imported_profile('one', self.store, self.builtin))
        self.assertEqual({p['id'] for p in comp.load_profiles(self.builtin, self.store)}, {'base', 'two'})
        self.assertFalse(comp.remove_imported_profile('missing', self.store, self.builtin))
        with self.assertRaisesRegex(comp.CompatibilityError, 'BUNDLED'):
            comp.remove_imported_profile('base', self.store, self.builtin)
        self.assertTrue(comp.remove_imported_profile('two', self.store, self.builtin))
        self.assertEqual([p['id'] for p in comp.load_profiles(self.builtin, self.store)], ['base'])

    def test_store_lock_blocks_concurrent_mutations(self):
        self.store.mkdir()
        (self.store / '.import.lock').write_text('busy')
        with self.assertRaisesRegex(comp.CompatibilityError, 'STORE_BUSY'):
            comp.import_profile_pack(self.write('new.json', profile()), self.store, self.builtin)

    def test_cache_key_changes_for_all_identity_inputs(self):
        args = ['c' * 64, 'a' * 64, 'b' * 64, profile()]
        original = comp.cache_key(*args)
        for index in range(3):
            changed = copy.deepcopy(args)
            changed[index] = 'd' * 64
            self.assertNotEqual(comp.cache_key(*changed), original)
        changed = copy.deepcopy(args)
        changed[3]['runtime']['functions'][next(iter(changed[3]['runtime']['functions']))] = 'e' * 64
        self.assertNotEqual(comp.cache_key(*changed), original)

    def test_cache_never_claims_live_probe_can_be_skipped(self):
        args = [self.root / 'cache', 'c' * 64, 'a' * 64, 'b' * 64, profile()]
        self.assertIsNone(comp.load_cache(*args))
        record = comp.save_cache(*args, {'display': 'READY'})
        self.assertTrue(record['requires_runtime_probe'])
        self.assertTrue(comp.load_cache(*args)['requires_runtime_probe'])
        edited = copy.deepcopy(args)
        edited[-1]['revision'] = 2
        self.assertIsNone(comp.load_cache(*edited))

    def test_corrupt_cache_ignored_and_clear_preserves_unrelated_files(self):
        directory = self.root / 'cache'
        args = [directory, 'c' * 64, 'a' * 64, 'b' * 64, profile()]
        record = comp.save_cache(*args, {'display': 'READY'})
        (directory / 'notes.json').write_text('{"user":"keep"}')
        (directory / ('d' * 64 + '.json')).write_text('{"user":"keep"}')
        self.assertEqual(comp.clear_cache(directory), 1)
        self.assertTrue((directory / 'notes.json').exists())
        self.assertTrue((directory / ('d' * 64 + '.json')).exists())
        (directory / (record['key'] + '.json')).write_text('broken')
        self.assertIsNone(comp.load_cache(*args))

    def probe_result(self):
        item = profile()
        observed = copy.deepcopy(item['runtime'])
        return {'schema': 1, 'nonce': 'a' * 32, 'pid': 123, 'python_version': '3.11',
                'callback_thread': 55, 'main_thread': 55, 'world_type': 'WorldArticle',
                'frames': 1, 'done': True, 'read_only': True, 'model_setters_called': 0,
                'profiles': [{'id': item['id'], 'diagnostics': {'capabilities': {
                    'display': {'status': 'SUPPORTED'}, 'training': {'status': 'UNSUPPORTED_INTERFACE'}}},
                    'observed_runtime': observed,
                    'observed_runtime_fingerprint': hashlib.sha256(comp.canonical_bytes(observed)).hexdigest(),
                    'observation_errors': {}}]}

    def test_valid_probe_result_is_bound_to_expected_identity(self):
        value = self.probe_result()
        self.assertIs(comp.validate_probe_result(value, 'a' * 32, 123, [profile()]), value)

    def test_probe_wrong_nonce_pid_and_thread_are_rejected(self):
        for key, changed in (('nonce', 'c' * 32), ('pid', 456), ('main_thread', None),
                             ('callback_thread', 56), ('callback_thread', True),
                             ('frames', 0), ('read_only', False), ('model_setters_called', 1)):
            value = self.probe_result()
            value[key] = changed
            with self.subTest(key=key, changed=changed), self.assertRaises(comp.CompatibilityError):
                comp.validate_probe_result(value, 'a' * 32, 123, [profile()])

    def test_probe_duplicate_or_unexpected_profile_ids_rejected(self):
        for variant in ('duplicate', 'unexpected'):
            value = self.probe_result()
            if variant == 'duplicate':
                value['profiles'].append(copy.deepcopy(value['profiles'][0]))
            else:
                value['profiles'][0]['id'] = 'unexpected'
            with self.subTest(variant=variant), self.assertRaises(comp.CompatibilityError):
                comp.validate_probe_result(value, 'a' * 32, 123, [profile()])

    def test_probe_observation_fingerprint_must_match_body(self):
        value = self.probe_result()
        value['profiles'][0]['observed_runtime_fingerprint'] = 'f' * 64
        with self.assertRaisesRegex(comp.CompatibilityError, 'OBSERVATION_DIGEST'):
            comp.validate_probe_result(value, 'a' * 32, 123, [profile()])

    def test_probe_supported_status_cannot_override_changed_implementation(self):
        for variant in ('function', 'native', 'version'):
            value = self.probe_result()
            record = value['profiles'][0]
            if variant == 'function':
                record['observed_runtime']['functions'][next(iter(profile()['runtime']['functions']))] = 'f' * 64
            elif variant == 'native':
                record['observed_runtime']['native_interfaces']['game3d.frame_delay_exec']['name'] = 'wrong'
            else:
                value['python_version'] = '3.12'
                record['observed_runtime']['python_version'] = '3.12'
            record['observed_runtime_fingerprint'] = hashlib.sha256(comp.canonical_bytes(record['observed_runtime'])).hexdigest()
            with self.subTest(variant=variant), self.assertRaisesRegex(comp.CompatibilityError, 'SUPPORTED_EVIDENCE'):
                comp.validate_probe_result(value, 'a' * 32, 123, [profile()])

    def test_probe_volatile_flags_cannot_claim_unselected_normalization(self):
        for expected_mask, observed_mask in ((0, 0x10000000), (0x10000000, 0), (0, True)):
            item = profile()
            item['runtime']['volatile_code_flags'] = expected_mask
            value = self.probe_result()
            record = value['profiles'][0]
            record['observed_runtime']['volatile_code_flags'] = observed_mask
            record['observed_runtime_fingerprint'] = hashlib.sha256(comp.canonical_bytes(record['observed_runtime'])).hexdigest()
            with self.subTest(expected_mask=expected_mask, observed_mask=observed_mask), self.assertRaisesRegex(comp.CompatibilityError, 'VOLATILE_CODE_FLAGS'):
                comp.validate_probe_result(value, 'a' * 32, 123, [item])

    def test_probe_accepts_matching_reviewed_volatile_flags(self):
        item = profile()
        item['runtime']['volatile_code_flags'] = 0x10000000
        value = self.probe_result()
        record = value['profiles'][0]
        record['observed_runtime']['volatile_code_flags'] = 0x10000000
        record['observed_runtime_fingerprint'] = hashlib.sha256(comp.canonical_bytes(record['observed_runtime'])).hexdigest()
        self.assertIs(comp.validate_probe_result(value, 'a' * 32, 123, [item]), value)


if __name__ == '__main__':
    unittest.main()
