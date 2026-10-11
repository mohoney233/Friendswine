"""Offline mock of the observed immediate/deferred TRAINING composition."""
import unittest
from training_core import TrainingController, close, shape, mc_ratio


class Model:
    def __init__(self, uid, native_multiplier=1., registered=1., filename='costume.gim'):
        self.uid, self.filename = uid, filename
        self.multiplier, self.registered = native_multiplier, registered
        self.scale = [native_multiplier*registered]*3
        self.pending = None
        self.valid = True
        self.writes = []
        self.restores = 0
        self.write_fail = self.restore_fail = None
        self.unobservable = False

    def next_frame(self):
        if self.pending is not None:
            self.scale[:] = [component*self.multiplier for component in self.pending]
            self.pending = None


class Adapter:
    def identity(self, model):
        return (model.uid, model.filename)

    def is_valid(self, model):
        return model.valid

    def read_scale(self, model):
        # A released/hidden model reports an unusable vector while its validity
        # flag still reads true; this is the observed production failure.
        if model.unobservable:
            raise ValueError('NONPOSITIVE_OR_NONFINITE_SCALE')
        return model.scale

    def read_scale_optional(self, model):
        try:
            return self.read_scale(model)
        except ValueError:
            return None

    def registered_scale(self, model):
        if model.registered is None:
            raise ValueError('mock official factors not validated')
        return (model.registered,)*3

    def write_scale(self, model, value):
        failure, model.write_fail = model.write_fail, None
        if failure == 'before':
            raise RuntimeError('mock setter failed before write')
        model.scale[:] = value
        model.pending = tuple(value)
        model.writes.append(tuple(value))
        if failure == 'after':
            raise RuntimeError('mock setter failed after write')

    def restore_registered_scale(self, model):
        failure, model.restore_fail = model.restore_fail, None
        if failure == 'before':
            raise RuntimeError('mock official force failed before write')
        model.restores += 1
        model.scale[:] = (model.registered,)*3
        model.pending = tuple(model.scale)
        if failure == 'after':
            raise RuntimeError('mock official force failed after write')


