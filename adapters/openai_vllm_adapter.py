#!/usr/bin/env python3
"""OpenAI-compatible front for a vLLM engine, with a fixed sampler policy and two safety guards.

The agent (Hermes) talks to this adapter as if it were a llama.cpp-style lane: it answers /health, /props, /slots
and /v1/models itself and proxies everything else to vLLM. Written for a Qwen3.8-27B EXL3 engine on vLLM + MTP
(see 0xSero's repositories for the EXL3/XPU build), but nothing here is model specific.

POST /v1/chat/completions applies the request policy:
  * the lane alias is rewritten to the vLLM served name;
  * min_p and repeat_penalty are removed (vLLM with MTP speculative decoding rejects min_p);
  * sampler defaults when the client sent none: temperature 1.0, top_p 0.95, top_k 20, repetition_penalty 1.0,
    presence/frequency penalty 0;
  * thinking_token_budget (ADAPTER_REASONING_BUDGET, default 4096). Note: some vLLM builds enforce it only when the
    server was started with --reasoning-config; without that flag it is silently ignored;
  * on streams: chat_template_kwargs.enable_thinking, include_reasoning and stream_options.include_usage.

Guards:
  * Client disconnect: a watcher polls the client socket and closes the upstream connection as soon as the client
    goes away, so vLLM aborts the request (also during prefill) instead of generating for nobody.
  * Degenerate stream: ADAPTER_DEGENERATE_RUN (default 64) identical reasoning/content deltas of at most 2 characters
    in a row abort the upstream request and end the stream with an OpenAI-style error event (code
    "degenerate_stream"). This is the signature of a NaN collapse (an endless run of "!" tokens). The OpenAI SDK
    raises APIError, so the agent fails fast or retries instead of waiting while the loop runs to max_tokens.

Configuration (environment):
  ADAPTER_LISTEN_HOST / ADAPTER_LISTEN_PORT   where this adapter listens (default 127.0.0.1:8242)
  ADAPTER_UPSTREAM_HOST / ADAPTER_UPSTREAM_PORT  vLLM (default 127.0.0.1:8260)
  ADAPTER_ALIAS            model name the agent uses (default "main")
  ADAPTER_SERVED_NAME      vLLM --served-model-name (default "qwen3.8-27b-exl3")
  ADAPTER_CTX              context length reported in /props and /v1/models (default 131072)
  ADAPTER_MAX_SEQS         sequence slots reported in /slots (default 16)
  ADAPTER_REASONING_BUDGET thinking_token_budget default (default 4096; 0 disables)
  ADAPTER_DEGENERATE_RUN   guard length (default 64; 0 disables)
"""
import http.client
import http.server
import json
import os
import select
import socket
import socketserver
import sys
import threading
import time
import urllib.request

LISTEN_HOST = os.environ.get('ADAPTER_LISTEN_HOST', '127.0.0.1')
LISTEN_PORT = int(os.environ.get('ADAPTER_LISTEN_PORT', '8242'))
UP_HOST = os.environ.get('ADAPTER_UPSTREAM_HOST', '127.0.0.1')
UP_PORT = int(os.environ.get('ADAPTER_UPSTREAM_PORT', '8260'))
ALIAS = os.environ.get('ADAPTER_ALIAS', 'main')
SERVED = os.environ.get('ADAPTER_SERVED_NAME', 'qwen3.8-27b-exl3')
CTX = int(os.environ.get('ADAPTER_CTX', '131072'))
SLOTS = int(os.environ.get('ADAPTER_MAX_SEQS', '16'))
BUDGET = int(os.environ.get('ADAPTER_REASONING_BUDGET', '4096') or 0)
DEGENERATE_RUN = int(os.environ.get('ADAPTER_DEGENERATE_RUN', '64') or 0)
SAMPLER = dict(temperature=1.0, top_p=0.95, top_k=20, repetition_penalty=1.0, presence_penalty=0, frequency_penalty=0)
DROP = ('min_p', 'repeat_penalty')


def transform(body):
    body = dict(body)
    body['model'] = SERVED
    for k in DROP:
        body.pop(k, None)
    eb = body.get('extra_body')
    if isinstance(eb, dict):  # an OpenAI-SDK style nested extra_body should not reach vLLM as-is
        eb = {k: v for k, v in eb.items() if k not in DROP}
        body.pop('extra_body')
        body = {**eb, **body}
    for k, v in SAMPLER.items():
        body.setdefault(k, v)
    if BUDGET > 0:
        body.setdefault('thinking_token_budget', BUDGET)
    if body.get('stream'):
        ctk = dict(body.get('chat_template_kwargs') or {})
        ctk.setdefault('enable_thinking', True)
        body['chat_template_kwargs'] = ctk
        body.setdefault('include_reasoning', True)
        so = dict(body.get('stream_options') or {})
        so['include_usage'] = True
        body['stream_options'] = so
    return body


