"""Convert reviewed S3 lines into a build plan using timestamped S2 evidence."""
import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from plan_story import validate_plan  # noqa: E402


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def safe_stem(value: str) -> str:
    value = re.sub(r'[^\w\-\u4e00-\u9fff]+', '_', value, flags=re.UNICODE).strip('_')
    return value[:80] or 'storyboard'


def make_plan(task: Path, name: str | None = None, series_index: int = 1):
    source = read_json(task / 'source.json')
    evidence = read_json(task / 'evidence.json')
    script = read_json(task / 'script.json')
    ep = source.get('episode')
    if isinstance(ep, bool) or not isinstance(ep, int) or ep < 1:
        raise ValueError('source.json requires a positive integer episode')
    if not isinstance(evidence, list) or not isinstance(script.get('lines'), list):
        raise ValueError('evidence.json must be an array and script.json must contain lines')
    lint = script.get('lint')
    if not isinstance(lint, dict):
        raise ValueError('script.json is missing the S3 lint report')
    required_lint = {
        'in_budget': lint.get('in_budget') is True,
        'no_evidence': lint.get('no_evidence') == 0,
        'line_lengths': not lint.get('long_lines'),
        'gender': lint.get('gender_ok') is True,
        'inference_labels': (lint.get('inference_labels') or {}).get('verdict') == 'PASS',
        'readability': (lint.get('readability') or {}).get('verdict') == 'PASS',
        'cohesion': (lint.get('cohesion') or {}).get('verdict') == 'PASS',
        'semantic_dup': (lint.get('semantic_dup') or {}).get('verdict') == 'PASS',
        'block_structure': (lint.get('block_structure') or {}).get('verdict') == 'PASS',
        'subject_match': (lint.get('subject_match') or {}).get('verdict') == 'PASS',
        'evidence_diversity': (lint.get('evidence_diversity') or {}).get('verdict') == 'PASS',
        'evidence_schedule': (lint.get('evidence_schedule') or {}).get('verdict') == 'PASS',
        'grounding': (lint.get('grounding') or {}).get('verdict') == 'PASS',
    }
    failed_lint = [name for name, passed in required_lint.items() if not passed]
    if failed_lint:
        raise ValueError('S3 script lint not accepted: ' + ', '.join(failed_lint))
    # expected_ids is the S3-selected per-line primary evidence contract.
    # coverage_suggestions are advisory and must never overwrite a citation.
    expected_ids = (lint.get('evidence_schedule') or {}).get('expected_ids')
    if not isinstance(expected_ids, list) or len(expected_ids) != len(script['lines']):
        raise ValueError('S3 lint evidence_schedule must list one expected ID per script line')
    for i, (line, expected_id) in enumerate(zip(script['lines'], expected_ids), 1):
        is_cta = (line.get('block') or '').strip().lower() in ('cta', 'ending_cta')
        refs = line.get('evidence_ids') or []
        if is_cta:
            if refs or line.get('primary_evidence_id') or expected_id:
                raise ValueError(f'lines[{i}] CTA must not carry factual evidence or a primary_evidence_id')
        elif (not isinstance(refs, list) or expected_id not in refs
              or line.get('primary_evidence_id') != expected_id):
            raise ValueError(f'lines[{i}] must include assigned primary shot {expected_id!r} in its factual evidence set')

    by_id = {}
    events = []
    for row in evidence:
        if not isinstance(row, dict) or not isinstance(row.get('evidence_id'), str):
            raise ValueError('each evidence row requires evidence_id')
        eid = row['evidence_id']
        if eid in by_id:
            raise ValueError(f'duplicate evidence_id: {eid}')
        by_id[eid] = row
        row_ep, t = row.get('ep'), row.get('t')
        if row_ep == ep and isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(float(t)) and t >= 0:
            events.append({
                'id': eid, 'ep': ep, 't': float(t),
                'event': str(row.get('desc') or ''),
                'dialogue': str(row.get('subtitle_text') or ''),
                'speaker': str(row.get('speaker') or ''),
                'frame': str(row.get('frame') or ''),
            })
    event_ids = {e['id'] for e in events}
    segments = []
    errors = []
    last_primary = None
    for i, line in enumerate(script['lines'], 1):
        if not isinstance(line, dict):
            errors.append(f'lines[{i}] must be an object')
            continue
        text = line.get('text')
        ids = line.get('evidence_ids')
        if not isinstance(text, str) or not text.strip():
            errors.append(f'lines[{i}] has no narration text')
            continue
        is_cta = (line.get('block') or '').strip().lower() in ('cta', 'ending_cta')
        if is_cta:
            if ids:
                errors.append(f'lines[{i}] CTA must not claim factual evidence')
                continue
            if not last_primary:
                errors.append(f'lines[{i}] CTA has no preceding visual anchor')
                continue
            row = by_id[last_primary]
            segments.append({
                'ep': ep, 't': row['t'], 'text': text.strip(), 'raw': 0,
                'event_id': last_primary, 'primary_evidence_id': '',
                'evidence_ids': [], 'visual_anchor_only': True,
                'block': line.get('block'), 'tone': line.get('tone'),
                'inference': False,
            })
            continue
        if not isinstance(ids, list) or not ids or any(eid not in by_id for eid in ids):
            errors.append(f'lines[{i}] has missing or unknown evidence_ids')
            continue
        primary = line.get('primary_evidence_id')
        if not primary and len(ids) == 1:
            primary = ids[0]
        if primary not in ids:
            errors.append(f'lines[{i}] must select primary_evidence_id from evidence_ids')
            continue
        if any(eid not in event_ids for eid in ids):
            errors.append(f'lines[{i}] cites evidence without trusted episode/time metadata')
            continue
        row = by_id[primary]
        if primary not in event_ids:
            errors.append(f'lines[{i}] primary evidence {primary!r} has no trusted episode/time metadata')
            continue
        segments.append({
            'ep': ep, 't': row['t'], 'text': text.strip(), 'raw': 0,
            'event_id': primary, 'primary_evidence_id': primary, 'evidence_ids': ids,
            'block': line.get('block'), 'tone': line.get('tone'),
            'inference': bool(line.get('inference', False)),
        })
        last_primary = primary
    if errors:
        raise ValueError('; '.join(errors))
    stem = name or f'EP{ep:02d}_{Path(source.get("video", "story")).stem}_解说'
    plan = {'name': safe_stem(stem), 'series_index': series_index,
            'source_video': source.get('video'), 'source_script': 'script.json',
            'segments': segments}
    normalized, validation_errors = validate_plan(plan, {e['id']: e for e in events}, 0.0)
    if validation_errors:
        raise ValueError('plan validation failed: ' + '; '.join(validation_errors))
    return normalized, {'events': events}


