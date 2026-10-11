"""Pure main-thread controller for the verified TRAINING scale composition.

Discovery/scene restrictions belong to the adapter. Required adapter methods:
identity, is_valid, read_scale, write_scale, registered_scale, and
restore_registered_scale. registered_scale returns the verified latest official
uniform vector in native precision. restore_registered_scale performs the
verified official forced visual-scale update; this core supplies no game API.
Never use this module as evidence of ordinary/public battle compatibility.
"""
import math
import time
from collections import Counter, deque


def vector(value):
    if all(hasattr(value, axis) for axis in ('x', 'y', 'z')):
        value = (value.x, value.y, value.z)
    result = tuple(float(component) for component in value)
    if len(result) != 3 or not all(math.isfinite(x) and x > 0. for x in result):
        raise ValueError('INVALID_POSITIVE_SCALE')
    return result


def close(actual, expected):
    return all(abs(a-b) <= 1e-6 + 1e-5*abs(b) for a, b in zip(actual, expected))


def uniform(value):
    return close((value[0]/value[2], value[1]/value[2], 1.), (1., 1., 1.))


def shape(value):
    return (value[0]/value[2], value[1]/value[2], 1.)


def mc_ratio(elapsed):
    keys, levels = (0., .25, .45833, .70833, .91667), (0., 1., 0., 1., 0.)
    phase = max(float(elapsed), 0.) % keys[-1]
    for index in range(4):
        if phase <= keys[index+1]:
            u = (phase-keys[index])/(keys[index+1]-keys[index])
            q = levels[index] + (levels[index+1]-levels[index])*u*u*(3.-2.*u)
            return (1.+.5*q, 1.-.5*q, 1.)
    return (1., 1., 1.)


class _Target:
    def __init__(self, handle, token):
        self.handle, self.token = handle, token
        self.baseline = None  # latest REGISTERED uniform, never observed scale
        self.initial_observed_scale = None
        self.last_observed_scale = None
        self.observed_expected_ratio = None
        self.candidate = None
        self.stable_frames = 0
        self.last_ratio = (1., 1., 1.)
        self.previous_ratio = None
        self.last_written = None
        self.modified = False
        self.unresolved = False
        self.blocked = False
        self.reason = ''
        self.restore_pending = False
        self.restore_at = None
        self.restore_registered = None
        self.restore_attempts = 0


