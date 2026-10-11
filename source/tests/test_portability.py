"""Portable-package tests. Never starts a watcher, injector or game callback."""
import ast
import hashlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode=True
import friendswine as launcher
import release_paths as paths


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='Friendswine 中文 空格 ')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.game=self.root/'游戏目录 空格'/'dwrg.exe'
        self.game.parent.mkdir()
        self.game.write_bytes(b'not-a-real-game')
        (self.game.parent/'neox_engine.dll').write_bytes(b'unsupported-engine-test-only')

    def test_absolute_unicode_game_path_and_quoted_input(self):
        self.assertEqual(paths.validate_game_path(str(self.game)),self.game.resolve())
        self.assertEqual(paths.validate_game_path('"'+str(self.game)+'"'),self.game.resolve())

    def test_reject_relative_wrong_name_missing_engine_and_null(self):
        wrong=self.root/'another.exe'; wrong.write_bytes(b'test')
        for raw in ('dwrg.exe',str(wrong),'a\x00b',str(self.root/'missing'/'dwrg.exe')):
            with self.subTest(raw=raw), self.assertRaises((ValueError,OSError)):
                paths.validate_game_path(raw)
        (self.game.parent/'neox_engine.dll').unlink()
        with self.assertRaises(ValueError):
            paths.validate_game_path(str(self.game))

    def test_unique_actual_image_autodetection_and_config_mismatch(self):
        self.assertEqual(paths.select_game({},[123],lambda pid:self.game),self.game.resolve())
        self.assertEqual(paths.select_game({'game_exe':str(self.game)},[123],lambda pid:self.game),self.game.resolve())
        other=self.root/'另一游戏'/'dwrg.exe'; other.parent.mkdir()
        other.write_bytes(b'test'); (other.parent/'neox_engine.dll').write_bytes(b'test')
        with self.assertRaises(RuntimeError):
            paths.select_game({'game_exe':str(other)},[123],lambda pid:self.game)
        for pids in ([],[123,124]):
            with self.assertRaises(RuntimeError):
                paths.select_game({},pids,lambda pid:self.game)

    def test_configure_only_changes_selected_user_configuration(self):
        configuration=self.root/'用户状态'/'config.json'
        with patch.object(launcher,'CONFIG',configuration):
            result=launcher.configure(str(self.game))
        self.assertTrue(result['configured'])
        self.assertTrue(result['compatibility_check_required'])
        self.assertEqual(paths.load_configuration(configuration)['game_exe'],str(self.game.resolve()))
        self.assertEqual(paths.ENGINE_SHA,'fbc8e87c97bb5b5b7e9d3d5de3668c04255ce9afd6038bdf29fa1f7de4ae8af6')

    def test_corrupt_configuration_does_not_silently_autodetect(self):
        path=self.root/'config.json'; path.write_text('[1,2]')
        with self.assertRaises(ValueError): paths.load_configuration(path)
        path.write_text('{broken')
        with self.assertRaises(ValueError): paths.load_configuration(path)

    def test_user_state_path_and_mutex_are_installation_specific(self):
        app=self.root/'安装程序 中文'/'app'
        data=paths.state_directory(app,self.root/'用户数据')
        self.assertEqual(data.parent.name,'Friendswine')
        self.assertNotEqual(paths.installation_key(app),paths.installation_key(self.root/'other'/'app'))
        self.assertNotEqual(launcher.watch_mutex_name(app),launcher.watch_mutex_name(self.root/'other'/'app'))
        self.assertFalse(data.exists())
        with self.assertRaises(ValueError): paths.state_directory(app,'relative')

    def test_default_state_directory_is_portable_and_ignores_local_appdata(self):
        app=self.root/'便携 安装'/'app'; app.mkdir(parents=True)
        unrelated=self.root/'不该使用的 LocalAppData'
        with patch.dict(launcher.os.environ,{'LOCALAPPDATA':str(unrelated)}):
            data=paths.state_directory(app)
        self.assertEqual(data.parent.parent,app.parent/'data')
        self.assertEqual(data.parent.name,'Friendswine')
        self.assertFalse(str(data).startswith(str(unrelated)))

    def test_state_directory_ignores_markers_and_user_profile(self):
        install=self.root/'便携 安装'; app=install/'app'; app.mkdir(parents=True)
        # A leftover marker from an older release must not redirect anything now:
        # the data directory is purely a function of the app path.
        (install/'.friendswine-install.json').write_text(json.dumps(
            {'product':'Friendswine','install_root':str(install),'data_home':str(self.root/'别处')}),
            encoding='utf-8')
        unrelated=self.root/'不该使用的 LocalAppData'
        with patch.dict(launcher.os.environ,{'LOCALAPPDATA':str(unrelated)}):
            data=paths.state_directory(app)
        self.assertEqual(data.parent.parent,install/'data')
        self.assertEqual(paths.state_directory(app,self.root/'显式覆盖').parents[1],self.root/'显式覆盖')
        with self.assertRaises(ValueError): paths.state_directory(app,'')

    def test_streaming_hash_without_read_bytes(self):
        file=self.root/'large.bin'; payload=b'abcdef'*600000; file.write_bytes(payload)
        with patch.object(Path,'read_bytes',side_effect=AssertionError('whole-file read forbidden')):
            self.assertEqual(paths.sha256_file(file),hashlib.sha256(payload).hexdigest())

    def test_bootstrap_preserves_spaces_unicode_options_and_source_file(self):
        payload=self.root/'安装 空格 中文'/'app'/'persistent_runtime.py'
        options={'app_root':str(payload.parent),'channel':str(self.root/'用户状态'),'pid':123,
                 'run_id':'persistent-'+'a'*32,'battle_enabled':False}
        tree=ast.parse(launcher.bootstrap_source(payload,options))
        namespace=ast.literal_eval(tree.body[0].value.args[1])
        self.assertEqual(namespace['__file__'],str(payload))
        self.assertEqual(namespace['__fw_options__'],options)

    def test_injector_environment_explicitly_overrides_legacy_machine_defaults(self):
        toolroot=self.root/'导出表 工具目录'
        with patch.dict(launcher.os.environ,{'FUXIANG_GAME_DIR':'legacy-machine-path','FUXIANG_TOOL_ROOT':'legacy-tool'}):
            env=launcher.injection_environment(self.game.parent,toolroot)
            self.assertEqual(env['FUXIANG_GAME_DIR'],str(self.game.parent))
            self.assertEqual(env['FUXIANG_TOOL_ROOT'],str(toolroot))
            self.assertEqual(launcher.os.environ['FUXIANG_GAME_DIR'],'legacy-machine-path')

    def test_runtime_definitions_relocate_to_explicit_root_and_channel(self):
        app=self.root/'迁移 中文 空格'/'app'; app.mkdir(parents=True)
        channel=self.root/'独立状态'/'persistent'; channel.mkdir(parents=True)
        payload=app/'persistent_runtime.py'
        source=(launcher.ROOT/'persistent_runtime.py').read_text(encoding='utf8')
        prefix=source.split('\ntry:\n    if (type(expected_pid)')[0]
        namespace={'__file__':str(payload),'__fw_options__':{'app_root':str(app),'channel':str(channel)}}
        exec(compile(prefix,str(payload),'exec'),namespace)
        self.assertEqual(Path(namespace['ROOT']),app)
        self.assertEqual(Path(namespace['OUT']),channel/'status.json')
        self.assertEqual(namespace['VERSION'],'1.3.0')
        (app/'mock_private.py').write_text("MARKER='relocated'\n",encoding='utf8')
        module=namespace['load_private']('mock_private.py','offline_private')
        self.assertEqual(module.MARKER,'relocated')
        self.assertEqual(Path(module.__file__),app/'mock_private.py')
        self.assertFalse((channel/'status.json').exists())

    def test_doctor_no_game_never_writes_or_launches(self):
        missing=self.root/'read-only-doctor'/'config.json'
        with patch.object(launcher,'CONFIG',missing), patch.object(launcher,'game_pids',return_value=[]), \
             patch.object(launcher.subprocess,'run',side_effect=AssertionError('no process probe for absent game')), \
             patch.object(launcher,'atomic_json',side_effect=AssertionError('doctor must not write')):
            result=launcher.doctor()
        self.assertFalse(result['ready_to_start'])
        self.assertFalse(result['native_supported'])
        self.assertIn('diagnostic_error',result)
        self.assertFalse(missing.parent.exists())

    def test_unsupported_engine_preflight_never_reaches_injector(self):
        channel=self.root/'用户状态'/'persistent'
        injector=self.root/'bin'/'fuxiang-inject.exe'; injector.parent.mkdir(); injector.write_bytes(b'mock')
        calls=[]
        def run(arguments,**kwargs):
            calls.append(arguments)
            if 'raw' in arguments:
                self.fail('unsupported engine reached raw injector')
            output=Path(arguments[arguments.index('--out')+1])
            diagnostic={'observer_token':{'elevated':True},'target_image':str(self.game),
                'engine_modules':[{'name':'neox_engine.dll','path':str(self.game.parent/'neox_engine.dll'),
                                   'disk_sha256':'0'*64}]}
            output.write_text(json.dumps(diagnostic),encoding='utf8')
            return types.SimpleNamespace(returncode=0,stderr='',stdout='')
        with patch.object(launcher,'DATA',self.root/'用户状态'), patch.object(launcher,'CHANNEL',channel), \
             patch.object(launcher,'CONFIG',self.root/'nonexistent-config.json'), \
             patch.object(launcher,'INJECTOR',injector), patch.object(launcher,'game_pids',return_value=[123]), \
             patch.object(launcher,'select_game',return_value=self.game), patch.object(launcher,'live_status',return_value={}), \
             patch.object(launcher.ctypes.windll.shell32,'IsUserAnAdmin',return_value=True), \
             patch.object(launcher,'inspect_engine',return_value={'engine_sha256':'0'*64,'architecture':'x64','native_fingerprint':'a'*64}), \
             patch.object(launcher,'available_profiles',return_value=[]), \
             patch.object(launcher.subprocess,'run',side_effect=run):
            with self.assertRaisesRegex(RuntimeError,'UNSUPPORTED_NATIVE_IMPLEMENTATION'):
                launcher.start()
        self.assertEqual(len(calls),1)

    def test_release_sources_have_no_machine_absolute_paths_or_private_ipc(self):
        for name in ('friendswine.py','persistent_runtime.py','persistent_adapter.py',
                     'persistent_core.py','training_core.py','probe_environment.py','release_paths.py'):
            source=(launcher.ROOT/name).read_text(encoding='utf8')
            self.assertNotIn('D:/FeverApps',source,name)
            self.assertNotIn('C:/Users/',source,name)
            compile(source,name,'exec')
        probe=(launcher.ROOT/'probe_environment.py').read_text(encoding='utf8')
        self.assertNotIn('bridge_state',probe)
        self.assertNotIn('fuxiang/ipc',probe)
        self.assertEqual(launcher.VERSION,'1.3.0')


if __name__=='__main__':
    unittest.main(verbosity=2)
