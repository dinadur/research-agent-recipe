#!/usr/bin/env python3
"""Offline test of the Clef adapter: request mapping plus a round trip against a mock /v1/systemone server.

Run: python3 adapters/tests/test_clef_adapter.py
"""
import http.client
import http.server
import importlib.util
import json
import os
import pathlib
import socket
import socketserver
import subprocess
import sys
import threading
import time

ADAPTER = pathlib.Path(__file__).resolve().parents[1] / 'clef_groundedness_adapter.py'
CRITERION = ("<guardian><no-think>As a judge agent ...\n\n### Criteria: The last assistant claim is fully supported "
             "by the supplied excerpts.\n\n### Scoring Schema: If the last assistant's text meets the criteria, "
             "return 'yes'; otherwise, return 'no'.")
GROUNDEDNESS = [
    {'role': 'user', 'content': 'Use only these frozen evidence excerpts:\n\nEXCERPT 1\nURL: https://vendor.example/p\n'
                                'TEXT: Battery life is up to 60 minutes.'},
    {'role': 'assistant', 'content': 'The vacuum runs for up to 60 minutes.'},
    {'role': 'user', 'content': CRITERION},
]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Mock(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    p_yes = 0.9

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.path == '/v1/systemone' and body['questions']['verdict']['type'] == 'noul'
        b = json.dumps({'answers': {'verdict': {'noul': Mock.p_yes}}, 'usage': {'input_tokens': 123}}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    fails = []

    def check(name, cond):
        print(('PASS' if cond else 'FAIL'), name)
        cond or fails.append(name)

    spec = importlib.util.spec_from_file_location('clef_adapter', ADAPTER)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    req, shape = mod.to_systemone(GROUNDEDNESS)
    q = req['questions']['verdict']
    check('groundedness shape detected', shape == 'groundedness')
    check('state carries excerpts and claim', req['state']['assistant_claim'].startswith('The vacuum')
          and req['state']['evidence_excerpts'].startswith('EXCERPT 1'))
    check('criteria text becomes the instructions', q['instructions'].startswith('The last assistant claim'))
    _, shape = mod.to_systemone([{'role': 'user', 'content': 'Is the sky blue?'}])
    check('other chats use the generic shape', shape == 'generic')

    up_port, ad_port = free_port(), free_port()
    threading.Thread(target=S(('127.0.0.1', up_port), Mock).serve_forever, daemon=True).start()
    env = dict(os.environ, CLEF_LISTEN_PORT=str(ad_port), CLEF_UPSTREAM_PORT=str(up_port), CLEF_THRESHOLD='0.85')
    env.pop('CLEF_DECISION_LOG', None)
    ad = subprocess.Popen([sys.executable, str(ADAPTER)], env=env)
    time.sleep(1)
    try:
        for p_yes, want in ((0.9, 'yes'), (0.84, 'no')):
            Mock.p_yes = p_yes
            c = http.client.HTTPConnection('127.0.0.1', ad_port, timeout=10)
            c.request('POST', '/v1/chat/completions', body=json.dumps({'messages': GROUNDEDNESS}),
                      headers={'Content-Type': 'application/json'})
            out = json.loads(c.getresponse().read())
            content = out['choices'][0]['message']['content']
            check(f'p_yes {p_yes} -> {want}', f'<score> {want} </score>' in content and out['clef']['p_yes'] == p_yes)
    finally:
        ad.terminate()
    print('FAILURES', fails)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
