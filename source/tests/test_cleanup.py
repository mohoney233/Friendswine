"""Residue cleanup: archive first, never disturb a live session, claim nothing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import friendswine as fw


class CleanupTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        base = Path(holder.name)
        self.data = base / 'data'
        self.channel = self.data / 'persistent'
        self.channel.mkdir(parents=True)
        patch.object(fw, 'DATA', self.data).start()
        patch.object(fw, 'CHANNEL', self.channel).start()
        self.addCleanup(patch.stopall)

    def write(self, name, value):
        (self.channel / name).write_text(json.dumps(value), encoding='utf8')

    def test_archives_then_clears_the_channel(self):
        self.write('status.json', {'phase': 'RESTORE_FAULT', 'modified': 1})
        self.write('control.json', {'mode': 'STOP'})
        self.write('rejected-persistent-abc.json', {'phase': 'STARTUP_ERROR'})
        with patch.object(fw, 'live_status', return_value={}):
            output = fw.cleanup()
        self.assertEqual(sorted(output['archived']),
                         ['control.json', 'rejected-persistent-abc.json', 'status.json'])
        self.assertFalse((self.channel / 'status.json').exists())
        self.assertFalse((self.channel / 'control.json').exists())
        self.assertFalse((self.channel / 'rejected-persistent-abc.json').exists())
        archive = Path(output['archived_to'])
        kept = json.loads((archive / 'status.json').read_text(encoding='utf8'))
        self.assertEqual(kept['phase'], 'RESTORE_FAULT')
        self.assertFalse(output['live_session'])
        self.assertIn('不代表模型已恢复', output['note'])

    def test_stops_a_running_watcher_and_archives_its_files(self):
        self.write('watcher.json', {'state': 'WATCHING'})
        self.write('watch-control.json', {'enabled': True})
        self.write('status.json', {'phase': 'STOPPED'})
        with patch.object(fw, 'live_status', return_value={}):
            output = fw.cleanup()
        self.assertIn('watcher.json', output['archived'])
        self.assertIn('watch-control.json', output['archived'])
        control = json.loads((Path(output['archived_to']) / 'watch-control.json')
                             .read_text(encoding='utf8'))
        self.assertFalse(control['enabled'])  # auto-reconnect disabled before archiving
        self.assertFalse((self.channel / 'watcher.json').exists())

    def test_refuses_while_a_session_is_alive(self):
        self.write('status.json', {'phase': 'RUNNING', 'modified': 1})
        with patch.object(fw, 'live_status', return_value={'done': False}), \
             patch.object(fw, 'heartbeat_info', return_value={'heartbeat_age_s': 0., 'stale': False}):
            with self.assertRaisesRegex(RuntimeError, '停止'):
                fw.cleanup()
        self.assertTrue((self.channel / 'status.json').is_file())

    def test_finished_session_residue_can_be_cleared(self):
        self.write('status.json', {'phase': 'RESTORE_FAULT', 'done': True, 'modified': 1})
        with patch.object(fw, 'live_status', return_value={'done': True, 'modified': 1}):
            output = fw.cleanup()
        self.assertIn('status.json', output['archived'])
        self.assertFalse((self.channel / 'status.json').exists())

    def test_nothing_to_clean_reports_an_empty_archive(self):
        with patch.object(fw, 'live_status', return_value={}):
            output = fw.cleanup()
        self.assertEqual(output['archived'], [])
        self.assertIsNone(output['archived_to'])
        self.assertTrue(output['cleaned'])

    def test_verdict_points_at_start_after_a_real_cleanup(self):
        self.assertIn('启动', fw.activity_verdict('cleanup', {'archived': ['status.json']}))
        self.assertIn('没有', fw.activity_verdict('cleanup', {'archived': []}))


if __name__ == '__main__':
    unittest.main()
