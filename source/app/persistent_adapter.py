"""Small NeoX render-model adapter. All methods run on the game main thread.

No scheduler, UI navigation, game imports, file IO or gameplay setters. Discovery
is restricted to the visible costume world and ordinary battle unit.model.
"""
import math
import sys
from collections import Counter, deque
from itertools import islice
from types import FunctionType, BuiltinFunctionType, BuiltinMethodType


def _fields(obj):
    try:
        return vars(obj)
    except TypeError:
        return {}


def _plain_field(obj, name, default=None):
    """Allow ordinary instance/class constants without executing descriptors."""
    data = _fields(obj)
    if name in data:
        return data[name]
    for cls in type(obj).__mro__:
        if name in vars(cls):
            value = vars(cls)[name]
            return value if type(value) in (int, float, str, bool, type(None)) else default
    return default


def _part(value):
    return value if type(value) in (int, float, str, bool, type(None)) else ('object', id(value))


def normalize_path(value):
    if not isinstance(value, str):
        return ''
    return '/'.join(part for part in value.replace('\\', '/').lower().split('/') if part)


def _world_detail(world, fields):
    return '%s is_battle=%r is_battle_client=%r' % (
        type(world).__name__, fields.get('is_battle'), fields.get('is_battle_client'))


def _scale(value):
    result = (float(value.x), float(value.y), float(value.z))
    if not all(math.isfinite(component) and component > 0 for component in result):
        raise ValueError('NONPOSITIVE_OR_NONFINITE_SCALE')
    return result


def _close(actual, expected):
    return all(abs(a-b) <= 1e-6 + 1e-5*abs(b) for a, b in zip(actual, expected))


def _positive_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except (OverflowError, ValueError):
        return False


class _Handle:
    __slots__ = ('kind', 'world', 'owner', 'model', 'key', 'official_allowed', 'initial_nonuniform')

    def __init__(self, kind, world, owner, model, key):
        self.kind, self.world, self.owner, self.model, self.key = kind, world, owner, model, key
        self.official_allowed = None
        self.initial_nonuniform = False


