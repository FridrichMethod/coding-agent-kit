"""QC research: same-bearer quota lease and admission/rotation contract.

Research code, not a product gate. A lease binds (exact bearer, registered
account/organization identity, quota family, observation time). Under one
admission lock the gate compares the outgoing bearer of each Messages request
with the lease before any upstream byte. On mismatch the request is held, a
fresh model-free quota observation is obtained with the OBSERVED bearer
(memory-only), identity is re-checked, and the request is then admitted under
a new lease or refused with a persistent pause. An old bearer is never
substituted and nothing is ever forwarded under a lease it does not match.

Freshness uses a monotonic deadline measured from the start of the last fresh
observation request; cache-only data, retries, 401/403/429 and transport
failures never extend it. A pause is a latch: passive recovery never reopens,
only a one-use trusted continuation issued after the pause plus a fresh
passing check can.
"""

import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

_HERE = Path(os.path.realpath(__file__)).parent


def _load(name):
    spec = importlib.util.spec_from_file_location("qc_" + name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bearer_mod = _load("bearer")
usage_mod = _load("usage")

METADATA_FILE = ".claude.json"
METADATA_LIMIT = 16 * 1024 * 1024
_UUID = re.compile(r"[A-Za-z0-9-]{1,64}")
# Model-specific rows that make the general windows insufficient for a model.
# The substring mapping from a model id to a family is a research heuristic;
# the real allowed_models list and its family mapping are a G0 input.
MODEL_SPECIFIC = {"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}


class Clock:
    def monotonic(self):
        return time.monotonic()

    def wall_ms(self):
        return int(time.time() * 1000)


class FakeClock(Clock):
    """Deterministic clock; wall can jump independently to simulate anomalies."""

    def __init__(self, wall_ms=1_800_000_000_000, monotonic=1000.0):
        self._mono, self._wall = monotonic, wall_ms
        self._lock = threading.Lock()

    def monotonic(self):
        with self._lock:
            return self._mono

    def wall_ms(self):
        with self._lock:
            return self._wall

    def advance(self, seconds):
        with self._lock:
            self._mono += seconds
            self._wall += int(seconds * 1000)

    def jump_wall(self, milliseconds):
        with self._lock:
            self._wall += milliseconds


class Policy:
    __slots__ = ("reserves", "max_age_s", "clock_tolerance_ms", "server_skew_ms",
                 "revalidations_per_minute", "allowed_models", "family")

    def __init__(self, reserves, max_age_s=300.0, clock_tolerance_ms=2000, server_skew_ms=300000,
                 revalidations_per_minute=4, allowed_models=("claude-fixture-general",),
                 family="claude-subscription-general"):
        for name, value in reserves.items():
            if name not in usage_mod.POLICY_WINDOWS:
                raise ValueError("unknown window")
            if value is not None and (type(value) not in (int, float) or not 0 <= value <= 100):
                raise ValueError("reserve out of range")
        self.reserves = dict(reserves)
        self.max_age_s, self.clock_tolerance_ms, self.server_skew_ms = max_age_s, clock_tolerance_ms, server_skew_ms
        self.revalidations_per_minute = revalidations_per_minute
        self.allowed_models, self.family = frozenset(allowed_models), family

    def enabled(self):
        return {name for name, value in self.reserves.items() if value is not None}


class IdentityUnavailable(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def read_local_identity(config_dir, uid=None, private_depth=3):
    """PA0's allowed read: only oauthAccount account/organization UUIDs from .claude.json."""
    uid = os.getuid() if uid is None else uid
    try:
        directory = bearer_mod.open_account_directory(config_dir, uid, private_depth)
    except bearer_mod.BearerRefused as error:
        raise IdentityUnavailable(error.reason) from None
    try:
        try:
            fd = os.open(METADATA_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory)
        except OSError:
            raise IdentityUnavailable("metadata_unusable") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022
                    or info.st_nlink != 1 or not 0 < info.st_size <= METADATA_LIMIT):
                raise IdentityUnavailable("metadata_not_private")
            chunks, total = [], 0
            while total <= info.st_size:
                chunk = os.read(fd, info.st_size + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=bearer_mod._unique_object,
                              parse_constant=bearer_mod._reject_constant)
        account = document["oauthAccount"]["accountUuid"]
        organization = document["oauthAccount"]["organizationUuid"]
    except (bearer_mod.BearerRefused, UnicodeDecodeError, ValueError, RecursionError, KeyError, TypeError):
        raise IdentityUnavailable("metadata_malformed") from None
    finally:
        document = raw = None
    if not all(isinstance(v, str) and _UUID.fullmatch(v) for v in (account, organization)):
        raise IdentityUnavailable("metadata_malformed")
    return (account, organization)


class LocalMetadataIdentity:
    """Before/after local UUID reads. Detects many switches, but cannot tie an
    opaque bearer to an account (documented gap; see BearerProfileIdentity)."""

    kind = "local-metadata"

    def __init__(self, reader):
        self.reader = reader

    def begin(self, observed):
        return self.reader()

    def finish(self, observed, before):
        after = self.reader()
        if after != before:
            raise IdentityUnavailable("identity_changed_during_check")
        return after


class BearerProfileIdentity:
    """G0 option: server-asserted identity for the same bearer, plus stable local metadata."""

    kind = "bearer-profile"

    def __init__(self, reader, profile):
        self.reader, self.profile = reader, profile

    def begin(self, observed):
        return self.reader()

    def finish(self, observed, before):
        server = self.profile(observed)
        after = self.reader()
        if after != before:
            raise IdentityUnavailable("identity_changed_during_check")
        if server is None:
            raise IdentityUnavailable("server_identity_unavailable")
        if server != after:
            raise IdentityUnavailable("bearer_identity_mismatch")
        return server


class Lease:
    __slots__ = ("bearer", "identity", "family", "observed_wall_ms", "base_monotonic", "deadline",
                 "clock_offset_ms", "windows", "other_families", "unproved_rows", "epoch")

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)

    def __repr__(self):
        return "Lease(epoch=%d, family=%s, <bearer redacted>)" % (self.epoch, self.family)


class TrustedContinuation:
    """One-use token created only by the trusted-terminal path (PC5/PA1C parser)."""

    __slots__ = ("nonce", "issued_monotonic", "used")

    def __init__(self, issued_monotonic):
        self.nonce, self.issued_monotonic, self.used = secrets.token_hex(16), issued_monotonic, False


class Decision:
    __slots__ = ("ok", "reason", "stream_id")

    def __init__(self, ok, reason, stream_id=None):
        self.ok, self.reason, self.stream_id = ok, reason, stream_id

    def __repr__(self):
        return "Decision(%s, %s)" % (self.ok, self.reason)


def presented_bearer(headers):
    """Exactly one 'Authorization: Bearer X' and no other auth selector, else None."""
    auth = [v for k, v in headers if k.lower() == "authorization"]
    others = [k for k, _ in headers if k.lower() in ("x-api-key", "proxy-authorization")]
    if len(auth) != 1 or others or not auth[0].startswith("Bearer "):
        return None
    token = auth[0][7:]
    return token if bearer_mod.TOKEN_RE.fullmatch(token) else None


class AdmissionController:
    def __init__(self, registered_identity, policy, quota, identity, clock=None, events=None):
        self.registered = registered_identity
        self.policy, self.quota, self.identity = policy, quota, identity
        self.clock = clock or Clock()
        self.events = events if events is not None else []
        self.lock = threading.RLock()
        self.state, self.reason, self.lease = "checking", None, None
        self.paused_monotonic = None
        self.streams = {}
        self._ids = itertools.count(1)
        self._epochs = itertools.count(1)
        self._revalidations = []
        self._continuations = set()

    # -- helpers ---------------------------------------------------------
    def _event(self, kind, **fields):
        self.events.append(dict(kind=kind, **fields))

    def _offset(self):
        return self.clock.wall_ms() - int(self.clock.monotonic() * 1000)

    def _clock_ok(self, lease=None):
        lease = lease or self.lease
        return lease is None or abs(self._offset() - lease.clock_offset_ms) <= self.policy.clock_tolerance_ms

    def _pause(self, reason):
        if self.state == "checking":
            self.state = "refused"
        elif self.state != "refused":
            self.state = "paused"
        self.reason = reason
        self.paused_monotonic = self.clock.monotonic()
        self._continuations.clear()  # anything issued before this pause is void
        closers, self.streams = list(self.streams.values()), {}
        self._event("latched", state=self.state, reason=reason, closed=len(closers))
        for close in closers:
            close()

    def _evaluate(self, observed, base_monotonic):
        """Fresh check with the observed bearer; returns (lease, None) or (None, reason)."""
        try:
            token = self.identity.begin(observed)
        except IdentityUnavailable as error:
            return None, error.reason
        observation = self.quota(observed)
        self._event("quota_fetch", outcome=observation.outcome, status=observation.http_status,
                    fresh=observation.fresh)
        try:
            identity = self.identity.finish(observed, token)
        except IdentityUnavailable as error:
            return None, error.reason
        enabled = self.policy.enabled()
        if not observation.valid_for(enabled):
            if observation.outcome == "response" and observation.fresh:
                return None, "window_unavailable"
            return None, "quota_" + str(observation.http_status or observation.outcome)
        if identity != self.registered:
            return None, "identity_mismatch"
        offset = self._offset()
        if self.lease is not None and abs(offset - self.lease.clock_offset_ms) > self.policy.clock_tolerance_ms:
            return None, "clock_anomaly"
        if self.lease is not None and observation.received_wall_ms < self.lease.observed_wall_ms:
            return None, "clock_anomaly"
        if (observation.server_date_ms is not None and
                abs(observation.server_date_ms - observation.received_wall_ms) > self.policy.server_skew_ms):
            return None, "clock_anomaly"
        for name in sorted(enabled):
            window = observation.windows[usage_mod.POLICY_WINDOWS[name]]
            if window.remaining() <= self.policy.reserves[name]:
                return None, "threshold_" + name
        lease = Lease(bearer=observed, identity=identity, family=self.policy.family,
                      observed_wall_ms=observation.received_wall_ms, base_monotonic=base_monotonic,
                      deadline=base_monotonic + self.policy.max_age_s, clock_offset_ms=offset,
                      windows={k: v.remaining() for k, v in observation.windows.items()},
                      other_families=observation.other_families, unproved_rows=observation.unproved_rows,
                      epoch=next(self._epochs))
        return lease, None

    def _model_refusal(self, model):
        """None if the lease's windows are proved to govern this model, else a reason.

        Fails closed while the lease holds any row of unproved semantics
        (model-scoped limits[] entries, credit pools, OAuth-apps or unknown rows).
        """
        if not isinstance(model, str) or model not in self.policy.allowed_models:
            return "model_family_unproved"
        lowered = model.lower()
        if any(row in self.lease.other_families and marker in lowered for row, marker in MODEL_SPECIFIC.items()):
            return "model_family_unproved"
        if self.lease.unproved_rows:
            return "quota_row_unproved"
        return None

    def _register(self, close):
        stream_id = next(self._ids)
        self.streams[stream_id] = close
        return stream_id

    # -- public contract ---------------------------------------------------
    def startup(self, observed):
        """Startup preflight with the bearer native will use. No model request."""
        with self.lock:
            if self.state != "checking":
                return Decision(False, "not_checking")
            base = self.clock.monotonic()
            lease, reason = self._evaluate(observed, base)
            if lease is None:
                self._pause(reason)
                return Decision(False, reason)
            self.lease, self.state = lease, "open"
            self._event("lease", epoch=lease.epoch, cause="startup")
            return Decision(True, "startup_ok")

    def admit(self, headers, model, close=lambda: None):
        """Decide one outgoing Messages request before any upstream byte."""
        with self.lock:
            if self.state != "open":
                self._event("refused", reason=self.state)
                return Decision(False, self.reason or self.state)
            token = presented_bearer(headers)
            if token is None:
                self._pause("auth_selector_conflict")
                return Decision(False, "auth_selector_conflict")
            if not self._clock_ok():
                self._pause("clock_anomaly")
                return Decision(False, "clock_anomaly")
            if self.clock.monotonic() >= self.lease.deadline:
                self._pause("freshness_expired")
                return Decision(False, "freshness_expired")
            if not isinstance(model, str) or model not in self.policy.allowed_models:
                self._event("refused", reason="model_family_unproved")
                return Decision(False, "model_family_unproved")
            if not self.lease.bearer.matches(token):
                decision = self._rotate(token)
                if not decision.ok:
                    return decision
            refusal = self._model_refusal(model)
            if refusal is not None:
                self._event("refused", reason=refusal)
                return Decision(False, refusal)
            stream_id = self._register(close)
            self._event("admitted", epoch=self.lease.epoch, stream=stream_id)
            return Decision(True, "admitted", stream_id)

    def _rotate(self, token):
        """Hold the request (lock held); fresh same-bearer check; then admit or pause."""
        now = self.clock.monotonic()
        self._revalidations = [t for t in self._revalidations if now - t < 60]
        if len(self._revalidations) >= self.policy.revalidations_per_minute:
            self._pause("revalidation_budget")
            return Decision(False, "revalidation_budget")
        self._revalidations.append(now)
        observed = bearer_mod.AccessBearer(token, bearer_mod.MAX_EXPIRES_MS - 1, False, True, "observed-request")
        self._event("rotation_hold", previous_epoch=self.lease.epoch)
        lease, reason = self._evaluate(observed, now)
        if lease is None:
            self._pause(reason)
            return Decision(False, reason)
        self.lease = lease
        self._event("lease", epoch=lease.epoch, cause="rotation")
        return Decision(True, "rotated")

    def monitor(self):
        """One bounded poll with the lease bearer. Only fresh valid data extends age."""
        with self.lock:
            if self.state != "open":
                if self.state == "paused" and self.lease is not None:
                    observation = self.quota(self.lease.bearer)
                    self._event("paused_poll", outcome=observation.outcome, fresh=observation.fresh)
                return Decision(False, self.reason or self.state)
            lease = self.lease
            base = self.clock.monotonic()
        candidate, reason = self._evaluate(lease.bearer, base)
        with self.lock:
            if self.state != "open" or self.lease is not lease:
                return Decision(False, "superseded")
            if candidate is not None:
                candidate.epoch = lease.epoch
                self.lease = candidate
                self._event("extended", epoch=lease.epoch, deadline=candidate.deadline)
                return Decision(True, "extended")
            if (reason.startswith(("threshold_", "identity", "bearer_identity", "clock_", "window_"))
                    or reason == "server_identity_unavailable"):
                self._pause(reason)
                return Decision(False, reason)
            if self.clock.monotonic() >= lease.deadline:
                self._pause("freshness_expired")
                return Decision(False, "freshness_expired")
            self._event("not_extended", reason=reason)
            return Decision(False, reason)

    def issue_continuation(self):
        """Called only by the trusted-terminal path after a deliberate user action."""
        with self.lock:
            token = TrustedContinuation(self.clock.monotonic())
            self._continuations.add(token.nonce)
            return token

    def continue_manually(self, continuation, observed):
        with self.lock:
            if self.state != "paused":
                return Decision(False, "not_paused")
            if (not isinstance(continuation, TrustedContinuation) or continuation.used
                    or continuation.nonce not in self._continuations
                    or continuation.issued_monotonic < self.paused_monotonic):
                self._event("continuation_rejected")
                return Decision(False, "continuation_rejected")
            continuation.used = True
            self._continuations.discard(continuation.nonce)
            base = self.clock.monotonic()
            previous, self.lease = self.lease, None
            lease, reason = self._evaluate(observed, base)
            if lease is None:
                self.lease = previous
                self.reason = reason
                self._event("continuation_failed", reason=reason)
                return Decision(False, reason)
            self.lease, self.state, self.reason = lease, "open", None
            self._event("lease", epoch=lease.epoch, cause="manual_continuation")
            return Decision(True, "resumed")

    def release(self, stream_id):
        with self.lock:
            self.streams.pop(stream_id, None)
