"""Host update gates: native eligibility never substitutes for script checks."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import friendswine as fw


def probe_record(identifier,display='SUPPORTED',training='SUPPORTED'):
    return {'id':identifier,'diagnostics':{'capabilities':{
        'display':{'status':display},'training':{'status':training}}},
        'observed_runtime':{'functions':{'module:method':'a'*64}}}


class UpdateLauncherTests(unittest.TestCase):
    def test_native_environment_binds_pid_file_and_implementation(self):
        native={'engine_sha256':'a'*64,'native_fingerprint':'b'*64}
        env=fw.injection_environment(Path('E:/game'),Path('E:/state'),native,77)
        self.assertEqual(env['FUXIANG_EXPECTED_PID'],'77')
        self.assertEqual(env['FUXIANG_EXPECTED_ENGINE_SHA'],'a'*64)
        self.assertEqual(env['FUXIANG_EXPECTED_NATIVE_FINGERPRINT'],'b'*64)

    def test_display_survives_training_script_change(self):
        profile={'id':'current'}
        result=fw.choose_runtime_profile([profile],{'profiles':[
            probe_record('current',training='IMPLEMENTATION_CHANGED')]},True)
        self.assertIs(result,profile)

    def test_both_changed_never_select_a_profile(self):
        with self.assertRaisesRegex(RuntimeError,'SCRIPT_IMPLEMENTATION_CHANGED'):
            fw.choose_runtime_profile([{'id':'old'}],{'profiles':[
                probe_record('old','IMPLEMENTATION_CHANGED','UNSUPPORTED_INTERFACE')]},True)

    def test_training_support_does_not_enable_display_only_request(self):
        with self.assertRaisesRegex(RuntimeError,'SCRIPT_IMPLEMENTATION_CHANGED'):
            fw.choose_runtime_profile([{'id':'training'}],{'profiles':[
                probe_record('training','IMPLEMENTATION_CHANGED','SUPPORTED')]},False)

    def test_not_loaded_is_deferred_not_incompatible(self):
        profile={'id':'expected'}
        self.assertIs(fw.choose_runtime_profile([profile],{'profiles':[
            probe_record('expected','WAITING_FOR_SCENE','WAITING_FOR_SCENE')]},True),profile)

    def test_ambiguous_profiles_are_not_chosen_by_filename_order(self):
        with self.assertRaisesRegex(RuntimeError,'AMBIGUOUS_RUNTIME_PROFILE'):
            fw.choose_runtime_profile([{'id':'a'},{'id':'b'}],
                {'profiles':[probe_record('a'),probe_record('b')]},True)

    def test_probe_record_missing_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError,'SCRIPT_IMPLEMENTATION_CHANGED'):
            fw.choose_runtime_profile([{'id':'expected'}],{'profiles':[]},True)

    def test_offline_inspection_rejects_file_changed_during_inspection(self):
        answer={'architecture':'x64','engine_sha256':'a'*64}
        with patch.object(fw.subprocess,'run',return_value=types.SimpleNamespace(
            returncode=0,stdout=json.dumps(answer),stderr='')), \
             patch.object(fw,'sha256_file',return_value='b'*64):
            with self.assertRaisesRegex(RuntimeError,'ENGINE_CHANGED_DURING_INSPECTION'):
                fw.inspect_engine(Path('engine.dll'))

    def test_cache_keys_actual_observation_not_only_expected_profile(self):
        native={'engine_sha256':'a'*64,'native_fingerprint':'b'*64}
        probe={'profiles':[probe_record('profile')]}
        with patch.object(fw.compat,'save_cache',return_value={}) as save:
            fw.cache_probe(native,{'id':'profile'},probe)
            first=save.call_args.args[3]
            probe['profiles'][0]['observed_runtime']['functions']['module:method']='c'*64
            fw.cache_probe(native,{'id':'profile'},probe)
            self.assertNotEqual(first,save.call_args.args[3])
            self.assertFalse(save.call_args.args[-1]['model_behavior_validated'])

    def test_readonly_check_has_no_probe_without_live_request(self):
        with patch.object(fw,'doctor',return_value={'native_supported':True}), \
             patch.object(fw,'probe_runtime',side_effect=AssertionError('must remain offline')):
            result=fw.check_update()
        self.assertFalse(result['model_writes'])


if __name__=='__main__':
    unittest.main()
