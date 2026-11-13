"""codex-as protected mode: the Codex gate provider, one gate process per protected invocation.

The supervisor (supervise.py) starts it as `python3 -I -B gate.py GATE-OPTIONS`; the kit gate
(lib/coding-agent-kit/gate.py) documents the options and the core configuration keys. The
launcher (protect.py) adds: account (the codex-as name), expected ([account_id, user_id] of the
launch credential), reserves ({window: [value or null, source]}), native (a reviewed release),
sqlite_home, mode ("run" or "refresh") and, for the refresh gate, connect_allow.

Routes (codex 0.167.2, default deny, upstream chatgpt.com over verified TLS):
- POST /backend-api/codex/responses: a model request, admitted only under a fresh lease whose
  bearer it carries, for a reviewed codex-family model;
- GET /backend-api/wham/usage: forwarded while running, checking and paused; its reply is the
  quota observation (account_id and user_id must equal the launch claims, plan_type a reviewed
  plan, windows mapped by their duration, never by slot);
- GET /backend-api/wham/config/bundle (D14): forwarded unchanged while running, checking and
  paused, never fabricated; a bundle whose requirements forbid protected mode (protect.py
  BLOCKERS) pauses, or refuses at startup;
- GET models, accounts/check, settings/user and rate-limit-reset-credits: forwarded while running.
A refresh gate forwards the config bundle alone (native needs it before any managed request on a
workspace plan, its refresh included) and tunnels only to auth.openai.com:443.

Every request must carry exactly one Authorization bearer and one ChatGPT-Account-ID equal to the
expected account, and no other credential; a bearer whose JWT claims name an account must name the
same one. Refusals are 503, never 401 (a 401 would start a native refresh).
"""

from decimal import Decimal
import base64
import importlib.util
import json
import os
from pathlib import Path
import random
import re
import sys
import time


HERE = Path(os.path.realpath(__file__)).parent
KIT = HERE.parent / "coding-agent-kit"


