"""Fabricated identities and a loopback fake ChatGPT backend for QX (no real service).

The fake upstream behaves like a server that derives identity from the bearer:
usage answers for the token's account/user, model requests need a live token,
and the token endpoint rotates access tokens. Only fixture values exist here.
"""
import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time

PLAN = 'pro'


def jwt(payload):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    return encode({'alg': 'none', 'typ': 'JWT'}) + '.' + encode(payload) + '.fixture-signature'


def token(account, user, lifetime=3600, plan=PLAN, now=None):
    """A fabricated access/id token; every call yields a distinct value."""
    now = int(time.time()) if now is None else now
    return jwt({'email': user + '@example.invalid', 'sub': user, 'iat': now, 'exp': now + lifetime,
                'jti': base64.urlsafe_b64encode(os.urandom(9)).decode(),
                'https://api.openai.com/auth': {'chatgpt_account_id': account, 'chatgpt_user_id': user,
                                                'chatgpt_plan_type': plan}})


def auth_json(access, refresh, account, user, plan=PLAN):
    """A native file-store login record built only from fabricated values."""
    return json.dumps({'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
                       'tokens': {'id_token': token(account, user, plan=plan), 'access_token': access,
                                  'refresh_token': refresh, 'account_id': account},
                       'last_refresh': datetime.now(timezone.utc).isoformat()})


def usage_body(account, user, five=25, weekly=60, plan=PLAN, wall=None, swap=False, **extra):
    wall = int(time.time()) if wall is None else wall
    slots = [{'used_percent': five, 'limit_window_seconds': 18000, 'reset_after_seconds': 9000,
              'reset_at': wall + 9000},
             {'used_percent': weekly, 'limit_window_seconds': 604800, 'reset_after_seconds': 302400,
              'reset_at': wall + 302400}]
    if swap:
        slots.reverse()
    body = {'account_id': account, 'user_id': user, 'plan_type': plan,
            'rate_limit': {'allowed': True, 'limit_reached': False,
                           'primary_window': slots[0], 'secondary_window': slots[1]}}
    body.update(extra)
    return body


def sse(text, response_id='fixture-response'):
    events = [{'type': 'response.created', 'response': {'id': response_id}},
              {'type': 'response.output_item.done',
               'item': {'type': 'message', 'role': 'assistant', 'id': 'msg-' + response_id,
                        'content': [{'type': 'output_text', 'text': text}]}},
              {'type': 'response.completed',
               'response': {'id': response_id,
                            'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]
    return b''.join(('data: ' + json.dumps(event) + '\n\n').encode() for event in events)


class Upstream:
    """Fake backend: tokens -> identity; usage, responses, models and the token endpoint."""

    def __init__(self):
        self.lock = threading.Lock()
        self.identities = {}  # access token -> {'account', 'user', 'plan', 'revoked'}
        self.refreshes = {}  # refresh token -> (account, user)
        self.events = []
        self.usage_override = None  # callable(identity) -> (status, bytes) or None
        self.response_headers = {}  # extra headers for model responses
        self.held = False  # True: model responses stay open until hold is set
        self.hold = threading.Event()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.hold.set()
        self.server.shutdown()
        self.server.server_close()

    def issue(self, account, user, lifetime=3600, plan=PLAN):
        access, refresh = token(account, user, lifetime, plan), 'fixture-refresh-' + os.urandom(6).hex()
        with self.lock:
            self.identities[access] = {'account': account, 'user': user, 'plan': plan, 'revoked': False}
            self.refreshes[refresh] = (account, user)
        return access, refresh

    def revoke(self, access):
        with self.lock:
            self.identities[access]['revoked'] = True

    def count(self, kind):
        with self.lock:
            return sum(event['kind'] == kind for event in self.events)

    def _identity(self, headers):
        value = headers.get('Authorization') or ''
        access = value[7:] if value.startswith('Bearer ') else None
        with self.lock:
            found = self.identities.get(access)
            return (access, dict(found)) if found and not found['revoked'] else (access, None)

    def _handler(self):
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, content_type='application/json', headers=()):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def note(self, kind, **data):
                with upstream.lock:
                    upstream.events.append({'kind': kind, 'path': self.path, **data})

            def do_GET(self):
                access, identity = upstream._identity(self.headers)
                if self.path.split('?')[0].endswith(('/wham/usage', '/api/codex/usage')):
                    self.note('usage', live=identity is not None)
                    if identity is None:
                        return self.reply(401, {'error': {'code': 'token_expired'}})
                    override = upstream.usage_override and upstream.usage_override(identity)
                    if override:
                        return self.reply(override[0], override[1])
                    return self.reply(200, usage_body(identity['account'], identity['user'], plan=identity['plan']))
                if self.path.split('?')[0].endswith('/models'):
                    self.note('models')
                    return self.reply(200, {'models': []})
                self.note('other_get')
                self.reply(503, {'error': 'fixture has no such endpoint'})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path.split('?')[0].endswith('/oauth/token'):
                    try:
                        request = json.loads(raw)
                    except ValueError:
                        request = {}
                    with upstream.lock:
                        owner = upstream.refreshes.pop(request.get('refresh_token'), None)
                    self.note('refresh', accepted=owner is not None)
                    if owner is None:
                        return self.reply(401, {'error': 'invalid_grant'})
                    access, refresh = upstream.issue(*owner)
                    return self.reply(200, {'access_token': access, 'refresh_token': refresh,
                                            'id_token': token(*owner)})
                if self.path.split('?')[0].endswith('/responses'):
                    access, identity = upstream._identity(self.headers)
                    self.note('model', live=identity is not None)
                    if identity is None:
                        return self.reply(401, {'error': {'code': 'token_expired', 'message': 'fixture'}})
                    headers = list(upstream.response_headers.items())
                    if not upstream.held:
                        return self.reply(200, sse('fixture reply'), 'text/event-stream', headers)
                    # A held stream: one event, then heartbeats until released or disconnected.
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    try:
                        self.wfile.write(b'data: {"type":"response.created","response":{"id":"held"}}\n\n')
                        self.wfile.flush()
                        while not upstream.hold.wait(.05):
                            self.wfile.write(b': fixture heartbeat\n\n')
                            self.wfile.flush()
                    except OSError:
                        self.note('model_stream_lost')
                    return
                self.note('other_post')
                self.reply(503, {'error': 'fixture has no such endpoint'})

            def do_CONNECT(self):
                self.note('connect')
                self.send_error(403)

        return Handler
