"""Validate and normalize a human-curated edit plan for build.py.

This script does not invent narration or select shots. When an events.json file
is supplied, every segment must point to a VLM-analyzed event at the same
episode and a nearby source timestamp.
"""
import argparse
import json
import math
import os
import re
import sys
from pathlib import Path


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


# ★ v3.7.1 冷开场钩子上限（与 engine.json compliance.cold_open.max_sec 同源）。
#   validate_plan 用它对 cold_open 段的 duration 做 sanity 上界校验；取不到回落 5.0。
try:
    _CFG = json.loads((Path(__file__).resolve().parent.parent / 'config' / 'engine.json').read_text(encoding='utf-8'))
    COLD_OPEN_MAX = float((_CFG.get('compliance', {}).get('cold_open') or {}).get('max_sec', 5.0))
except Exception:
    COLD_OPEN_MAX = 5.0


# ── 集号 → 源文件解析（唯一实现）────────────────────
# ★ 为什么收到这里（2026-10-21 源头优化）：
#   build.py::find_src 原本只认 `{ep}.mp4` 与 `第{ep}集.mp4` 两种命名，
#   而真实素材目录普遍是 `001_剧名-第1集.mp4` 这种「序号_标题-第N集」命名。
#   后果：series_decompose 能分解（它有自己的 ep_key），build.py 却找不到文件，
#   整条「多集系列混剪」路径在最后一步崩掉。
#   series_decompose.py 也有一份 ep_key —— 同一份逻辑两个副本，
#   改一处漏一处。→ 统一到本模块，两个消费方都 import。

_EP_NUM = re.compile(r'(\d+)')


def ep_from_name(stem: str) -> int:
    """从文件名解析集号：取第一个数字（`001_xxx-第1集` → 1，`第12集` → 12）。"""
    m = _EP_NUM.search(stem or '')
    return int(m.group(1)) if m else 0


def find_ep_file(dataset: Path, ep: int) -> Path:
    """在素材目录里定位第 ep 集的源文件。

    依次尝试：`{ep}.mp4` → `第{ep}集.mp4` → 扫描目录按文件名解析集号。
    扫描时同集号多份副本取文件更大的那份（正片通常更大）。
    """
    if not isinstance(ep, int) or isinstance(ep, bool) or ep < 1:
        raise ValueError(f'集号必须是正整数，收到 {ep!r}')
    for cand in (dataset / f'{ep}.mp4', dataset / f'第{ep}集.mp4'):
        if cand.is_file():
            return cand
    if not dataset.is_dir():
        raise FileNotFoundError(f'素材目录不存在: {dataset}')
    best: Path | None = None
    best_size = -1
    for v in sorted(dataset.glob('*.mp4')):
        if ep_from_name(v.stem) != ep:
            continue
        try:
            size = v.stat().st_size
        except OSError:
            continue
        if size > best_size:
            best, best_size = v, size
    if best is None:
        raise FileNotFoundError(
            f'第{ep}集 源文件不存在（找过 {ep}.mp4、第{ep}集.mp4，'
            f'并扫描了 {dataset} 下全部 mp4 的文件名集号）')
    return best


