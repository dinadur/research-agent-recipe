#!/usr/bin/env python3
"""Post-deployment health check for a multi-lane local research agent. Prints a markdown report and saves it.

It checks, for a time window:
  * systemd unit states (units expected active, and units expected inactive because a swap flag holds them off);
  * swap flag files;
  * the main lane's engine field (/props) and vLLM counters behind the adapter (/metrics): finished requests by
    reason, preemptions, token totals and cumulative MTP acceptance;
  * the groundedness judge's decision log (counts, decisions near the threshold, latency);
  * warning-level journal lines per unit, kernel GPU/OOM faults, and free disk space.

Usage (journal access usually needs root):
  health_check.py --since "2026-10-07 12:42" --out /var/tmp/health \\
      --units hermes-gateway,research-main-engine,research-main-adapter,research-worker,research-judge,research-judge-adapter \\
      --expected-off llama-main,granite-guardian \\
      --flag /etc/research-agent/main-vllm.active --flag /etc/research-agent/judge-clef.active \\
      --main-url http://127.0.0.1:8242 --judge-url http://127.0.0.1:8244 \\
      --decision-log /var/log/research-agent/judge-decisions.jsonl --disk / --disk /srv/models

Limitation: MTP acceptance is cumulative since engine start, so a short window of zero acceptance (the signature of a
NaN collapse) is not visible here. Sample the engine log for per-window speculative-decoding metrics if you need that.
"""
import argparse
import json
import re
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

NOISE = re.compile(r'check_fn .* returned False|Normal final-send NOT suppressed')
FAULT = re.compile(r'(xe |i915|\[drm\]|pcieport|AER).*(reset|hang|wedge|lost|fault|error|timed out|GuC|CAT|AER)'
                   r'|Out of memory|oom-kill', re.I)
BENIGN = re.compile(r'peer-to-peer DMA|Using \d+-bit DMA', re.I)


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def get(url, t=5):
    try:
        with urllib.request.urlopen(url, timeout=t) as r:
            return r.read().decode()
    except Exception:
        return ''


