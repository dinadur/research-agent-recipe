#!/usr/bin/env python3
"""Offline test: a mock vLLM upstream plus the adapter as a subprocess, both on free loopback ports.

Run: python3 adapters/tests/test_openai_vllm_adapter.py
"""
import http.client
import http.server
import json
import os
import pathlib
import socket
import socketserver
import subprocess
import sys
import threading
import time

ADAPTER = pathlib.Path(__file__).resolve().parents[1] / 'openai_vllm_adapter.py'
seen, aborted = [], []


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Mock(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200); self.send_header('Content-Length', '0'); self.end_headers(); return
        if self.path == '/metrics':
            b = b'vllm:num_requests_running{model_name="x"} 3\n'
            self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers()
            self.wfile.write(b); return
        self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        seen.append(body)
        if not body.get('stream'):
            b = json.dumps({'choices': [{'message': {'content': 'ok'}}]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        msgs = json.dumps(body['messages'])
        try:
            if 'nan' in msgs or 'near' in msgs:
                for _ in range(400 if 'nan' in msgs else 63):
                    d = {'choices': [{'index': 0, 'delta': {'reasoning': '!'}}]}
                    self.wfile.write(b'data: ' + json.dumps(d).encode() + b'\n\n'); self.wfile.flush()
                    time.sleep(0.005)
                d = {'choices': [{'index': 0, 'delta': {'content': 'done'}}]}
                self.wfile.write(b'data: ' + json.dumps(d).encode() + b'\n\ndata: [DONE]\n\n'); self.wfile.flush()
            else:
                for i in range(200 if 'slow' in msgs else 3):
                    self.wfile.write(f'data: {{"i": {i}}}\n\n'.encode()); self.wfile.flush(); time.sleep(0.05)
                self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            aborted.append(('nan' if 'nan' in msgs else 'slow', time.time()))
        self.close_connection = True


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    up_port, ad_port = free_port(), free_port()
    threading.Thread(target=S(('127.0.0.1', up_port), Mock).serve_forever, daemon=True).start()
    env = dict(os.environ, ADAPTER_LISTEN_PORT=str(ad_port), ADAPTER_UPSTREAM_PORT=str(up_port),
               ADAPTER_ALIAS='main', ADAPTER_SERVED_NAME='served-model', ADAPTER_CTX='65536')
    ad = subprocess.Popen([sys.executable, str(ADAPTER)], env=env)
    time.sleep(1)

    def req(m, p, b=None):
        c = http.client.HTTPConnection('127.0.0.1', ad_port, timeout=10)
        c.request(m, p, body=json.dumps(b) if b else None, headers={'Content-Type': 'application/json'})
        r = c.getresponse()
        return r.status, r.read()

    fails = []

    def check(name, cond):
        print(('PASS' if cond else 'FAIL'), name)
        cond or fails.append(name)

    try:
        s, b = req('GET', '/health'); check('health ok', s == 200 and json.loads(b)['status'] == 'ok')
        s, b = req('GET', '/props'); p = json.loads(b)
        check('props alias/ctx', p['model_alias'] == 'main' and p['default_generation_settings']['n_ctx'] == 65536)
        s, b = req('GET', '/slots'); check('slots busy=3', sum(x['is_processing'] for x in json.loads(b)) == 3)
        req('POST', '/v1/chat/completions', {'model': 'main', 'messages': [{'role': 'user', 'content': 'hi'}],
                                             'min_p': 0.05, 'repeat_penalty': 1.1, 'temperature': 0.7})
        t = seen[-1]
        check('non-stream transform', t['model'] == 'served-model' and 'min_p' not in t and 'repeat_penalty' not in t
              and t['temperature'] == 0.7 and t['top_k'] == 20 and t['thinking_token_budget'] > 0
              and 'chat_template_kwargs' not in t)
        s, b = req('POST', '/v1/chat/completions', {'model': 'main', 'stream': True,
                                                    'messages': [{'role': 'user', 'content': 'hi'}]})
        t = seen[-1]
        check('stream transform', t['chat_template_kwargs'] == {'enable_thinking': True} and t['include_reasoning']
              and t['stream_options']['include_usage'])
        check('stream body relayed', b.count(b'data:') == 4 and b'[DONE]' in b)
        c = http.client.HTTPConnection('127.0.0.1', ad_port, timeout=10)
        c.request('POST', '/v1/chat/completions', headers={'Content-Type': 'application/json'},
                  body=json.dumps({'model': 'a', 'stream': True, 'messages': [{'role': 'user', 'content': 'slow'}]}))
        r = c.getresponse(); r.read1(100) if hasattr(r, 'read1') else r.read(50)
        t0 = time.time(); c.sock.close(); c.close(); time.sleep(1.5)
        slow = [a for a in aborted if a[0] == 'slow']
        check('client disconnect aborts upstream within 1.5 s', bool(slow) and slow[-1][1] - t0 < 1.5)
        s, b = req('POST', '/v1/chat/completions', {'model': 'a', 'stream': True,
                                                    'messages': [{'role': 'user', 'content': 'nan'}]})
        time.sleep(0.5)
        check('degenerate stream ends with an error event', b'degenerate_stream' in b and b'[DONE]' not in b
              and b.count(b'"!"') < 100)
        check('degenerate stream aborts upstream', any(a[0] == 'nan' for a in aborted))
        s, b = req('POST', '/v1/chat/completions', {'model': 'a', 'stream': True,
                                                    'messages': [{'role': 'user', 'content': 'near'}]})
        check('63 identical deltas pass through', b'degenerate_stream' not in b and b'[DONE]' in b
              and b.count(b'"!"') == 63)
    finally:
        ad.terminate()
    print('FAILURES', fails)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
