#!/usr/bin/env python3
import json, glob, os, time
from datetime import datetime, timezone

AGENTS_DIR = '/root/.openclaw/agents'
OUT = '/root/.openclaw/workspace-todo/dashboard/data/stats.json'

# Bedrock Claude Opus 4 pricing per 1M tokens
PRICE_INPUT = 15.0
PRICE_OUTPUT = 75.0
PRICE_CACHE_WRITE = 18.75
PRICE_CACHE_READ = 3.75

def scan_agent(agent_id):
    pattern = os.path.join(AGENTS_DIR, agent_id, 'sessions', '*.trajectory.jsonl')
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_write = 0
    calls = 0
    sessions = 0
    daily = {}
    tools_used = {}
    first_ts = None
    last_ts = None

    for f in sorted(glob.glob(pattern)):
        sessions += 1
        for line in open(f):
            try:
                d = json.loads(line.strip())
            except:
                continue

            ts = d.get('ts', '')
            day = ts[:10] if ts else None

            if d.get('type') == 'model.completed':
                usage = d.get('data', {}).get('usage', {})
                inp = usage.get('input', 0)
                out = usage.get('output', 0)
                total_in += inp
                total_out += out

                cache = d.get('data', {}).get('promptCache', {}).get('lastCallUsage', {})
                total_cache_read += cache.get('cacheRead', 0)
                total_cache_write += cache.get('cacheWrite', 0)
                calls += 1

                if day:
                    if day not in daily:
                        daily[day] = {'input': 0, 'output': 0, 'calls': 0}
                    daily[day]['input'] += inp
                    daily[day]['output'] += out
                    daily[day]['calls'] += 1

                if not first_ts or ts < first_ts:
                    first_ts = ts
                if not last_ts or ts > last_ts:
                    last_ts = ts

            if d.get('type') == 'tool.completed':
                tool = d.get('data', {}).get('toolName', d.get('toolName', 'unknown'))
                tools_used[tool] = tools_used.get(tool, 0) + 1

    cost_in = total_in / 1_000_000 * PRICE_INPUT
    cost_out = total_out / 1_000_000 * PRICE_OUTPUT
    cost_cw = total_cache_write / 1_000_000 * PRICE_CACHE_WRITE
    cost_cr = total_cache_read / 1_000_000 * PRICE_CACHE_READ

    return {
        'agent': agent_id,
        'sessions': sessions,
        'calls': calls,
        'inputTokens': total_in,
        'outputTokens': total_out,
        'cacheRead': total_cache_read,
        'cacheWrite': total_cache_write,
        'totalTokens': total_in + total_out + total_cache_read + total_cache_write,
        'cost': {
            'input': round(cost_in, 4),
            'output': round(cost_out, 4),
            'cacheWrite': round(cost_cw, 4),
            'cacheRead': round(cost_cr, 4),
            'total': round(cost_in + cost_out + cost_cw + cost_cr, 4)
        },
        'daily': dict(sorted(daily.items())),
        'toolsUsed': dict(sorted(tools_used.items(), key=lambda x: -x[1])),
        'firstActivity': first_ts,
        'lastActivity': last_ts
    }

def main():
    agents_data = []
    for d in sorted(os.listdir(AGENTS_DIR)):
        sess_dir = os.path.join(AGENTS_DIR, d, 'sessions')
        if os.path.isdir(sess_dir):
            data = scan_agent(d)
            if data['calls'] > 0:
                agents_data.append(data)

    grand_cost = sum(a['cost']['total'] for a in agents_data)
    grand_calls = sum(a['calls'] for a in agents_data)
    grand_input = sum(a['inputTokens'] for a in agents_data)
    grand_output = sum(a['outputTokens'] for a in agents_data)
    grand_cache_r = sum(a['cacheRead'] for a in agents_data)
    grand_cache_w = sum(a['cacheWrite'] for a in agents_data)

    # Gateway uptime
    uptime_sec = None
    try:
        import subprocess
        r = subprocess.run(['systemctl', '--user', 'show', 'openclaw-gateway', '--property=ActiveEnterTimestamp'],
                          capture_output=True, text=True)
        ts_str = r.stdout.strip().split('=', 1)[1]
        if ts_str:
            from email.utils import parsedate_to_datetime
            try:
                started = datetime.strptime(ts_str.strip(), '%a %Y-%m-%d %H:%M:%S %Z')
                uptime_sec = int((datetime.now() - started).total_seconds())
            except:
                pass
    except:
        pass

    result = {
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'model': 'Claude Opus 4.6 (Bedrock)',
        'region': 'us-east-1',
        'gatewayUptimeSec': uptime_sec,
        'totals': {
            'cost': round(grand_cost, 4),
            'calls': grand_calls,
            'inputTokens': grand_input,
            'outputTokens': grand_output,
            'cacheRead': grand_cache_r,
            'cacheWrite': grand_cache_w,
        },
        'agents': agents_data
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as f:
        json.dump(result, f, indent=2)

if __name__ == '__main__':
    main()
