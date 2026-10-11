import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import compatibility
import runtime_compat
import runtime_probe


class RuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='friendswine-readonly-probe-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.nonce = 'a' * 32
        self.evidence = self.root / 'evidence' / self.nonce
        self.evidence.mkdir(parents=True)
        self.queue = []
        self.game = types.ModuleType('game3d')
        self.game.frame_delay_exec = lambda frames, callback: self.queue.append(callback)
        self.math = types.ModuleType('math3d')
        self.math.vector = type('vector', (), {})
        self.scene = types.ModuleType('ProbeFixture')
        exec('calls = 0\ndef update():\n    global calls\n    calls += 1\n    raise AssertionError("game setter was called")\n', vars(self.scene))
        self.modules = {'game3d': self.game, 'math3d': self.math, 'ProbeFixture': self.scene}
        self.descriptors = {
            'game3d.frame_delay_exec': {'type': 'builtin_function_or_method', 'module': 'game3d', 'name': 'frame_delay_exec'},
            'math3d.vector': {'type': 'type', 'module': 'math3d', 'name': 'vector'}}
        self.profile = {'schema': compatibility.PROFILE_SCHEMA, 'id': 'probe-test', 'revision': 1,
                        'architecture': 'x64', 'native_fingerprint': 'b' * 64,
                        'source': {'label': 'Test only', 'details': 'No real-game admission.'},
                        'runtime': {'schema': 1, 'python_version': '%s.%s' % sys.version_info[:2],
                                    'functions': {'ProbeFixture:update': runtime_compat.function_fingerprint(self.scene.update)},
                                    'capabilities': {'display': ['ProbeFixture:update'], 'training': []},
                                    'native_interfaces': self.descriptors}}
        self.options = {'pid': os.getpid(), 'nonce': self.nonce,
                        'app_root': str(Path(runtime_probe.__file__).parent),
                        'output_file': str(self.evidence / 'runtime-probe.json'),
                        'profiles': [self.profile]}
        self.now = [1.]

    def descriptor(self, value):
        if value is self.game.frame_delay_exec:
            return self.descriptors['game3d.frame_delay_exec']
        if value is self.math.vector:
            return self.descriptors['math3d.vector']
        raise runtime_compat.UnsupportedFingerprint('UNKNOWN_TEST_VALUE')

    def probe(self):
        return runtime_probe.ReadonlyProbe(self.options, runtime_compat, compatibility,
                                          modules=self.modules, clock=lambda: self.now[0])

    def test_profile_functions_are_fingerprinted_never_executed_and_registry_untouched(self):
        registry_before = sys.modules.get('friendswine_persistent_v1')
        probe = self.probe()
        with patch.object(runtime_compat, 'native_descriptor', self.descriptor):
            probe.begin()
            self.assertEqual(len(self.queue), 1)
            self.queue.pop()()
        self.assertTrue(probe.finished.is_set())
        self.assertNotIn('error', probe.record)
        self.assertEqual(probe.record['profiles'][0]['diagnostics']['capabilities']['display']['status'], 'SUPPORTED')
        self.assertEqual(self.scene.calls, 0)
        self.assertEqual(probe.record['model_setters_called'], 0)
        self.assertIs(sys.modules.get('friendswine_persistent_v1'), registry_before)

    def test_unknown_scheduler_never_called(self):
        probe = self.probe()
        with patch.object(runtime_compat, 'native_descriptor', return_value={'type': 'other', 'module': 'game3d', 'name': 'wrong'}):
            probe.begin()
        self.assertEqual(self.queue, [])
        self.assertEqual(probe.record['error'], 'PROBE_SCHEDULER_DESCRIPTOR_MISMATCH')

    def test_stored_verified_scheduler_reference_is_used(self):
        probe = self.probe()
        self.modules.pop('ProbeFixture')
        with patch.object(runtime_compat, 'native_descriptor', self.descriptor):
            probe.begin()
            checked = probe.scheduler
            original = self.game.frame_delay_exec
            self.game.frame_delay_exec = lambda *_: self.fail('replacement scheduler called')
            probe._schedule()
            self.assertIs(probe.scheduler, checked)
            self.assertEqual(len(self.queue), 2)
            self.game.frame_delay_exec = original

    def test_expired_callback_does_not_observe_or_reschedule(self):
        probe = self.probe()
        with patch.object(runtime_compat, 'native_descriptor', self.descriptor):
            probe.begin()
            self.now[0] = 32.
            self.queue.pop()()
        self.assertEqual(probe.record['error'], 'PROBE_EXPIRED')
        self.assertEqual(probe.record['frames'], 0)
        self.assertEqual(self.queue, [])

    def test_waiting_scene_stops_after_three_frames_without_import(self):
        self.modules.pop('ProbeFixture')
        probe = self.probe()
        with patch.object(runtime_compat, 'native_descriptor', self.descriptor):
            probe.begin()
            for _ in range(3):
                self.queue.pop(0)()
        self.assertTrue(probe.finished.is_set())
        self.assertEqual(probe.record['frames'], 3)
        self.assertEqual(self.queue, [])
        self.assertNotIn('ProbeFixture', self.modules)
        self.assertEqual(probe.record['profiles'][0]['diagnostics']['capabilities']['display']['status'], 'WAITING_FOR_SCENE')

    def test_wrong_callback_thread_rejected_before_observation(self):
        probe = self.probe()
        probe.record['main_thread'] = threading.get_ident() + 1
        probe.callback()
        self.assertEqual(probe.record['error'], 'PROBE_NOT_ON_MAIN_THREAD')
        self.assertEqual(probe.record['frames'], 0)

    def test_options_reject_pid_nonce_output_or_source_mismatch(self):
        runtime_probe._validate_options(self.options, runtime_probe.__file__)
        for key, bad in (('pid', os.getpid() + 1), ('nonce', '../escape'),
                         ('output_file', str(self.root / 'runtime-probe.json'))):
            value = copy.deepcopy(self.options)
            value[key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                runtime_probe._validate_options(value, runtime_probe.__file__)
        with self.assertRaisesRegex(ValueError, 'SOURCE_ROOT'):
            runtime_probe._validate_options(self.options, self.root / 'runtime_probe.py')

    def test_old_nonce_output_rejected_and_atomic_result_has_bound_identity(self):
        import json
        probe = self.probe()
        probe.finish('SYNTHETIC_DIAGNOSTIC')
        probe.write_result()
        record = json.loads(Path(self.options['output_file']).read_text(encoding='utf-8'))
        self.assertEqual(record['nonce'], self.nonce)
        self.assertEqual(record['pid'], os.getpid())
        self.assertTrue(record['done'])
        with self.assertRaisesRegex(ValueError, 'ALREADY_EXISTS'):
            runtime_probe._validate_options(self.options, runtime_probe.__file__)

    def test_timed_out_writer_does_not_touch_game(self):
        probe = self.probe()
        self.now[0] = 32.
        probe.write_result()
        self.assertEqual(probe.record['error'], 'PROBE_CALLBACK_TIMEOUT')
        self.assertEqual(self.scene.calls, 0)
        self.assertEqual(self.queue, [])


if __name__ == '__main__':
    unittest.main()
