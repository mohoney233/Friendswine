"""Offline regressions for transient Windows status-file sharing failures."""
import ast
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode=True
import friendswine as launcher


def windows_error(code):
    error=PermissionError('mock Windows sharing/access failure')
    error.winerror=code
    return error


class StatusIOTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='Friendswine status IO ')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.output=self.root/'status.json'
        self.output.write_text('{"phase":"RUNNING"}',encoding='utf8')
        source=(launcher.ROOT/'persistent_runtime.py').read_text(encoding='utf8')
        self.tree=ast.parse(source)
        definitions=[node for node in self.tree.body if isinstance(node,ast.FunctionDef)
                     and node.name in ('atomic_status_write','publish')]
        self.ns={'os':os,'time':time,'threading':threading,'json':json,
                 'publish_lock':threading.Lock(),'lock':threading.RLock(),
                 'state':{'phase':'STOPPED'},'OUT':str(self.output)}
        exec(compile(ast.Module(body=definitions,type_ignores=[]),'runtime:file-functions','exec'),self.ns)
        self.write=self.ns['atomic_status_write']

    def fake_time(self):
        now=[0.]; sleeps=[]
        def sleep(delay):
            sleeps.append(delay); now[0]+=delay
        return lambda:now[0],sleep,sleeps

    def test_transient_windows_sharing_errors_then_success(self):
        for code in (5,32,33):
            with self.subTest(code=code):
                calls=[]
                def replace(source,target):
                    calls.append((source,target))
                    if len(calls)<=2: raise windows_error(code)
                    os.replace(source,target)
                clock,sleeper,sleeps=self.fake_time()
                self.write(self.output,'{"phase":"STOPPED"}',replace=replace,sleeper=sleeper,clock=clock)
                self.assertEqual(json.loads(self.output.read_text())['phase'],'STOPPED')
                self.assertEqual(len(calls),3)
                self.assertLessEqual(sum(sleeps),.6)
                self.assertEqual(list(self.root.glob('status.json.*.tmp')),[])

    def test_persistent_error_is_bounded_preserves_old_json_and_cleans_temp(self):
        clock,sleeper,sleeps=self.fake_time()
        calls=[]
        def denied(source,target):
            calls.append(source)
            raise windows_error(5)
        with self.assertRaises(PermissionError):
            self.write(self.output,'{"phase":"STOPPED"}',replace=denied,sleeper=sleeper,clock=clock)
        self.assertLessEqual(sum(sleeps),.6)
        self.assertLessEqual(len(calls),6)
        self.assertEqual(json.loads(self.output.read_text()),{'phase':'RUNNING'})
        self.assertEqual(list(self.root.glob('status.json.*.tmp')),[])

    def test_nontransient_error_is_not_swallowed_or_retried(self):
        clock,sleeper,sleeps=self.fake_time()
        def fail(source,target): raise windows_error(112)
        with self.assertRaises(PermissionError):
            self.write(self.output,'{}',replace=fail,sleeper=sleeper,clock=clock)
        self.assertEqual(sleeps,[])
        self.assertEqual(json.loads(self.output.read_text()),{'phase':'RUNNING'})
        self.assertEqual(list(self.root.glob('status.json.*.tmp')),[])

    def test_main_thread_publication_never_sleeps_on_sharing_error(self):
        def denied(source,target): raise windows_error(32)
        with patch.object(os,'replace',side_effect=denied), \
             patch.object(time,'sleep',side_effect=AssertionError('main thread must not sleep')):
            with self.assertRaises(PermissionError):
                self.ns['publish'](retry=False,nonblocking=True)
        self.assertEqual(json.loads(self.output.read_text()),{'phase':'RUNNING'})

    def test_main_thread_publication_does_not_wait_for_reporter_lock(self):
        self.ns['publish_lock'].acquire()
        try:
            self.assertFalse(self.ns['publish'](retry=False,nonblocking=True))
        finally:
            self.ns['publish_lock'].release()
        callback=next(node for node in self.tree.body if isinstance(node,ast.FunctionDef) and node.name=='callback')
        publish_calls=[node for node in ast.walk(callback) if isinstance(node,ast.Call)
                       and isinstance(node.func,ast.Name) and node.func.id=='publish']
        self.assertEqual(len(publish_calls),1)
        self.assertEqual({arg.arg:ast.literal_eval(arg.value) for arg in publish_calls[0].keywords},
                         {'retry':False,'nonblocking':True})

    def test_heartbeat_age_missing_old_and_fresh(self):
        self.assertEqual(launcher.heartbeat_info({'heartbeat_at':100.},104.),
                         {'heartbeat_age_s':4.,'stale':False})
        self.assertTrue(launcher.heartbeat_info({'heartbeat_at':100.},105.01)['stale'])
        self.assertTrue(launcher.heartbeat_info({},105.)['stale'])
        self.assertTrue(launcher.heartbeat_info({'heartbeat_at':float('nan')},105.)['stale'])

    def test_start_rejects_live_stale_status_without_preflight_or_injection(self):
        game=self.root/'dwrg.exe'
        with patch.object(launcher.ctypes.windll.shell32,'IsUserAnAdmin',return_value=True), \
             patch.object(launcher,'game_pids',return_value=[123]), \
             patch.object(launcher,'select_game',return_value=game), \
             patch.object(launcher,'load_configuration',return_value={}), \
             patch.object(launcher,'live_status',return_value={'done':False,'heartbeat_at':1.}), \
             patch.object(launcher.subprocess,'run',side_effect=AssertionError('no preflight/raw on stale status')):
            with self.assertRaisesRegex(RuntimeError,'心跳'):
                launcher.start()


if __name__=='__main__':
    unittest.main(verbosity=2)
