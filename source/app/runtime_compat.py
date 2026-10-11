"""Read-only, versioned script capability checks; never invoke game functions.

Fingerprints intentionally ignore source file and line metadata. Profiles are
evidence of a previously reviewed implementation, not permission to learn and
execute an arbitrary new implementation on the first encounter.
"""
import hashlib
import json
import math
import sys
import time
import types

SCHEMA = 1
CAPABILITIES = ('display', 'training')
RESTORE_FUNCTION = 'ClientUnits.BaseUnit:BaseUnit.update_model_scale'
NEOX_EXECUTED_FLAG = 0x10000000


class UnsupportedFingerprint(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def _constant(value, depth=0, volatile_code_flags=0):
    if depth > 64:
        raise UnsupportedFingerprint('CONSTANT_DEPTH_LIMIT')
    kind = type(value)
    if value is None:
        return ['none']
    if value is Ellipsis:
        return ['ellipsis']
    if kind is bool:
        return ['bool', value]
    if kind is int:
        return ['int', str(value)]
    if kind is float:
        if not math.isfinite(value):
            raise UnsupportedFingerprint('NONFINITE_CONSTANT')
        return ['float', value.hex()]
    if kind is complex:
        return ['complex', _constant(value.real, depth+1, volatile_code_flags),
                _constant(value.imag, depth+1, volatile_code_flags)]
    if kind is str:
        return ['str', value]
    if kind is bytes:
        return ['bytes', value.hex()]
    if kind is tuple:
        return ['tuple', [_constant(item, depth+1, volatile_code_flags) for item in value]]
    if kind is frozenset:
        return ['frozenset', sorted((_constant(item, depth+1, volatile_code_flags)
                                     for item in value), key=_canonical)]
    if kind is types.CodeType:
        data = {name: getattr(value, name) for name in
                ('co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount',
                 'co_nlocals', 'co_stacksize', 'co_flags', 'co_name')}
        data['co_flags'] &= ~volatile_code_flags
        for name in ('co_names', 'co_varnames', 'co_freevars', 'co_cellvars'):
            data[name] = list(getattr(value, name))
        data['co_code'] = value.co_code.hex()
        data['co_consts'] = [_constant(item, depth+1, volatile_code_flags) for item in value.co_consts]
        data['co_exceptiontable'] = getattr(value, 'co_exceptiontable', b'').hex()
        return ['code', data]
    raise UnsupportedFingerprint('UNSUPPORTED_CONSTANT_TYPE:' + kind.__name__)


def _function_record(function, version, depth=0, active=None, volatile_code_flags=0):
    if type(function) is not types.FunctionType:
        raise UnsupportedFingerprint('NOT_PYTHON_FUNCTION')
    if depth > 32:
        raise UnsupportedFingerprint('FUNCTION_CLOSURE_DEPTH_LIMIT')
    active = set() if active is None else active
    if id(function) in active:
        raise UnsupportedFingerprint('CYCLIC_FUNCTION_CLOSURE')
    active = active | {id(function)}
    closure = []
    for cell in function.__closure__ or ():
        try:
            value = cell.cell_contents
        except ValueError:
            raise UnsupportedFingerprint('EMPTY_CLOSURE')
        # Decorator closures often contain the original Python function. Hash
        # its full implementation recursively, never execute or unwrap it.
        # Class/mutable closures remain unsupported rather than hashing names.
        closure.append(['function', _function_record(value, version, depth+1, active, volatile_code_flags)]
                       if type(value) is types.FunctionType else _constant(value, volatile_code_flags=volatile_code_flags))
    kwdefaults = function.__kwdefaults__ or {}
    if type(kwdefaults) is not dict or any(type(key) is not str for key in kwdefaults):
        raise UnsupportedFingerprint('UNSUPPORTED_KWDEFAULTS')
    return {'schema': SCHEMA, 'python_version': version,
            'code': _constant(function.__code__, volatile_code_flags=volatile_code_flags),
            'defaults': _constant(function.__defaults__, volatile_code_flags=volatile_code_flags),
            'kwdefaults': {key: _constant(value, volatile_code_flags=volatile_code_flags)
                           for key, value in kwdefaults.items()},
            'closure': closure}


def _valid_flag_policy(version, mask):
    return (type(mask) is int and mask in (0, NEOX_EXECUTED_FLAG) and
            (mask == 0 or version == '3.11'))


def function_fingerprint(function, python_version=None, *, volatile_code_flags=0):
    version = python_version or '%s.%s' % sys.version_info[:2]
    # Only the native-profile-bound NeoX 3.11 family has been observed to set
    # this bit on first execution. Default callers retain every flag bit.
    if not _valid_flag_policy(version, volatile_code_flags):
        raise UnsupportedFingerprint('UNSUPPORTED_VOLATILE_CODE_FLAGS_POLICY')
    record = _function_record(function, version, volatile_code_flags=volatile_code_flags)
    return hashlib.sha256(_canonical(record).encode('utf8')).hexdigest()


def _function_token(function, depth=0, seen=None):
    """Cheap replacement detection also follows already admitted decorators."""
    seen = set() if seen is None else seen
    if depth > 32 or id(function) in seen:
        return ('unfingerprintable_cycle_or_depth', id(function))
    cells = []
    for cell in function.__closure__ or ():
        try:
            value = cell.cell_contents
        except ValueError:
            cells.append(('empty',))
            continue
        cells.append(_function_token(value, depth+1, seen | {id(function)})
                     if type(value) is types.FunctionType else ('constant', id(value)))
    return (id(function), id(function.__code__), id(function.__defaults__),
            id(function.__kwdefaults__), tuple(cells))


def resolve_function(identifier, modules=None):
    """Resolve plain namespaces only, with no imports or descriptor evaluation."""
    modules = sys.modules if modules is None else modules
    module_name, separator, path = identifier.partition(':')
    if not separator or not module_name or not path:
        return None, 'UNSUPPORTED_INTERFACE'
    value = modules.get(module_name)
    if value is None:
        return None, 'WAITING_FOR_SCENE'
    for name in path.split('.'):
        if type(value) is types.ModuleType or isinstance(value, type):
            value = vars(value).get(name)
        else:
            return None, 'UNSUPPORTED_INTERFACE'
        if value is None:
            return None, 'UNSUPPORTED_INTERFACE'
    if type(value) is not types.FunctionType:
        return None, 'UNSUPPORTED_INTERFACE'
    return value, 'SUPPORTED'


def native_descriptor(value):
    kind = type(value)
    if kind not in (types.BuiltinFunctionType, type):
        raise UnsupportedFingerprint('UNSUPPORTED_NATIVE_INTERFACE_TYPE:' + kind.__name__)
    return {'type': kind.__name__, 'module': value.__module__, 'name': value.__name__}


def collect_observed(modules=None, *, volatile_code_flags=0):
    """Main-thread sampling helper. Returns observations, never a trusted profile.

    Enumerates directly defined Python methods for the three relevant classes;
    a reviewed subset is selected by the release author when making a profile.
    Missing classes are observations, not inferred compatibility.
    """
    modules = sys.modules if modules is None else modules
    if not _valid_flag_policy('%s.%s' % sys.version_info[:2], volatile_code_flags):
        raise UnsupportedFingerprint('UNSUPPORTED_VOLATILE_CODE_FLAGS_POLICY')
    result = {'schema': SCHEMA, 'python_version': '%s.%s' % sys.version_info[:2],
              'functions': {}, 'native_interfaces': {}, 'classes': {}, 'errors': {},
              'function_metadata': {}, 'volatile_code_flags': volatile_code_flags}
    for module_name, module in tuple(modules.items()):
        if type(module) is not types.ModuleType:
            continue
        for name in ('WorldArticle', 'UIArticleRole', 'BaseUnit'):
            cls = vars(module).get(name)
            if not isinstance(cls, type) or cls.__name__ != name:
                continue
            # Use the defining module to avoid aliases producing unstable IDs.
            if cls.__module__ != module_name:
                continue
            key = module_name + ':' + name
            result['classes'][key] = []
            for method, function in vars(cls).items():
                if type(function) is not types.FunctionType:
                    continue
                identifier = key + '.' + method
                result['classes'][key].append(method)
                result['function_metadata'][identifier] = {
                    'names': list(function.__code__.co_names),
                    'freevars': list(function.__code__.co_freevars),
                    'file': function.__code__.co_filename,
                    'line': function.__code__.co_firstlineno}
                try:
                    result['functions'][identifier] = function_fingerprint(function, volatile_code_flags=volatile_code_flags)
                except UnsupportedFingerprint as error:
                    result['errors'][identifier] = str(error)
    for identifier in ('game3d.frame_delay_exec', 'math3d.vector'):
        module_name, name = identifier.rsplit('.', 1)
        module = modules.get(module_name)
        value = vars(module).get(name) if type(module) is types.ModuleType else None
        try:
            result['native_interfaces'][identifier] = native_descriptor(value)
        except UnsupportedFingerprint as error:
            result['errors'][identifier] = str(error)
    return result


class RuntimeCompatibility:
    """Independent capabilities. Mismatches latch until a fresh clean session."""
    def __init__(self, profile, *, modules=None, clock=time.monotonic):
        self.modules = sys.modules if modules is None else modules
        self.clock = clock
        self.profile = profile if type(profile) is dict else {}
        self.runtime = self.profile.get('runtime', {})
        self.cache = {}
        self.last_full = {key: -float('inf') for key in CAPABILITIES}
        self.capabilities = {key: {'status': 'WAITING_FOR_SCENE', 'reason': 'NOT_CHECKED',
                                   'ever_supported': False, 'latched': False}
                             for key in CAPABILITIES}

    def _state(self, capability, status, reason, latch=False):
        item = self.capabilities[capability]
        item.update(status=status, reason=reason)
        item['latched'] = item['latched'] or latch
        if status == 'SUPPORTED':
            item['ever_supported'] = True
        return status == 'SUPPORTED'

    def reject_interface(self, capability, reason):
        return self._state(capability, 'UNSUPPORTED_INTERFACE', reason, True)

    def _native_ok(self):
        expected = self.runtime.get('native_interfaces')
        if type(expected) is not dict:
            return False
        for identifier in ('game3d.frame_delay_exec', 'math3d.vector'):
            module_name, name = identifier.rsplit('.', 1)
            module = self.modules.get(module_name)
            value = vars(module).get(name) if type(module) is types.ModuleType else None
            try:
                if native_descriptor(value) != expected.get(identifier):
                    return False
            except UnsupportedFingerprint:
                return False
        return True

    def check(self, capability, *, force=False):
        item = self.capabilities[capability]
        if item['latched']:
            return False
        runtime = self.runtime
        if (type(runtime) is not dict or runtime.get('schema') != SCHEMA or
            not _valid_flag_policy(runtime.get('python_version'), runtime.get('volatile_code_flags', 0)) or
            runtime.get('python_version') != '%s.%s' % sys.version_info[:2] or not self._native_ok()):
            return self._state(capability, 'UNSUPPORTED_INTERFACE', 'PYTHON_OR_NATIVE_INTERFACE_MISMATCH', True)
        functions = runtime.get('functions', {})
        required = runtime.get('capabilities', {}).get(capability)
        if type(functions) is not dict or type(required) is not list or not required:
            return self._state(capability, 'UNSUPPORTED_INTERFACE', 'MISSING_CAPABILITY_PROFILE', True)
        now = self.clock()
        full = force or now-self.last_full[capability] >= 1.
        for identifier in required:
            function, status = resolve_function(identifier, self.modules)
            if function is None:
                if item['ever_supported']:
                    status = 'IMPLEMENTATION_CHANGED'
                return self._state(capability, status, identifier, status != 'WAITING_FOR_SCENE')
            if (identifier == RESTORE_FUNCTION and 'math3d' in function.__code__.co_names and
                function.__globals__.get('math3d') is not self.modules.get('math3d')):
                return self._state(capability, 'IMPLEMENTATION_CHANGED', 'RESTORE_NATIVE_GLOBAL_CHANGED', True)
            token = _function_token(function)
            cached = self.cache.get(identifier)
            if full or cached is None or cached[0] != token:
                try:
                    digest = function_fingerprint(function, volatile_code_flags=runtime.get('volatile_code_flags', 0))
                except UnsupportedFingerprint as error:
                    return self._state(capability, 'UNSUPPORTED_INTERFACE', identifier+':'+str(error), True)
                self.cache[identifier] = (token, digest)
            else:
                digest = cached[1]
            if digest != functions.get(identifier):
                item.update(function_id=identifier, expected_sha256=functions.get(identifier),
                            observed_sha256=digest)
                return self._state(capability, 'IMPLEMENTATION_CHANGED', identifier, True)
        if full:
            self.last_full[capability] = now
        return self._state(capability, 'SUPPORTED', 'VERIFIED_IMPLEMENTATION')

    def function_matches(self, identifier, function, *, force=True):
        """Restoration guard remains usable after another function invalidates a capability."""
        # A matching Python body can still call a replaced native dependency.
        # Keep dirty ownership records rather than call through that change.
        if not self._native_ok():
            return False
        if not _valid_flag_policy(self.runtime.get('python_version'), self.runtime.get('volatile_code_flags', 0)):
            return False
        expected = self.runtime.get('functions', {}).get(identifier)
        resolved, _ = resolve_function(identifier, self.modules)
        if resolved is not function or expected is None:
            return False
        if identifier == RESTORE_FUNCTION and 'math3d' in function.__code__.co_names:
            if function.__globals__.get('math3d') is not self.modules.get('math3d'):
                return False
        try:
            token = _function_token(function)
            cached = self.cache.get(identifier)
            if force or cached is None or cached[0] != token:
                cached = (token, function_fingerprint(function, volatile_code_flags=self.runtime.get('volatile_code_flags', 0)))
                self.cache[identifier] = cached
            return cached[1] == expected
        except UnsupportedFingerprint:
            return False

    def diagnostics(self):
        return {'profile_id': self.profile.get('profile_id', self.profile.get('id')),
                'python_version': '%s.%s' % sys.version_info[:2],
                'expected_python_version': self.runtime.get('python_version'),
                'fingerprint_schema': SCHEMA,
                'volatile_code_flags': self.runtime.get('volatile_code_flags', 0),
                'scope': 'reviewed_script_implementations_and_native_interface_structure',
                'capabilities': {key: dict(value) for key, value in self.capabilities.items()}}
