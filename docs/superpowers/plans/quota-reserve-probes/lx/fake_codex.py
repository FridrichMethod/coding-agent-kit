"""Fake `codex` for LX tests: `--version` and a minimal stdio app-server (test-only).

It behaves like native 0.167.2 where the probe depends on it: `-c` overrides
select chatgpt_base_url, account/rateLimits/read reads the linked auth.json and
sends one GET to <chatgpt_base_url>wham/usage with the bearer and account
header, and the RPC result mirrors native's shape (including accountId and an
error message that quotes the response body, which the probe must not keep).
Like native, it builds the User-Agent it sends upstream from the initialize
clientInfo name. FAKE_CODEX_MODE (comma separated) adds misbehavior for tests;
FAKE_CODEX_REPORT names a JSON-lines file for what it saw. Loopback only.
"""
import base64
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

MODES = set(filter(None, os.environ.get('FAKE_CODEX_MODE', '').split(',')))
REPORT = os.environ.get('FAKE_CODEX_REPORT')
CLIENT = {'name': 'fake-unset'}  # replaced by the initialize clientInfo
FOREIGN_ACCOUNT, FOREIGN_USER = 'fixture-account-lx-b', 'fixture-user-lx-b'


def report(event, **data):
    if REPORT:
        with open(REPORT, 'a') as stream:
            stream.write(json.dumps({'event': event, **data}) + '\n')


def claims(jwt):
    part = jwt.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


def overrides(argv):
    values = {}
    for index, arg in enumerate(argv):
        if arg == '-c' and index + 1 < len(argv):
            key, _, value = argv[index + 1].partition('=')
            values[key] = value
    return values


