"""claude-as protected mode: the Claude provider of the request gate, run as the gate process.

    python3 -I -B lib/claude-as/provider.py --listen-fds A[,B] --control-fd N [--monitor-fd N] --mode M

lib/coding-agent-kit/gate.py documents the process interface; the launcher (protect.py) writes the
configuration to stdin: the core keys (max_age_s, heartbeat_max_s, proxy) plus account, reserves
({"five_hour": "V" | null, "weekly": ...}, the resolved percentages), sources, identity ([account
UUID, organization UUID] of the enrollment), config_dir (the exact CLAUDE_CONFIG_DIR), platform
("linux" | "darwin"), keychain_user and home (macOS Keychain lookup), and for a refresh gate
connect_allow. Only the gate holds the bearer: it reads the account's access token memory-only
(bearer.py), checks it with its own usage and profile reads, and compares each Messages request's
bearer with it under the admission lock.

Claude Code 2.1.322 (reviewed): with ANTHROPIC_BASE_URL at the gate it sends POST /v1/messages and
POST /v1/messages/count_tokens there (both admitted only under a matching fresh lease, the model
checked); every other route is refused. Its auxiliary calls (profile, bootstrap, telemetry, OAuth
refresh) go through HTTPS_PROXY, the gate, as CONNECT tunnels, which a startup or restart gate
refuses; a refresh gate tunnels only platform.claude.com:443 for the D1 pre-launch refresh. Refusals
use status 400 (invalid_request_error): native renders 403 as a /login hint and retries 429.

Organization policy (D15 and the user's decision to refuse rather than run without it): Claude Code
2.1.322 applies an organization's server-managed policy (remote settings and policy limits, fetched
from api.anthropic.com and cached in CONFIG) only while ANTHROPIC_BASE_URL is unset or names
api.anthropic.com (S: its base-URL check answers "custom_base_url", and then it neither fetches nor
applies either document, cached copies included). Protected mode points ANTHROPIC_BASE_URL at this
gate, so no organization policy is in force in a protected session. Startup therefore refuses
("organization_policy") every account native would apply such a policy to: every subscription type
other than pro or max, a missing or unrecognized one included (S: native's eligibility checks answer
"prosumer_oauth" and "unsupported_subscription" only for a known type other than enterprise or team).
A machine whose managed settings pin the sign-in organization makes pro and max eligible too; the
launcher refuses that case (policy.py, protect.py), since only it reads the managed settings.

Quota reads (QC option 3): GET https://api.anthropic.com/api/oauth/profile and /api/oauth/usage with
the same in-memory bearer, the neutral User-Agent and the proxy Claude Code would use for HTTPS (HTTPS_PROXY, then HTTP_PROXY, then ALL_PROXY, a plain http:// one only), captured by the launcher. The profile proves
the identity (the two UUIDs only; nothing else is kept), the usage response gives the windows (a window
with utilization exactly 0 and resets_at null has not started: it counts as valid without a reset; any
other null reset is unavailable). A poll with the lease's bearer reads usage only.

Usage rows (the coordinator's decision "Claude usage rows", from the review of 2.1.322's parser, research
RQ; normalize_usage):
- five_hour and seven_day are the five-hour and weekly windows. limits[] entries of kind session and
  weekly_all without a scope report them again: each window takes the more used of its reports.
- seven_day_opus, seven_day_sonnet and limits[] weekly_scoped entries with a model scope are model
  families' own weekly limits; the family is the display name's first word (native names models
  "Family version"). While the weekly reserve is on, a family is admitted while its own row is above
  that reserve and refused alone otherwise (family_at_reserve; family_window_unavailable for a row
  that is unavailable), with no pause and no continuation: the general windows decide everything
  else. A weekly_scoped row of another known family (Fable, Mythos) is ignored, since family()
  refuses those models anyway.
- seven_day_oauth_apps, cinder_cove, wattle_ember and extra_usage are ignored: no Claude Code request is
  rejected by them (other surfaces' buckets and credit pools; native names no claim after them).
- Every other present row (an unknown key, a limits[] entry of another kind or scope, with an unknown
  family word, or malformed) pauses every request, or refuses the start (unproved_quota_row); the
  message and the rows field name those rows (sanitized names, never a value).

Token lifetime (D1): startup refuses a token that is inside native's 300 s refresh window
(native_refresh_window: the pre-launch refresh did not renew it) or within MIN_SESSION_MS beyond it
(token_expires_soon), since no renewal passes this gate once the session runs. Decisions carry
token_expires, the expiry of the token the launch started with, so that the launcher can say when
the session needs a relaunch; no value derived from the token itself leaves this process.

Evaluator wiring: Decider turns the gate's events into calls of the reserve evaluator (reserve.py),
one per gate instance; a restarted gate builds it paused. The evaluator hears the monitor (control_up)
once the gate recorded a heartbeat, and the startup check runs only then.
"""

