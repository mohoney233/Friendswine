"""Implementation-update tests; no game process, UI, or game callbacks."""
import sys
import types
import unittest
from unittest.mock import patch

from runtime_compat import (RuntimeCompatibility, UnsupportedFingerprint,
                            function_fingerprint, native_descriptor)


def make_profile(training_function=None):
    def show(self):
        return self.model
    if training_function is None:
        def training_function(self, force=False):
            return self.scale if force else None
    class UIArticleRole:
        pass
    class BaseUnit:
        pass
    UIArticleRole.show = show
    BaseUnit.update_model_scale = training_function
    modules = {}
    for name in ('ui.UIArticleRole', 'ClientUnits.BaseUnit', 'game3d', 'math3d'):
        modules[name] = types.ModuleType(name)
    modules['ui.UIArticleRole'].UIArticleRole = UIArticleRole
    modules['ClientUnits.BaseUnit'].BaseUnit = BaseUnit
    modules['game3d'].frame_delay_exec = len
    modules['math3d'].vector = tuple
    display_id = 'ui.UIArticleRole:UIArticleRole.show'
    training_id = 'ClientUnits.BaseUnit:BaseUnit.update_model_scale'
    profile = {'profile_id': 'offline-reviewed', 'runtime': {
        'schema': 1, 'python_version': '%s.%s' % sys.version_info[:2],
        'functions': {display_id: function_fingerprint(show),
                      training_id: function_fingerprint(training_function)},
        'capabilities': {'display': [display_id], 'training': [training_id]},
        'native_interfaces': {'game3d.frame_delay_exec': native_descriptor(len),
                              'math3d.vector': native_descriptor(tuple)}}}
    return profile, modules


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_volatile_flag_requires_explicit_known_python_policy(self):
        def function(value):
            return value + 1
        cold = function_fingerprint(function, '3.11')
        function.__code__ = function.__code__.replace(co_flags=function.__code__.co_flags | 0x10000000)
        self.assertNotEqual(cold, function_fingerprint(function, '3.11'))
        self.assertEqual(cold, function_fingerprint(function, '3.11', volatile_code_flags=0x10000000))
        for version, mask in (('3.12', 0x10000000), ('3.11', 0x80), ('3.11', True)):
            with self.subTest(version=version, mask=mask), self.assertRaises(UnsupportedFingerprint):
                function_fingerprint(function, version, volatile_code_flags=mask)

    def test_mask_preserves_other_flags_and_body_changes(self):
        def function(value):
            return value + 1
        original = function.__code__
        expected = function_fingerprint(function, '3.11', volatile_code_flags=0x10000000)
        function.__code__ = original.replace(co_flags=original.co_flags | 0x10000000 | 0x80)
        self.assertNotEqual(expected, function_fingerprint(function, '3.11', volatile_code_flags=0x10000000))
        def changed(value):
            return value + 2
        function.__code__ = changed.__code__.replace(co_flags=changed.__code__.co_flags | 0x10000000)
        self.assertNotEqual(expected, function_fingerprint(function, '3.11', volatile_code_flags=0x10000000))

    def test_volatile_mask_applies_to_nested_code_and_function_closures(self):
        def original(value):
            def inner(x):
                return x + 1
            return inner(value)
        def decorate(function):
            def wrapped(value):
                return function(value)
            return wrapped
        wrapped = decorate(original)
        expected = function_fingerprint(wrapped, '3.11', volatile_code_flags=0x10000000)
        original.__code__ = original.__code__.replace(
            co_flags=original.__code__.co_flags | 0x10000000,
            co_consts=tuple(value.replace(co_flags=value.co_flags | 0x10000000)
                            if type(value) is types.CodeType else value for value in original.__code__.co_consts))
        wrapped.__code__ = wrapped.__code__.replace(co_flags=wrapped.__code__.co_flags | 0x10000000)
        self.assertEqual(expected, function_fingerprint(wrapped, '3.11', volatile_code_flags=0x10000000))

    def test_runtime_profile_accepts_only_its_explicit_volatile_bit(self):
        profile, modules = make_profile()
        profile['runtime']['python_version'] = '3.11'
        profile['runtime']['volatile_code_flags'] = 0x10000000
        from runtime_compat import resolve_function
        for identifier in profile['runtime']['functions']:
            function, _ = resolve_function(identifier, modules)
            profile['runtime']['functions'][identifier] = function_fingerprint(function, '3.11', volatile_code_flags=0x10000000)
        with patch('runtime_compat.sys.version_info', (3, 11, 0)):
            monitor = RuntimeCompatibility(profile, modules=modules)
            self.assertTrue(monitor.check('training'))
            function = modules['ClientUnits.BaseUnit'].BaseUnit.update_model_scale
            function.__code__ = function.__code__.replace(co_flags=function.__code__.co_flags | 0x10000000)
            self.assertTrue(monitor.check('training'))
            function.__code__ = function.__code__.replace(co_flags=function.__code__.co_flags | 0x80)
            self.assertFalse(monitor.check('training'))

    def test_line_and_filename_only_change_is_compatible(self):
        namespace = {}
        exec(compile('def f(x=1):\n return x + 2\n', 'old/path.py', 'exec'), namespace)
        before = function_fingerprint(namespace['f'])
        exec(compile('\n\n\n# inserted comment\ndef f(x=1):\n return x + 2\n', 'new/path.py', 'exec'), namespace)
        self.assertEqual(before, function_fingerprint(namespace['f']))

    def test_body_and_default_changes_are_distinct(self):
        namespace = {}
        values = []
        for source in ('def f(x=1): return x+2', 'def f(x=1): return x+3',
                       'def f(x=9): return x+2'):
            exec(source, namespace)
            values.append(function_fingerprint(namespace['f']))
        self.assertEqual(len(set(values)), 3)

    def test_nested_code_line_metadata_ignored(self):
        def f():
            def inner():
                return 1
            return inner
        before = function_fingerprint(f)
        f.__code__ = f.__code__.replace(co_firstlineno=800,
            co_consts=tuple(value.replace(co_firstlineno=900, co_filename='moved.py')
                            if type(value) is types.CodeType else value for value in f.__code__.co_consts))
        self.assertEqual(before, function_fingerprint(f))

    def test_unknown_constant_and_mutable_default_rejected(self):
        def f(value=[]):
            return value
        with self.assertRaises(UnsupportedFingerprint):
            function_fingerprint(f)
        def g():
            return 1
        g.__code__ = g.__code__.replace(co_consts=(None, object()))
        with self.assertRaises(UnsupportedFingerprint):
            function_fingerprint(g)

    def test_decorator_function_closure_hashes_inner_implementation(self):
        def decorate(function):
            def wrapped(*args):
                return function(*args)
            return wrapped
        def original():
            return 1
        wrapped = decorate(original)
        before = function_fingerprint(wrapped)
        original.__code__ = original.__code__.replace(co_firstlineno=900)
        self.assertEqual(before, function_fingerprint(wrapped))
        def changed():
            return 2
        original.__code__ = changed.__code__
        self.assertNotEqual(before, function_fingerprint(wrapped))

    def test_cyclic_function_and_class_closures_rejected(self):
        def factory():
            function = None
            def recurse():
                return function
            function = recurse
            return recurse
        with self.assertRaisesRegex(UnsupportedFingerprint, 'CYCLIC_FUNCTION_CLOSURE'):
            function_fingerprint(factory())
        class Example:
            def method(self):
                return __class__
        with self.assertRaisesRegex(UnsupportedFingerprint, 'UNSUPPORTED_CONSTANT_TYPE:type'):
            function_fingerprint(Example.method)

    def test_training_change_does_not_disable_display(self):
        profile, modules = make_profile()
        monitor = RuntimeCompatibility(profile, modules=modules)
        self.assertTrue(monitor.check('display'))
        self.assertTrue(monitor.check('training'))
        def changed(self, force=False):
            raise AssertionError('never invoked')
        modules['ClientUnits.BaseUnit'].BaseUnit.update_model_scale = changed
        self.assertFalse(monitor.check('training'))
        self.assertTrue(monitor.check('display'))
        self.assertEqual(monitor.diagnostics()['capabilities']['training']['status'], 'IMPLEMENTATION_CHANGED')

    def test_code_replaced_on_same_function_detected_without_period_delay(self):
        profile, modules = make_profile()
        monitor = RuntimeCompatibility(profile, modules=modules, clock=lambda: 0.)
        self.assertTrue(monitor.check('training'))
        function = modules['ClientUnits.BaseUnit'].BaseUnit.update_model_scale
        def changed(self, force=False):
            return 'changed'
        function.__code__ = changed.__code__
        self.assertFalse(monitor.check('training'))

    def test_decorator_inner_code_change_detected_without_period_delay(self):
        def original(self, force=False):
            return 1
        def decorate(function):
            def wrapped(self, force=False):
                return function(self, force=force)
            return wrapped
        wrapped = decorate(original)
        profile, modules = make_profile(wrapped)
        monitor = RuntimeCompatibility(profile, modules=modules, clock=lambda: 0.)
        self.assertTrue(monitor.check('training'))
        def changed(self, force=False):
            return 2
        original.__code__ = changed.__code__
        self.assertFalse(monitor.check('training'))

    def test_hot_update_latches_even_if_original_function_returns(self):
        profile, modules = make_profile()
        monitor = RuntimeCompatibility(profile, modules=modules)
        cls = modules['ClientUnits.BaseUnit'].BaseUnit
        original = cls.update_model_scale
        self.assertTrue(monitor.check('training'))
        cls.update_model_scale = lambda self: None
        self.assertFalse(monitor.check('training'))
        cls.update_model_scale = original
        self.assertFalse(monitor.check('training'))
        self.assertTrue(monitor.function_matches('ClientUnits.BaseUnit:BaseUnit.update_model_scale', original))

    def test_missing_scene_module_waits_but_missing_member_rejects(self):
        profile, modules = make_profile()
        scene = modules.pop('ui.UIArticleRole')
        monitor = RuntimeCompatibility(profile, modules=modules)
        self.assertFalse(monitor.check('display'))
        self.assertEqual(monitor.capabilities['display']['status'], 'WAITING_FOR_SCENE')
        modules['ui.UIArticleRole'] = scene
        self.assertTrue(monitor.check('display'))
        del scene.UIArticleRole.show
        self.assertFalse(monitor.check('display'))
        self.assertEqual(monitor.capabilities['display']['status'], 'IMPLEMENTATION_CHANGED')
        fresh = RuntimeCompatibility(profile, modules=modules)
        self.assertFalse(fresh.check('display'))
        self.assertEqual(fresh.capabilities['display']['status'], 'UNSUPPORTED_INTERFACE')

    def test_unknown_restore_implementation_is_never_called(self):
        profile, modules = make_profile()
        monitor = RuntimeCompatibility(profile, modules=modules)
        function = modules['ClientUnits.BaseUnit'].BaseUnit.update_model_scale
        self.assertTrue(monitor.function_matches('ClientUnits.BaseUnit:BaseUnit.update_model_scale', function))
        function.__defaults__ = (True,)
        self.assertFalse(monitor.function_matches('ClientUnits.BaseUnit:BaseUnit.update_model_scale', function))

    def test_unchanged_restore_body_rejects_changed_native_dependency(self):
        profile, modules = make_profile()
        monitor = RuntimeCompatibility(profile, modules=modules)
        function = modules['ClientUnits.BaseUnit'].BaseUnit.update_model_scale
        self.assertTrue(monitor.function_matches('ClientUnits.BaseUnit:BaseUnit.update_model_scale', function))
        modules['math3d'].vector = lambda *args: self.fail('must not be called')
        self.assertFalse(monitor.function_matches('ClientUnits.BaseUnit:BaseUnit.update_model_scale', function))

    def test_restore_global_cannot_shadow_verified_math_module(self):
        namespace = {}
        exec('def update_model_scale(self, force=False):\n return math3d.vector(1, 1, 1)', namespace)
        function = namespace['update_model_scale']
        profile, modules = make_profile(function)
        monitor = RuntimeCompatibility(profile, modules=modules)
        namespace['math3d'] = modules['math3d']
        self.assertTrue(monitor.function_matches('ClientUnits.BaseUnit:BaseUnit.update_model_scale', function))
        namespace['math3d'] = types.SimpleNamespace(vector=lambda *args: self.fail('must not be called'))
        self.assertFalse(monitor.function_matches('ClientUnits.BaseUnit:BaseUnit.update_model_scale', function))
        self.assertFalse(monitor.check('training'))
        self.assertEqual(monitor.capabilities['training']['reason'], 'RESTORE_NATIVE_GLOBAL_CHANGED')

    def test_python_minor_version_and_native_replacement_rejected(self):
        profile, modules = make_profile()
        profile['runtime']['python_version'] = '0.0'
        self.assertFalse(RuntimeCompatibility(profile, modules=modules).check('display'))
        profile, modules = make_profile()
        modules['game3d'].frame_delay_exec = lambda *a: self.fail('must not execute')
        self.assertFalse(RuntimeCompatibility(profile, modules=modules).check('display'))


if __name__ == '__main__':
    unittest.main()
