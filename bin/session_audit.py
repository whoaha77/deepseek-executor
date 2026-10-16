"""Local metadata-only Codex trace audit; never exports message contents."""
import argparse
import collections
import json
from pathlib import Path


def audit(path):
    calls = collections.Counter()
    result = dict(session=path.name, token_events=0, malformed_lines=0,
                  tool_output_chars=0, max_tool_output_chars=0, cumulative_resets=0)
    segments, previous = [], None
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                result['malformed_lines'] += 1
                continue
            p = row.get('payload', {})
            if row.get('type') == 'event_msg' and p.get('type') == 'token_count':
                usage = (p.get('info') or {}).get('total_token_usage')
                if isinstance(usage, dict):
                    current = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
                    if previous and current.get('total_tokens', 0) < previous.get('total_tokens', 0):
                        segments.append(previous)
                        result['cumulative_resets'] += 1
                    previous = current
                    result['token_events'] += 1
            if row.get('type') == 'response_item':
                if p.get('type') in ('function_call', 'custom_tool_call'):
                    calls[p.get('name', 'unknown')] += 1
                if p.get('type') in ('function_call_output', 'custom_tool_call_output'):
                    output = p.get('output', '')
                    n = len(output) if isinstance(output, str) else len(json.dumps(output, ensure_ascii=False))
                    result['tool_output_chars'] += n
                    result['max_tool_output_chars'] = max(result['max_tool_output_chars'], n)
    if previous is not None:
        segments.append(previous)
    result['usage_status'] = 'reported' if segments else 'unknown'
    result['reported_usage'] = {k: sum(s.get(k, 0) for s in segments)
                                for k in set().union(*(s.keys() for s in segments))}
    result['tool_calls'] = dict(calls)
    result['limitations'] = ('Provider-reported cumulative segment totals, not summed token events. '
                            'Cache tokens are a subset of input. Resets may indicate restarts; '
                            'billing, subscription quota, hidden context and per-task attribution unknown. '
                            'Characters are not tokens. Missing usage means unknown, not zero.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--files', nargs='+', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    rows = [audit(p) for p in args.files]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'sessions': len(rows), 'report': str(args.out)}))
    return int(any(r['malformed_lines'] for r in rows))


if __name__ == '__main__':
    raise SystemExit(main())