import sys

if sys.version_info < (3, 11):
    print("claude-as gate: protected mode needs Python 3.11 or later.", file=sys.stderr)
    raise SystemExit(2)

from datetime import datetime
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import threading
import time

HERE = Path(os.path.realpath(__file__)).parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    gate = _load("claude_as_gate_core", HERE.parent / "coding-agent-kit/gate.py")
    reserve = _load("claude_as_reserve", HERE.parent / "coding-agent-kit/reserve.py")
    bearer = _load("claude_as_bearer", HERE / "bearer.py")
except (OSError, ImportError, SyntaxError):
    print("claude-as gate: incomplete install at %s" % HERE.parent.parent, file=sys.stderr)
    raise SystemExit(2) from None

VERSION = "2.1.322"
API = gate.Upstream("api.anthropic.com", 443)
USAGE_PATH, PROFILE_PATH = "/api/oauth/usage", "/api/oauth/profile"
OAUTH_BETA = "oauth-2025-04-20"
REFRESH_TARGETS = {"platform.claude.com:443": ("platform.claude.com", 443)}
NATIVE_REFRESH_WINDOW_MS = 300000  # native refreshes when now + 300000 >= expiresAt (S, 2.1.322)
MIN_SESSION_MS = 600000  # what a protected session gets at least beyond native's window (a tunable, I)
PERSONAL_PLANS = frozenset(("pro", "max"))  # native applies no organization policy to these (S, 2.1.322)
ROUTES = gate.RouteTable("claude-" + VERSION, (
    gate.Route("POST", "/v1/messages", gate.MODEL, query=True),
    gate.Route("POST", "/v1/messages/count_tokens", gate.MODEL, query=True),
))
GENERAL = {"five_hour": "five_hour", "seven_day": "weekly"}
FAMILY_ROWS = {"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}
# Not quota rows of Claude Code's requests: error and type, the overage and credit pools (extra_usage,
# cinder_cove, wattle_ember) and the third-party OAuth apps' bucket (seven_day_oauth_apps).
IGNORED = ("error", "type", "extra_usage", "seven_day_oauth_apps", "cinder_cove", "wattle_ember")
LIMIT_WINDOWS = {"session": "five_hour", "weekly_all": "weekly"}  # limits[] kinds of a general window
SCOPED = "weekly_scoped"  # a limits[] kind whose model scope names a family's own weekly limit
FAMILIES = ("opus", "sonnet", "haiku")
OTHER_FAMILIES = ("fable", "mythos")  # 2.1.322's other model families, whose models family() refuses
ROW_NAME = re.compile(r"[a-z0-9_]{1,64}")
KIND_NAME = re.compile(r"[a-z0-9_]{1,52}")  # limits_kind_ + this stays a 64-character row name
MODEL = re.compile(r"claude-[a-z0-9][a-z0-9.-]{0,95}")
UUID_TEXT = re.compile(r"[A-Za-z0-9-]{1,64}")
RFC3339 = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})")
REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
CONTROL = {"monitor_eof": "monitor_lost", "monitor_stale": "monitor_stale", "launcher_eof": "launcher_lost",
           "gate_error": "gate_error"}
IDENTITY = ("identity_unattributed", "identity_mismatch", "bearer_identity_mismatch")
REFUSAL_TEXT = {
    "model_unproved": "protected mode does not cover this model; use a Claude opus, sonnet or haiku model",
    "family_at_reserve": "this model family's own weekly limit is at or below the weekly reserve; use a model of "
                         "another family",
    "family_window_unavailable": "this model family's own weekly limit is unavailable in the usage report; use a "
                                 "model of another family",
}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError("non-finite number")