class TrainingTests(unittest.TestCase):
    def make(self, **kwargs):
        return TrainingController(Adapter(), settle_frames=1, **kwargs)

    def test_five_native_multipliers_120_frames_no_squaring(self):
        c = TrainingController(Adapter())
        multipliers = (1.1301775, 1.2318108, 2.0786476, 1., 1.2849090)
        models = [Model(index, scale) for index, scale in enumerate(multipliers)]
        for frame in range(120):
            for model in models:
                model.next_frame()
                if model.writes:
                    self.assertTrue(close(shape(model.scale), shape(mc_ratio(c.elapsed))))
            c.tick(models, frame/60.)
            if frame >= 5:
                self.assertEqual(c.status()['bound'], 5)
                self.assertEqual(c.status()['blocked'], 0)
                for model in models:
                    self.assertTrue(close(model.writes[-1], mc_ratio(c.elapsed)))
                    self.assertEqual(c.targets[(model.uid,model.filename)].baseline, (1.,1.,1.))
        outcomes = c.stop(2.)
        self.assertTrue(all(value == 'RESTORE_PENDING' for value in outcomes.values()))
        self.assertEqual(c.status()['modified'], 5)
        for model in models:
            model.next_frame()
        c.stop(2.02)
        self.assertEqual(c.status()['modified'], 0)
        self.assertEqual(c.status()['pending_restores'], 0)
        for model in models:
            self.assertEqual(tuple(model.scale), (model.multiplier,)*3)

    def test_uniform_sampling_waits_six_frames_and_does_not_capture_native_baseline(self):
        c, model = TrainingController(Adapter()), Model(1,2.,.8)
        for frame in range(5):
            c.tick([model],frame*.01)
        self.assertEqual(model.writes, [])
        c.tick([model],.05)
        item = c.targets[(1,model.filename)]
        self.assertEqual(item.baseline, (.8,)*3)
        self.assertEqual(item.initial_observed_scale, (1.6,)*3)
        self.assertTrue(close(model.writes[-1], tuple(.8*x for x in mc_ratio(.05))))

    def test_nonuniform_loading_never_receives_a_write(self):
        c, model = TrainingController(Adapter()), Model(1,2.)
        model.scale[:] = (2.,3.,2.)
        for frame in range(10):
            c.tick([model],frame*.01)
        self.assertEqual(model.writes, [])
        model.scale[:] = (2.,)*3
        for frame in range(6):
            c.tick([model],1.+frame*.01)
        self.assertTrue(model.writes)

    def test_registered_change_with_composed_own_shape(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.)
        model.next_frame()
        c.tick([model],.25)
        model.next_frame()
        model.registered=.8
        c.tick([model],.25)
        self.assertTrue(close(model.writes[-1], (1.2,.4,.8)))
        self.assertEqual(c.status()['blocked'],0)
        model.next_frame()
        self.assertTrue(close(model.scale,(2.4,.8,1.6)))
        model.registered=1.2
        c.stop(.3)
        model.next_frame()
        c.stop(.31)
        self.assertEqual(tuple(model.scale),(2.4,)*3)
        self.assertEqual(c.status()['modified'],0)

    def test_new_official_uniform_reset_accepted(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.)
        model.next_frame()
        c.tick([model],.25)
        model.registered=.8
        model.scale[:]=(.8,)*3
        model.pending=None
        c.tick([model],.25)
        self.assertEqual(c.status()['blocked'],0)
        self.assertTrue(close(model.writes[-1],(1.2,.4,.8)))

    def test_new_official_reset_after_native_uniform_composition_accepted(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.); model.next_frame()
        c.tick([model],.25); model.next_frame()
        model.registered=.8
        model.scale[:]=(1.6,)*3
        model.pending=None
        c.tick([model],.25)
        self.assertEqual(c.status()['blocked'],0)
        self.assertTrue(close(model.writes[-1],(1.2,.4,.8)))

    def test_changed_official_does_not_authorize_unrelated_nonuniform_shape(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.); model.next_frame()
        c.tick([model],.25); model.next_frame()
        model.registered=.8
        model.scale[:]=(9.,8.,7.)
        model.pending=None
        c.tick([model],.3)
        self.assertEqual(c.status()['unresolved'],1)
        self.assertEqual(c.stop(.3)[(1,model.filename)],'OWNERSHIP_CONFLICT')
        self.assertEqual(model.restores,0)

    def test_pause_requires_later_frame_confirmation_and_freezes_phase(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.)
        model.next_frame()
        c.tick([model],.25)
        self.assertEqual(c.pause(.25)[(1,model.filename)],'RESTORE_PENDING')
        c.tick([model],.25)
        self.assertEqual(c.status()['modified'],1)
        self.assertEqual(c.status()['pending_restores'],1)
        model.next_frame()
        c.tick([model],.27)
        self.assertEqual(c.status()['modified'],0)
        self.assertEqual(tuple(model.scale),(2.,)*3)
        c.resume(10.)
        c.tick([model],10.)
        self.assertEqual(model.writes[-1],(1.5,.5,1.))
        self.assertEqual(c.elapsed,.25)

    def test_unknown_shape_retains_dirty_and_never_forces_restore(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.)
        model.next_frame()
        c.tick([model],.25)
        model.pending=None
        model.scale[:]=(9.,8.,7.)
        writes=len(model.writes)
        c.tick([model],.3)
        self.assertEqual(c.status()['unresolved'],1)
        self.assertEqual(c.stop(.3)[(1,model.filename)],'OWNERSHIP_CONFLICT')
        self.assertEqual(model.restores,0)
        self.assertEqual(len(model.writes),writes)
        self.assertEqual(c.status()['modified'],1)
        self.assertFalse(c.retry_target((1,model.filename)))

    def test_retired_dirty_reference_survives_then_destroy_cleans(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.)
        model.next_frame()
        c.tick([model],.25)
        model.pending=None
        model.scale[:]=(9.,8.,7.)
        c.tick([], .3)
        self.assertEqual(c.status()['pending_restores'],1)
        self.assertEqual(c.status()['modified'],1)
        model.valid=False
        c.tick([], .4)
        self.assertEqual(c.status()['modified'],0)
        self.assertEqual(c.status()['pending_restores'],0)

    def test_costume_replacement_restores_retired_and_binds_new(self):
        c, old, new = self.make(), Model(1,2.,filename='old.gim'), Model(2,1.2,filename='new.gim')
        c.tick([old],0.)
        old.next_frame()
        c.tick([old],.25)
        c.tick([new],.26)
        self.assertEqual(c.status()['pending_restores'],1)
        old.next_frame(); new.next_frame()
        c.tick([new],.27)
        self.assertEqual(tuple(old.scale),(2.,)*3)
        self.assertEqual(c.status()['pending_restores'],0)
        self.assertEqual(c.status()['bound'],1)

    def test_repurposed_old_model_is_not_forced(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.)
        model.filename='new.gim'
        c.tick([], .1)
        self.assertEqual(model.restores,0)
        self.assertEqual(c.status()['modified'],0)

    def test_invalid_official_metadata_never_restores_unknown(self):
        c, model = self.make(), Model(1,2.)
        c.tick([model],0.)
        model.next_frame()
        c.tick([model],.25)
        model.registered=None
        c.tick([model],.3)
        self.assertEqual(c.status()['unresolved'],1)
        c.stop(.3)
        self.assertEqual(model.restores,0)
        self.assertEqual(c.status()['modified'],1)

    def test_direct_readback_mismatch_blocks_without_assuming_native_semantics(self):
        class ImmediateComposition(Adapter):
            def write_scale(self, model, value):
                super().write_scale(model,value)
                model.next_frame()
        c, model = TrainingController(ImmediateComposition(),settle_frames=1), Model(1,2.)
        c.tick([model],0.)
        self.assertEqual(c.counts['WRITE_MISMATCH'],1)
        self.assertEqual(c.status()['blocked'],1)
        self.assertEqual(c.status()['modified'],1)
        model.next_frame()
        c.pause(.1)
        self.assertEqual(c.status()['modified'],0)

    def test_write_error_before_and_after_remains_owned_for_official_recovery(self):
        for failure in ('before','after'):
            with self.subTest(failure=failure):
                c, model=self.make(),Model(1,2.)
                c.tick([model],0.); model.next_frame()
                c.tick([model],.25); model.next_frame()
                model.write_fail=failure
                c.tick([model],.4)
                self.assertEqual(c.status()['pending_restores'],1)
                model.next_frame()
                c.pause(.41)
                self.assertEqual(c.status()['modified'],0)

    def test_force_write_then_error_still_requires_next_frame(self):
        c, model=self.make(),Model(1,2.)
        c.tick([model],0.); model.next_frame()
        c.tick([model],.25)
        model.restore_fail='after'
        self.assertEqual(c.stop(.3)[(1,model.filename)],'RESTORE_FAILED')
        self.assertEqual(c.status()['modified'],1)
        model.next_frame()
        self.assertEqual(c.stop(.31)[(1,model.filename)],'RESTORED')
        self.assertEqual(c.status()['modified'],0)

    def test_changed_registered_while_restore_pending_requires_another_confirmation(self):
        c, model=self.make(),Model(1,2.)
        c.tick([model],0.); model.next_frame()
        c.tick([model],.25)
        c.stop(.3); model.next_frame()
        model.registered=.8
        self.assertEqual(c.stop(.31)[(1,model.filename)],'RESTORE_PENDING')
        self.assertEqual(c.status()['modified'],1)
        model.next_frame()
        c.stop(.32)
        self.assertEqual(tuple(model.scale),(1.6,)*3)
        self.assertEqual(c.status()['modified'],0)

    def test_unobservable_model_stops_being_written_and_is_retired(self):
        c, model = self.make(), Model(1, 2.)
        c.tick([model], 0.)
        writes = len(model.writes)
        model.unobservable = True
        c.tick([model], .01)
        self.assertEqual(model.restores, 1)          # verified official rebuild
        self.assertEqual(len(model.writes), writes)  # never a direct write
        self.assertEqual(c.status()['modified'], 1)
        c.tick([model], .02)
        self.assertEqual(c.status()['modified'], 0)
        self.assertEqual(c.status()['unresolved'], 0)
        self.assertEqual(c.counts['UNOBSERVABLE_RETIRED'], 1)
        self.assertEqual(c.counts['RESTORE_EXCEPTION'], 0)

    def test_unobservable_model_is_retired_after_an_official_rebuild(self):
        c, model = self.make(), Model(1, 2.)
        c.tick([model], 0.); model.next_frame()
        c.tick([model], .25)
        self.assertEqual(c.status()['modified'], 1)
        model.unobservable = True
        self.assertEqual(c.stop(.3)[(1, model.filename)], 'RESTORE_PENDING')
        self.assertEqual(model.restores, 1)
        self.assertEqual(c.status()['modified'], 1)
        self.assertEqual(c.stop(.31)[(1, model.filename)], 'UNOBSERVABLE_RETIRED')
        self.assertEqual(c.status()['modified'], 0)
        self.assertEqual(c.status()['unresolved'], 0)
        self.assertEqual(c.counts['UNOBSERVABLE_RETIRED'], 1)

    def test_official_rebuild_restores_a_model_that_becomes_observable_again(self):
        c, model = self.make(), Model(1, 2.)
        c.tick([model], 0.); model.next_frame()
        c.tick([model], .25)
        model.unobservable = True
        c.stop(.3)
        model.unobservable = False
        model.scale[:] = (1.,)*3   # the official rebuild recomposed the model
        model.pending = None
        self.assertEqual(c.stop(.31)[(1, model.filename)], 'RESTORED')
        self.assertEqual(c.status()['modified'], 0)
        self.assertEqual(c.status()['unresolved'], 0)
        self.assertEqual(c.counts['UNOBSERVABLE_RETIRED'], 0)

    def test_failing_official_rebuild_also_retires_an_unobservable_model(self):
        c, model = self.make(), Model(1, 2.)
        c.tick([model], 0.); model.next_frame()
        c.tick([model], .25)
        model.unobservable = True
        model.restore_fail = 'before'
        self.assertEqual(c.stop(.3)[(1, model.filename)], 'UNOBSERVABLE_RETIRED')
        self.assertEqual(c.status()['modified'], 0)
        self.assertEqual(c.status()['unresolved'], 0)

    def test_observable_ownership_conflict_still_latches_unresolved(self):
        # Regression guard: the retirement above is limited to models the
        # engine no longer exposes; a visible model with an unknown shape is
        # still retained as a dirty, unresolved record.
        c, model = self.make(), Model(1, 2.)
        c.tick([model], 0.); model.next_frame()
        c.tick([model], .25); model.pending = None
        model.scale[:] = (9., 8., 7.)
        c.tick([model], .3)
        self.assertEqual(c.status()['unresolved'], 1)
        self.assertEqual(c.counts['UNOBSERVABLE_RETIRED'], 0)
        self.assertEqual(model.restores, 0)

    def test_backward_clock_and_bounded_diagnostics(self):
        c=self.make(max_events=8)
        c.tick([],10.); c.tick([],10.25); c.tick([],9.); c.tick([],10.25)
        self.assertEqual(c.elapsed,.25)
        for index in range(50):
            c.tick([Model(index)],11.+index)
        self.assertEqual(len(c.events),8)
        self.assertGreater(c.counts['ADDED'],8)


if __name__ == '__main__':
    unittest.main(verbosity=2)
