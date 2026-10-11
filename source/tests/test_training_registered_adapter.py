"""Offline authority-boundary tests; no game imports or process access."""
import math
import unittest
from types import SimpleNamespace
from persistent_adapter import NeoXAdapter
from runtime_compat import RuntimeCompatibility
from test_runtime_compat import make_profile
from training_core import TrainingController


def vector(x, y, z):
    return SimpleNamespace(x=x, y=y, z=z)


class BaseUnit:
    def update_model_scale(self, force=False):
        self.calls.append(force)
        self.model.scale = vector(self.last_scale, self.last_scale, self.last_scale)


# Location metadata is deliberately not an authority boundary.
BaseUnit.update_model_scale.__code__ = BaseUnit.update_model_scale.__code__.replace(
    co_filename='ClientUnits\\BaseUnit.py', co_firstlineno=621)


class CivilianUnit(BaseUnit):
    pass


class WorldBattleTrain:
    pass


class WorldBattle:
    pass


class RegisteredAdapterTests(unittest.TestCase):
    def fixture(self):
        model = SimpleNamespace(valid=True, filename='chr/player/doctor.gim',
                                scale=vector(1.25, 1.25, 1.25))
        unit = CivilianUnit()
        unit.uid, unit.pid, unit.unit_type = 123, 14, 1
        unit.is_civilian, unit.is_butcher = True, False
        unit.model, unit.scale_dct, unit.last_scale = model, {}, 1.
        unit.calls = []
        world = WorldBattleTrain()
        world.unit_mgr = SimpleNamespace(units={unit.uid: unit})
        GK = SimpleNamespace(g_world_mgr=SimpleNamespace(cur_world=world))
        profile, modules = make_profile(BaseUnit.update_model_scale)
        modules['ClientUnits.BaseUnit'].BaseUnit = BaseUnit
        compatibility = RuntimeCompatibility(profile, modules=modules)
        adapter = NeoXAdapter(GK, SimpleNamespace(vector=vector), compatibility=compatibility)
        handle = adapter._bind('battle', world, unit, model)
        return adapter, handle, world, unit

    def reject(self, adapter, handle, unit):
        self.assertIsNone(adapter.registered_scale(handle))
        self.assertIsNone(adapter.restore_registered_scale(handle))
        self.assertEqual(unit.calls, [])

    def test_composed_observation_is_not_registered_setter_factor(self):
        adapter, handle, world, unit = self.fixture()
        self.assertEqual(adapter.read_scale(handle), (1.25,)*3)
        self.assertEqual(adapter.registered_scale(handle), (1.,)*3)
        unit.scale_dct, unit.last_scale = {'skill': 1.5, 'other': 2.}, 3.
        self.assertEqual(adapter.registered_scale(handle), (3.,)*3)
        record = adapter.restore_registered_scale(handle)
        self.assertEqual(unit.calls, [True])
        self.assertEqual(record['registered_scale'], (3.,)*3)
        self.assertEqual(record['immediate_scale'], (3.,)*3)
        self.assertEqual(record['line'], 621)
        self.assertEqual(unit.scale_dct, {'skill': 1.5, 'other': 2.})
        self.assertEqual(unit.last_scale, 3.)

    def test_match_world_uses_the_same_registered_path(self):
        """A match world is eligible; the reviewed function fingerprint is the gate."""
        adapter, handle, world, unit = self.fixture()
        public = WorldBattle()
        public.unit_mgr = world.unit_mgr
        handle.world = public
        handle.key = adapter.identity(handle)
        adapter.GK.g_world_mgr.cur_world = public
        self.assertEqual(adapter.registered_scale(handle), (1.,)*3)
        record = adapter.restore_registered_scale(handle)
        self.assertEqual(unit.calls, [True])
        self.assertEqual(record['registered_scale'], (1.,)*3)
        self.assertEqual(record['world_id'], id(public))

    def test_discover_accepts_a_real_match_world_without_the_client_flag(self):
        """Observed live: a real match is WorldBattle with is_battle_client unset."""
        adapter, handle, world, unit = self.fixture()
        public = WorldBattle()
        public.unit_mgr = world.unit_mgr
        adapter.GK.g_world_mgr.cur_world = public
        handles = adapter.discover()
        self.assertEqual([item.kind for item in handles], ['battle'])
        diagnostics = adapter.diagnostics()
        self.assertEqual(diagnostics['scene_capability'], 'training')
        self.assertEqual(diagnostics['skip_counts'], {})
        self.assertEqual(diagnostics['world_type'], 'WorldBattle')

    def test_discover_still_rejects_worlds_outside_the_battle_family(self):
        adapter, handle, world, unit = self.fixture()

        class WorldCityHall:
            pass

        hall = WorldCityHall()
        hall.unit_mgr = world.unit_mgr
        adapter.GK.g_world_mgr.cur_world = hall
        self.assertEqual(adapter.discover(), [])
        diagnostics = adapter.diagnostics()
        self.assertEqual(diagnostics['skip_counts'], {'WORLD_NOT_ADAPTED': 1})
        self.assertIsNone(diagnostics['scene_capability'])
        self.assertEqual(diagnostics['world_type'], 'WorldCityHall')

    def test_departed_world_rejected(self):
        adapter, handle, world, unit = self.fixture()
        adapter.GK.g_world_mgr.cur_world = WorldBattleTrain()
        self.reject(adapter, handle, unit)

    def test_owner_removed_from_registry_rejected(self):
        adapter, handle, world, unit = self.fixture()
        world.unit_mgr.units[unit.uid] = object()
        self.reject(adapter, handle, unit)

    def test_replaced_owner_model_rejected(self):
        adapter, handle, world, unit = self.fixture()
        unit.model = SimpleNamespace(valid=True, filename='chr/player/doctor.gim',
                                     scale=vector(1., 1., 1.))
        self.reject(adapter, handle, unit)

    def test_same_model_new_generation_rejected(self):
        adapter, handle, world, unit = self.fixture()
        unit.cloth_id = 99
        self.reject(adapter, handle, unit)

    def test_unverified_instance_override_never_called(self):
        adapter, handle, world, unit = self.fixture()
        unit.update_model_scale = lambda **kwargs: self.fail('unknown override invoked')
        self.reject(adapter, handle, unit)

    def test_unverified_class_override_never_called(self):
        adapter, handle, world, unit = self.fixture()
        class ReplacementUnit(CivilianUnit):
            def update_model_scale(self, force=False):
                raise AssertionError('unknown class override invoked')
        unit.__class__ = ReplacementUnit
        self.reject(adapter, handle, unit)

    def test_invalid_and_nearby_factor_registration_rejected(self):
        for factor, last in ((True, 1.), (0., 1.), (-1., 1.),
                             (math.nan, 1.), (math.inf, 1.),
                             (1., 1. + 1e-12), (1., True)):
            with self.subTest(factor=factor, last=last):
                adapter, handle, world, unit = self.fixture()
                unit.scale_dct, unit.last_scale = {'x': factor}, last
                self.reject(adapter, handle, unit)

    def test_destroyed_model_rejected(self):
        adapter, handle, world, unit = self.fixture()
        unit.model.valid = False
        self.reject(adapter, handle, unit)

    def test_unknown_validity_callback_is_not_invoked(self):
        adapter, handle, world, unit = self.fixture()
        unit.model.valid = lambda: self.fail('unknown callback invoked')
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_MODEL_VALID_INTERFACE'):
            adapter.is_valid(handle)

    def test_unknown_model_validity_is_reported_as_interface_failure(self):
        adapter, handle, world, unit = self.fixture()
        world.is_battle = True
        unit.model.valid = lambda: self.fail('unknown callback invoked')
        self.assertEqual(adapter.discover(), [])
        training = adapter.diagnostics()['compatibility']['capabilities']['training']
        self.assertEqual(training['status'], 'UNSUPPORTED_INTERFACE')
        self.assertEqual(training['reason'], 'UNSUPPORTED_MODEL_VALID_INTERFACE')

    def test_restore_survives_source_line_shift(self):
        adapter, handle, world, unit = self.fixture()
        function = BaseUnit.update_model_scale
        original = function.__code__
        try:
            function.__code__ = original.replace(co_filename='moved/BaseUnit.py', co_firstlineno=999)
            result = adapter.restore_registered_scale(handle)
            self.assertEqual(result['line'], 999)
            self.assertEqual(unit.calls, [True])
        finally:
            function.__code__ = original

    def test_hot_changed_restore_keeps_dirty_target_without_call_or_reentry(self):
        adapter, handle, world, unit = self.fixture()
        controller = TrainingController(adapter, settle_frames=1)
        controller.tick([handle], 0.)
        controller.tick([handle], .25)
        self.assertEqual(controller.status()['modified'], 1)
        original = BaseUnit.update_model_scale
        def changed(self, force=False):
            raise AssertionError('unverified function must never be called')
        try:
            BaseUnit.update_model_scale = changed
            self.assertFalse(adapter.compatibility.check('training'))
            controller.tick([], .3)
            status = controller.status()
            self.assertEqual(status['modified'], 1)
            self.assertEqual(status['unresolved'], 1)
            self.assertEqual(status['pending_restores'], 1)
            self.assertEqual(unit.calls, [])
            BaseUnit.update_model_scale = original
            # The old verified restore can recover its owned shape; the
            # capability remains latched and no new model can be bound.
            controller.tick([], .4)
            controller.tick([], .5)
            self.assertEqual(controller.status()['modified'], 0)
            self.assertFalse(adapter.compatibility.check('training'))
            self.assertEqual(unit.calls, [True])
        finally:
            BaseUnit.update_model_scale = original


    def test_unusable_model_scale_is_reported_as_unobservable(self):
        adapter, handle, world, unit = self.fixture()
        self.assertEqual(adapter.read_scale_optional(handle), (1.25, 1.25, 1.25))
        for broken in (vector(0., 0., 0.), vector(-1., 1., 1.),
                       vector(float('nan'), 1., 1.), vector(float('inf'), 1., 1.)):
            unit.model.scale = broken
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    adapter.read_scale(handle)
                self.assertIsNone(adapter.read_scale_optional(handle))

    def test_unobservable_model_never_blocks_a_later_session(self):
        adapter, handle, world, unit = self.fixture()
        controller = TrainingController(adapter, settle_frames=1)
        controller.tick([handle], 0.)
        controller.tick([handle], .25)
        self.assertEqual(controller.status()['modified'], 1)
        unit.model.scale = vector(0., 0., 0.)
        controller.stop(.3)
        controller.stop(.31)
        status = controller.status()
        self.assertEqual(status['modified'], 0)
        self.assertEqual(status['unresolved'], 0)
        self.assertEqual(unit.calls, [True])   # verified official rebuild only


if __name__ == '__main__':
    unittest.main()