def _json(headers, body):
    kind = next((value for name, value in headers if name.lower() == "content-type"), "")
    if kind.split(";")[0].strip().lower() != "application/json":
        raise ValueError("not JSON")
    return json.loads(body.decode("utf-8"), object_pairs_hook=_unique, parse_float=Decimal,
                      parse_constant=_reject_constant)


def epoch(text):
    """Epoch seconds (Decimal) of an RFC 3339 time, or the value itself when it is not one."""
    if not isinstance(text, str) or not RFC3339.fullmatch(text):
        return text
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return Decimal(int(moment.timestamp() * 1000)) / 1000


def _reading(value, used_key, name, family=None):
    """The Reading of one row or limits[] entry; another shape is a malformed reading."""
    if not isinstance(value, dict):
        return reserve.Reading(used=None, name=name, family=family, merge=True)
    used = value.get(used_key)
    # A window not started yet (U1): used exactly 0 and resets_at null, as an idle account reports it;
    # any other reading without a reset stays unavailable (reserve.judge).
    idle = "resets_at" in value and value["resets_at"] is None and reserve.number(used) == 0
    return reserve.Reading(used=used, resets_at=epoch(value.get("resets_at")), name=name, family=family,
                           started=not idle, merge=True)


def _scope(value):
    """(none | model | surface | both | other, the model display name) of a limits[] entry's scope;
    None when it is malformed."""
    if value is None:
        return "none", None
    if not isinstance(value, dict):
        return None
    parts = {}
    for key in ("model", "surface"):
        part = value.get(key)
        if part is None:
            continue
        if not isinstance(part, dict) or not isinstance(part.get("display_name"), str):
            return None
        parts[key] = part["display_name"]
    if any(item is not None for key, item in value.items() if key not in ("model", "surface")):
        return "other", None
    kind = {0: "none", 2: "both"}.get(len(parts)) or next(iter(parts))
    return kind, parts.get("model")


def _limit(entry):
    """(reading or None, unproved row name or None) of one limits[] entry."""
    if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
        return None, "limits_malformed"
    kind, scope = entry["kind"], _scope(entry.get("scope"))
    if scope is None:
        return None, "limits_malformed"
    where, display = scope
    if kind in LIMIT_WINDOWS and where == "none":
        return _reading(entry, "percent", LIMIT_WINDOWS[kind]), None
    if kind == SCOPED and where == "model":
        words = display.split()
        word = words[0].lower() if words else ""
        if word in FAMILIES:
            return _reading(entry, "percent", "weekly", family=word), None
        return None, (None if word in OTHER_FAMILIES else "limits_weekly_scoped_model_unknown")
    if kind in LIMIT_WINDOWS or kind == SCOPED:
        return None, "limits_%s_%s" % (kind, "unscoped" if where == "none" else where)
    return None, "limits_kind_" + (kind if KIND_NAME.fullmatch(kind) else "unknown")


def normalize_usage(document):
    """(readings, unproved row names) of one usage response, as the module docstring maps its rows. Every
    reading has merge: a window may be reported twice (a row and its limits[] entry)."""
    if not isinstance(document, dict):
        raise ValueError("not an object")
    readings, unproved = [], []
    for name, value in document.items():
        if name in GENERAL:
            if value is not None:  # missing: the evaluator reports an enabled window as unavailable
                readings.append(_reading(value, "utilization", GENERAL[name]))
        elif name in IGNORED or value is None or value == {} or value == []:
            continue
        elif name in FAMILY_ROWS:
            readings.append(_reading(value, "utilization", "weekly", family=FAMILY_ROWS[name]))
        elif name == "limits":
            if not isinstance(value, list):
                unproved.append("limits_malformed")
                continue
            for entry in value:
                reading, row = _limit(entry)
                if reading is not None:
                    readings.append(reading)
                if row is not None:
                    unproved.append(row)
        else:
            unproved.append(name if ROW_NAME.fullmatch(name) else "unknown")
    return tuple(readings), tuple(sorted(set(unproved)))


