"""Engine-independent, main-thread scale controller.

The adapter supplies identity(target), is_valid(target), read_scale(target), and
write_scale(target, tuple3). identity must be hashable and distinguish native
model generations, including filename/asset identity. Discovery is external:
tick() receives only approved render models, never gameplay/collision objects.
Optional authoritative_baseline(target) returns None or the latest verified
official scale, expressed with the engine vector's native numeric precision.
No scheduling, game imports, file IO, or background engine access occurs here.
"""
import math
import time
from collections import Counter, deque


def vector_copy(value):
    """Copy a tuple/list or x/y/z vector into immutable finite Python floats."""
    if all(hasattr(value, attr) for attr in ('x', 'y', 'z')):
        result = (float(value.x), float(value.y), float(value.z))
    else:
        result = tuple(float(component) for component in value)
    if len(result) != 3 or not all(math.isfinite(component) for component in result):
        raise ValueError('scale must have exactly three finite components')
    return result


def scale_close(actual, expected):
    return all(abs(a-b) <= 1e-6 + 1e-5*abs(b) for a, b in zip(actual, expected))


def mc_ratio(elapsed):
    keys = (0., .25, .45833, .70833, .91667)
    levels = (0., 1., 0., 1., 0.)
    phase = max(float(elapsed), 0.) % keys[-1]
    for index in range(4):
        if phase <= keys[index+1]:
            u = (phase-keys[index])/(keys[index+1]-keys[index])
            q = levels[index] + (levels[index+1]-levels[index])*u*u*(3-2*u)
            return (1+.5*q, 1-.5*q, 1.)
    return (1., 1., 1.)


class _Target:
    def __init__(self, handle, token):
        self.handle = handle
        self.token = token
        self.baseline = None
        self.candidate = None
        self.stable_frames = 0
        self.last_written = None
        self.pending_previous = None
        self.modified = False
        self.unresolved = False
        self.blocked = False
        self.reason = ''


