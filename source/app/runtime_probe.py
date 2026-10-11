"""Finite, read-only main-thread compatibility observations.

The host must first validate process identity and the native implementation.
This probe never uses the persistent controller registry, calls game setters,
imports game modules, or treats observed implementations as trusted profiles.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import types
import uuid


def _same_path(left, right):
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _no_links(path):
    path = Path(path)
    for current in (path, *path.parents):
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('PROBE_REPARSE_POINT_REJECTED')


def _load_helper(app_root, filename):
    path = Path(app_root) / filename
    _no_links(path)
    module = types.ModuleType('friendswine_readonly_' + Path(filename).stem)
    module.__file__ = str(path)
    source = path.read_text(encoding='utf-8-sig')
    exec(compile(source, str(path), 'exec'), vars(module))
    return module


def _validate_options(options, source_file):
    if type(options) is not dict or set(options) != {'pid', 'nonce', 'app_root', 'output_file', 'profiles'}:
        raise ValueError('INVALID_PROBE_OPTIONS')
    if type(options['pid']) is not int or options['pid'] != os.getpid():
        raise ValueError('PROBE_PID_MISMATCH')
    nonce = options['nonce']
    if type(nonce) is not str or not re.fullmatch('[0-9a-f]{32}', nonce):
        raise ValueError('INVALID_PROBE_NONCE')
    root, output = options['app_root'], options['output_file']
    if type(root) is not str or not os.path.isabs(root) or not os.path.isdir(root):
        raise ValueError('INVALID_PROBE_APP_ROOT')
    if (Path(source_file).name != 'runtime_probe.py'
            or not _same_path(Path(source_file).parent, root)):
        raise ValueError('PROBE_SOURCE_ROOT_MISMATCH')
    if type(output) is not str or not os.path.isabs(output):
        raise ValueError('INVALID_PROBE_OUTPUT_PATH')
    path = Path(output)
    if (path.name != 'runtime-probe.json' or path.parent.name != nonce
            or path.parent.parent.name != 'evidence' or not path.parent.is_dir()):
        raise ValueError('INVALID_PROBE_EVIDENCE_DIRECTORY')
    _no_links(root)
    _no_links(path)
    if path.exists():
        raise ValueError('PROBE_OUTPUT_ALREADY_EXISTS')
    if type(options['profiles']) is not list or not 1 <= len(options['profiles']) <= 32:
        raise ValueError('INVALID_PROBE_PROFILES')
    return dict(options)


def _native_value(modules, identifier):
    module_name, name = identifier.rsplit('.', 1)
    module = modules.get(module_name)
    return vars(module).get(name) if type(module) is types.ModuleType else None


def _world_type(modules):
    """Read plain stored fields only: no world/property descriptors are called."""
    module = modules.get('ui.UIArticleRole')
    gk = vars(module).get('GK') if type(module) is types.ModuleType else None
    if type(gk) is types.ModuleType:
        manager = vars(gk).get('g_world_mgr')
    else:
        manager = None
    try:
        world = vars(manager).get('cur_world')
    except TypeError:
        return None
    return type(world).__name__ if world is not None else None


class ReadonlyProbe:
    def __init__(self, options, runtime, profiles_api, *, modules=None, clock=None):
        self.options = options
        self.runtime = runtime
        self.profiles_api = profiles_api
        self.modules = sys.modules if modules is None else modules
        self.clock = time.monotonic if clock is None else clock
        self.deadline = self.clock() + 30.
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.scheduler = None
        self.checkers = [(profile, runtime.RuntimeCompatibility(profile, modules=self.modules, clock=self.clock))
                         for profile in options['profiles']]
        self.record = {'schema': 1, 'nonce': options['nonce'], 'pid': os.getpid(),
                       'python_version': '%s.%s' % sys.version_info[:2],
                       'callback_thread': None, 'main_thread': threading.main_thread().ident,
                       'world_type': None, 'profiles': [], 'frames': 0, 'done': False,
                       'read_only': True, 'model_setters_called': 0}

    def finish(self, error=None, **changes):
        with self.lock:
            if self.finished.is_set():
                return
            self.record.update(changes, done=True)
            if error is not None:
                self.record['error'] = str(error)[:1000]
            self.finished.set()

    def begin(self):
        if self.clock() >= self.deadline:
            self.finish('PROBE_EXPIRED')
            return
        current = _native_value(self.modules, 'game3d.frame_delay_exec')
        try:
            descriptor = self.runtime.native_descriptor(current)
        except self.runtime.UnsupportedFingerprint as error:
            self.finish('UNSUPPORTED_PROBE_SCHEDULER:' + str(error))
            return
        if not any(profile['runtime']['native_interfaces']['game3d.frame_delay_exec'] == descriptor
                   for profile, _ in self.checkers):
            self.finish('PROBE_SCHEDULER_DESCRIPTOR_MISMATCH')
            return
        # Keep the checked reference. Never fetch another function before a call.
        self.scheduler = current
        self._schedule()

    def _schedule(self):
        if self.finished.is_set():
            return
        if self.clock() >= self.deadline:
            self.finish('PROBE_EXPIRED')
            return
        try:
            self.scheduler(1, self.callback)
        except BaseException as error:
            self.finish('PROBE_SCHEDULER_ERROR:' + type(error).__name__ + ':' + str(error))

    def _observed(self, profile):
        mask = profile['runtime'].get('volatile_code_flags', 0)
        observed = {'schema': 1, 'python_version': '%s.%s' % sys.version_info[:2],
                    'functions': {}, 'capabilities': profile['runtime']['capabilities'],
                    'native_interfaces': {}, 'volatile_code_flags': mask}
        errors = {}
        for identifier in profile['runtime']['functions']:
            function, status = self.runtime.resolve_function(identifier, self.modules)
            if function is None:
                errors[identifier] = status
                continue
            try:
                observed['functions'][identifier] = self.runtime.function_fingerprint(
                    function, volatile_code_flags=mask)
            except self.runtime.UnsupportedFingerprint as error:
                errors[identifier] = str(error)
        for identifier in ('game3d.frame_delay_exec', 'math3d.vector'):
            try:
                observed['native_interfaces'][identifier] = self.runtime.native_descriptor(
                    _native_value(self.modules, identifier))
            except self.runtime.UnsupportedFingerprint as error:
                errors[identifier] = str(error)
        digest = hashlib.sha256(self.profiles_api.canonical_bytes(observed)).hexdigest()
        return observed, digest, errors

    def callback(self):
        if self.finished.is_set():
            return
        if self.clock() >= self.deadline:
            self.finish('PROBE_EXPIRED')
            return
        callback_thread = threading.get_ident()
        if callback_thread != self.record['main_thread']:
            self.finish('PROBE_NOT_ON_MAIN_THREAD', callback_thread=callback_thread)
            return
        try:
            results = []
            waiting = False
            for profile, checker in self.checkers:
                for capability in ('display', 'training'):
                    checker.check(capability, force=True)
                diagnostics = checker.diagnostics()
                waiting = waiting or any(value['status'] == 'WAITING_FOR_SCENE'
                                         for value in diagnostics['capabilities'].values())
                observed, fingerprint, errors = self._observed(profile)
                results.append({'id': profile['id'], 'diagnostics': diagnostics,
                                'observed_runtime': observed,
                                'observed_runtime_fingerprint': fingerprint,
                                'observation_errors': errors})
            with self.lock:
                if self.finished.is_set():
                    return
                self.record.update(callback_thread=callback_thread, world_type=_world_type(self.modules),
                                   profiles=results, frames=self.record['frames'] + 1)
                frames = self.record['frames']
            if not waiting or frames >= 3:
                self.finish()
            else:
                self._schedule()
        except BaseException as error:
            self.finish('PROBE_CALLBACK_ERROR:' + type(error).__name__ + ':' + str(error),
                        callback_thread=callback_thread)

    def write_result(self):
        """Only this bounded helper thread does disk IO; it never accesses game objects."""
        if not self.finished.wait(max(0., self.deadline - self.clock())):
            self.finish('PROBE_CALLBACK_TIMEOUT')
        with self.lock:
            snapshot = dict(self.record)
        output = Path(self.options['output_file'])
        temporary = output.with_name('.runtime-probe.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
            # Nonce path was reserved by the host, and a stale result was rejected.
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)


def start_probe(options, *, source_file=None):
    options = _validate_options(options, __file__ if source_file is None else source_file)
    profiles_api = _load_helper(options['app_root'], 'compatibility.py')
    ids = set()
    for profile in options['profiles']:
        profiles_api.profile_digest(profile)
        if profile['id'] in ids:
            raise ValueError('DUPLICATE_PROBE_PROFILE')
        ids.add(profile['id'])
    runtime = _load_helper(options['app_root'], 'runtime_compat.py')
    probe = ReadonlyProbe(options, runtime, profiles_api)
    threading.Thread(target=probe.write_result, name='friendswine-readonly-probe', daemon=True).start()
    probe.begin()
    return probe


if '__fw_probe_options__' in globals():
    start_probe(globals()['__fw_probe_options__'])
