"""QX research model: bind the bearer and account a gate admits to fresh Codex quota.

Feasibility code only (not product runtime). Standard library, injected clocks.
The gate sees each request's bearer and ChatGPT-Account-ID in transit. A usage
response it forwarded for that exact pair is the only thing that can create or
renew a binding. Bearers are compared through a per-process keyed digest and
are never stored, logged or returned.
"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os

FIVE_HOUR, WEEKLY = 'five_hour', 'weekly'
# Normalize by the provider's stated duration, never by the primary/secondary slot.
WINDOW_BY_MINUTES = {300: FIVE_HOUR, 10080: WEEKLY}
# Research default only: which plans may be protected is a G0 decision.
DEFAULT_PLANS = frozenset({'plus', 'pro', 'prolite', 'promax', 'team', 'business'})
# Reasons that cannot heal by retrying; they latch a pause instead of counting as transient.
PERMANENT = frozenset({'account-mismatch', 'user-mismatch', 'unsupported-plan', 'ambiguous-window',
                       'identity-changed', 'unsupported-family'})
RESET_SKEW = 120  # seconds of clock skew tolerated around a reported reset time


class Unavailable(Exception):
    """Quota data that must never be read as zero usage or unlimited allowance."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Window:
    name: str
    used_percent: float
    minutes: int
    resets_at: int

    @property
    def remaining(self):
        return 100.0 - self.used_percent


@dataclass(frozen=True)
class Observation:
    account_id: str
    user_id: object
    plan: str
    windows: dict
    other_families: tuple
    limit_reached: bool
    source: str


@dataclass(frozen=True)
class Policy:
    five_hour: object  # None (off) or a reserve percentage 0..100
    weekly: object
    max_age: float  # seconds since the last fresh observation, recovery included

    def enabled(self):
        return {name: value for name, value in ((FIVE_HOUR, self.five_hour), (WEEKLY, self.weekly))
                if value is not None}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Unavailable('malformed')
    return value