class Controller:
    """Call from the engine main thread; callers serialize every public method.

    External scale changes quarantine just the affected identity. retry_target()
    explicitly allows stable-baseline sampling again; there is no automatic
    adoption of per-frame animation scale, because it could absorb our effect.
    New or changed identities are sampled for settle_frames before first write.
    Removing/replacing an identity attempts restoration of its original handle.
    """
    def __init__(self, adapter, *, clock=time.monotonic, settle_frames=3,
                 max_events=128, ratio=mc_ratio):
        if settle_frames < 1 or max_events < 1:
            raise ValueError('settle_frames and max_events must be positive')
        self.adapter = adapter
        self.clock = clock
        self.ratio = ratio
        self.settle_frames = settle_frames
        self.targets = {}
        self.pending_restores = {}
        self.events = deque(maxlen=max_events)
        self.counts = Counter()
        self.mode = 'RUNNING'
        self.elapsed = 0.
        self._last_now = None

    def _event(self, kind, token=None, detail=None):
        self.counts[kind] += 1
        self.events.append({'kind': kind, 'identity': repr(token)[:512],
                            'detail': str(detail)[:512] if detail is not None else ''})

    def _advance(self, now=None):
        now = float(self.clock() if now is None else now)
        if not math.isfinite(now):
            raise ValueError('clock must be finite')
        if self._last_now is None:
            self._last_now = now
        else:
            # A faulty/backward clock never rewinds phase or duplicates time.
            next_now = max(now, self._last_now)
            if self.mode == 'RUNNING':
                self.elapsed += next_now-self._last_now
            self._last_now = next_now

    def _same_valid(self, item):
        return bool(self.adapter.is_valid(item.handle)) and self.adapter.identity(item.handle) == item.token

    def _official(self, item):
        getter = getattr(self.adapter, 'authoritative_baseline', None)
        value = getter(item.handle) if getter is not None else None
        return vector_copy(value) if value is not None else None

    def _update_official(self, item, official):
        if item.baseline != official:
            item.baseline = official
            self._event('BASELINE_UPDATED', item.token, official)

    def _block(self, item, reason, detail=None, lose_ownership=False):
        item.blocked = True
        item.reason = reason
        if lose_ownership:
            # Losing write ownership does not prove that our deformation has
            # disappeared. Preserve the dirty record without overwriting it.
            if item.modified:
                item.unresolved = True
        self._event(reason, item.token, detail)

    def _restore(self, item):
        if not item.modified or item.baseline is None:
            return 'NO_WRITE'
        try:
            if not bool(self.adapter.is_valid(item.handle)):
                item.modified = False
                item.unresolved = False
                return 'TARGET_DESTROYED'
            if self.adapter.identity(item.handle) != item.token:
                item.modified = False
                item.unresolved = False
                return 'TARGET_REPURPOSED'
            current = vector_copy(self.adapter.read_scale(item.handle))
            official = self._official(item)
            owned = item.last_written is not None and scale_close(current, item.last_written)
            owned = owned or (item.pending_previous is not None and scale_close(current, item.pending_previous))
            if official is not None:
                owned = owned or scale_close(current, official)
                if owned:
                    self._update_official(item, official)
            if current == item.baseline:
                item.modified = False
                item.unresolved = False
                item.last_written = current
                item.pending_previous = None
                return 'ALREADY_AT_BASELINE'
            if not owned:
                item.unresolved = True
                return 'OWNERSHIP_CONFLICT'
            # Exact restoration matters: close-to-baseline is not sufficient.
            item.pending_previous = current
            item.last_written = item.baseline
            self.adapter.write_scale(item.handle, item.baseline)
            actual = vector_copy(self.adapter.read_scale(item.handle))
            if actual != item.baseline:
                item.unresolved = True
                return 'RESTORE_FAILED'
            item.last_written = actual
            item.pending_previous = None
            item.modified = False
            item.unresolved = False
            return 'RESTORED'
        except Exception as error:
            if item.modified:
                item.unresolved = True
            self._event('RESTORE_EXCEPTION', item.token, error)
            return 'RESTORE_FAILED'

    def _retire(self, token):
        item = self.targets.pop(token)
        result = self._restore(item)
        if result in ('RESTORE_FAILED', 'OWNERSHIP_CONFLICT'):
            self.pending_restores[token] = item
        self._event('REMOVED', token, result)
        return result

    def _retry_retired(self):
        outcomes = {}
        for token, item in tuple(self.pending_restores.items()):
            result = self._restore(item)
            outcomes[token] = result
            if result not in ('RESTORE_FAILED', 'OWNERSHIP_CONFLICT'):
                del self.pending_restores[token]
                self._event('RETIRED_RECOVERY', token, result)
        return outcomes

    def _step(self, item, ratio):
        if item.blocked:
            return
        try:
            if not self._same_valid(item):
                self._block(item, 'INVALID_OR_REPURPOSED')
                return
            current = vector_copy(self.adapter.read_scale(item.handle))
            official = self._official(item)
            if item.baseline is None:
                if official is not None and not scale_close(current, official):
                    self._block(item, 'AUTHORITATIVE_BASELINE_CONFLICT', current,
                                lose_ownership=True)
                    return
                candidate = official if official is not None else current
                if item.candidate is not None and scale_close(candidate, item.candidate):
                    item.stable_frames += 1
                else:
                    item.candidate = candidate
                    item.stable_frames = 1
                if item.stable_frames < self.settle_frames:
                    return
                item.baseline = candidate  # immutable copy, never a live vector
                item.last_written = current
                self._event('BOUND', item.token, candidate)
            owned = scale_close(current, item.last_written)
            if official is not None:
                owned = owned or scale_close(current, official)
            if not owned:
                self._block(item, 'OWNERSHIP_CONFLICT', current, lose_ownership=True)
                return
            if official is not None:
                self._update_official(item, official)
                # Accept an engine reset to its authoritative scale, while
                # preserving ownership of our previous value if it is intact.
                if scale_close(current, official):
                    item.last_written = current
            if self.mode != 'RUNNING':
                return
            expected = tuple(base*factor for base, factor in zip(item.baseline, ratio))
            # Save intended ownership before setter: it may write then raise.
            item.pending_previous = item.last_written
            item.last_written = expected
            item.modified = True
            self.adapter.write_scale(item.handle, expected)
            actual = vector_copy(self.adapter.read_scale(item.handle))
            if not scale_close(actual, expected):
                self._block(item, 'WRITE_MISMATCH', actual)
                result = self._restore(item)
                self._event('RECOVERY', item.token, result)
                return
            item.last_written = actual
            item.pending_previous = None
        except Exception as error:
            self._block(item, 'TARGET_ERROR', error)
            result = self._restore(item)
            self._event('RECOVERY', item.token, result)

    def tick(self, discovered, now=None):
        """Reconcile a full approved target snapshot and advance all models."""
        if self.mode == 'STOPPED':
            return self.status()
        self._advance(now)
        self._retry_retired()
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
                self._retire(token)
        for token, handle in present.items():
            if token not in self.targets and token not in self.pending_restores:
                self.targets[token] = _Target(handle, token)
                self._event('ADDED', token)
        ratio = vector_copy(self.ratio(self.elapsed))
        for item in tuple(self.targets.values()):
            self._step(item, ratio)
        return self.status()

    def pause(self, now=None):
        if self.mode == 'STOPPED':
            return {}
        self._advance(now)
        self.mode = 'PAUSED'
        outcomes = {}
        outcomes.update(self._retry_retired())
        for token, item in self.targets.items():
            result = self._restore(item)
            outcomes[token] = result
            if result in ('RESTORE_FAILED', 'OWNERSHIP_CONFLICT', 'TARGET_REPURPOSED'):
                self._block(item, result)
            self._event('PAUSE_RESTORE', token, result)
        return outcomes

    def resume(self, now=None):
        if self.mode == 'STOPPED':
            raise RuntimeError('stopped controller requires a new instance')
        self._advance(now)
        self.mode = 'RUNNING'

    def retry_target(self, token):
        """Explicitly accept a new stable baseline for one quarantined model."""
        item = self.targets[token]
        if item.modified and item.unresolved:
            self._event('RETRY_DENIED', token, 'UNRESOLVED_DIRTY_TARGET')
            return False
        if not self._same_valid(item):
            return False
        result = self._restore(item)
        if result in ('RESTORE_FAILED', 'OWNERSHIP_CONFLICT'):
            self._event('RETRY_DENIED', token, result)
            return False
        item.baseline = item.candidate = item.last_written = None
        item.pending_previous = None
        item.stable_frames = 0
        item.modified = item.blocked = False
        item.unresolved = False
        item.reason = ''
        self._event('RETRY', token)
        return True

    def stop(self, now=None):
        self._advance(now)
        self.mode = 'STOPPED'
        outcomes = {}
        outcomes.update(self._retry_retired())
        for token, item in self.targets.items():
            result = self._restore(item)
            outcomes[token] = result
            self._event('STOP_RESTORE', token, result)
        return outcomes

    def status(self):
        return {'mode': self.mode, 'elapsed_s': self.elapsed,
                'targets': len(self.targets),
                'bound': sum(item.baseline is not None for item in self.targets.values()),
                'blocked': sum(item.blocked for item in self.targets.values()),
                'modified': sum(item.modified for item in self.targets.values())
                            + sum(item.modified for item in self.pending_restores.values()),
                'unresolved': sum(item.unresolved for item in self.targets.values())
                              + sum(item.unresolved for item in self.pending_restores.values()),
                'pending_restores': len(self.pending_restores),
                'counts': dict(self.counts), 'events': list(self.events)}