def normalize_profile(document):
    """(account UUID, organization UUID) of a profile response; None for any other shape."""
    try:
        pair = (document["account"]["uuid"], document["organization"]["uuid"])
    except (KeyError, TypeError):
        return None
    return pair if all(isinstance(value, str) and UUID_TEXT.fullmatch(value) for value in pair) else None


def family(model):
    """The quota family of a model id (opus, sonnet or haiku), or None when it names none or several."""
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        return None
    found = {token for token in model.split("-") if token in FAMILIES}
    return found.pop() if len(found) == 1 else None


class ClaudeProvider(gate.BaseProvider):
    name = "claude"
    routes = ROUTES
    usage_reads = (20, 60.0)  # two GETs per check: about ten checks a minute

    def __init__(self, config):
        upstream = config.get("upstream")
        self.upstream = gate.Upstream(**upstream) if upstream else API
        self.expected_identity = tuple(config["identity"])
        if len(self.expected_identity) != 2 or not all(isinstance(value, str) and UUID_TEXT.fullmatch(value)
                                                        for value in self.expected_identity):
            raise ValueError("identity")
        self.config_dir, self.platform = config["config_dir"], config["platform"]
        self.keychain_user, self.home = config.get("keychain_user"), config.get("home")
        self.security = config.get("security") or bearer.SECURITY  # another path only from the test driver
        # The tunnel address is the reviewed one; only the in-process test driver, which also sets the
        # upstream, may name another (a loopback fake).
        self.connect_allow = {target: (tuple(address) if upstream else REFRESH_TARGETS[target])
                              for target, address in (config.get("connect_allow") or {}).items()
                              if target in REFRESH_TARGETS or upstream}
        self.rebind_timeout = gate.READ_TIMEOUT
        self.families = {}  # family -> reserve.WindowStatus of its own weekly limit; set under the gate lock
        self.gate = None
        self.token_expires = None  # epoch seconds of the token the launch started with
        self.clock_ms = lambda: int(time.time() * 1000)

    # -- the store bearer -----------------------------------------------------------------------
    def credential(self):
        try:
            return bearer.read(self.config_dir, platform=self.platform, wrap=gate.Secret, now_ms=self.clock_ms(),
                               keychain_user=self.keychain_user, home=self.home, security=self.security)
        except bearer.Refused as error:
            raise gate.Refusal("credential_" + error.reason) from None

    def stored(self):
        return gate.Presented(self.credential().secret)

    # -- admission -------------------------------------------------------------------------------
    def check_model(self, presented, body):
        try:  # a duplicated key could read differently upstream, so it refuses
            model = json.loads(body, object_pairs_hook=_unique).get("model")
        except (ValueError, AttributeError, UnicodeDecodeError, RecursionError):
            model = None
        kind = family(model)
        if kind is None:
            raise gate.Refusal("model_unproved")
        own = self.families.get(kind)  # the latest attributed reading's, which the gate's lock orders
        if own is not None and own.status != reserve.OK:
            raise gate.Refusal("family_at_reserve" if own.status == reserve.AT_RESERVE else "family_window_unavailable")

    def refusal(self, reason):
        status, payload = gate.refusal_response(self.name, reason)
        if reason in REFUSAL_TEXT:
            message = "quota reserve: %s (%s)" % (REFUSAL_TEXT[reason], reason)
            payload = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                             "message": message}}).encode()
        return status, payload

    # -- quota reads -------------------------------------------------------------------------------
    def _get(self, gate_, path, presented, cause):
        # Every GET gets the gate's READ_TIMEOUT, as a poll's does: a rebind's each get rebind_timeout
        # (the same), the per-fetch bound its held requests wait for rebind_reads times plus
        # REBIND_SLACK, and a continuation's store read and two GETs stay within the launcher's
        # continuation wait (supervise.CONTINUE_TIMEOUT), so a check that can pass is never cut short.
        timeout = self.rebind_timeout if cause == "rebind" else None
        status, headers, body = gate_.fetch("GET", path, secret=presented.secret, timeout=timeout,
                                            headers=(("Accept", "application/json"), ("anthropic-beta", OAUTH_BETA)))
        if status != 200:
            raise gate.Unavailable("http_%d" % status if 100 <= status < 600 else "http_status")
        try:
            return _json(headers, body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise gate.Unavailable("response_malformed") from None

    def read_quota(self, gate_, presented, cause):
        lease = gate_.lease
        if cause == "poll" and lease is not None and lease.secret.matches(presented.secret):
            identity = lease.identity  # this bearer's identity was proved when the lease took it
        else:
            identity = normalize_profile(self._get(gate_, PROFILE_PATH, presented, cause))
            if identity != self.expected_identity:
                # Not the enrolled account's bearer (another account signed in, or an unattributed
                # profile): the gate pauses on the identity alone, so its usage is never read.
                return gate.Observation(0.0, True, identity=identity)
        document = self._get(gate_, USAGE_PATH, presented, cause)
        wall = time.time()
        try:
            readings, unproved = normalize_usage(document)
        except ValueError:
            raise gate.Unavailable("response_malformed") from None
        finally:
            document = None
        if unproved:  # the sanitized row names, never a value, so that the message can name them
            return gate.Observation(0.0, False, identity=identity, reason="unproved_quota_row", permanent=True,
                                    data={"rows": unproved})
        return gate.Observation(0.0, True, identity=identity, data={"readings": readings, "wall_at": wall})

    # -- lifecycle --------------------------------------------------------------------------------
    def startup(self, gate_):
        """Local refusals before the gate serves; the first quota check once the monitor is heard.

        This read is the check of the D1 pre-launch refresh, whose exit status says nothing about
        it: native renews its token only inside its refresh window, and once the session runs this
        gate refuses every renewal. A token that needed the refresh and did not get it is still
        inside the window (time only moves on), so it refuses (native_refresh_window); one that
        native left alone because it was just outside the window would end the session within
        minutes, so less than MIN_SESSION_MS beyond the window refuses too (token_expires_soon)."""
        self.gate = gate_
        credential = self.credential()
        if credential.subscription not in PERSONAL_PLANS:
            raise gate.Refusal("organization_policy")
        remaining = credential.expires_at_ms - self.clock_ms()
        if remaining <= NATIVE_REFRESH_WINDOW_MS:
            raise gate.Refusal("native_refresh_window")
        if remaining <= NATIVE_REFRESH_WINDOW_MS + MIN_SESSION_MS:
            raise gate.Refusal("token_expires_soon")
        self.token_expires = credential.expires_at_ms // 1000
        presented = gate.Presented(credential.secret)
        credential = None
        threading.Thread(target=self._first_check, args=(gate_, presented), daemon=True).start()

    def _first_check(self, gate_, presented):
        deadline = time.monotonic() + gate_.attach_grace + 1.0
        while gate_.last_beat is None and time.monotonic() < deadline:
            if gate_.closed or gate_.lost:
                return
            time.sleep(0.02)
        if gate_.last_beat is not None:
            gate_.check(presented, "startup")

    def poll(self, gate_):
        """The monitor's poll: the lease's bearer; a rejected or rotated one is re-read from the store
        and checked as a rebind (never as a poll, which cannot rebind). A paused gate without a lease
        (a restarted one until a check binds one, or one whose lease an identity pause dropped: another
        account signed in) checks the store's bearer as a rebind, with its profile read, which keeps
        the pause (only a continuation leaves it) but keeps its readings fresh, and never reads the
        signed-out bearer's quota."""
        self.gate = gate_
        with gate_.lock:
            ended = gate_.lost is not None or gate_.closed
            lease = None if ended else gate_.lease
            paused = gate_.state == "paused"
        if lease is None:
            if not ended and paused:
                try:
                    current = self.stored()
                except gate.Refusal:
                    return
                gate_.check(current, "rebind")
            return
        outcome = gate_.check(lease.presented, "poll")
        if outcome in ("http_401", "superseded"):
            try:
                current = self.stored()
            except gate.Refusal:
                return
            if not current.secret.matches(lease.presented.secret):
                gate_.check(current, "rebind")

    def continue_check(self, gate_):
        """A continuation checks the store's current bearer, which a refused attempt never keeps."""
        self.gate = gate_
        return gate_.check(self.stored(), "continuation")


class Decider:
    """decide(Event) -> Decision for the gate, through one reserve Evaluator per gate instance."""

    def __init__(self, policy, provider, max_age=reserve.MAX_AGE, clock=time.monotonic):
        self.policy, self.provider, self.real, self.max_age = policy, provider, clock, max_age
        self.override = None
        self.evaluator = reserve.Evaluator(policy, self.clock, max_age=max_age)
        self.up = False

    def clock(self):
        return self.real() if self.override is None else self.override

    def __call__(self, event):
        current = self.provider.gate
        if not self.up and current is not None and current.last_beat is not None:
            self.evaluator.control_up()
            self.up = True
        evaluator = self.evaluator
        if event.kind == "observation":
            data = event.observation.data
            verdict = evaluator.observe(reserve.Observation(tuple(data["readings"]), event.at, data["wall_at"], True))
        elif event.kind == "continuation":
            self.override = event.at - 1e-6  # armed when the gate armed it, before the read that answers
            try:
                verdict = evaluator.continuation()
            finally:
                self.override = None
        elif event.name == "gate_restart":
            self.evaluator = evaluator = reserve.Evaluator(self.policy, self.clock, paused="gate_restarted",
                                                           max_age=self.max_age)
            self.up = False
            verdict = evaluator.verdict()
        elif event.name == "stale":
            verdict = evaluator.tick()
        elif event.name == "quota_unavailable":
            observation = event.observation
            reason = observation.reason if isinstance(observation.reason, str) and REASON.fullmatch(observation.reason) \
                else "quota_unavailable"
            data = observation.data if isinstance(observation.data, dict) else {}
            verdict = (self._fault(reason, data.get("rows") or ()) if observation.permanent
                       else evaluator.failure(reason, at=event.at))
        elif event.name in CONTROL:
            verdict = evaluator.control_down(CONTROL[event.name])
        elif event.name in IDENTITY:
            verdict = evaluator.fault("identity_changed")
        else:
            verdict = self._fault(event.name if isinstance(event.name, str) else "gate_fault")
        return self._decision(verdict)

    def _fault(self, reason, names=()):
        try:
            return self.evaluator.fault(reason, names=names)
        except ValueError:
            pass
        try:
            return self.evaluator.fault(reason)  # names the evaluator refuses never cost the reason
        except ValueError:
            return self.evaluator.fault("gate_fault")

    def _decision(self, verdict):
        # Called under the gate's lock, as check_model is: a family is judged by the latest verdict.
        self.provider.families = {status.family: status for status in verdict.families}
        fields = {"account": verdict.account}
        at = [window for window in verdict.windows if window.status == reserve.AT_RESERVE]
        if at:
            window = at[0]
            fields.update(window=reserve.LABELS[window.window], remaining=reserve.percent(window.remaining, floor=True),
                          reserve=reserve.percent(window.reserve))
            if window.resets_at is not None:  # None: a window not started yet
                fields["reset"] = reserve.timestamp(window.resets_at)
        if verdict.names:  # what an unproved_quota_row pause names, bounded as the message is
            fields["rows"] = reserve.listed(verdict.names)
        if verdict.last_fresh is not None:
            fields["last_fresh"] = reserve.timestamp(verdict.last_fresh)
        if self.provider.token_expires is not None:
            fields["token_expires"] = reserve.timestamp(self.provider.token_expires)
        fields["message"] = verdict.message()[:1000]
        running = verdict.decision == "run"
        reason = None if running else (verdict.reason or ("awaiting_continuation" if verdict.eligible else verdict.cause)
                                       or verdict.decision)
        return gate.Decision(running, reason, fields, next_poll=self.evaluator.next_poll(random.uniform))


def policy_of(config):
    settings = {}
    for window in reserve.WINDOWS:
        value = config["reserves"].get(window)
        source = (config.get("sources") or {}).get(window, "invocation")
        settings[window] = reserve.Setting(window, None if value is None else Decimal(str(value)),
                                           source if source in reserve.SOURCES else "invocation")
    return reserve.Policy("claude-as", str(config["account"]), **settings)


def make(config):
    provider = ClaudeProvider(config)
    return provider, Decider(policy_of(config), provider, max_age=float(config["max_age_s"]))


if __name__ == "__main__":
    raise SystemExit(gate.main(sys.argv[1:], make))
