"""Fake loopback gate for the NM native cases (fabricated replies only).

One port on 127.0.0.1 and ::1 serves the Claude Messages path and the Codex
discovery/usage/models/responses paths, because the profile under test admits
exactly one gate port. It is a fixture, not a quota gate or relay: nothing is
forwarded anywhere. The first model turn that carries the NM_TOOL_TURN marker
receives one native shell tool call running the configured egress command.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _sse(handler, rows, named):
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.end_headers()
    for row in rows:
        prefix = ('event: ' + row['type'] + '\n') if named else ''
        handler.wfile.write((prefix + 'data: ' + json.dumps(row) + '\n\n').encode())
    handler.wfile.flush()


def claude_text(model, text):
    return [
        {'type': 'message_start', 'message': {'id': 'msg_nm', 'type': 'message', 'role': 'assistant',
         'model': model, 'content': [], 'stop_reason': None, 'stop_sequence': None,
         'usage': {'input_tokens': 10, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
         'usage': {'output_tokens': 3}},
        {'type': 'message_stop'}]


def claude_bash(model, command):
    return [
        {'type': 'message_start', 'message': {'id': 'msg_nm_tool', 'type': 'message', 'role': 'assistant',
         'model': model, 'content': [], 'stop_reason': None, 'stop_sequence': None,
         'usage': {'input_tokens': 10, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {
         'type': 'tool_use', 'id': 'toolu_nm', 'name': 'Bash', 'input': {}}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json':
         json.dumps({'command': command, 'description': 'NM fixture egress probe', 'timeout': 60000})}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None},
         'usage': {'output_tokens': 10}},
        {'type': 'message_stop'}]


def codex_call(ident, tools, command):
    names = {t.get('name'): t for t in tools}
    if 'exec_command' in names:
        call = {'name': 'exec_command', 'arguments': json.dumps({'cmd': command, 'yield_time_ms': 30000})}
    elif 'shell' in names:
        call = {'name': 'shell', 'arguments': json.dumps({'command': ['/bin/sh', '-c', command],
                                                          'timeout_ms': 60000})}
    else:
        return None
    return [{'type': 'response.created', 'response': {'id': 'nm-' + ident}},
            {'type': 'response.output_item.done', 'item': {'type': 'function_call',
             'call_id': 'call-nm-' + ident, **call}},
            {'type': 'response.completed', 'response': {'id': 'nm-' + ident,
             'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]


def codex_text(ident, text):
    return [{'type': 'response.created', 'response': {'id': 'nm-' + ident}},
            {'type': 'response.output_item.done', 'item': {'type': 'message', 'role': 'assistant',
             'id': 'msg-nm-' + ident, 'content': [{'type': 'output_text', 'text': text}]}},
            {'type': 'response.completed', 'response': {'id': 'nm-' + ident,
             'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _text(value.get('text', value.get('content', value.get('output', ''))))
    if isinstance(value, list):
        return ' '.join(_text(v) for v in value)
    return ''


def last_tool_output(body):
    """First 600 characters of the newest tool result in a request, or None."""
    for message in reversed(body.get('messages', [])[-1:]):
        for block in message.get('content', []) if isinstance(message.get('content'), list) else []:
            if isinstance(block, dict) and block.get('type') == 'tool_result':
                return _text(block.get('content'))[:600]
    items = body.get('input') or []
    if items and isinstance(items[-1], dict) and items[-1].get('type') == 'function_call_output':
        return _text(items[-1].get('output'))[:600]
    return None


class FakeGate:
    def __init__(self, command, bearer):
        self.events = []
        self.command = command
        self.bearer = bearer
        self.tool_sent = set()
        self.lock = threading.Lock()
        self.began = time.monotonic()
        self.servers = []
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _record(self, kind, **data):
                gate.record(kind, method=self.command, path=self.path,
                            family='ipv6' if ':' in self.client_address[0] else 'ipv4',
                            bearer_matches=self.headers.get('Authorization') == 'Bearer ' + gate.bearer,
                            **data)

            def do_CONNECT(self):
                self._record('connect_refused')
                self.send_error(403)

            def do_GET(self):
                path = self.path.split('?')[0]
                if path == '/api/codex/accounts/check':
                    body = {'accounts': [{'id': 'nm-account', 'name': 'NM Fixture',
                            'workspace_backend_origin': 'https://fixture.invalid',
                            'account_routing_override': 'NO_CONSTRAINT'}]}
                elif path.endswith('/usage'):
                    body = {'account_id': 'nm-account', 'plan_type': 'pro', 'rate_limit': {
                            'allowed': True, 'limit_reached': False,
                            'primary_window': {'used_percent': 10, 'limit_window_seconds': 18000,
                                               'reset_at': 2000000000, 'reset_after_seconds': 18000},
                            'secondary_window': {'used_percent': 20, 'limit_window_seconds': 604800,
                                                 'reset_at': 2000400000, 'reset_after_seconds': 604800}}}
                elif path.endswith('/models'):
                    body = {'models': []}
                else:
                    self._record('get_refused')
                    self.send_error(404)
                    return
                self._record('get_served')
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                size = int(self.headers.get('Content-Length', 0) or 0)
                if size > 4_000_000:
                    self.send_error(413)
                    return
                try:
                    body = json.loads(self.rfile.read(size) or b'{}')
                except ValueError:
                    self._record('post_refused', reason='json')
                    self.send_error(400)
                    return
                path = self.path.split('?')[0]
                text = json.dumps(body)
                if path == '/v1/messages':
                    self._claude(body, text)
                elif path.endswith('/responses'):
                    self._codex(body, text)
                else:
                    self._record('post_refused')
                    self.send_error(404)

            def _claude(self, body, text):
                last = body.get('messages', [])[-1:]
                has_result = any(isinstance(m.get('content'), list) and any(
                    isinstance(c, dict) and c.get('type') == 'tool_result' for c in m['content']) for m in last)
                wants = 'NM_TOOL_TURN' in text and '"tool_result"' not in text
                with gate.lock:
                    send = wants and 'claude' not in gate.tool_sent
                    if send:
                        gate.tool_sent.add('claude')
                self._record('claude_model', tool_call=send, tool_result=has_result,
                             second_turn='NM_SECOND_TURN' in text, injected='NMTOOLINJECT' in text,
                             tool_output=last_tool_output(body),
                             tools=sorted(t.get('name', '') for t in body.get('tools', []))[:40])
                rows = claude_bash(body.get('model'), gate.command) if send else claude_text(
                    body.get('model'), 'NM fixture reply after tool' if has_result else 'NM fixture reply')
                try:
                    _sse(self, rows, True)
                except OSError:
                    pass

            def _codex(self, body, text):
                items = body.get('input') or []
                has_output = bool(items) and isinstance(items[-1], dict) and \
                    items[-1].get('type') == 'function_call_output'
                wants = 'NM_TOOL_TURN' in text and '"function_call_output"' not in text
                ident = str(len(gate.events))
                rows = None
                with gate.lock:
                    if wants and 'codex' not in gate.tool_sent:
                        rows = codex_call(ident, body.get('tools', []), gate.command)
                        if rows:
                            gate.tool_sent.add('codex')
                self._record('codex_model', tool_call=rows is not None, tool_result=has_output,
                             second_turn='NM_SECOND_TURN' in text, injected='NMTOOLINJECT' in text,
                             tool_output=last_tool_output(body),
                             tools=sorted(t.get('name', '') or t.get('type', '') for t in body.get('tools', []))[:40])
                if rows is None:
                    rows = codex_text(ident, 'NM fixture reply after tool' if has_output else 'NM fixture reply')
                try:
                    _sse(self, rows, False)
                except OSError:
                    pass

        for _ in range(50):
            v4 = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            try:
                v6 = _V6Server(('::1', v4.server_port), Handler)
                break
            except OSError:
                v4.server_close()
        else:
            raise OSError('no common IPv4/IPv6 loopback port for the gate')
        self.port = v4.server_port
        self.servers.extend((v4, v6))
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def record(self, kind, **data):
        with self.lock:
            self.events.append({'at': round(time.monotonic() - self.began, 3), 'event': kind, **data})

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