class NeoXAdapter:
    def __init__(self, GK, math3d, *, compatibility=None, max_units=512, max_targets=64):
        self.GK, self.math3d = GK, math3d
        self.compatibility = compatibility
        # Keep the verified constructor for ownership recovery; do not follow
        # a subsequently replaced module attribute while restoring old handles.
        self._vector = vars(math3d).get('vector')
        self.max_units = min(max(int(max_units), 1), 512)
        self.max_targets = min(max(int(max_targets), 1), 64)
        self.skip_reasons = deque(maxlen=64)
        self.skip_counts = Counter()
        self.inventory = []
        self.scene_capability = None
        self.scene_world_type = None
        self._handles = {}

    def _skip(self, reason, detail=''):
        self.skip_counts[reason] += 1
        self.skip_reasons.append({'reason': reason, 'detail': str(detail)[:240]})

    def _filename(self, model):
        return normalize_path(model.filename)

    def _model_ok(self, model):
        if model is None:
            return False
        validity = model.valid
        if not self._validity(validity):
            return False
        name = self._filename(model)
        return name.startswith(('chr/player/', 'chr/boss/')) and name.endswith('.gim')

    def identity(self, handle):
        """Live owner metadata plus filename catches a reused model generation."""
        world, owner = handle.world, handle.owner
        wd, od = _fields(world), _fields(owner)
        if handle.kind == 'display':
            generation = (_part(_plain_field(owner, 'ROLE_ID')),
                          _part(wd.get('cloth_id')), _part(wd.get('show_cloth_id')))
        else:
            generation = tuple(_part(od.get(name)) for name in
                               ('uid', 'pid', 'unit_type', 'role_id', 'cloth_id', 'model_path'))
        return (handle.kind, id(world), id(owner), id(handle.model),
                self._filename(handle.model), generation)

    def is_valid(self, handle):
        # A retired model can still be restored after a world/UI closes, provided
        # its live generation is unchanged. No unknown parent/bounding APIs.
        validity = handle.model.valid
        return self._validity(validity)

    @staticmethod
    def _validity(value):
        if type(value) is bool:
            return value
        if type(value) in (BuiltinFunctionType, BuiltinMethodType):
            return bool(value())
        raise ValueError('UNSUPPORTED_MODEL_VALID_INTERFACE')

    def read_scale(self, handle):
        return _scale(handle.model.scale)

    def read_scale_optional(self, handle):
        """Composed scale, or None when the engine no longer exposes one.

        A model that was released or hidden can report a zeroed or garbage
        vector while its validity flag still reads true. That is "not
        observable", not a shape we own: it can neither be confirmed restored
        nor authorized for a write, so the caller must fall back to the
        verified official rebuild instead of latching a permanent error.
        """
        try:
            return self.read_scale(handle)
        except ValueError:
            return None

    def write_scale(self, handle, value):
        copied = tuple(float(component) for component in value)
        if len(copied) != 3 or not all(math.isfinite(x) and x > 0 for x in copied):
            raise ValueError('INVALID_SCALE_WRITE')
        handle.model.scale = self._vector(*copied)

    def _official_vector(self, handle):
        data = _fields(handle.owner)
        factors = data.get('scale_dct')
        last = data.get('last_scale')
        if type(factors) is not dict or len(factors) > 32 or type(last) not in (int, float):
            return None
        if not _positive_number(last):
            return None
        total = 1.
        for value in factors.values():
            if not _positive_number(value):
                return None
            total *= value
            if not math.isfinite(total) or total <= 0:
                return None
        # update_model_scale records this exact Python product in last_scale;
        # a merely nearby value could be an official update not yet applied.
        if total != float(last):
            return None
        # Copy through the actual native vector to preserve float precision for
        # exact restoration; never assign it to a unit/collider/official state.
        try:
            vector = self._vector(total, total, total)
            _scale(vector)
        except (ValueError, OverflowError):
            return None
        return vector

    def _training_registered_context(self, handle, restoring=False):
        """Validate the exact battle owner and verified game rebuild function.

        Registered-scale restore is the same mechanism in every battle world this
        client owns, so matches and the training ground share this one path. What
        gates it is the reviewed function fingerprint below -- not the world type.
        """
        if handle.kind != 'battle':
            return None
        world, owner = handle.world, handle.owner
        wd, data = _fields(world), _fields(owner)
        if wd.get('destroyed') is True or data.get('_destroyed') is True:
            return None
        manager = getattr(self.GK, 'g_world_mgr', None)
        if manager is None or getattr(manager, 'cur_world', None) is not world:
            return None
        units = _fields(wd.get('unit_mgr')).get('units')
        uid = data.get('uid')
        if type(units) is not dict or type(uid) not in (int, str) or units.get(uid) is not owner:
            return None
        if data.get('model') is not handle.model or not self.is_valid(handle):
            return None
        if not (data.get('is_civilian') is True or data.get('is_butcher') is True):
            return None
        if handle.key is None or self.identity(handle) != handle.key:
            return None
        vector = self._official_vector(handle)
        if vector is None or 'update_model_scale' in data:
            return None
        # Inspect the actual first MRO definition, without executing an owner
        # descriptor or accepting an unverified subclass/instance override.
        rebuild = None
        for cls in type(owner).__mro__:
            if 'update_model_scale' in vars(cls):
                rebuild = vars(cls)['update_model_scale']
                break
        if type(rebuild) is not FunctionType:
            return None
        # Source filename/line are diagnostic only. The reviewed implementation
        # fingerprint and Python version determine compatibility.
        if self.compatibility is None or not self.compatibility.function_matches(
                'ClientUnits.BaseUnit:BaseUnit.update_model_scale', rebuild, force=restoring):
            return None
        return vector, rebuild

    def registered_scale(self, handle):
        """Training-only setter factor, distinct from composed model.scale."""
        context = self._training_registered_context(handle)
        return _scale(context[0]) if context is not None else None

    def restore_registered_scale(self, handle):
        """Ask the verified game function to rebuild its own registered factor.

        Caller must first establish ownership of the current shape. This method
        never writes scale_dct, last_scale, collision state, or monitor code.
        Immediate scale is recorded; native composition finishes next frame.
        """
        context = self._training_registered_context(handle, restoring=True)
        if context is None:
            return None
        vector, rebuild = context
        registered = _scale(vector)
        rebuild(handle.owner, force=True)
        return {'method': 'BaseUnit.update_model_scale(force=True)',
                'file': rebuild.__code__.co_filename,
                'line': rebuild.__code__.co_firstlineno,
                'world_id': id(handle.world), 'owner_id': id(handle.owner),
                'model_id': id(handle.model), 'registered_scale': registered,
                'immediate_scale': self.read_scale(handle)}

    def authoritative_baseline(self, handle):
        if handle.kind != 'battle':
            return None
        vector = self._official_vector(handle)
        if vector is None:
            return None
        if handle.official_allowed is not True:
            # A first nonuniform value is preserved. An official uniform reset
            # may become authoritative only after it is actually observed on
            # this model and agrees with the validated official factor product.
            current = self.read_scale(handle)
            if not _close(current, _scale(vector)):
                return None
            handle.official_allowed = True
        return vector

    def _bind(self, kind, world, owner, model):
        transient = _Handle(kind, world, owner, model, None)
        key = self.identity(transient)
        handle = self._handles.get(key)
        if handle is None:
            if self.compatibility is None or not self.compatibility.check(
                    'display' if kind == 'display' else 'training', force=True):
                raise RuntimeError('CAPABILITY_NOT_VERIFIED')
            handle = transient
            handle.key = key
            initial = self.read_scale(handle)
            if kind == 'battle' and not _close(initial, (initial[0],)*3):
                handle.official_allowed = False
                handle.initial_nonuniform = True
                self._skip('INITIAL_NONUNIFORM_OFFICIAL_UNCONFIRMED', key)
            self._handles[key] = handle
        return handle

    def _display(self, world):
        gui = getattr(self.GK, 'g_ui_mgr', None)
        registry = _fields(gui).get('ui_dict')
        ui = registry.get('UIArticleRole') if isinstance(registry, dict) else None
        if type(ui).__name__ != 'UIArticleRole' or _plain_field(ui, '_logic_visible') is not True:
            self._skip('DISPLAY_NOT_VISIBLE')
            return []
        ud, wd = _fields(ui), _fields(world)
        loaded_module = sys.modules.get('ui.UIArticleRole')
        keys = getattr(loaded_module, 'ComuKeys', None) if loaded_module is not None else None
        article = getattr(keys, 'ARTICLE_TYPE', None) if keys is not None else None
        cloth_kind = getattr(article, 'CLOTH', None) if article is not None else None
        gift_kind = getattr(article, 'GIFT_BAG', None) if article is not None else None
        if cloth_kind is None or _plain_field(ui, 'cur_article_type') != cloth_kind:
            self._skip('DISPLAY_NOT_COSTUME_UI')
            return []
        if gift_kind is not None and _plain_field(ui, 'shop_type') == gift_kind:
            self._skip('DISPLAY_GIFT_BAG_NOT_ADAPTED')
            return []
        if ud.get('_owner_world') is not world:
            self._skip('DISPLAY_WORLD_MISMATCH')
            return []
        selected = ud.get('selected_article_id')
        loaded, shown = wd.get('cloth_id'), wd.get('show_cloth_id')
        if type(selected) is not int or selected <= 0 or selected != loaded or selected != shown:
            self._skip('DISPLAY_COSTUME_LOADING_OR_ALIAS', (selected, loaded, shown))
            return []
        role = _plain_field(ui, 'ROLE_ID')
        if type(role) is not int or role <= 0:
            self._skip('DISPLAY_ROLE_UNKNOWN')
            return []
        model = wd.get('player_model')
        if not self._model_ok(model):
            self._skip('DISPLAY_MODEL_UNAVAILABLE')
            return []
        return [self._bind('display', world, ui, model)]

    def _battle(self, world):
        wd = _fields(world)
        manager = wd.get('unit_mgr')
        units = _fields(manager).get('units')
        if type(units) is not dict:
            self._skip('BATTLE_UNITS_NOT_PLAIN_DICT', type(units).__name__)
            return []
        handles = []
        for uid, unit in list(islice(units.items(), self.max_units)):
            data = _fields(unit)
            # Instance booleans only; no property/callable role guesses.
            if data.get('_destroyed') is True or not (data.get('is_civilian') is True or data.get('is_butcher') is True):
                continue
            if any(cls.__name__ == 'UgcLogicUnit' for cls in type(unit).__mro__):
                self._skip('UGC_LOGIC_UNIT_EXCLUDED', uid)
                continue
            entry = {'uid': _part(uid), 'unit_id': id(unit), 'type': type(unit).__name__,
                     'is_civilian': data.get('is_civilian') is True,
                     'is_butcher': data.get('is_butcher') is True,
                     'cloth_id': _part(data.get('cloth_id'))}
            self.inventory.append(entry)
            if len(handles) >= self.max_targets:
                self._skip('BATTLE_TARGET_LIMIT', self.max_targets)
                break
            model = data.get('model')
            try:
                if not self._model_ok(model):
                    self._skip('BATTLE_MODEL_UNAVAILABLE', uid)
                    continue
                handle = self._bind('battle', world, unit, model)
                entry['model_id'] = id(model)
                entry['filename'] = self._filename(model)
                handles.append(handle)
            except Exception as error:
                self._skip('BATTLE_TARGET_READ_ERROR', type(error).__name__ + ': ' + str(error))
                if str(error) == 'UNSUPPORTED_MODEL_VALID_INTERFACE' and self.compatibility is not None:
                    self.compatibility.reject_interface('training', str(error))
                    return []
        if len(units) > self.max_units:
            self._skip('BATTLE_UNIT_SCAN_LIMIT', len(units))
        return handles

    def discover(self):
        """A scene transition is an empty snapshot, never a permanent failure."""
        self.inventory = []
        self.scene_capability = None
        handles = []
        try:
            manager = getattr(self.GK, 'g_world_mgr', None)
            world = getattr(manager, 'cur_world', None) if manager is not None else None
            wd = _fields(world)
            self.scene_world_type = type(world).__name__ if world is not None else None
            if world is None or wd.get('destroyed') is True:
                self._skip('WORLD_UNAVAILABLE')
            elif type(world).__name__ == 'WorldArticle':
                self.scene_capability = 'display'
                if self.compatibility is not None and self.compatibility.check('display'):
                    handles = self._display(world)
                else:
                    self._skip('DISPLAY_CAPABILITY_NOT_VERIFIED')
            elif type(world).__name__.startswith('WorldBattle'):
                # Eligibility is the game's own battle-world family, never one mode's
                # class name: a real match is WorldBattle with is_battle_client unset,
                # the training ground is WorldBattleTrain. Keying on the family is what
                # makes matches work at all. Every unit is still filtered by _battle and
                # the reviewed script fingerprints below still gate every write.
                if self.compatibility is not None and self.compatibility.check('training'):
                    self.scene_capability = 'training'
                    handles = self._battle(world)
                else:
                    self.scene_capability = 'training'
                    self._skip('TRAINING_CAPABILITY_NOT_VERIFIED')
            else:
                self._skip('WORLD_NOT_ADAPTED', _world_detail(world, wd))
        except Exception as error:
            self._skip('DISCOVERY_READ_ERROR', type(error).__name__ + ': ' + str(error))
            if (str(error) == 'UNSUPPORTED_MODEL_VALID_INTERFACE' and self.compatibility is not None
                    and self.scene_capability is not None):
                self.compatibility.reject_interface(self.scene_capability, str(error))
        # Controller owns retired handles until restoration; adapter need not
        # retain closed worlds or unbounded generations in its discovery cache.
        current = {handle.key: handle for handle in handles}
        self._handles = current
        self.inventory = self.inventory[:self.max_targets]
        return handles

    def metadata(self, handle):
        data = _fields(handle.owner)
        result = {'kind': handle.kind, 'world_type': type(handle.world).__name__,
                  'world_id': id(handle.world), 'owner_id': id(handle.owner),
                  'model_id': id(handle.model), 'filename': self._filename(handle.model),
                  'identity': repr(self.identity(handle))[:512]}
        if handle.kind == 'display':
            result.update(role_id=_part(_plain_field(handle.owner, 'ROLE_ID')),
                          cloth_id=_part(_fields(handle.world).get('cloth_id')))
        else:
            result.update(uid=_part(data.get('uid')), cloth_id=_part(data.get('cloth_id')),
                          official_baseline_confirmed=handle.official_allowed is True,
                          initial_nonuniform=handle.initial_nonuniform)
        return result

    def diagnostics(self):
        return {'skip_counts': dict(self.skip_counts), 'recent_skips': list(self.skip_reasons),
                'inventory': list(self.inventory), 'scene_capability': self.scene_capability,
                'world_type': self.scene_world_type,
                'compatibility': self.compatibility.diagnostics() if self.compatibility else
                {'capabilities': {'display': {'status': 'UNSUPPORTED_INTERFACE'},
                                  'training': {'status': 'UNSUPPORTED_INTERFACE'}}}}