class DegenerateGuard:
    """Detects an endless run of identical tiny deltas (the NaN "!" loop).

    feed() takes raw SSE bytes and returns True once DEGENERATE_RUN consecutive reasoning/content deltas carry the
    same text of at most 2 characters.
    """

    def __init__(self):
        self.buf, self.last, self.run = b'', None, 0

    def feed(self, data):
        if DEGENERATE_RUN <= 0:
            return False
        self.buf += data
        *lines, self.buf = self.buf.split(b'\n')
        for line in lines:
            if not line.startswith(b'data: ') or line.startswith(b'data: [DONE]'):
                continue
            try:
                delta = (json.loads(line[6:])['choices'] or [{}])[0].get('delta') or {}
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                continue
            text = delta.get('reasoning') or delta.get('reasoning_content') or delta.get('content')
            if not isinstance(text, str) or not text:
                continue
            if text == self.last and len(text) <= 2:
                self.run += 1
            else:
                self.last, self.run = text, 1
            if self.run >= DEGENERATE_RUN:
                return True
        return False


def upstream_metrics():
    try:
        with urllib.request.urlopen(f'http://{UP_HOST}:{UP_PORT}/metrics', timeout=3) as r:
            text = r.read().decode()
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        for k in ('num_requests_running', 'num_requests_waiting'):
            if line.startswith('vllm:' + k):
                try:
                    out[k] = out.get(k, 0) + float(line.rsplit(' ', 1)[1])
                except ValueError:
                    pass
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/health':
            try:
                with urllib.request.urlopen(f'http://{UP_HOST}:{UP_PORT}/health', timeout=4) as r:
                    ok = r.status == 200
            except Exception:
                ok = False
            return self._json(200 if ok else 503, {'status': 'ok' if ok else 'unavailable'})
        if path == '/props':
            return self._json(200, {'model_alias': ALIAS, 'default_generation_settings': {'n_ctx': CTX},
                                    'total_slots': SLOTS, 'engine': 'vllm', 'upstream_model': SERVED})
        if path == '/slots':
            running = int(upstream_metrics().get('num_requests_running', 0))
            return self._json(200, [{'id': i, 'is_processing': i < running, 'n_ctx': CTX} for i in range(SLOTS)])
        if path == '/v1/models':
            return self._json(200, {'object': 'list', 'data': [{'id': ALIAS, 'object': 'model', 'owned_by': 'local',
                                                                 'max_model_len': CTX, 'upstream_model': SERVED}]})
        return self._proxy('GET', None)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        if self.path.split('?')[0] in ('/v1/chat/completions', '/chat/completions'):
            try:
                raw = json.dumps(transform(json.loads(raw))).encode()
            except ValueError:
                return self._json(400, {'error': {'message': 'invalid JSON body'}})
        return self._proxy('POST', raw)

    def _proxy(self, method, raw):
        up = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=3600)
        headers = {'Content-Type': self.headers.get('Content-Type', 'application/json')}
        if self.headers.get('Accept'):
            headers['Accept'] = self.headers['Accept']
        stop = threading.Event()

        def watch():  # client gone (EOF on its socket) -> drop the upstream connection so vLLM aborts
            sock = self.connection
            while not stop.is_set():
                try:
                    r, _, _ = select.select([sock], [], [], 0.25)
                    if r and not sock.recv(1, socket.MSG_PEEK):
                        try:
                            up.sock and up.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                except (OSError, ValueError):
                    return
        try:
            up.request(method, self.path, body=raw, headers=headers)
            threading.Thread(target=watch, daemon=True).start()
            resp = up.getresponse()
            self.send_response(resp.status)
            stream = 'text/event-stream' in (resp.getheader('Content-Type') or '')
            for k, v in resp.getheaders():
                if k.lower() not in ('transfer-encoding', 'connection', 'content-length', 'server', 'date'):
                    self.send_header(k, v)
            if stream:
                self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                guard = DegenerateGuard()
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, 'read1') else resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(b'%x\r\n%s\r\n' % (len(chunk), chunk))
                    self.wfile.flush()
                    if guard.feed(chunk):
                        try:
                            up.sock and up.sock.shutdown(socket.SHUT_RDWR)  # vLLM aborts the request
                        except OSError:
                            pass
                        msg = (f'adapter aborted a degenerate stream: {guard.run} identical '
                               f'{guard.last!r} deltas (NaN signature)')
                        sys.stderr.write(f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} {msg}\n')
                        sys.stderr.flush()
                        event = b'data: ' + json.dumps({'error': {'message': msg, 'type': 'server_error',
                                                                  'code': 'degenerate_stream'}}).encode() + b'\n\n'
                        self.wfile.write(b'%x\r\n%s\r\n' % (len(event), event))
                        break
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            else:
                data = resp.read()
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            try:
                self._json(502, {'error': {'message': f'upstream error: {e}'}})
            except OSError:
                pass
        finally:
            stop.set()
            up.close()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == '__main__':
    Server((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