def request(url, method, path, headers, body=None):
    target = urlsplit(url)
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def connect_via_proxy(proxy, destination):
    target = urlsplit(proxy)
    with socket.create_connection((target.hostname, target.port), timeout=5) as sock:
        sock.sendall(('CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (destination, destination)).encode())
        reply = sock.recv(256).split(b'\r\n', 1)[0].decode(errors='replace')
        if ' 200 ' in reply + ' ':
            sock.sendall(b'fixture tunnel bytes')
            echoed = sock.recv(64)
            return reply, len(echoed)
        return reply, 0


def rewrite_credential(home):
    """Native-style atomic refresh through the account link: same identity, new access token."""
    link = Path(home) / 'auth.json'
    target = Path(os.path.realpath(link))
    data = json.loads(target.read_text())
    payload = claims(data['tokens']['access_token'])
    payload['jti'] = base64.urlsafe_b64encode(os.urandom(9)).decode()
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    data['tokens']['access_token'] = encode({'alg': 'none'}) + '.' + encode(payload) + '.fixture-signature'
    staged = target.parent / ('.auth-fixture-' + os.urandom(4).hex())
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, json.dumps(data).encode())
    os.close(fd)
    os.replace(staged, target)


def unsigned(payload):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    return encode({'alg': 'none'}) + '.' + encode(payload) + '.fixture-signature'


def usage_read(config, home, index):
    data = json.loads((Path(home) / 'auth.json').read_text())
    tokens = data['tokens']
    base = config['chatgpt_base_url'].strip('"')
    bearer, header_account = tokens['access_token'], tokens['account_id']
    if 'foreign-bearer' in MODES:  # a bearer of another login (account claim B), header of the owner
        bearer = unsigned({'exp': int(time.time()) + 3600, 'https://api.openai.com/auth': {
            'chatgpt_account_id': FOREIGN_ACCOUNT, 'chatgpt_user_id': FOREIGN_USER}})
    if 'foreign-header' in MODES:  # the owner's bearer, a workspace header for another account
        header_account = FOREIGN_ACCOUNT
    headers = {'Authorization': 'Bearer ' + bearer, 'ChatGPT-Account-ID': header_account,
               'Accept': 'application/json', 'User-Agent': '%s/0.167.2 (fake; linux)' % CLIENT['name']}
    path = urlsplit(base).path + 'wham/usage'
    status, body = request(base, 'GET', path, headers)
    report('usage_get', index=index, status=status)
    if 'triple' in MODES:
        extra, _ = request(base, 'GET', path, headers)
        report('usage_get_extra', index=index, status=extra)
    if 'model-post' in MODES:
        provider = config['model_providers.lxgate.base_url'].strip('"')
        posted, _ = request(provider, 'POST', urlsplit(provider).path + '/responses',
                            dict(headers, **{'Content-Type': 'application/json'}),
                            json.dumps({'model': 'gpt-fixture', 'input': []}).encode())
        report('model_post', status=posted)
    if status != 200:
        # Native quotes status and body in its error (backend-client decode/exec errors).
        return None, {'code': -32603, 'message': 'failed to fetch codex rate limits: unexpected status %d; body=%s; '
                                                 'bearer=%s' % (status, body.decode(errors='replace'), tokens['access_token'])}
    payload = json.loads(body)
    user = claims(tokens['id_token'])['https://api.openai.com/auth']['chatgpt_user_id']

    def window(raw):
        if not raw:
            return None
        return {'usedPercent': raw['used_percent'], 'windowDurationMins': -(-raw['limit_window_seconds'] // 60),
                'resetsAt': raw['reset_at']}
    rate = payload.get('rate_limit') or {}
    snapshot = {'limitId': 'codex', 'limitName': None, 'normalModelSlug': None, 'primary': window(rate.get('primary_window')),
                'secondary': window(rate.get('secondary_window')), 'credits': None, 'individualLimit': None,
                'spendControlReached': None, 'planType': payload.get('plan_type'), 'rateLimitReachedType': None}
    matches = payload.get('account_id') == tokens['account_id'] and payload.get('user_id') == user
    return {'ordinaryUsageAllowed': rate.get('allowed') if matches else None, 'rateLimits': snapshot,
            'rateLimitsByLimitId': {'codex': snapshot}, 'rateLimitResetCredits': None,
            'accountId': payload.get('account_id'), 'rateLimitUpsell': None}, None


def send(message):
    sys.stdout.write(json.dumps(message) + '\n')
    sys.stdout.flush()


def app_server(argv):
    config = overrides(argv)
    home = os.environ['CODEX_HOME']
    fds = {}
    for name in os.listdir('/proc/self/fd'):
        try:
            fds[name] = os.readlink('/proc/self/fd/' + name)
        except OSError:
            pass
    report('start', overrides=config, env=sorted(os.environ), codex_home=home,
           proxies={key: os.environ.get(key) for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')},
           lock_fds=[path for path in fds.values() if path.endswith('.lock')], cwd=os.getcwd(),
           tmpdir=os.environ.get('TMPDIR'))
    if 'grandchild' in MODES:
        child = subprocess.Popen(['/bin/sleep', '300'])
        report('grandchild', pid=child.pid)
    if 'ignore-term' in MODES:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if 'unsolicited' in MODES:
        base = config['chatgpt_base_url'].strip('"')
        data = json.loads((Path(home) / 'auth.json').read_text())['tokens']
        status, _ = request(base, 'GET', urlsplit(base).path + 'wham/usage',
                            {'Authorization': 'Bearer ' + data['access_token'], 'ChatGPT-Account-ID': data['account_id']})
        report('unsolicited', status=status)
    if 'connect' in MODES:
        for destination in ('auth.openai.com:443', 'evil.example:443'):
            reply, echoed = connect_via_proxy(os.environ['HTTPS_PROXY'], destination)
            report('connect', destination=destination, reply=reply, echoed=echoed)
    reads = 0
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get('method')
        if method == 'initialize':
            info = (message.get('params') or {}).get('clientInfo')
            report('initialize', client_info=info)
            if isinstance(info, dict) and isinstance(info.get('name'), str):
                CLIENT['name'] = info['name']
            if 'crash-init' in MODES:
                os._exit(7)
            # Native's reply to the client only: the host name here tests that the probe keeps nothing of it.
            send({'id': message['id'], 'result': {'userAgent': '%s/0.167.2 (fake; %s)' % (CLIENT['name'], socket.gethostname()),
                                                  'codexHome': home, 'platformFamily': 'unix', 'platformOs': 'linux'}})
            # Native notifications can carry the host name and an installation id.
            send({'method': 'remoteControl/status/changed', 'params': {
                'status': 'disabled', 'serverName': socket.gethostname(),
                'installationId': '00000000-0000-4000-8000-00000000f1c7'}})
            if 'server-request' in MODES:
                send({'id': 'srv-1', 'method': 'account/chatgptAuthTokens/refresh', 'params': {'reason': 'fixture'}})
        elif method == 'account/rateLimits/read':
            reads += 1
            if 'refresh' in MODES and reads == 2:
                rewrite_credential(home)
                report('refreshed')
            result, error = usage_read(config, home, reads)
            send({'id': message['id'], 'error': error} if error else {'id': message['id'], 'result': result})
        elif 'id' in message and method:
            send({'id': message['id'], 'error': {'code': -32601, 'message': 'fake does not implement ' + method}})
        elif 'id' in message:
            report('client_response', id=message['id'], error='error' in message)
    report('stdin_closed')
    if 'ignore-stdin-eof' in MODES:
        while True:
            time.sleep(1)


def main(argv):
    if '--version' in argv:
        report('version')  # lets a test prove an unreviewed binary is never executed
        print('codex-cli ' + os.environ.get('FAKE_CODEX_VERSION', '0.167.2'))
        return 0
    if 'app-server' in argv:
        app_server(argv)
        return 0
    print('fake codex: unsupported invocation', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