def _window(raw, wall_now):
    if not isinstance(raw, dict):
        raise Unavailable('malformed')
    used = _number(raw.get('used_percent'))
    seconds = raw.get('limit_window_seconds')
    reset = raw.get('reset_at')
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
        raise Unavailable('malformed')
    if not isinstance(reset, int) or isinstance(reset, bool):
        raise Unavailable('malformed')
    if not 0 <= used <= 100:
        raise Unavailable('out-of-range')
    minutes = -(-seconds // 60)  # native rounds seconds up to whole minutes
    if reset <= wall_now - RESET_SKEW or reset > wall_now + seconds + RESET_SKEW:
        raise Unavailable('reset-anomaly')  # a passed or impossible reset is not current data
    return WINDOW_BY_MINUTES.get(minutes), Window(WINDOW_BY_MINUTES.get(minutes), float(used), minutes, reset)


def normalize_usage(body, *, request_account, expected_account, expected_user, wall_now, plans=DEFAULT_PLANS):
    """Raw usage JSON (as forwarded) -> Observation, or Unavailable with a reason."""
    try:
        data = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise Unavailable('malformed') from None
    if not isinstance(data, dict) or not isinstance(data.get('plan_type'), str):
        raise Unavailable('malformed')
    account, user = data.get('account_id'), data.get('user_id')
    if account != request_account or account != expected_account:
        raise Unavailable('account-mismatch')
    if expected_user is not None and user != expected_user:
        raise Unavailable('user-mismatch')
    if data['plan_type'] not in plans:
        raise Unavailable('unsupported-plan')
    rate = data.get('rate_limit')
    if not isinstance(rate, dict):
        raise Unavailable('no-codex-family')
    windows = {}
    for slot in ('primary_window', 'secondary_window'):
        if rate.get(slot) is None:
            continue
        name, window = _window(rate[slot], wall_now)
        if name is None:
            continue  # an unrecognized duration is not mapped onto an enabled window
        if name in windows:
            raise Unavailable('ambiguous-window')
        windows[name] = window
    others = data.get('additional_rate_limits') or []
    if not isinstance(others, list):
        raise Unavailable('malformed')
    # The model -> family mapping is unproved (I/U). Fail closed: a request whose model equals any
    # name a non-codex row carries (limit_name, normal_model_slug or metered_feature) is refused.
    families = tuple(sorted({row[key] for row in others if isinstance(row, dict)
                             for key in ('limit_name', 'normal_model_slug', 'metered_feature')
                             if isinstance(row.get(key), str)}))
    reached = rate.get('limit_reached') is True or rate.get('allowed') is False
    return Observation(account, user, data['plan_type'], windows, families, reached, 'usage')


def normalize_headers(headers, wall_now):
    """Default-family windows from forwarded model-response headers (running state only)."""
    lowered = {key.lower(): value for key, value in headers.items()}
    windows = {}
    for slot in ('primary', 'secondary'):
        prefix = 'x-codex-' + slot
        if prefix + '-used-percent' not in lowered:
            continue
        try:
            used = float(lowered[prefix + '-used-percent'])
            minutes = int(lowered[prefix + '-window-minutes'])
            reset = int(lowered[prefix + '-reset-at'])
        except (KeyError, ValueError):
            raise Unavailable('malformed') from None
        if not math.isfinite(used) or not 0 <= used <= 100 or minutes <= 0:
            raise Unavailable('out-of-range')
        if reset <= wall_now - RESET_SKEW or reset > wall_now + minutes * 60 + RESET_SKEW:
            raise Unavailable('reset-anomaly')
        name = WINDOW_BY_MINUTES.get(minutes)
        if name is None:
            continue
        if name in windows:
            raise Unavailable('ambiguous-window')
        windows[name] = Window(name, used, minutes, reset)
    return windows


class Lease:
    """One invocation's gate binding: (credential epoch, account) -> fresh windows -> admission.

    The lease never refreshes or rewrites credentials and never resumes on its own. `paused` is
    the latch (the cause that first paused); `blocking` is the latest blocking condition, or None
    once a fresh read passes and only a deliberate continuation is missing.
    """

    def __init__(self, *, expected_account, expected_user, policy, clock, wall, plans=DEFAULT_PLANS,
                 models=None, continuation_window=30.0, on_close=None):
        self.expected_account, self.expected_user = expected_account, expected_user
        self.policy, self.clock, self.wall, self.plans = policy, clock, wall, plans
        # None: research mode. A set: only these reviewed codex-family model slugs are admitted.
        self.models = models
        # A continuation is answered only by a read the gate received within this many seconds.
        self.continuation_window = continuation_window
        self.on_close = on_close or (lambda reason: None)
        self._key = os.urandom(32)
        self.epoch, self.credential, self.observation, self.observed_at = 0, None, None, None
        # The recovery deadline runs from the last fresh observation of any epoch: a rotation
        # or a run of failures never restarts it.
        self.last_fresh = None
        self.paused, self.blocking, self.continuation, self.failures = None, None, None, 0
        self.log = []

    def digest(self, bearer):
        return hmac.new(self._key, (bearer or '').encode(), hashlib.sha256).digest()

    def _note(self, event, **data):
        self.log.append({'event': event, 'epoch': self.epoch, **data})

    def _rotate(self, credential, cause):
        # A different credential starts a new epoch; no older observation can authorize it, and
        # no continuation requested under the old epoch carries over.
        self.epoch += 1
        self.credential, self.observation, self.observed_at = credential, None, None
        self._note('epoch', cause=cause)
        self._disarm('epoch-changed')

    def _disarm(self, outcome):
        if self.continuation is not None:
            self.continuation = None
            self._note('continuation-used', outcome=outcome)

    def _pause(self, reason, close=True):
        # Every blocking condition updates `blocking`; only the first latches and closes streams.
        self.blocking = reason
        if self.paused is None:
            self.paused = reason
            self._note('pause', reason=reason)
            if close:
                self.on_close(reason)
        else:
            self._note('blocking', reason=reason)

    def reason(self):
        """The current condition for a refusal message while paused."""
        return self.blocking or 'awaiting-continuation'

    def _identity(self, cause):
        self._rotate(None, cause)
        self._pause('identity-changed')

    def _answers(self, started):
        """Whether a read the gate received at `started` is the check a continuation asked for.

        A read received before the request never answers it (and does not use it up). The first
        read received after it does, whatever its outcome; one received too late only expires it.
        """
        if self.continuation is None or started <= self.continuation:
            return False
        if started - self.continuation > self.continuation_window:
            self._disarm('expired')
            return False
        self.continuation = None
        return True

    def _verdict(self, observation):
        missing = [name for name in self.policy.enabled() if name not in observation.windows]
        if missing:
            return 'window-missing:' + ','.join(missing)
        if observation.limit_reached and self.policy.enabled():
            return 'limit-reached'
        for name, reserve in sorted(self.policy.enabled().items()):
            if observation.windows[name].remaining <= reserve:
                return 'threshold:' + name
        return None

    def observe_usage(self, bearer, account_header, status, body, started=None):
        """Record a forwarded native usage exchange; returns the outcome label.

        `started` is the lease clock when the gate received the request (default: now). Freshness
        and the continuation order use it, never the time the response arrived.
        """
        started = self.clock() if started is None else started
        if account_header != self.expected_account:
            self._identity('request-account')
            return 'identity-changed'
        credential = self.digest(bearer)
        if credential != self.credential:
            self._rotate(credential, 'usage-credential')
        if self.observed_at is not None and started < self.observed_at:
            return 'superseded'  # an older read answered late; it cannot replace newer data
        answers = self._answers(started)
        if answers:
            self._note('continuation-used', outcome='check')
        if status != 200:
            self.failures += 1
            self._note('failure', status=status)
            return 'failure'
        try:
            observation = normalize_usage(body, request_account=account_header,
                                          expected_account=self.expected_account,
                                          expected_user=self.expected_user, wall_now=self.wall(),
                                          plans=self.plans)
        except Unavailable as error:
            self.failures += 1
            self._note('unavailable', reason=error.reason)
            if error.reason in ('account-mismatch', 'user-mismatch'):
                self._identity(error.reason)
            elif error.reason in PERMANENT:
                self._pause(error.reason)
            return error.reason
        self.observation, self.observed_at, self.last_fresh, self.failures = observation, started, started, 0
        verdict = self._verdict(observation)
        self._note('fresh', verdict=verdict)
        if verdict:
            self._pause(verdict)
        elif self.paused:
            self.blocking = None
            if answers:
                # Only a deliberate continuation answered by this newer passing read reopens.
                self._note('resume', after=self.paused)
                self.paused = None
        return 'fresh'

    def observe_headers(self, bearer, account_header, headers):
        """Renew from a forwarded model response only within the same bound credential epoch."""
        if self.observation is None or self.digest(bearer) != self.credential \
                or account_header != self.expected_account:
            return 'ignored'
        try:
            windows = normalize_headers(headers, self.wall())
        except Unavailable as error:
            return error.reason
        if any(name not in windows for name in self.policy.enabled()):
            return 'ignored'
        merged = dict(self.observation.windows, **windows)
        self.observation = Observation(self.observation.account_id, self.observation.user_id,
                                       self.observation.plan, merged, self.observation.other_families,
                                       False, 'headers')
        self.observed_at = self.last_fresh = self.clock()
        verdict = self._verdict(self.observation)
        if verdict:
            self._pause(verdict)
        return 'fresh'

    def tick(self):
        """Monitor step (K drives it on a timer): an expired observation pauses with no request
        pending. Staleness replaces a data-derived blocking reason, never an identity or plan one."""
        if self.last_fresh is not None and self.clock() - self.last_fresh > self.policy.max_age \
                and self.blocking != 'stale' and self.blocking not in PERMANENT:
            self._pause('stale')
        return self.paused

    def admit(self, bearer, account_header, model):
        """Decide one model request before any upstream byte; returns (admitted, reason)."""
        if account_header != self.expected_account:
            self._identity('request-account')
            return False, 'identity-changed'
        credential = self.digest(bearer)
        if credential != self.credential:
            self._rotate(credential, 'model-credential')
        if self.tick():
            return False, 'paused:' + self.reason()
        if self.observation is None:
            return False, 'rebind-required'  # same account, new bearer: fresh same-bearer read first
        if model in self.observation.other_families or (self.models is not None and model not in self.models):
            self._pause('unsupported-family')
            return False, 'paused:unsupported-family'
        verdict = self._verdict(self.observation)
        if verdict:
            self._pause(verdict)
            return False, 'paused:' + verdict
        return True, 'admitted'

    def ready(self):
        """Startup preflight: True only with a fresh passing observation for this epoch."""
        return (self.tick() is None and self.observation is not None
                and self._verdict(self.observation) is None)

    def request_continuation(self, trusted):
        """A deliberate action from the trusted control; never from model traffic or a timer.

        It arms one check: the first usage read the gate receives after this call, within
        `continuation_window`, resumes only if it passes; any other outcome uses it up.
        """
        if not trusted or self.paused is None:
            return False
        self.continuation = self.clock()
        self._note('continuation-requested')
        return True