class TrainingController:
    """One serialized engine-main-thread tick per actual frame.

    A pause/stop's official forced restoration is not complete until a later
    call with a strictly later monotonic timestamp confirms uniform scale.
    tick(), pause(), and stop() can poll this pending confirmation. Unknown
    shape changes retain dirty references and never authorize forced updates.
    A model whose composed scale is no longer observable is retired after the
    verified official rebuild rather than latching a dirty record that no
    later frame could ever clear.
    """
    def __init__(self, adapter, *, clock=time.monotonic, settle_frames=6,
                 max_events=128, max_restore_attempts=3, ratio=mc_ratio):
        if settle_frames < 1 or max_events < 1 or max_restore_attempts < 1:
            raise ValueError('controller limits must be positive')
        self.adapter, self.clock, self.ratio = adapter, clock, ratio
        self.settle_frames = settle_frames
        self.max_restore_attempts = max_restore_attempts
        self.targets, self.pending_restores = {}, {}
        self.events, self.counts = deque(maxlen=max_events), Counter()
        self.mode, self.elapsed, self._now = 'RUNNING', 0., None

    def _event(self, kind, token=None, detail=''):
        self.counts[kind] += 1
        self.events.append({'kind': kind, 'identity': repr(token)[:512],
                            'detail': str(detail)[:512]})

    def _advance(self, now=None):
        observed = float(self.clock() if now is None else now)
        if not math.isfinite(observed):
            raise ValueError('INVALID_MONOTONIC_CLOCK')
        if self._now is None:
            self._now = observed
        else:
            new = max(observed, self._now)
            if self.mode == 'RUNNING':
                self.elapsed += new-self._now
            self._now = new

    def _registered(self, item):
        official = vector(self.adapter.registered_scale(item.handle))
        if official[0] != official[1] or official[1] != official[2]:
            raise ValueError('REGISTERED_SCALE_NOT_STRICTLY_UNIFORM')
        return official

    def _valid_generation(self, item):
        return bool(self.adapter.is_valid(item.handle)) and self.adapter.identity(item.handle) == item.token

    def _observed(self, item):
        """Composed scale, or None when this model is no longer observable."""
        reader = getattr(self.adapter, 'read_scale_optional', None)
        try:
            value = self.adapter.read_scale(item.handle) if reader is None else reader(item.handle)
            return None if value is None else vector(value)
        except ValueError:
            return None

    def _official_factor(self, item):
        """Verified official uniform scale, or None when the game offers none."""
        try:
            return self._registered(item)
        except Exception:
            return None

    def _owns_shape(self, item, current):
        current_shape = shape(current)
        return close(current_shape, shape(item.last_ratio)) or (
            item.previous_ratio is not None and close(current_shape, shape(item.previous_ratio)))

    def _accept_current(self, item, current, official):
        if self._owns_shape(item, current) or close(current, official):
            return True
        # A fully validated official factor change can be rendered through the
        # native uniform multiplier before our next callback. Its uniform shape
        # is expected; unrelated nonuniform shapes remain unauthorized.
        return item.baseline is not None and official != item.baseline and uniform(current)

    def _block(self, item, reason, detail=''):
        item.blocked, item.reason = True, reason
        if item.modified:
            item.unresolved = True
        self._event(reason, item.token, detail)

    def _clean(self, item):
        item.modified = item.unresolved = item.restore_pending = False
        item.restore_at = item.restore_registered = None
        item.previous_ratio = None
        item.restore_attempts = 0

    def _force_restore(self, item, official):
        if item.restore_attempts >= self.max_restore_attempts:
            item.unresolved = item.modified
            return 'RESTORE_FAILED'
        item.restore_attempts += 1
        # The official updater may write then raise: keep the dirty record.
        if official is not None:
            item.baseline = official
            item.restore_registered = official
        item.restore_pending = True
        item.restore_at = self._now
        self.adapter.restore_registered_scale(item.handle)
        self._event('RESTORE_REQUESTED', item.token, official)
        return 'RESTORE_PENDING'

    def _restore_unobservable(self, item):
        """Retire a model the engine no longer exposes, after an official reset.

        Such a model cannot be confirmed restored by reading and must never be
        written. The verified official rebuild is asked to reset its own
        registered factor; the record is then retired instead of latching
        `unresolved` forever, which would block every later start inside the
        same game process. Retirement is recorded as an event and never as a
        confirmed restoration -- it is a statement that there is nothing left
        for this controller to do, not a claim that the model is intact.
        """
        if item.restore_pending:
            if self._now <= item.restore_at:
                return 'RESTORE_PENDING'
        else:
            self._event('TARGET_UNOBSERVABLE', item.token, item.last_observed_scale)
            try:
                if self._force_restore(item, self._official_factor(item)) == 'RESTORE_PENDING':
                    return 'RESTORE_PENDING'
            except Exception as error:
                # Nothing else can reach this model, so a failing rebuild also
                # retires it instead of blocking later sessions.
                self._event('UNOBSERVABLE_REBUILD_FAILED', item.token, error)
        # The verified rebuild was asked to reset this model; whether it landed
        # cannot be read back. Judge any later observation against the official
        # shape, record the retirement, and stop tracking this model.
        item.last_ratio = (1., 1., 1.)
        self._event('UNOBSERVABLE_RETIRED', item.token, item.restore_registered)
        self._clean(item)
        return 'UNOBSERVABLE_RETIRED'

    def _restore(self, item):
        if not item.modified:
            return 'NO_WRITE'
        try:
            if not bool(self.adapter.is_valid(item.handle)):
                self._clean(item)
                return 'TARGET_DESTROYED'
            if self.adapter.identity(item.handle) != item.token:
                self._clean(item)
                return 'TARGET_REPURPOSED'
            current = self._observed(item)
            if current is None:
                return self._restore_unobservable(item)
            official = self._registered(item)
            if item.restore_pending:
                if self._now <= item.restore_at:
                    return 'RESTORE_PENDING'
                if uniform(current):
                    if official != item.restore_registered:
                        # Reconfirm against a force update using the new factor.
                        return self._force_restore(item, official)
                    item.baseline = official
                    item.last_ratio = (1., 1., 1.)
                    item.last_written = current
                    self._clean(item)
                    self._event('RESTORED', item.token, current)
                    return 'RESTORED'
                if not self._owns_shape(item, current):
                    self._block(item, 'OWNERSHIP_CONFLICT', current)
                    return 'OWNERSHIP_CONFLICT'
                return self._force_restore(item, official)
            if not self._accept_current(item, current, official):
                self._block(item, 'OWNERSHIP_CONFLICT', current)
                return 'OWNERSHIP_CONFLICT'
            return self._force_restore(item, official)
        except Exception as error:
            item.unresolved = item.modified
            self._event('RESTORE_EXCEPTION', item.token, error)
            return 'RESTORE_FAILED'

    def _pump_retired(self):
        outcomes = {}
        for token, item in tuple(self.pending_restores.items()):
            result = self._restore(item)
            outcomes[token] = result
            if not item.modified:
                del self.pending_restores[token]
                self._event('RETIRED_RECOVERY', token, result)
        return outcomes

    def _step(self, item, ratio):
        if item.restore_pending:
            if self._restore(item) != 'RESTORED':
                return
        if item.blocked:
            return
        try:
            if not self._valid_generation(item):
                self._block(item, 'INVALID_OR_REPURPOSED')
                return
            current = self._observed(item)
            if current is None:
                # Not observable: never write, and restore if we ever did.
                if item.modified:
                    self._restore(item)
                return
            official = self._registered(item)
            item.last_observed_scale = current
            item.observed_expected_ratio = item.last_ratio
            if item.baseline is None:
                if not uniform(current):
                    item.candidate, item.stable_frames = None, 0
                    item.reason = 'WAITING_FOR_UNIFORM'
                    return
                if item.candidate is not None and close(current, item.candidate):
                    item.stable_frames += 1
                else:
                    item.candidate, item.stable_frames = current, 1
                if item.stable_frames < self.settle_frames:
                    return
                item.baseline = official
                item.initial_observed_scale = current
                item.last_written = current
                item.reason = ''
                self._event('BOUND', item.token, {'registered': official, 'observed': current})
            if not self._accept_current(item, current, official):
                self._block(item, 'OWNERSHIP_CONFLICT', current)
                return
            if official != item.baseline:
                item.baseline = official
                self._event('BASELINE_UPDATED', item.token, official)
            if self.mode != 'RUNNING':
                return
            expected = tuple(factor*component for factor, component in zip(official, ratio))
            item.previous_ratio, item.last_ratio = item.last_ratio, ratio
            item.last_written, item.modified = expected, True
            self.adapter.write_scale(item.handle, expected)
            actual = self._observed(item)
            if actual is None:
                self._restore(item)
                return
            if not close(actual, expected):
                self._block(item, 'WRITE_MISMATCH', actual)
                self._restore(item)
                return
            item.last_written, item.previous_ratio = actual, None
        except Exception as error:
            self._block(item, 'TARGET_ERROR', error)
            if item.modified:
                self._restore(item)

    def tick(self, discovered, now=None):
        self._advance(now)
        self._pump_retired()
        if self.mode == 'STOPPED':
            for item in self.targets.values():
                self._restore(item)
            return self.status()
        present = {}
        for handle in discovered:
            try:
                token = self.adapter.identity(handle)
                hash(token)
                if bool(self.adapter.is_valid(handle)):
                    present.setdefault(token, handle)
            except Exception as error:
                self._event('DISCOVERY_ERROR', detail=error)
        for token in tuple(self.targets):
            if token not in present:
                item = self.targets.pop(token)
                outcome = self._restore(item)
                if item.modified:
                    self.pending_restores[token] = item
                self._event('REMOVED', token, outcome)
        for token, handle in present.items():
            if token not in self.targets and token not in self.pending_restores:
                self.targets[token] = _Target(handle, token)
                self._event('ADDED', token)
        ratio = vector(self.ratio(self.elapsed))
        for item in tuple(self.targets.values()):
            self._step(item, ratio)
        return self.status()

    def pause(self, now=None):
        self._advance(now)
        if self.mode != 'STOPPED':
            self.mode = 'PAUSED'
        outcomes = self._pump_retired()
        for token, item in self.targets.items():
            outcomes[token] = self._restore(item)
        return outcomes

    def resume(self, now=None):
        if self.mode == 'STOPPED':
            raise RuntimeError('STOPPED_CONTROLLER_REQUIRES_NEW_INSTANCE')
        self._advance(now)
        self.mode = 'RUNNING'

    def stop(self, now=None):
        self._advance(now)
        self.mode = 'STOPPED'
        outcomes = self._pump_retired()
        for token, item in self.targets.items():
            outcomes[token] = self._restore(item)
        return outcomes

    def retry_target(self, token):
        item = self.targets[token]
        if item.modified or item.unresolved or not self._valid_generation(item):
            self._event('RETRY_DENIED', token, 'DIRTY_OR_INVALID_TARGET')
            return False
        self.targets[token] = _Target(item.handle, token)
        self._event('RETRY', token)
        return True

    def status(self):
        all_items = list(self.targets.values()) + list(self.pending_restores.values())
        return {'mode': self.mode, 'elapsed_s': self.elapsed, 'targets': len(self.targets),
                'bound': sum(item.baseline is not None for item in self.targets.values()),
                'blocked': sum(item.blocked for item in self.targets.values()),
                'modified': sum(item.modified for item in all_items),
                'unresolved': sum(item.unresolved for item in all_items),
                'pending_restores': len(self.pending_restores)
                                    + sum(item.restore_pending for item in self.targets.values()),
                'counts': dict(self.counts), 'events': list(self.events)}
