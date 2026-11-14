"""Versioned, data-only compatibility profiles and diagnostic caches.

Profiles identify a *verified native implementation*, not just exported names.
An engine hash identifies an observation; it never grants ABI compatibility.
Cached results are diagnostics only. Every attachment still needs a live probe.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
import uuid
import zipfile

PROFILE_SCHEMA = 'friendswine.compatibility.v1'
PACK_SCHEMA = 'friendswine.profile-pack.v1'
CACHE_SCHEMA = 'friendswine.compatibility-cache.v1'
CONTRACT_SCHEMA = 'friendswine.native-contract.v1'
REQUIRED_EXPORTS = ('PyGILState_Ensure', 'PyGILState_Release', 'PyRun_SimpleString')
MAX_BYTES = 1024 * 1024
MAX_PROFILES = 32
MAX_STORE_FILES = 128
_SHA = re.compile(r'^[0-9a-f]{64}$')
_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{0,79}$')
_FUNCTION = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$')
_RESERVED = {'_content_digest', '_origin', '_path'}


class CompatibilityError(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _fail(reason):
    raise CompatibilityError(reason)


def _keys(value, expected, context, optional=()):
    if (type(value) is not dict or not set(expected).issubset(value)
            or set(value) - set(expected) - set(optional)):
        _fail(context + ':INVALID_FIELDS')


def _text(value, limit, context):
    if type(value) is not str or not value or len(value) > limit or '\x00' in value:
        _fail(context + ':INVALID_STRING')


def _sha(value, context):
    if type(value) is not str or not _SHA.fullmatch(value):
        _fail(context + ':INVALID_SHA256')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('DUPLICATE_JSON_KEY:' + str(key))
        result[key] = value
    return result


def _loads(raw):
    if len(raw) > MAX_BYTES:
        _fail('JSON_TOO_LARGE')
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs,
                          parse_constant=lambda value: _fail('NONFINITE_JSON:' + value))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CompatibilityError('INVALID_JSON') from exc


def validate_runtime(runtime):
    _keys(runtime, ('schema', 'python_version', 'functions', 'capabilities',
                    'native_interfaces'), 'RUNTIME', optional=('volatile_code_flags',))
    if type(runtime['schema']) is not int or runtime['schema'] != 1:
        _fail('UNKNOWN_RUNTIME_SCHEMA')
    if type(runtime['python_version']) is not str or not re.fullmatch(r'3\.\d{1,2}', runtime['python_version']):
        _fail('INVALID_PYTHON_VERSION')
    _validate_volatile_flags(runtime, 'RUNTIME')
    functions = runtime['functions']
    if type(functions) is not dict or not 1 <= len(functions) <= 128:
        _fail('INVALID_RUNTIME_FUNCTIONS')
    for name, fingerprint in functions.items():
        if type(name) is not str or len(name) > 240 or not _FUNCTION.fullmatch(name):
            _fail('INVALID_FUNCTION_ID')
        _sha(fingerprint, 'FUNCTION_FINGERPRINT')
    capabilities = runtime['capabilities']
    _keys(capabilities, ('display', 'training'), 'CAPABILITIES')
    for name, refs in capabilities.items():
        if type(refs) is not list or len(refs) > 128 or any(type(ref) is not str for ref in refs):
            _fail('INVALID_CAPABILITY:' + name)
        if len(set(refs)) != len(refs) or any(ref not in functions for ref in refs):
            _fail('INVALID_CAPABILITY_REFERENCE:' + name)
    if not any(capabilities.values()):
        _fail('NO_CAPABILITIES')
    if capabilities['training'] and 'ClientUnits.BaseUnit:BaseUnit.update_model_scale' not in capabilities['training']:
        _fail('TRAINING_RESTORE_NOT_VERIFIED')
    interfaces = runtime['native_interfaces']
    _keys(interfaces, ('game3d.frame_delay_exec', 'math3d.vector'), 'NATIVE_INTERFACES')
    for descriptor in interfaces.values():
        _keys(descriptor, ('type', 'module', 'name'), 'NATIVE_DESCRIPTOR')
        for key, value in descriptor.items():
            _text(value, 160, 'NATIVE_DESCRIPTOR_' + key)
    return runtime


def _validate_volatile_flags(runtime, context):
    mask = runtime.get('volatile_code_flags', 0)
    if (type(mask) is not int or mask not in (0, 0x10000000)
            or (mask != 0 and runtime['python_version'] != '3.11')):
        _fail(context + ':INVALID_VOLATILE_CODE_FLAGS')
    return mask


def runtime_fingerprint(runtime):
    return _digest(validate_runtime(runtime))


def native_contract(report):
    """Structural calling contract of an inspected engine build.

    The contract describes *how* the injector reaches CPython - which exports
    exist, and which reviewed boundary each one has - not the bytes of one
    build. It is therefore stable across engine updates, while the byte
    fingerprint stays the exact evidence for a single build.
    """
    if type(report) is not dict:
        return None
    exports = report.get('exports')
    if type(exports) is not dict or set(exports) != set(REQUIRED_EXPORTS):
        return None
    contract = {}
    for name, procedure in exports.items():
        if type(procedure) is not dict:
            return None
        kind = procedure.get('boundary_kind')
        if type(kind) is not str or not kind or len(kind) > 64:
            return None
        contract[name] = kind
    return {'schema': CONTRACT_SCHEMA, 'exports': contract}


def validate_contract(contract):
    _keys(contract, ('schema', 'exports'), 'CONTRACT')
    if contract['schema'] != CONTRACT_SCHEMA:
        _fail('UNKNOWN_CONTRACT_SCHEMA')
    exports = contract['exports']
    if type(exports) is not dict or set(exports) != set(REQUIRED_EXPORTS):
        _fail('INVALID_CONTRACT_EXPORTS')
    for kind in exports.values():
        _text(kind, 64, 'CONTRACT_BOUNDARY_KIND')
    return contract


def contract_digest(contract):
    return _digest(validate_contract(contract))


def validate_profile(profile):
    _keys(profile, ('schema', 'id', 'revision', 'architecture', 'native_fingerprint',
                    'runtime', 'source'), 'PROFILE', optional=('native_contract',))
    if profile['schema'] != PROFILE_SCHEMA:
        _fail('UNKNOWN_PROFILE_SCHEMA')
    if type(profile['id']) is not str or not _ID.fullmatch(profile['id']):
        _fail('INVALID_PROFILE_ID')
    if type(profile['revision']) is not int or not 1 <= profile['revision'] <= 2147483647:
        _fail('INVALID_PROFILE_REVISION')
    if profile['architecture'] != 'x64':
        _fail('UNSUPPORTED_ARCHITECTURE')
    _sha(profile['native_fingerprint'], 'NATIVE_FINGERPRINT')
    if 'native_contract' in profile:
        validate_contract(profile['native_contract'])
    validate_runtime(profile['runtime'])
    _keys(profile['source'], ('label', 'details'), 'SOURCE')
    _text(profile['source']['label'], 160, 'SOURCE_LABEL')
    _text(profile['source']['details'], 2048, 'SOURCE_DETAILS')
    if len(canonical_bytes(profile)) > MAX_BYTES // 2:
        _fail('PROFILE_TOO_LARGE')
    return profile


def profile_digest(profile):
    plain = {key: value for key, value in profile.items() if key not in _RESERVED}
    return _digest(validate_profile(plain))


def plain_profile(profile):
    """The profile document without the store bookkeeping fields."""
    return {key: value for key, value in profile.items() if key not in _RESERVED}


def make_profile_pack(profiles):
    values = [{key: value for key, value in profile.items() if key not in _RESERVED}
              for profile in profiles]
    _validate_profiles(values)
    return {'schema': PACK_SCHEMA, 'profiles': values, 'sha256': _digest(values)}


def _validate_profiles(profiles):
    if type(profiles) is not list or not 1 <= len(profiles) <= MAX_PROFILES:
        _fail('INVALID_PROFILE_COUNT')
    ids = set()
    for profile in profiles:
        validate_profile(profile)
        if profile['id'] in ids:
            _fail('DUPLICATE_PROFILE_ID:' + profile['id'])
        ids.add(profile['id'])
    return profiles


def _decode_document(raw):
    document = _loads(raw)
    if type(document) is not dict:
        _fail('INVALID_DOCUMENT')
    if document.get('schema') == PROFILE_SCHEMA:
        return _validate_profiles([document])
    _keys(document, ('schema', 'profiles', 'sha256'), 'PACK')
    if document['schema'] != PACK_SCHEMA:
        _fail('UNKNOWN_PACK_SCHEMA')
    profiles = _validate_profiles(document['profiles'])
    _sha(document['sha256'], 'PACK_SHA256')
    if _digest(profiles) != document['sha256']:
        _fail('PACK_INTEGRITY_MISMATCH')
    return profiles


def _no_link(path):
    for current in (Path(path), *Path(path).parents):
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            _fail('REPARSE_POINT_REJECTED:' + str(current))


def _read_file(path):
    path = Path(path)
    _no_link(path)
    if path.stat().st_size > MAX_BYTES:
        _fail('FILE_TOO_LARGE')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        _fail('FILE_TOO_LARGE')
    return raw


def _builtin_dir():
    return Path(__file__).resolve().parent / 'resources' / 'compatibility'


def load_profiles(bundled_dir=None, user_dir=None):
    result, ids = [], set()
    for directory, origin in ((bundled_dir if bundled_dir is not None else _builtin_dir(), 'bundled'),
                              (user_dir, 'imported')):
        if directory is None:
            continue
        directory = Path(directory)
        _no_link(directory)
        if not directory.exists():
            continue
        files = sorted(directory.glob('*.json'))
        if len(files) > MAX_STORE_FILES:
            _fail('TOO_MANY_PROFILE_FILES')
        for path in files:
            for profile in _decode_document(_read_file(path)):
                if profile['id'] in ids:
                    _fail('DUPLICATE_PROFILE_ID:' + profile['id'])
                ids.add(profile['id'])
                result.append(dict(profile, _content_digest=profile_digest(profile),
                                   _origin=origin, _path=str(path.resolve())))
    return result


def _architecture(report):
    architecture = report.get('architecture')
    if architecture is not None:
        return architecture
    return 'x64' if report.get('machine') in (34404, '0x8664', '8664', 'AMD64', 'x64') else None


def matching_profiles(native_report, profiles):
    if type(native_report) is not dict or _architecture(native_report) != 'x64':
        return []
    fingerprint = native_report.get('native_fingerprint')
    if type(fingerprint) is not str or not _SHA.fullmatch(fingerprint):
        return []
    matches = [profile for profile in profiles
               if profile['architecture'] == 'x64' and profile['native_fingerprint'] == fingerprint]
    return sorted(matches, key=lambda profile: (profile.get('_origin') != 'imported',
                                               profile['id'], -profile['revision']))


def select_profile(native_report, profiles, profile_id=None):
    matches = matching_profiles(native_report, profiles)
    if profile_id is not None:
        matches = [profile for profile in matches if profile['id'] == profile_id]
    if not matches:
        return None
    if len(matches) != 1:
        _fail('AMBIGUOUS_PROFILE:' + ','.join(profile['id'] for profile in matches))
    return matches[0]


def match_profiles(native_report, profiles):
    """Match profiles against an inspected engine, keeping the admitting evidence.

    'exact'    - the reviewed byte fingerprint of this very build matches.
    'contract' - the reviewed native calling contract still holds for this
                 build. The reviewed script implementations are still proven
                 live by the read-only probe before anything is written.
    """
    if type(native_report) is not dict or _architecture(native_report) != 'x64':
        return []
    fingerprint = native_report.get('native_fingerprint')
    exact_fingerprint = type(fingerprint) is str and bool(_SHA.fullmatch(fingerprint))
    contract = native_contract(native_report)
    exact, compatible = [], []
    for profile in profiles:
        if profile.get('architecture') != 'x64':
            continue
        if exact_fingerprint and profile['native_fingerprint'] == fingerprint:
            exact.append(profile)
            continue
        expected = profile.get('native_contract')
        if contract is not None and type(expected) is dict and expected == contract:
            compatible.append(profile)
    order = lambda profile: (profile.get('_origin') != 'imported', profile['id'], -profile['revision'])
    return ([(profile, 'exact') for profile in sorted(exact, key=order)] +
            [(profile, 'contract') for profile in sorted(compatible, key=order)])


def changed_functions(baseline, observed):
    """Which reviewed script implementations no longer match the observation.

    Returns (changed, unresolved): changed functions were observed with a
    different implementation, unresolved ones were not observed at all.
    """
    expected = baseline.get('runtime', {}).get('functions', {})
    seen = observed.get('functions', {}) if type(observed) is dict else {}
    if type(expected) is not dict or type(seen) is not dict:
        return [], sorted(expected)
    changed = sorted(identifier for identifier, digest in seen.items()
                     if identifier in expected and expected[identifier] != digest)
    unresolved = sorted(identifier for identifier in expected if identifier not in seen)
    return changed, unresolved


def mint_adapted_profile(baseline, report, *, profile_id, details):
    """Bind a reviewed runtime specification to the engine build in front of us."""
    profile = {key: value for key, value in baseline.items() if key not in _RESERVED}
    fingerprint = report.get('native_fingerprint') if type(report) is dict else None
    if type(fingerprint) is not str or not _SHA.fullmatch(fingerprint):
        _fail('INVALID_NATIVE_FINGERPRINT')
    contract = native_contract(report)
    if contract is None:
        _fail('MISSING_NATIVE_CONTRACT')
    profile['id'] = profile_id
    profile['revision'] = min(int(baseline['revision']) + 1, 2147483647)
    profile['native_fingerprint'] = fingerprint
    profile['native_contract'] = contract
    profile['source'] = {'label': '自动重新适配', 'details': details}
    return validate_profile(profile)


def mint_candidate_profile(baseline, report, observed, *, profile_id, details):
    """Draft a profile for a build whose reviewed script implementations changed.

    The draft is never loaded as a verified profile. Only a maintainer may
    import it, after reviewing which functions changed.
    """
    profile = {key: value for key, value in baseline.items() if key not in _RESERVED}
    fingerprint = report.get('native_fingerprint') if type(report) is dict else None
    if type(fingerprint) is not str or not _SHA.fullmatch(fingerprint):
        _fail('INVALID_NATIVE_FINGERPRINT')
    contract = native_contract(report)
    if contract is None:
        _fail('MISSING_NATIVE_CONTRACT')
    runtime = dict(profile['runtime'])
    functions = dict(runtime['functions'])
    seen = observed.get('functions', {}) if type(observed) is dict else {}
    if type(seen) is not dict:
        _fail('INVALID_OBSERVED_FUNCTIONS')
    for identifier, digest in seen.items():
        if identifier in functions and _SHA.fullmatch(str(digest)):
            functions[identifier] = digest
    runtime['functions'] = functions
    profile['runtime'] = runtime
    profile['id'] = profile_id
    profile['revision'] = min(int(baseline['revision']) + 1, 2147483647)
    profile['native_fingerprint'] = fingerprint
    profile['native_contract'] = contract
    profile['source'] = {'label': '自动适配候选（待维护者复核）', 'details': details}
    return validate_profile(profile)


def _validate_function_details(details, expected, observed):
    """Review evidence for changed implementations. It never grants a match.

    The detail record only says what the new implementation looks like now;
    the fingerprint it carries must still be the observed one, so a report
    can never be used to claim an unreviewed implementation was verified.
    """
    if details is None:
        return
    if type(details) is not dict:
        _fail('PROBE_FUNCTION_DETAILS')
    for identifier, record in details.items():
        if identifier not in expected:
            _fail('PROBE_FUNCTION_DETAILS_ID')
        _keys(record, ('fingerprint', 'argcount', 'flags', 'names', 'varnames',
                       'freevars', 'cellvars', 'consts', 'code_len', 'instructions'),
              'PROBE_FUNCTION_DETAIL')
        _sha(record['fingerprint'], 'PROBE_FUNCTION_DETAIL')
        if record['fingerprint'] != observed.get(identifier):
            _fail('PROBE_FUNCTION_DETAIL_FINGERPRINT')
        if type(record['argcount']) is not int or type(record['flags']) is not int:
            _fail('PROBE_FUNCTION_DETAIL')
        if type(record['code_len']) is not int or record['code_len'] < 0:
            _fail('PROBE_FUNCTION_DETAIL')
        for key in ('names', 'varnames', 'freevars', 'cellvars', 'consts', 'instructions'):
            value = record[key]
            if type(value) is not list or len(value) > 512:
                _fail('PROBE_FUNCTION_DETAIL')
            if any(type(item) is not str or len(item) > 512 for item in value):
                _fail('PROBE_FUNCTION_DETAIL')


def validate_probe_result(result, nonce, pid, profiles):
    """Validate this trusted probe's response envelope and internal consistency.

    This is not a signature or independent proof of game behavior. The host
    must dispatch its fixed probe after native admission and bind process
    identity before and after receiving this nonce-isolated result.
    """
    _keys(result, ('schema', 'nonce', 'pid', 'python_version', 'callback_thread',
                   'main_thread', 'world_type', 'profiles', 'frames', 'done',
                   'read_only', 'model_setters_called'), 'PROBE')
    if (type(result['schema']) is not int or result['schema'] != 1
            or result['nonce'] != nonce or type(result['nonce']) is not str
            or not re.fullmatch('[0-9a-f]{32}', result['nonce'])
            or type(result['pid']) is not int or result['pid'] != pid
            or result['done'] is not True or result['read_only'] is not True
            or type(result['model_setters_called']) is not int or result['model_setters_called'] != 0):
        _fail('PROBE_ENVELOPE_MISMATCH')
    if (type(result['frames']) is not int or not 1 <= result['frames'] <= 3
            or type(result['main_thread']) is not int or result['main_thread'] <= 0
            or type(result['callback_thread']) is not int
            or result['callback_thread'] != result['main_thread']):
        _fail('PROBE_THREAD_OR_FRAME_MISMATCH')
    version = result['python_version']
    if type(version) is not str or not re.fullmatch(r'3\.\d{1,2}', version):
        _fail('PROBE_PYTHON_VERSION_INVALID')
    if result['world_type'] is not None:
        _text(result['world_type'], 160, 'PROBE_WORLD_TYPE')
    candidates = {profile['id']: profile for profile in profiles}
    if len(candidates) != len(profiles):
        _fail('DUPLICATE_CANDIDATE_PROFILE')
    records = result['profiles']
    if type(records) is not list or len(records) != len(candidates):
        _fail('PROBE_PROFILE_COUNT_MISMATCH')
    seen = set()
    statuses = {'SUPPORTED', 'WAITING_FOR_SCENE', 'UNSUPPORTED_INTERFACE', 'IMPLEMENTATION_CHANGED'}
    for record in records:
        _keys(record, ('id', 'diagnostics', 'observed_runtime', 'observed_runtime_fingerprint',
                       'observation_errors'), 'PROBE_PROFILE')
        profile_id = record['id']
        if type(profile_id) is not str or profile_id not in candidates or profile_id in seen:
            _fail('PROBE_PROFILE_ID_MISMATCH')
        seen.add(profile_id)
        expected = candidates[profile_id]['runtime']
        observed = record['observed_runtime']
        _keys(observed, ('schema', 'python_version', 'functions', 'capabilities',
                        'native_interfaces'), 'PROBE_OBSERVED_RUNTIME',
              optional=('volatile_code_flags', 'function_details'))
        if (type(observed['schema']) is not int or observed['schema'] != 1
                or observed['python_version'] != version
                or observed['capabilities'] != expected['capabilities']):
            _fail('PROBE_OBSERVED_RUNTIME_MISMATCH')
        if _validate_volatile_flags(observed, 'PROBE') != expected.get('volatile_code_flags', 0):
            _fail('PROBE_VOLATILE_CODE_FLAGS_MISMATCH')
        functions = observed['functions']
        if type(functions) is not dict or not set(functions).issubset(expected['functions']):
            _fail('PROBE_UNEXPECTED_FUNCTION')
        for value in functions.values():
            _sha(value, 'PROBE_FUNCTION')
        interfaces = observed['native_interfaces']
        if type(interfaces) is not dict or not set(interfaces).issubset(expected['native_interfaces']):
            _fail('PROBE_UNEXPECTED_INTERFACE')
        for descriptor in interfaces.values():
            _keys(descriptor, ('type', 'module', 'name'), 'PROBE_NATIVE_DESCRIPTOR')
            for value in descriptor.values():
                # An observed unknown interface may have no module. It can be
                # reported but can never satisfy the admitted descriptor.
                if value is not None:
                    _text(value, 160, 'PROBE_NATIVE_DESCRIPTOR_VALUE')
        _validate_function_details(observed.get('function_details'), expected['functions'],
                                   observed['functions'])
        _sha(record['observed_runtime_fingerprint'], 'PROBE_RUNTIME_FINGERPRINT')
        if record['observed_runtime_fingerprint'] != _digest(observed):
            _fail('PROBE_OBSERVATION_DIGEST_MISMATCH')
        errors = record['observation_errors']
        if (type(errors) is not dict
                or not set(errors).issubset(set(expected['functions']) | set(expected['native_interfaces']))
                or any(type(value) is not str or len(value) > 1000 for value in errors.values())):
            _fail('PROBE_INVALID_OBSERVATION_ERRORS')
        diagnostics = record['diagnostics']
        if type(diagnostics) is not dict or type(diagnostics.get('capabilities')) is not dict:
            _fail('PROBE_MISSING_CAPABILITY_DIAGNOSTICS')
        capabilities = diagnostics['capabilities']
        _keys(capabilities, ('display', 'training'), 'PROBE_CAPABILITIES')
        for capability, state in capabilities.items():
            if (type(state) is not dict or type(state.get('status')) is not str
                    or state['status'] not in statuses):
                _fail('PROBE_UNKNOWN_CAPABILITY_STATUS')
            if state['status'] == 'SUPPORTED':
                required = expected['capabilities'][capability]
                if (not required or version != expected['python_version']
                        or interfaces != expected['native_interfaces']
                        or any(functions.get(identifier) != expected['functions'][identifier]
                               for identifier in required)
                        or any(identifier in errors for identifier in required)
                        or any(identifier in errors for identifier in expected['native_interfaces'])):
                    _fail('PROBE_SUPPORTED_EVIDENCE_MISMATCH:' + capability)
    return result


def _read_pack_source(source):
    source = Path(source)
    if source.suffix.lower() == '.json':
        return _decode_document(_read_file(source))
    if source.suffix.lower() != '.zip':
        _fail('ONLY_JSON_OR_ZIP_PACKS_SUPPORTED')
    _no_link(source)
    if source.stat().st_size > MAX_BYTES:
        _fail('ZIP_TOO_LARGE')
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            names = set()
            for item in entries:
                name = item.filename
                path = PurePosixPath(name)
                if ('\\' in name or ':' in name or path.is_absolute() or '..' in path.parts
                        or name in names or item.is_dir()
                        or stat.S_ISLNK(item.external_attr >> 16)):
                    _fail('UNSAFE_ZIP_ENTRY:' + name)
                names.add(name)
            if len(entries) != 1 or entries[0].filename != 'pack.json':
                _fail('ZIP_MUST_CONTAIN_ONLY_PACK_JSON')
            item = entries[0]
            if item.file_size > MAX_BYTES or item.flag_bits & 1:
                _fail('ZIP_ENTRY_TOO_LARGE_OR_ENCRYPTED')
            with archive.open(item) as stream:
                raw = stream.read(MAX_BYTES + 1)
            return _decode_document(raw)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise CompatibilityError('INVALID_ZIP') from exc


@contextlib.contextmanager
def _store_lock(store):
    store = Path(store)
    _no_link(store)
    store.mkdir(parents=True, exist_ok=True)
    lock = store / '.import.lock'
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CompatibilityError('PROFILE_STORE_BUSY') from exc
    try:
        os.close(fd)
        yield store
    finally:
        lock.unlink(missing_ok=True)


def _atomic_json(path, value):
    _no_link(path)
    raw = canonical_bytes(value) + b'\n'
    if len(raw) > MAX_BYTES:
        _fail('DOCUMENT_TOO_LARGE')
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def import_profile_pack(source, store, bundled_dir=None):
    """Import explicitly selected local data; a self-hash is not a signature."""
    profiles = _read_pack_source(source)
    with _store_lock(store) as directory:
        existing = load_profiles(bundled_dir, directory)
        reserved = {profile['id'] for profile in existing}
        duplicate = sorted(reserved.intersection(profile['id'] for profile in profiles))
        if duplicate:
            _fail('PROFILE_ID_ALREADY_EXISTS:' + ','.join(duplicate))
        if len(list(directory.glob('*.json'))) >= MAX_STORE_FILES:
            _fail('TOO_MANY_PROFILE_FILES')
        pack = make_profile_pack(profiles)
        destination = directory / ('import-' + pack['sha256'] + '.json')
        if destination.exists():
            _fail('PACK_ALREADY_EXISTS')
        _atomic_json(destination, pack)
    return {'ids': [profile['id'] for profile in profiles], 'path': str(destination.resolve()),
            'source': str(Path(source).resolve()), 'sha256': pack['sha256'],
            'signature_verified': False, 'requires_runtime_probe': True}


def save_profile(profile, store, bundled_dir=None):
    """Write one locally adapted profile into the user store.

    The profile is bound to one engine build; it is data, and every use still
    needs a live probe.
    """
    validate_profile(profile)
    with _store_lock(store) as directory:
        existing = load_profiles(bundled_dir, directory)
        if any(item['id'] == profile['id'] for item in existing):
            _fail('PROFILE_ID_ALREADY_EXISTS:' + profile['id'])
        if len(list(directory.glob('*.json'))) >= MAX_STORE_FILES:
            _fail('TOO_MANY_PROFILE_FILES')
        path = directory / ('local-' + profile['id'] + '.json')
        _atomic_json(path, profile)
    return {'id': profile['id'], 'path': str(path.resolve()), 'revision': profile['revision'],
            'native_fingerprint': profile['native_fingerprint'], 'origin': 'imported',
            'signature_verified': False, 'requires_runtime_probe': True}


def save_candidate(profile, store):
    """Store a drafted profile that is not a verified configuration."""
    validate_profile(profile)
    directory = Path(store)
    _no_link(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (profile['id'] + '.json')
    _atomic_json(path, profile)
    return {'id': profile['id'], 'path': str(path.resolve()), 'revision': profile['revision'],
            'origin': 'candidate', 'requires_review': True, 'requires_runtime_probe': True}


def load_candidates(store):
    """List drafted candidates. They are never returned as verified profiles."""
    result = []
    directory = Path(store)
    _no_link(directory)
    if not directory.exists():
        return result
    for path in sorted(directory.glob('*.json')):
        try:
            document = _loads(_read_file(path))
        except (OSError, CompatibilityError):
            continue
        if type(document) is dict and document.get('schema') == PROFILE_SCHEMA:
            try:
                result.append(dict(document, _content_digest=profile_digest(document),
                                   _origin='candidate', _path=str(path.resolve())))
            except CompatibilityError:
                continue
    return result


def remove_imported_profile(profile_id, store, bundled_dir=None):
    """Remove user data only; bundled profiles are immutable fallback data."""
    with _store_lock(store) as directory:
        profiles = load_profiles(bundled_dir, directory)
        found = [profile for profile in profiles if profile['id'] == profile_id]
        if not found:
            return False
        profile = found[0]
        if profile['_origin'] != 'imported':
            _fail('CANNOT_REMOVE_BUNDLED_PROFILE')
        path = Path(profile['_path'])
        if path.parent != directory.resolve():
            _fail('PROFILE_OUTSIDE_STORE')
        values = [value for value in _decode_document(_read_file(path)) if value['id'] != profile_id]
        if values:
            _atomic_json(path, make_profile_pack(values))
        else:
            path.unlink()
        return True


def cache_key(engine_sha256, native_fingerprint, runtime_fingerprint, profile):
    for name, value in (('ENGINE', engine_sha256), ('NATIVE', native_fingerprint),
                        ('RUNTIME', runtime_fingerprint)):
        _sha(value, name)
    return _digest({'engine_sha256': engine_sha256, 'native_fingerprint': native_fingerprint,
                    'runtime_fingerprint': runtime_fingerprint,
                    'profile_digest': profile_digest(profile)})


def load_cache(store, engine_sha256, native_fingerprint, runtime_fingerprint, profile):
    key = cache_key(engine_sha256, native_fingerprint, runtime_fingerprint, profile)
    path = Path(store) / (key + '.json')
    try:
        record = _loads(_read_file(path))
        if (type(record) is not dict or record.get('schema') != CACHE_SCHEMA
                or record.get('key') != key or record.get('requires_runtime_probe') is not True):
            return None
        return record
    except (OSError, CompatibilityError):
        return None


def save_cache(store, engine_sha256, native_fingerprint, runtime_fingerprint, profile, result):
    if type(result) is not dict:
        _fail('INVALID_CACHE_RESULT')
    key = cache_key(engine_sha256, native_fingerprint, runtime_fingerprint, profile)
    directory = Path(store)
    _no_link(directory)
    directory.mkdir(parents=True, exist_ok=True)
    record = {'schema': CACHE_SCHEMA, 'key': key, 'created_at': time.time(),
              'engine_sha256': engine_sha256, 'native_fingerprint': native_fingerprint,
              'runtime_fingerprint': runtime_fingerprint, 'profile_id': profile['id'],
              'profile_digest': profile_digest(profile), 'requires_runtime_probe': True,
              'result': result}
    _atomic_json(directory / (key + '.json'), record)
    return record


def clear_cache(store):
    directory = Path(store)
    _no_link(directory)
    count = 0
    if not directory.exists():
        return count
    for path in directory.glob('*.json'):
        if not _SHA.fullmatch(path.stem):
            continue
        try:
            record = _loads(_read_file(path))
        except (OSError, CompatibilityError):
            continue
        if type(record) is dict and record.get('schema') == CACHE_SCHEMA and record.get('key') == path.stem:
            path.unlink()
            count += 1
    return count