def write_atomic(path: Path, value) -> None:
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Map S3 narration lines to timestamped S2 evidence and validate a build plan.')
    ap.add_argument('task', help='task directory with source.json, evidence.json, and script.json')
    ap.add_argument('--out', help='output plan path (default: <task>/plan.json)')
    ap.add_argument('--events-out', help='evidence event library (default: <task>/plan-events.json)')
    ap.add_argument('--name', help='output video filename stem')
    ap.add_argument('--series-index', type=int, default=1)
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args(argv)
    task = Path(args.task).resolve()
    out = Path(args.out).resolve() if args.out else task / 'plan.json'
    events_out = Path(args.events_out).resolve() if args.events_out else task / 'plan-events.json'
    if args.series_index < 1:
        print('[ERR] --series-index must be positive', file=sys.stderr)
        return 2
    if out == events_out:
        print('[ERR] --out and --events-out must be different files', file=sys.stderr)
        return 2
    existing = [str(p) for p in (out, events_out) if p.exists()]
    if existing and not args.force:
        print('[ERR] output already exists; use a new task or --force: ' + ', '.join(existing), file=sys.stderr)
        return 2
    try:
        plan, event_data = make_plan(task, args.name, args.series_index)
    except Exception as e:
        print(f'[ERR] {type(e).__name__}: {e}', file=sys.stderr)
        return 2
    write_atomic(events_out, event_data)
    write_atomic(out, plan)
    print(json.dumps({'status': 'valid', 'segments': len(plan['segments']),
                      'narration_chars': plan['total_narration_chars'],
                      'episode': plan['segments'][0]['ep'],
                      'event_evidence_checked': True,
                      'plan': str(out), 'events': str(events_out)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
