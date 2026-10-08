#!/usr/bin/env python3
"""Groundedness-judge adapter: Granite-Guardian-style chat requests served by Clef-Flash (/v1/systemone).

Hermes' product groundedness check (agent/product_groundedness.py in the patch series) speaks the Granite Guardian
chat format and expects "<score> yes|no </score>". Clef-Flash answers yes/no questions through llama.cpp's
/v1/systemone endpoint with a probability. This adapter translates between the two, so the agent needs no change
when the judge model changes.

POST /v1/chat/completions with Hermes' groundedness messages:
  [user "Use only these frozen evidence excerpts:\\n\\n<EXCERPT blocks>", assistant <claim>,
   user <judge criterion with "### Criteria: ... ### Scoring Schema: ...">]
-> Clef "noul" question (instructions = the criteria text; state = {evidence_excerpts, assistant_claim})
-> reply "<think>\\n</think>\\n<score> yes|no </score>", yes iff P(yes) >= CLEF_THRESHOLD (default 0.85).
The reply also carries {"clef": {"p_yes", "threshold", "shape"}} so callers can log the probability.
Any other chat shape: state = the conversation, question = the last user message (same yes/no contract).

GET /health, /props, /slots and /v1/models answer like a llama.cpp lane, so an agent-side health preflight passes.

Configuration (environment):
  CLEF_LISTEN_HOST / CLEF_LISTEN_PORT     where this adapter listens (default 127.0.0.1:8244)
  CLEF_UPSTREAM_HOST / CLEF_UPSTREAM_PORT llama-server running Clef-Flash (default 127.0.0.1:8297)
  CLEF_ALIAS          model name the agent uses (default "guardian")
  CLEF_CTX            context length reported in /props (default 2048)
  CLEF_THRESHOLD      yes threshold on P(yes) (default 0.85)
  CLEF_DECISION_LOG   optional JSONL path; one row per decision (time, shape, p_yes, verdict, tokens, latency)
"""
import http.client
import http.server
import json
import os
import re
import socketserver
import time
import urllib.request

LISTEN_HOST = os.environ.get('CLEF_LISTEN_HOST', '127.0.0.1')
LISTEN_PORT = int(os.environ.get('CLEF_LISTEN_PORT', '8244'))
UP_HOST = os.environ.get('CLEF_UPSTREAM_HOST', '127.0.0.1')
UP_PORT = int(os.environ.get('CLEF_UPSTREAM_PORT', '8297'))
ALIAS = os.environ.get('CLEF_ALIAS', 'guardian')
CTX = int(os.environ.get('CLEF_CTX', '2048'))
THRESHOLD = float(os.environ.get('CLEF_THRESHOLD', '0.85'))
DECISION_LOG = os.environ.get('CLEF_DECISION_LOG', '')
CRIT_RE = re.compile(r'###\s*Criteria:\s*(.*?)\s*(?:###\s*Scoring Schema:|$)', re.S)
EVIDENCE_PREFIX = 'Use only these frozen evidence excerpts:'


def text(m):
    c = m.get('content')
    if isinstance(c, list):
        return '\n'.join(p.get('text', '') for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ''


def to_systemone(messages):
    msgs = [m for m in messages if m.get('role') != 'system']
    last_user = next((text(m) for m in reversed(msgs) if m.get('role') == 'user'), '')
    crit = CRIT_RE.search(last_user)
    if (len(msgs) >= 3 and msgs[0].get('role') == 'user' and text(msgs[0]).startswith(EVIDENCE_PREFIX)
            and msgs[1].get('role') == 'assistant' and crit):
        state = {'evidence_excerpts': text(msgs[0])[len(EVIDENCE_PREFIX):].lstrip('\n'),
                 'assistant_claim': text(msgs[1])}
        instructions, shape = crit.group(1).strip(), 'groundedness'
    else:
        state = {'conversation': [{'role': m.get('role'), 'content': text(m)} for m in msgs[:-1]]}
        instructions, shape = (crit.group(1).strip() if crit else last_user), 'generic'
    if shape == 'groundedness':
        criteria = {'true': 'Every factual clause of the claim is proven by the excerpts.',
                    'false': 'At least one factual clause is not proven by the excerpts.'}
    else:
        criteria = {'true': 'Yes.', 'false': 'No.'}
    q = {'type': 'noul', 'instructions': instructions, 'criteria': criteria}
    return {'model': 'clef-flash', 'state': state, 'questions': {'verdict': q}}, shape


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
                                    'total_slots': 1, 'engine': 'clef-flash-systemone', 'threshold': THRESHOLD})
        if path == '/slots':
            return self._json(200, [{'id': 0, 'is_processing': False, 'n_ctx': CTX}])
        if path == '/v1/models':
            return self._json(200, {'object': 'list', 'data': [{'id': ALIAS, 'object': 'model', 'owned_by': 'local'}]})
        return self._json(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) if n else b'{}')
        except ValueError:
            return self._json(400, {'error': {'message': 'invalid JSON body'}})
        if self.path.split('?')[0] not in ('/v1/chat/completions', '/chat/completions'):
            return self._json(404, {'error': {'message': 'only chat completions are served'}})
        req, shape = to_systemone(body.get('messages') or [])
        t0 = time.monotonic()
        try:
            up = http.client.HTTPConnection(UP_HOST, UP_PORT, timeout=120)
            up.request('POST', '/v1/systemone', json.dumps(req), {'Content-Type': 'application/json'})
            r = up.getresponse()
            raw = r.read()
            up.close()
            if r.status != 200:
                # e.g. llama-server "input too large" when the request exceeds the server batch (-b/-ub)
                return self._json(502, {'error': {'message': f'clef upstream {r.status}: {raw[:200]!r}'}})
            out = json.loads(raw)
            p = float(out['answers']['verdict']['noul'])
        except Exception as e:
            return self._json(502, {'error': {'message': f'clef upstream error: {e}'}})
        verdict = 'yes' if p >= THRESHOLD else 'no'
        latency = round(time.monotonic() - t0, 3)
        usage = out.get('usage') or {}
        if DECISION_LOG:
            try:
                with open(DECISION_LOG, 'a') as f:
                    f.write(json.dumps(dict(t=time.time(), shape=shape, p_yes=round(p, 4), verdict=verdict,
                                            threshold=THRESHOLD, input_tokens=usage.get('input_tokens'),
                                            latency_s=latency)) + '\n')
            except OSError:
                pass
        content = f'<think>\n</think>\n<score> {verdict} </score>'
        tokens = usage.get('input_tokens', 0)
        return self._json(200, {'id': 'clef-' + str(time.time_ns()), 'object': 'chat.completion',
                                'created': int(time.time()), 'model': ALIAS,
                                'choices': [{'index': 0, 'finish_reason': 'stop',
                                             'message': {'role': 'assistant', 'content': content}}],
                                'usage': {'prompt_tokens': tokens, 'completion_tokens': 0, 'total_tokens': tokens},
                                'clef': {'p_yes': p, 'threshold': THRESHOLD, 'shape': shape}})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == '__main__':
    Server((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
