"""Engine-update adaptation: contract matching, re-binding, candidate drafts."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import compatibility as comp
import friendswine as fw

ENGINE_SHA = '7dd17b6f64e373c6293ca0b8aa09ab7b837814b996ce9037680994f23499e8ac'
OLD_ENGINE_SHA = '548e19490dcba4e4b1541d96275e8dc55911757e63d79c972698844dfb3303b3'


def native_report(engine_sha=ENGINE_SHA, fingerprint='d' * 64):
    return {'schema': 1, 'supported': True, 'machine': 34404, 'architecture': 'x64',
            'image_size': 179412992, 'engine_sha256': engine_sha,
            'exports': {
                'PyGILState_Ensure': {'rva': 62235760, 'size': 145, 'code_sha256': 'a' * 64,
                                      'boundary_kind': 'x64-unwind', 'target': None},
                'PyGILState_Release': {'rva': 62235952, 'size': 189, 'code_sha256': 'b' * 64,
                                       'boundary_kind': 'x64-unwind', 'target': None},
                'PyRun_SimpleString': {'rva': 62367024, 'size': 7, 'code_sha256': 'c' * 64,
                                       'boundary_kind': 'xor-edx-jmp-rel32-to-unwind',
                                       'target': {'rva': 62367040, 'size': 130, 'code_sha256': 'e' * 64,
                                                  'boundary_kind': 'x64-unwind', 'target': None}}},
            'executable_sections': [{'name': '.text', 'size': 81397760, 'virtual_size': 81397676,
                                     'sha256': 'f' * 64}],
            'native_fingerprint': fingerprint,
            'fingerprint_policy': 'x64-unwind-or-reviewed-thunk-plus-all-executable-sections-v1'}


class ContractTests(unittest.TestCase):
    def test_contract_describes_the_calling_shape_not_the_build(self):
        contract = comp.native_contract(native_report())
        self.assertEqual(contract['schema'], comp.CONTRACT_SCHEMA)
        self.assertEqual(contract['exports']['PyRun_SimpleString'],
                         'xor-edx-jmp-rel32-to-unwind')
        self.assertEqual(comp.native_contract(native_report(fingerprint='e' * 64)), contract)

    def test_incomplete_or_unrelated_reports_have_no_contract(self):
        for report in ({'exports': {}}, {'exports': {'PyRun_SimpleString': {'boundary_kind': 'x'}}},
                       {'architecture': 'x64'}, 'not-a-report'):
            self.assertIsNone(comp.native_contract(report))

    def test_contract_requires_the_three_reviewed_exports(self):
        broken = native_report()
        del broken['exports']['PyGILState_Release']
        self.assertIsNone(comp.native_contract(broken))

    def test_unknown_contract_fields_are_rejected(self):
        contract = comp.native_contract(native_report())
        with self.assertRaisesRegex(comp.CompatibilityError, 'INVALID_FIELDS'):
            comp.validate_contract(dict(contract, extra=1))


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'schema': comp.PROFILE_SCHEMA, 'id': 'fixture', 'revision': 1,
                        'architecture': 'x64', 'native_fingerprint': 'a' * 64,
                        'native_contract': comp.native_contract(native_report()),
                        'runtime': {'schema': 1, 'python_version': '3.11',
                                    'functions': {'m:C.f': 'b' * 64},
                                    'capabilities': {'display': ['m:C.f'], 'training': []},
                                    'native_interfaces': {
                                        'game3d.frame_delay_exec': {'type': 'builtin_function_or_method',
                                                                  'module': 'game3d', 'name': 'frame_delay_exec'},
                                        'math3d.vector': {'type': 'type', 'module': 'math3d',
                                                          'name': 'vector'}}},
                        'source': {'label': 'Synthetic fixture', 'details': 'No game claim.'}}

    def test_exact_fingerprint_wins_over_the_contract(self):
        report = native_report(fingerprint='a' * 64)
        self.assertEqual(comp.match_profiles(report, [self.profile]),
                         [(self.profile, 'exact')])

    def test_updated_build_matches_by_contract_only(self):
        matches = comp.match_profiles(native_report(), [self.profile])
        self.assertEqual(matches, [(self.profile, 'contract')])

    def test_changed_calling_contract_does_not_match(self):
        other = native_report()
        other['exports']['PyRun_SimpleString']['boundary_kind'] = 'x64-unwind'
        self.assertEqual(comp.match_profiles(other, [self.profile]), [])

    def test_profile_without_a_contract_only_matches_exactly(self):
        legacy = dict(self.profile)
        del legacy['native_contract']
        self.assertEqual(comp.match_profiles(native_report(), [legacy]), [])

    def test_non_x64_reports_never_match(self):
        self.assertEqual(comp.match_profiles(dict(native_report(), architecture='x86'),
                                             [self.profile]), [])


class ProfileMintingTests(unittest.TestCase):
    def setUp(self):
        self.baseline = {'schema': comp.PROFILE_SCHEMA, 'id': 'neox-20261009-py311',
                         'revision': 3, 'architecture': 'x64', 'native_fingerprint': 'a' * 64,
                         'native_contract': comp.native_contract(native_report()),
                         'runtime': {'schema': 1, 'python_version': '3.11',
                                     'functions': {'m:C.f': 'b' * 64},
                                     'capabilities': {'display': ['m:C.f'], 'training': []},
                                     'native_interfaces': {
                                         'game3d.frame_delay_exec': {'type': 'builtin_function_or_method',
                                                                   'module': 'game3d', 'name': 'frame_delay_exec'},
                                         'math3d.vector': {'type': 'type', 'module': 'math3d',
                                                           'name': 'vector'}}},
                         'source': {'label': 'Reviewed baseline', 'details': 'Reviewed.'}}

    def test_adapted_profile_binds_the_build_and_keeps_the_reviewed_spec(self):
        minted = comp.mint_adapted_profile(self.baseline, native_report(),
                                           profile_id='neox-auto-' + ENGINE_SHA[:12],
                                           details='adapted')
        self.assertEqual(minted['native_fingerprint'], 'd' * 64)
        self.assertEqual(minted['revision'], 4)
        self.assertEqual(minted['runtime'], self.baseline['runtime'])
        self.assertEqual(minted['source']['label'], '自动重新适配')
        self.assertEqual(minted['native_contract'], self.baseline['native_contract'])
        comp.validate_profile(minted)

    def test_candidate_profile_records_the_changed_implementation(self):
        observed = {'schema': 1, 'python_version': '3.11',
                    'functions': {'m:C.f': 'c' * 64},
                    'capabilities': {'display': ['m:C.f'], 'training': []},
                    'native_interfaces': self.baseline['runtime']['native_interfaces']}
        changed, unresolved = comp.changed_functions(self.baseline, observed)
        self.assertEqual(changed, ['m:C.f'])
        self.assertEqual(unresolved, [])
        candidate = comp.mint_candidate_profile(self.baseline, native_report(), observed,
                                                profile_id='neox-candidate-x', details='draft')
        self.assertEqual(candidate['runtime']['functions']['m:C.f'], 'c' * 64)
        self.assertIn('待维护者复核', candidate['source']['label'])

    def test_unobserved_functions_are_reported_as_unresolved(self):
        changed, unresolved = comp.changed_functions(self.baseline, {'functions': {}})
        self.assertEqual(changed, [])
        self.assertEqual(unresolved, ['m:C.f'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='friendswine-adapt-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / 'profiles'
        self.candidates = self.root / 'candidates'
        self.empty = self.root / 'empty'

    def profile(self, profile_id='neox-auto-abc'):
        return {'schema': comp.PROFILE_SCHEMA, 'id': profile_id, 'revision': 2,
                'architecture': 'x64', 'native_fingerprint': 'd' * 64,
                'native_contract': comp.native_contract(native_report()),
                'runtime': {'schema': 1, 'python_version': '3.11',
                            'functions': {'m:C.f': 'b' * 64},
                            'capabilities': {'display': ['m:C.f'], 'training': []},
                            'native_interfaces': {
                                'game3d.frame_delay_exec': {'type': 'builtin_function_or_method',
                                                          'module': 'game3d', 'name': 'frame_delay_exec'},
                                'math3d.vector': {'type': 'type', 'module': 'math3d',
                                                  'name': 'vector'}}},
                'source': {'label': '自动重新适配', 'details': 'adapted'}}

    def test_saved_profile_is_loaded_as_an_imported_profile(self):
        saved = comp.save_profile(self.profile(), self.store)
        self.assertTrue(Path(saved['path']).is_file())
        loaded = comp.load_profiles(self.empty, self.store)
        self.assertEqual([p['id'] for p in loaded], ['neox-auto-abc'])
        self.assertEqual(loaded[0]['_origin'], 'imported')

    def test_saving_the_same_profile_twice_is_refused(self):
        comp.save_profile(self.profile(), self.store)
        with self.assertRaisesRegex(comp.CompatibilityError, 'ALREADY_EXISTS'):
            comp.save_profile(self.profile(), self.store)

    def test_candidates_are_never_returned_as_verified_profiles(self):
        comp.save_candidate(self.profile('neox-candidate-abc'), self.candidates)
        self.assertEqual(comp.load_profiles(self.empty, self.store), [])
        self.assertEqual([p['id'] for p in comp.load_candidates(self.candidates)],
                         ['neox-candidate-abc'])
        self.assertEqual(comp.load_candidates(self.candidates)[0]['_origin'], 'candidate')


class LauncherAdaptationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='friendswine-adapt-launch-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.game = self.root / 'game' / 'dwrg.exe'
        self.game.parent.mkdir(parents=True)
        self.game.write_bytes(b'not-a-real-game')
        self.injector = self.root / 'bin' / 'fuxiang-inject.exe'
        self.injector.parent.mkdir(parents=True)
        self.injector.write_bytes(b'mock')
        self.baseline = comp.load_profiles(fw.BUNDLED_PROFILES)[0]
        self.observed = {'schema': 1, 'python_version': self.baseline['runtime']['python_version'],
                         'functions': dict(self.baseline['runtime']['functions']),
                         'capabilities': self.baseline['runtime']['capabilities'],
                         'native_interfaces': self.baseline['runtime']['native_interfaces'],
                         'volatile_code_flags': self.baseline['runtime'].get('volatile_code_flags', 0)}
        patches = [
            patch.object(fw, 'DATA', self.data),
            patch.object(fw, 'PROFILE_STORE', self.data / 'compatibility' / 'profiles'),
            patch.object(fw, 'PROFILE_CANDIDATES', self.data / 'compatibility' / 'candidates'),
            patch.object(fw, 'PROFILE_PREFERENCES', self.data / 'compatibility' / 'preferences.json'),
            patch.object(fw, 'BASELINE', self.data / 'compatibility' / 'baseline.json'),
            patch.object(fw, 'CONFIG', self.data / 'config.json'),
            patch.object(fw, 'INJECTOR', self.injector),
            patch.object(fw, 'game_pids', return_value=[123]),
            patch.object(fw, 'select_game', return_value=self.game),
            patch.object(fw, 'created_time', return_value=1700000000.0),
            patch.object(fw.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=True),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def probe(self, observed=None):
        record = {'id': self.baseline['id'],
                  'diagnostics': {'capabilities': {'display': {'status': 'SUPPORTED'},
                                                   'training': {'status': 'SUPPORTED'}}},
                  'observed_runtime': observed if observed is not None else self.observed,
                  'observed_runtime_fingerprint': 'a' * 64,
                  'observation_errors': {}}
        return {'schema': 1, 'nonce': 'a' * 32, 'pid': 123, 'python_version': '3.11',
                'callback_thread': 1, 'main_thread': 1, 'world_type': 'WorldArticle',
                'frames': 1, 'done': True, 'read_only': True, 'model_setters_called': 0,
                'profiles': [record]}

    def test_engine_update_state_tracks_the_last_verified_build(self):
        native = native_report()
        self.assertEqual(fw.engine_update_state(native, self.game.parent)['state'], 'UNVERIFIED')
        fw.write_baseline(native, self.baseline, {'label': 'release_1'}, 'exact')
        self.assertEqual(fw.engine_update_state(native, self.game.parent)['state'], 'CURRENT')
        newer = native_report(engine_sha=OLD_ENGINE_SHA)
        state = fw.engine_update_state(newer, self.game.parent)
        self.assertEqual(state['state'], 'UPDATED')
        self.assertEqual(state['baseline_engine_sha256'], ENGINE_SHA)

    def test_adapt_rebinds_the_reviewed_specification_to_the_new_build(self):
        with patch.object(fw, 'inspect_engine', return_value=native_report()), \
             patch.object(fw, 'probe_runtime', return_value=self.probe()):
            result = fw.adapt()
        self.assertTrue(result['adapted'])
        self.assertEqual(result['reason'], 'ADAPTED')
        self.assertEqual(result['changed_functions'], [])
        stored = comp.load_profiles(self.root / 'empty', fw.PROFILE_STORE)
        self.assertEqual([p['id'] for p in stored], ['neox-auto-' + ENGINE_SHA[:12]])
        self.assertEqual(stored[0]['native_fingerprint'], 'd' * 64)
        self.assertEqual(stored[0]['runtime'], self.baseline['runtime'])
        self.assertEqual(fw.baseline_state()['engine_sha256'], ENGINE_SHA)

    def test_adapt_drafts_a_candidate_when_the_reviewed_scripts_changed(self):
        changed = copy.deepcopy(self.observed)
        identifier = sorted(changed['functions'])[0]
        changed['functions'][identifier] = 'f' * 64
        with patch.object(fw, 'inspect_engine', return_value=native_report()), \
             patch.object(fw, 'probe_runtime', return_value=self.probe(changed)):
            result = fw.adapt()
        self.assertFalse(result['adapted'])
        self.assertEqual(result['reason'], 'SCRIPT_IMPLEMENTATION_CHANGED')
        self.assertEqual(result['changed_functions'], [identifier])
        self.assertEqual([p['id'] for p in comp.load_profiles(fw.BUNDLED_PROFILES, fw.PROFILE_STORE)],
                         [self.baseline['id']])
        candidates = comp.load_candidates(fw.PROFILE_CANDIDATES)
        self.assertEqual([p['id'] for p in candidates], ['neox-candidate-' + ENGINE_SHA[:12]])

    def test_adapt_refuses_when_the_calling_contract_changed(self):
        other = native_report()
        other['exports']['PyGILState_Ensure']['boundary_kind'] = 'leaf-thunk'
        with patch.object(fw, 'inspect_engine', return_value=other), \
             patch.object(fw, 'probe_runtime', side_effect=AssertionError('must not probe')):
            result = fw.adapt()
        self.assertFalse(result['adapted'])
        self.assertEqual(result['reason'], 'NO_COMPATIBLE_CONTRACT')

    def test_adapt_reports_an_already_matching_build_without_probing(self):
        exact = dict(self.baseline)
        exact = copy.deepcopy(exact)
        exact['native_fingerprint'] = 'd' * 64
        with patch.object(fw, 'inspect_engine', return_value=native_report()), \
             patch.object(fw, 'probe_runtime', side_effect=AssertionError('must not probe')):
            with patch.object(fw, 'available_profiles', return_value=[exact]):
                result = fw.adapt()
        self.assertEqual(result['reason'], 'ALREADY_MATCHED')
        self.assertFalse(result['adapted'])

    def test_reviewed_candidate_is_promoted_into_the_selected_profile(self):
        observed = copy.deepcopy(self.observed)
        identifier = sorted(observed['functions'])[0]
        observed['functions'][identifier] = 'f' * 64
        candidate = comp.mint_candidate_profile(self.baseline, native_report(), observed,
                                                profile_id='neox-candidate-x', details='draft')
        comp.save_candidate(candidate, fw.PROFILE_CANDIDATES)
        result = fw.promote_candidate('neox-candidate-x')
        self.assertTrue(result['promoted'])
        self.assertEqual(result['id'], 'neox-' + 'd' * 12 + '-py311')
        self.assertEqual(fw.preferred_profile(), result['id'])
        stored = comp.load_profiles(self.root / 'empty', fw.PROFILE_STORE)
        self.assertEqual([p['id'] for p in stored], [result['id']])
        self.assertEqual(stored[0]['runtime']['functions'][identifier], 'f' * 64)
        self.assertIn('人工复核', stored[0]['source']['label'])
        # The draft stays in the candidate store as the audit trail.
        self.assertEqual([p['id'] for p in comp.load_candidates(fw.PROFILE_CANDIDATES)],
                         ['neox-candidate-x'])

    def test_unknown_candidate_is_not_promoted(self):
        with self.assertRaisesRegex(ValueError, '未知适配候选'):
            fw.promote_candidate('missing')

    def test_doctor_flags_an_engine_update_that_can_be_adapted(self):
        native = native_report()
        fw.write_baseline(native, self.baseline, {'label': 'release_1'}, 'exact')
        with patch.object(fw, 'inspect_engine', return_value=native), \
             patch.object(fw.subprocess, 'run', return_value=type('R', (), {
                 'returncode': 0, 'stdout': json.dumps({
                     'observer_token': {'elevated': True}, 'target_image': str(self.game),
                     'engine_modules': [{'name': 'neox_engine.dll',
                                         'path': str(self.game.parent / 'neox_engine.dll'),
                                         'disk_sha256': ENGINE_SHA}]}),
                 'stderr': ''})()):
            result = fw.doctor()
        self.assertTrue(result['adaptation_needed'])
        self.assertEqual(result['contract_profile_matches'], [self.baseline['id']])
        self.assertEqual(result['exact_profile_matches'], [])
        self.assertIn('游戏更新', fw.activity_verdict('doctor', result))


if __name__ == '__main__':
    unittest.main()