def load(name, path):
    spec = importlib.util.spec_from_file_location("codex_as_gate_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


kit = load("kit_gate", KIT / "gate.py")
reserve = load("reserve", KIT / "reserve.py")
protect = load("protect", HERE / "protect.py")

UPSTREAM = kit.Upstream("chatgpt.com", 443)
MODEL_PATH = "/backend-api/codex/responses"
USAGE_PATH = "/backend-api/wham/usage"
BUNDLE_PATH = "/backend-api/wham/config/bundle"
BUNDLE_LIMIT = 256 * 1024
BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")  # one RFC 6750 token: no second one after a space
CLAIMS = "https://api.openai.com/auth"
DURATIONS = {300 * 60: "five_hour", 10080 * 60: "weekly"}


def routes(native, refresh):
    """The default-deny route table of a reviewed release; a refresh gate's has the bundle alone."""
    bundle = kit.Route("GET", BUNDLE_PATH, kit.USAGE, limit=BUNDLE_LIMIT)
    if refresh:
        return kit.RouteTable("codex-%s-refresh" % native.name, (bundle,))
    return kit.RouteTable("codex-%s" % native.name, (
        kit.Route("POST", MODEL_PATH, kit.MODEL),
        kit.Route("GET", USAGE_PATH, kit.USAGE),
        bundle,
        kit.Route("GET", "/backend-api/codex/models", kit.READ, query=True),
        kit.Route("GET", "/backend-api/wham/accounts/check", kit.READ),
        kit.Route("GET", "/backend-api/wham/settings/user", kit.READ),
        kit.Route("GET", "/backend-api/wham/rate-limit-reset-credits", kit.READ),
    ))


def bearer_account(token):
    """The chatgpt_account_id claim of a JWT bearer (decoded, never verified), or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeError, RecursionError):
        return None
    auth = payload.get(CLAIMS) if isinstance(payload, dict) else None
    account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return account if isinstance(account, str) and account else None


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def readings(rate_limit):
    """reserve.Readings of the codex family's two windows, by their stated duration."""
    found = []
    for slot in ("primary_window", "secondary_window"):
        window = rate_limit.get(slot)
        if window is None:
            continue
        if not isinstance(window, dict):
            found.append(reserve.Reading(None, None, None, None))
            continue
        seconds = window.get("limit_window_seconds")
        found.append(reserve.Reading(number(window.get("used_percent")), number(window.get("reset_at")),
                                     duration=seconds if number(seconds) else None))
    return tuple(found)


class CodexProvider(kit.BaseProvider):
    name = "codex"
    upstream = UPSTREAM
    family = "codex"
    rebind_timeout = reserve.REBIND_HOLD
    usage_reads = (reserve.USAGE_READS_PER_MINUTE, 60.0)
    revalidations = (reserve.REVALIDATIONS_PER_MINUTE, 60.0)
    continuation_window = reserve.CONTINUATION_WINDOW

    def __init__(self, account, user, native, sqlite_home, refresh=False, connect_allow=None, upstream=None):
        self.account, self.user, self.native, self.sqlite_home = account, user, native, sqlite_home
        self.expected_identity = (account, user)
        self.routes = routes(native, refresh)
        self.connect_allow = {target: tuple(address) for target, address in (connect_allow or {}).items()}
        if upstream is not None:
            self.upstream = upstream
        self.own_rows = frozenset()  # names of quota rows other than the codex family's

    def present(self, route, headers):
        refuse = lambda reason: kit.Refusal(reason, route.kind == kit.MODEL)
        auth = kit.values(headers, "authorization")
        accounts = kit.values(headers, "chatgpt-account-id")
        if (len(auth) != 1 or not auth[0].startswith("Bearer ") or kit.values(headers, "x-api-key")
                or kit.values(headers, "proxy-authorization") or kit.values(headers, "x-openai-fedramp")
                or len(accounts) > 1):
            raise refuse("selector_conflict")
        token = auth[0][7:]
        if not BEARER.fullmatch(token):
            raise refuse("selector_conflict")
        if not accounts or accounts[0] != self.account:
            raise refuse("foreign_account")
        claimed = bearer_account(token)
        if claimed is not None and claimed != self.account:
            raise refuse("foreign_account")
        try:
            return kit.Presented(kit.Secret(token), (self.account,))
        except ValueError:
            raise refuse("selector_conflict") from None

    def check_model(self, presented, body):
        try:
            request = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            request = None
        slug = request.get("model") if isinstance(request, dict) else None
        if not isinstance(slug, str) or slug not in self.native.models or slug in self.own_rows:
            raise kit.Refusal("model_not_covered")

    def read_quota(self, gate, presented, cause):
        status, _, body = gate.fetch("GET", USAGE_PATH, secret=presented.secret,
                                     headers=(("ChatGPT-Account-ID", self.account), ("Accept", "application/json")),
                                     timeout=self.rebind_timeout if cause == "rebind" else None)
        return self.usage(status, body, 0.0)

    def observe_forwarded(self, route, presented, status, headers, body, received_at):
        if route.path == BUNDLE_PATH:
            return self.bundle(status, body, received_at)
        return self.usage(status, body, received_at)

    def usage(self, status, body, at):
        """The Observation of one usage reply (never zero usage for missing data)."""
        if status != 200:
            return kit.Observation(at, False, reason="http_%d" % status if 100 <= status < 600 else "malformed_reply")
        try:
            data = json.loads(body, parse_constant=lambda _: None)
        except (ValueError, UnicodeError, RecursionError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("plan_type"), str):
            return kit.Observation(at, False, reason="malformed_reply")
        account, user = data.get("account_id"), data.get("user_id")
        identity = (account, user) if isinstance(account, str) and isinstance(user, str) else None
        if data["plan_type"] not in self.native.plans:
            return kit.Observation(at, False, identity=identity, reason="unsupported_plan", permanent=True)
        rate_limit = data.get("rate_limit")
        rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
        rows = data.get("additional_rate_limits")
        if identity == self.expected_identity:
            names = set()
            for row in rows if isinstance(rows, list) else ():
                if isinstance(row, dict):
                    names |= {row.get(key) for key in ("limit_name", "normal_model_slug", "metered_feature")
                              if isinstance(row.get(key), str)}
            self.own_rows = frozenset(names)
        limited = rate_limit.get("limit_reached") is True or rate_limit.get("allowed") is False
        return kit.Observation(at, True, identity=identity, family="codex",
                               data={"readings": readings(rate_limit), "wall": time.time(), "limit_reached": limited})

    def bundle(self, status, body, at):
        """Nothing for a bundle protected mode can run with (or a failed fetch: native then refuses
        managed traffic itself); a permanent refusal for one whose requirements forbid it."""
        if status != 200:
            return None
        try:
            document = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            document = None
        found = protect.bundle_blockers(document, self.sqlite_home)
        if not found:
            return None
        return kit.Observation(at, False, reason="requirements_" + sorted(found)[0], permanent=True)


class Decider:
    """The gate's decide(Event) on the reserve evaluator, one per gate instance (reserve.py).

    The gate admits no request and arms no continuation without a heard, current heartbeat, and
    the launcher starts the preflight only after the first one, so every event but a control loss
    reports control_up (which never clears a loss). A restarted gate's first event is health
    "gate_restart": its evaluator then starts paused."""

    def __init__(self, policy, clock=time.monotonic):
        self.policy, self.clock, self.at = policy, clock, None
        self.evaluator = None

    def now(self):
        return self.clock() if self.at is None else self.at

    def __call__(self, event):
        if self.evaluator is None:
            restart = event.kind == "health" and event.name == "gate_restart"
            self.evaluator = reserve.Evaluator(self.policy, self.now, paused="gate_restarted" if restart else None,
                                               max_age=protect.MAX_AGE)
            if restart:
                return self.decision(self.evaluator.verdict())
        evaluator = self.evaluator
        if not (event.kind == "health" and event.name in LOSSES):
            evaluator.control_up()
        if event.kind == "continuation":
            self.at = event.at  # the evaluator arms at the gate's arming time
            try:
                verdict = evaluator.continuation()
            finally:
                self.at = None
        elif event.kind == "observation":
            observation = event.observation
            data = observation.data if isinstance(observation.data, dict) else {}
            fresh = reserve.Observation(tuple(data.get("readings") or ()), event.at,
                                        data.get("wall", time.time()), True)
            verdict = evaluator.observe(fresh)
            if data.get("limit_reached"):
                verdict = evaluator.fault("limit_reached")
        else:
            verdict = self.health(event)
        return self.decision(verdict)

    def health(self, event):
        evaluator, name = self.evaluator, event.name
        if name in LOSSES:
            return evaluator.control_down(LOSSES[name])
        if name == "stale":
            return evaluator.tick()
        if name == "quota_unavailable":
            observation = event.observation
            reason = word(getattr(observation, "reason", None) or "quota_unavailable")
            if getattr(observation, "permanent", False):
                return evaluator.fault(reason)
            return evaluator.failure(reason, at=event.at)
        if name == "identity_unattributed":
            return evaluator.observe(reserve.Observation((), event.at, time.time(), False))
        if name in ("identity_mismatch", "bearer_identity_mismatch", "foreign_account"):
            return evaluator.fault("identity_changed")
        return evaluator.fault(word(name))

    def decision(self, verdict):
        evaluator = self.evaluator
        reason = verdict.reason
        if reason is None or (reason in reserve.WAITING and verdict.cause):  # what paused, not what waits
            reason = verdict.cause or ("awaiting_continuation" if verdict.eligible else None)
        if verdict.eligible:
            reason = "awaiting_continuation"
        return kit.Decision(verdict.decision == "run", None if verdict.decision == "run" else reason,
                            fields(verdict), evaluator.next_poll(jitter=random.uniform))


RESERVED = reserve.OWN_CONDITIONS | frozenset(reserve.CONTROL_TEXT)
# The gate's loss events and the evaluator's control_down() reasons for them.
LOSSES = {"monitor_stale": "monitor_stale", "monitor_eof": "monitor_lost", "launcher_eof": "launcher_lost",
          "gate_error": "gate_error"}


def word(reason):
    """A reason as the evaluator accepts it for a fault or failure: a short lower-case identifier
    that names none of its own conditions."""
    text = re.sub(r"[^a-z0-9_]", "_", str(reason).lower())[:60].strip("_") or "refused"
    if not text[0].isalpha():
        text = "r_" + text
    return "gate_" + text if text in RESERVED else text


def fields(verdict):
    """Message fields for the supervisor's pause line (account, window, remaining, reserve, reset,
    last fresh check) and the evaluator's own deterministic line as detail."""
    result = {"account": verdict.account, "detail": verdict.message()[:1000]}
    if verdict.last_fresh is not None:
        result["last_fresh"] = reserve.timestamp(verdict.last_fresh)
    shown = [w for w in verdict.windows if w.status == reserve.AT_RESERVE] or \
        [w for w in verdict.windows if w.status not in (reserve.OK, reserve.AT_RESERVE)]
    if shown:
        window = shown[0]
        result["window"] = reserve.LABELS[window.window]
        result["reserve"] = reserve.percent(window.reserve)
        if window.remaining is not None:
            result["remaining"] = reserve.percent(window.remaining, floor=True)
        if window.resets_at is not None:
            result["reset"] = reserve.timestamp(window.resets_at)
    return result


def refusing(event):
    """A refresh gate's evaluator: it has no lease, so any observation it applies pauses it."""
    name = getattr(event.observation, "reason", None) or event.name or "refused"
    return kit.Decision(False, word(name))


class CodexGate(kit.Gate):
    """A refresh gate also forwards USAGE-kind reads, of which its route table has only the config
    bundle: on a workspace plan native refuses every managed request, its refresh included, until it
    has a bundle."""

    def read_allowed(self, kind):
        if self.mode == "refresh":
            return not self.closed and self.lost is None and self.state == "refresh" and kind == kit.USAGE
        return super().read_allowed(kind)


def policy(config):
    settings = {}
    for window in reserve.WINDOWS:
        value, source = config["reserves"][window]
        settings[window] = reserve.Setting(window, None if value is None else Decimal(value), source)
    return reserve.Policy("codex-as", config["account"], **settings)


def make(config):
    native = protect.BY_NAME[config["native"]]
    refresh = config.get("mode") == "refresh"
    account, user = config["expected"]
    if not (isinstance(account, str) and account and isinstance(user, str) and user):
        raise kit.Refusal("expected_identity_missing")
    provider = CodexProvider(account, user, native, config["sqlite_home"], refresh=refresh,
                             connect_allow=config.get("connect_allow") if refresh else None)
    return provider, (refusing if refresh else Decider(policy(config)))


if __name__ == "__main__":
    kit.Gate = CodexGate  # kit.main() builds its gate through this module name
    raise SystemExit(kit.main(sys.argv[1:], make))