def validate_plan(plan: dict, events: dict[str, dict] | None,
                  time_tolerance: float) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if not isinstance(plan, dict):
        return {}, ['plan root must be a JSON object']

    name = plan.get('name')
    if not isinstance(name, str) or not name.strip():
        errors.append('name must be a non-empty string')
    elif name != Path(name).name or re.search(r'[<>:"/\\|?*]', name):
        errors.append('name must be a filename stem without path or reserved characters')
    if 'series_index' in plan:
        series_index = plan['series_index']
        if isinstance(series_index, bool) or not isinstance(series_index, int) or series_index < 1:
            errors.append('series_index must be a positive integer when supplied')

    segments = plan.get('segments')
    if not isinstance(segments, list) or not segments:
        errors.append('segments must be a non-empty array')
        return plan, errors

    normalized = []
    for idx, original in enumerate(segments, 1):
        where = f'segments[{idx - 1}]'
        if not isinstance(original, dict):
            errors.append(f'{where} must be an object')
            continue
        seg = dict(original)

        ep = seg.get('ep')
        if isinstance(ep, bool) or not isinstance(ep, int) or ep < 1:
            errors.append(f'{where}.ep must be a positive integer')

        t = seg.get('t')
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(float(t)) or t < 0:
            errors.append(f'{where}.t must be a finite non-negative number')
        else:
            seg['t'] = float(t)

        text = seg.get('text')
        if not isinstance(text, str):
            errors.append(f'{where}.text must be a string')
            text = ''
        text = text.strip()
        raw = seg.get('raw', 0)
        if isinstance(raw, bool):
            raw = int(raw)
        if raw not in (0, 1):
            errors.append(f'{where}.raw must be 0 or 1')
            raw = 0
        # build.py selects the audio path from text presence; normalize raw to
        # that same rule and reject contradictory plans instead of guessing.
        if text and raw == 1:
            errors.append(f'{where} has narration text but raw=1; clear raw or clear text')
        raw = 1 if not text else 0
        seg['text'] = text
        seg['raw'] = raw

        # These values are measured during assembly or derived from trusted
        # events. Never preserve hand-written/stale values from an input draft.
        # ★ v3.7.1：cold_open 段的 `duration` 是人工设定的目标时长（已校验 ≤
        #   cold_open.max_sec），属可信输入，必须保留（否则 retime/est_total
        #   失去基准）；普通段仍清掉手填 duration，避免陈旧值污染。
        _derived = ('raw_start', 'clip_duration', 'evidence')
        if not seg.get('is_cold_open'):
            _derived = ('duration',) + _derived
        for derived in _derived:
            seg.pop(derived, None)

        # ★ v3.7.1：冷开场钩子时长 sanity 校验（exempt 普通段）
        if seg.get('is_cold_open'):
            cd = seg.get('duration')
            if not isinstance(cd, (int, float)) or not math.isfinite(cd) or cd <= 0:
                errors.append(f'{where}.duration must be a positive number for cold_open')
            elif cd > COLD_OPEN_MAX:
                errors.append(f'{where}.duration {cd}s exceeds cold_open max_sec {COLD_OPEN_MAX}s')

        event_id = seg.get('event_id')
        if events is not None:
            if not isinstance(event_id, str) or not event_id:
                errors.append(f'{where}.event_id is required when --events is used')
            else:
                event = events.get(event_id)
                if event is None:
                    errors.append(f'{where}.event_id {event_id!r} is absent from events.json')
                else:
                    if event.get('ep') != ep:
                        errors.append(f'{where}.ep does not match event {event_id!r}')
                    event_t = event.get('t')
                    if (not isinstance(event_t, (int, float)) or isinstance(event_t, bool)
                            or not math.isfinite(float(event_t))):
                        errors.append(f'event {event_id!r} has no valid source timestamp')
                    elif isinstance(t, (int, float)) and abs(float(event_t) - float(t)) > time_tolerance:
                        errors.append(f'{where}.t differs from event {event_id!r} by more than '
                                      f'{time_tolerance:.2f}s')
                    else:
                        seg['evidence'] = {
                            'event_id': event_id,
                            'description': event.get('event', ''),
                            'dialogue': event.get('dialogue', ''),
                            'speaker': event.get('speaker', ''),
                            'tension': event.get('tension'),
                        }
                    if raw and not seg.get('is_cold_open') and not str(event.get('dialogue') or '').strip():
                        errors.append(f'{where} is raw audio but event {event_id!r} has no dialogue')

        normalized.append(seg)

    out = dict(plan)
    out['segments'] = normalized
    out['total_narration_chars'] = sum(len(s.get('text', '')) for s in normalized)
    out['plan_validation'] = {
        'validator': 'plan_story.py',
        'event_evidence_checked': events is not None,
        'time_tolerance_sec': time_tolerance if events is not None else None,
    }
    return out, errors


def load_events(path: Path) -> dict[str, dict]:
    data = load_json(path)
    rows = data.get('events') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError('events.json must contain an events array')
    out = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get('id'), str):
            if row['id'] in out:
                raise ValueError(f'duplicate event id: {row["id"]}')
            out[row['id']] = row
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Validate a curated plan.json; does not generate narration or choose shots.')
    ap.add_argument('draft', help='human-curated plan JSON')
    ap.add_argument('--out', required=True, help='new normalized plan path')
    ap.add_argument('--events', help='optional VLM event library; enables ep/time evidence checks')
    ap.add_argument('--time-tolerance', type=float, default=0.75,
                    help='maximum time difference from an event timestamp in seconds')
    ap.add_argument('--force', action='store_true', help='replace an existing output file')
    args = ap.parse_args(argv)

    source = Path(args.draft).resolve()
    target = Path(args.out).resolve()
    if source == target:
        print('[ERR] --out must differ from the draft; input plans are never overwritten', file=sys.stderr)
        return 2
    if target.exists() and not args.force:
        print(f'[ERR] output exists; choose another path or pass --force: {target}', file=sys.stderr)
        return 2
    if not math.isfinite(args.time_tolerance) or args.time_tolerance < 0:
        print('[ERR] --time-tolerance must be finite and non-negative', file=sys.stderr)
        return 2

    try:
        draft = load_json(source)
        events = load_events(Path(args.events).resolve()) if args.events else None
    except Exception as e:
        print(f'[ERR] cannot read input: {type(e).__name__}: {e}', file=sys.stderr)
        return 2

    normalized, errors = validate_plan(draft, events, args.time_tolerance)
    if errors:
        print(json.dumps({'status': 'invalid', 'errors': errors}, ensure_ascii=False, indent=2))
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)

    print(json.dumps({
        'status': 'valid',
        'segments': len(normalized['segments']),
        'narration_segments': sum(bool(s['text']) for s in normalized['segments']),
        'raw_segments': sum(s['raw'] == 1 for s in normalized['segments']),
        'total_narration_chars': normalized['total_narration_chars'],
        'event_evidence_checked': events is not None,
        'output': str(target),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
