"""Offline lifecycle tests; no game imports or process access."""
import unittest
from persistent_core import Controller, mc_ratio, scale_close


class Model:
    def __init__(self, uid, filename='costume.gim', scale=(1., 1., 1.)):
        self.uid = uid
        self.filename = filename
        self.scale = list(scale)
        self.valid = True
        self.writes = []
        self.fail_next = None


class Adapter:
    def identity(self, model):
        return (model.uid, model.filename)

    def is_valid(self, model):
        return model.valid

    def read_scale(self, model):
        return model.scale

    def write_scale(self, model, value):
        failure, model.fail_next = model.fail_next, None
        if failure == 'before':
            raise RuntimeError('mock setter failed before write')
        model.scale[:] = value
        model.writes.append(tuple(value))
        if failure == 'after':
            raise RuntimeError('mock setter failed after write')


class AuthoritativeAdapter(Adapter):
    def authoritative_baseline(self, model):
        return model.official


class CoreTests(unittest.TestCase):
    def make(self, **kwargs):
        return Controller(Adapter(), settle_frames=1, **kwargs)

    def test_curve_peaks_and_period(self):
        self.assertEqual(mc_ratio(0), (1., 1., 1.))
        self.assertEqual(mc_ratio(.25), (1.5, .5, 1.))
        self.assertTrue(scale_close(mc_ratio(.25+.91667), (1.5, .5, 1.)))

    def test_multiple_roles_baseline_copy_without_accumulation(self):
        c = self.make()
        a, b = Model(1), Model(2, scale=(2., .8, 3.))
        c.tick([a, b], 10.)
        c.tick([a, b], 10.25)
        self.assertEqual(tuple(a.scale), (1.5, .5, 1.))
        self.assertEqual(tuple(b.scale), (3., .4, 3.))
        baseline = c.targets[(2, b.filename)].baseline
        self.assertEqual(baseline, (2., .8, 3.))
        for _ in range(500):
            c.tick([a, b], 10.25)
        self.assertEqual(tuple(b.scale), (3., .4, 3.))
        self.assertEqual(baseline, (2., .8, 3.))
        c.stop(11.)
        self.assertEqual(tuple(a.scale), (1., 1., 1.))
        self.assertEqual(tuple(b.scale), baseline)

    def test_costume_replacement_restores_old_and_binds_new(self):
        c = self.make()
        old, new = Model(1, 'old.gim'), Model(2, 'new.gim', (1.2, 1.2, 1.2))
        c.tick([old], 0.)
        c.tick([old], .25)
        c.tick([new], .25)
        self.assertEqual(tuple(old.scale), (1., 1., 1.))
        self.assertTrue(scale_close(tuple(new.scale), (1.8, .6, 1.2)))
        self.assertEqual(c.status()['targets'], 1)
        c.stop(.25)
        self.assertEqual(tuple(new.scale), (1.2, 1.2, 1.2))

    def test_destroyed_model_never_written_during_retirement(self):
        c, a = self.make(), Model(1)
        c.tick([a], 0.)
        c.tick([a], .25)
        write_count = len(a.writes)
        a.valid = False
        c.tick([], .3)
        self.assertEqual(len(a.writes), write_count)
        self.assertEqual(c.events[-1]['detail'], 'TARGET_DESTROYED')

    def test_repurposed_handle_identity_prevents_wrong_restore(self):
        c, a = self.make(), Model(1)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.filename = 'replacement.gim'
        a.scale[:] = (2., 2., 2.)
        count = len(a.writes)
        c.tick([], .3)
        self.assertEqual(len(a.writes), count)
        self.assertEqual(tuple(a.scale), (2., 2., 2.))
        self.assertEqual(c.events[-1]['detail'], 'TARGET_REPURPOSED')

    def test_external_change_quarantines_only_affected_target(self):
        c = self.make()
        a, b = Model(1), Model(2)
        c.tick([a, b], 0.)
        c.tick([a, b], .25)
        a.scale[:] = (2., 2., 2.)
        a_count, b_count = len(a.writes), len(b.writes)
        c.tick([a, b], .4)
        self.assertEqual(len(a.writes), a_count)
        self.assertGreater(len(b.writes), b_count)
        self.assertEqual(c.status()['blocked'], 1)
        outcomes = c.stop(.4)
        self.assertEqual(tuple(a.scale), (2., 2., 2.))
        self.assertEqual(tuple(b.scale), (1., 1., 1.))
        self.assertEqual(outcomes[(1, a.filename)], 'OWNERSHIP_CONFLICT')
        self.assertEqual(c.status()['modified'], 1)
        self.assertEqual(c.status()['unresolved'], 1)

    def test_retry_denies_dirty_conflict_instead_of_adopting_residue(self):
        c, a = self.make(), Model(1)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.scale[:] = (2., 2., 2.)
        c.tick([a], .25)
        self.assertFalse(c.retry_target((1, a.filename)))
        c.tick([a], .25)
        self.assertEqual(tuple(a.scale), (2., 2., 2.))
        self.assertEqual(c.stop(.25)[(1, a.filename)], 'OWNERSHIP_CONFLICT')
        self.assertEqual(tuple(a.scale), (2., 2., 2.))
        self.assertEqual(c.status()['unresolved'], 1)

    def test_restore_exact_even_when_close_to_baseline(self):
        c, a = self.make(), Model(1)
        c.tick([a], 0.)
        c.tick([a], .0001)
        self.assertNotEqual(tuple(a.scale), (1., 1., 1.))
        self.assertTrue(scale_close(tuple(a.scale), (1., 1., 1.)))
        self.assertEqual(c.stop(.0001)[(1, a.filename)], 'RESTORED')
        self.assertEqual(tuple(a.scale), (1., 1., 1.))

    def test_float_readback_tolerance_preserves_ownership(self):
        c, a = self.make(), Model(1)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.scale[0] += 1e-6
        c.tick([a], .4)
        self.assertEqual(c.status()['blocked'], 0)
        c.stop(.4)
        self.assertEqual(tuple(a.scale), (1., 1., 1.))

    def test_pause_freezes_phase_and_stop_restores(self):
        c, a = self.make(), Model(1)
        c.tick([a], 10.)
        c.tick([a], 10.25)
        c.pause(10.25)
        self.assertEqual(tuple(a.scale), (1., 1., 1.))
        c.tick([a], 20.)
        self.assertEqual(c.elapsed, .25)
        c.resume(20.)
        c.tick([a], 20.)
        self.assertEqual(tuple(a.scale), (1.5, .5, 1.))
        c.pause(20.)
        c.stop(25.)
        writes = len(a.writes)
        c.tick([a], 26.)
        self.assertEqual(len(a.writes), writes)
        self.assertEqual(tuple(a.scale), (1., 1., 1.))
        with self.assertRaises(RuntimeError):
            c.resume(27.)

    def test_new_model_while_paused_binds_but_does_not_deform(self):
        c, a = self.make(), Model(1)
        c.tick([], 0.)
        c.pause(.25)
        c.tick([a], 1.)
        self.assertEqual(len(a.writes), 0)
        c.resume(1.)
        c.tick([a], 1.)
        self.assertEqual(tuple(a.scale), (1.5, .5, 1.))

    def test_backward_clock_does_not_reverse_or_add_duplicate_phase(self):
        c = self.make()
        c.tick([], 10.)
        c.tick([], 10.25)
        c.tick([], 9.)
        c.tick([], 10.25)
        self.assertEqual(c.elapsed, .25)

    def test_unstable_candidate_never_receives_write(self):
        c = Controller(Adapter(), settle_frames=3)
        a = Model(1)
        c.tick([a], 0.)
        for index in range(10):
            a.scale[:] = (1.+index, 1., 1.)
            c.tick([a], index+.1)
        self.assertEqual(a.writes, [])
        for index in range(3):
            c.tick([a], 20.+index*.01)
        self.assertTrue(a.writes)

    def test_setter_error_before_or_after_write_restores_owned_state(self):
        for failure in ('before', 'after'):
            with self.subTest(failure=failure):
                c, a = self.make(), Model(1)
                c.tick([a], 0.)
                c.tick([a], .25)
                a.fail_next = failure
                c.tick([a], .4)
                self.assertEqual(tuple(a.scale), (1., 1., 1.))
                self.assertEqual(c.status()['blocked'], 1)
                self.assertEqual(c.status()['modified'], 0)

    def test_diagnostics_are_bounded(self):
        c = self.make(max_events=8)
        for index in range(100):
            c.tick([Model(index)], index)
        self.assertEqual(len(c.events), 8)
        self.assertGreater(c.counts['ADDED'], 8)

    def test_failed_retirement_retains_baseline_until_next_frame_retry(self):
        c, a = self.make(), Model(1)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.fail_next = 'before'
        c.tick([], .3)
        self.assertEqual(c.status()['pending_restores'], 1)
        self.assertEqual(tuple(a.scale), (1.5, .5, 1.))
        c.tick([], .4)
        self.assertEqual(c.status()['pending_restores'], 0)
        self.assertEqual(tuple(a.scale), (1., 1., 1.))

    def test_official_change_composes_then_stop_restores_latest_scale(self):
        c = Controller(AuthoritativeAdapter(), settle_frames=1)
        a = Model(1)
        a.official = (1., 1., 1.)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.official = (2., 2., 2.)
        a.scale[:] = a.official  # engine update_model_scale wrote its baseline
        c.tick([a], .25)
        self.assertEqual(tuple(a.scale), (3., 1., 2.))
        c.stop(.25)
        self.assertEqual(tuple(a.scale), (2., 2., 2.))

    def test_official_reset_same_baseline_is_accepted_each_frame(self):
        c = Controller(AuthoritativeAdapter(), settle_frames=1)
        a = Model(1)
        a.official = (1., 1., 1.)
        c.tick([a], 0.)
        for _ in range(10):
            a.scale[:] = a.official
            c.tick([a], .25)
            self.assertEqual(tuple(a.scale), (1.5, .5, 1.))
        self.assertEqual(c.status()['blocked'], 0)

    def test_official_metadata_change_with_our_scale_still_present(self):
        c = Controller(AuthoritativeAdapter(), settle_frames=1)
        a = Model(1)
        a.official = (1., 1., 1.)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.official = (.8, .8, .8)
        c.tick([a], .25)
        self.assertTrue(scale_close(tuple(a.scale), (1.2, .4, .8)))
        c.stop(.25)
        self.assertEqual(tuple(a.scale), (.8, .8, .8))

    def test_stop_refreshes_official_scale_changed_since_last_tick(self):
        c = Controller(AuthoritativeAdapter(), settle_frames=1)
        a = Model(1)
        a.official = (1., 1., 1.)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.official = (2., 2., 2.)
        c.stop(.25)
        self.assertEqual(tuple(a.scale), (2., 2., 2.))

    def test_official_protocol_still_rejects_unknown_external_change(self):
        c = Controller(AuthoritativeAdapter(), settle_frames=1)
        a = Model(1)
        a.official = (1., 1., 1.)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.official = (2., 2., 2.)
        a.scale[:] = (9., 8., 7.)
        writes = len(a.writes)
        c.tick([a], .25)
        self.assertEqual(c.status()['blocked'], 1)
        self.assertEqual(len(a.writes), writes)
        self.assertEqual(c.stop(.25)[(1, a.filename)], 'OWNERSHIP_CONFLICT')
        self.assertEqual(tuple(a.scale), (9., 8., 7.))
        self.assertEqual(c.status()['unresolved'], 1)

    def test_none_official_protocol_preserves_display_behavior(self):
        c = Controller(AuthoritativeAdapter(), settle_frames=1)
        a = Model(1)
        a.official = None
        c.tick([a], 0.)
        c.tick([a], .25)
        c.stop(.25)
        self.assertEqual(tuple(a.scale), (1., 1., 1.))

    def test_official_restore_write_then_error_can_retry_with_new_baseline(self):
        c = Controller(AuthoritativeAdapter(), settle_frames=1)
        a = Model(1)
        a.official = (1., 1., 1.)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.official = (2., 2., 2.)
        a.fail_next = 'after'
        self.assertEqual(c.stop(.25)[(1, a.filename)], 'RESTORE_FAILED')
        a.official = (3., 3., 3.)
        self.assertEqual(c.stop(.25)[(1, a.filename)], 'RESTORED')
        self.assertEqual(tuple(a.scale), (3., 3., 3.))

    def test_retired_conflict_preserves_handle_and_dirty_counts(self):
        c, a = self.make(), Model(1)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.scale[:] = (2., 2., 2.)
        c.tick([], .3)
        self.assertEqual(c.status()['targets'], 0)
        self.assertEqual(c.status()['pending_restores'], 1)
        self.assertEqual(c.status()['modified'], 1)
        self.assertEqual(c.status()['unresolved'], 1)
        writes = len(a.writes)
        self.assertEqual(c.stop(.3)[(1, a.filename)], 'OWNERSHIP_CONFLICT')
        self.assertEqual(len(a.writes), writes)
        a.valid = False
        c.stop(.4)
        self.assertEqual(c.status()['pending_restores'], 0)
        self.assertEqual(c.status()['modified'], 0)
        self.assertEqual(c.status()['unresolved'], 0)

    def test_contested_record_can_clear_after_exact_original_baseline_returns(self):
        c, a = self.make(), Model(1)
        c.tick([a], 0.)
        c.tick([a], .25)
        a.scale[:] = (2., 2., 2.)
        c.tick([a], .3)
        self.assertEqual(c.status()['unresolved'], 1)
        a.scale[:] = (1., 1., 1.)
        self.assertEqual(c.pause(.3)[(1, a.filename)], 'ALREADY_AT_BASELINE')
        self.assertEqual(c.status()['modified'], 0)
        self.assertEqual(c.status()['unresolved'], 0)
        self.assertTrue(c.retry_target((1, a.filename)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