def csv(value):
    return [x.strip() for x in (value or '').split(',') if x.strip()]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--since', required=True, help='"YYYY-MM-DD HH:MM" (UTC)')
    p.add_argument('--out', required=True, help='directory for the report')
    p.add_argument('--units', default='hermes-gateway', help='comma-separated units expected active')
    p.add_argument('--expected-off', default='', help='comma-separated units expected inactive (held off by a flag)')
    p.add_argument('--flag', action='append', default=[], help='swap flag file to report (repeatable)')
    p.add_argument('--main-url', default='http://127.0.0.1:8242', help='main-lane adapter base URL')
    p.add_argument('--judge-url', default='http://127.0.0.1:8244', help='judge adapter base URL')
    p.add_argument('--decision-log', default='', help='judge decision JSONL (optional)')
    p.add_argument('--log-units', default='', help='units whose warnings to summarise (default: --units)')
    p.add_argument('--disk', action='append', default=[], help='mount point to report free space for (repeatable)')
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    active, off = csv(a.units), csv(a.expected_off)
    units = active + off

    L = [f'# Health check {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())} (since {a.since} UTC)', '']
    states = dict(zip(units, sh(['systemctl', 'is-active'] + units).split())) if units else {}
    flags = {f: Path(f).exists() for f in a.flag}
    bad = [u for u, s in states.items() if (s != 'active') != (u in off)]
    L += ['## Services', f'- {"OK" if not bad else "ATTENTION: " + ", ".join(bad)}; flags {flags}',
          '- ' + ', '.join(f'{u}={s}' for u, s in states.items()), '']

    try:
        main_props = json.loads(get(a.main_url + '/props') or '{}')
        judge_props = json.loads(get(a.judge_url + '/props') or '{}')
        L.append(f'- main engine: {main_props.get("engine", "unknown")}; judge engine: '
                 f'{judge_props.get("engine", "unknown")} (threshold {judge_props.get("threshold")})')
    except ValueError:
        L.append('- engine props unavailable')

    vals = {}
    for line in get(a.main_url + '/metrics').splitlines():
        mm = re.match(r'vllm:([a-z_]+)\{([^}]*)\} ([0-9.e+]+)', line)
        if mm:
            name, labels, v = mm.groups()
            reason = re.search(r'finished_reason="([a-z]+)"', labels)
            key = name + (':' + reason.group(1) if reason else '')
            vals[key] = vals.get(key, 0) + float(v)
    succ = {k.split(':')[1]: int(v) for k, v in vals.items() if k.startswith('request_success_total:')}
    acc = vals.get('spec_decode_num_accepted_tokens_total', 0) / max(1, vals.get('spec_decode_num_draft_tokens_total', 0))
    L += ['', '## Main engine (vLLM counters since engine start)',
          f'- finished requests by reason: {succ}',
          f'- preemptions: {int(vals.get("num_preemptions_total", 0))}; prompt tokens '
          f'{int(vals.get("prompt_tokens_total", 0))}; generated {int(vals.get("generation_tokens_total", 0))}; '
          f'MTP acceptance {acc:.0%}; running now {int(vals.get("num_requests_running", 0))}']

    L += ['', '## Groundedness judge decisions']
    rows = []
    if a.decision_log and Path(a.decision_log).exists():
        since_t = time.mktime(time.strptime(a.since, '%Y-%m-%d %H:%M')) - time.timezone
        rows = [json.loads(x) for x in Path(a.decision_log).read_text().splitlines() if x.strip()]
        rows = [r for r in rows if r.get('t', 0) >= since_t]
    if rows:
        lat = sorted(r['latency_s'] for r in rows)
        ps = [r['p_yes'] for r in rows]
        near = sum(1 for r in rows if abs(r['p_yes'] - r['threshold']) < 0.05)
        shapes = {s: sum(r['shape'] == s for r in rows) for s in {r['shape'] for r in rows}}
        L.append(f'- {len(rows)} decisions: yes {sum(r["verdict"] == "yes" for r in rows)}, '
                 f'no {sum(r["verdict"] == "no" for r in rows)}; within 0.05 of threshold {near}; '
                 f'p_yes median {statistics.median(ps):.2f}; latency p50 {lat[len(lat) // 2]:.2f} s, '
                 f'max {lat[-1]:.2f} s; shapes {shapes}')
    else:
        L.append('- no decisions logged in the window (or no log configured)')

    L += ['', '## Logs (warnings and errors, noise filtered)']
    for u in csv(a.log_units) or active:
        lines = [x for x in sh(['journalctl', '-u', u, '--since', a.since, '--no-pager', '-o', 'cat',
                                '-p', 'warning']).splitlines()
                 if x.strip() and not NOISE.search(x) and not x.startswith('-- ')]
        L.append(f'- {u}: {len(lines)} lines' + (f'; last: `{lines[-1][:160]}`' if lines else ''))
    k = [x for x in sh(['journalctl', '-k', '--since', a.since, '--no-pager', '-o', 'cat']).splitlines()
         if FAULT.search(x) and not BENIGN.search(x)]
    L += ['', '## Kernel GPU/OOM faults', f'- {len(k)}' + (f'; last: `{k[-1][:160]}`' if k else '')]

    df = sh(['df', '-h'] + (a.disk or ['/'])).splitlines()[1:]
    if df:
        L += ['', '## Disk', '- ' + '; '.join(f'{x.split()[3]} free on {x.split()[5]} ({x.split()[4]} used)'
                                               for x in df)]
    text = '\n'.join(L) + '\n'
    f = out / f'health-{time.strftime("%Y%m%dT%H%MZ", time.gmtime())}.md'
    f.write_text(text)
    print(text)
    print('report:', f)


if __name__ == '__main__':
    main()
