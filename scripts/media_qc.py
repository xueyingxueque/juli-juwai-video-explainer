# -*- coding: utf-8 -*-
r"""media_qc.py —— 成片客观媒体质检层（v6.0.0 新增 / v6.0.1 实链校正）。

★ 为什么新增（本地全链审计 + 全网 QC 调研）：
  build.verify()                    只管音频（音轨长度 / 静音 / LUFS / 真峰值）；
  quality_gate.hard_technical()     只管 ffprobe 元数据（分辨率 / 时长 / 双流 / 体积）；
  全链 **零 blackdetect / 零 freezedetect** —— 黑帧、卡帧、静止画面这类
  **纯视觉**故障没有任何自动检出，只能靠人抽帧（F12「校验通过 ≠ 画面正确」）。

做什么（ffmpeg/ffprobe 用 engine.json 里的绝对路径，零联网、不重编码）：
  ① blackdetect    黑帧区间    ② freezedetect 冻结帧区间
  ③ silencedetect  超长静音    ④ 音画流时长差  ⑤ 成片时长（policy.check_measured）
  ⑥ AIGC 隐式标识  compliance.has_aigc_tag()（唯一判据，P92）

★ v6.0.1（2026-11-13 真实剧集 MW1 实链验证）两处校正：
  ① 报告补 freeze_start / freeze_end **时间点** —— 旧版只报时长，排障靠猜，
     违反「失败要看得见」。
  ② 冻结帧分「预期静止」与「真异常」：封面版首帧（烧录静态封面）与
     gen 二创静态图段本就该静止，freezedetect 一律 fail 是纯噪声，会把真卡帧淹没
     （告警成立条件 = 状态异常 × **这件事还需要被做**）。预期静止区间由
     `--from-plan + --shotmap` 从装配产物推导，或 `--expected-still` 显式给。
     **都不给时维持保守：所有冻结仍判 fail**（读不到上下文 ≠ 没有异常，P84）。
     预期区间内的冻结仍完整列在报告里并标 `expected`，**绝不静默吞掉**。

★ 三条纪律：
  1. 解析失败 = unknown，不是 pass（滤镜挂了 / 输出读不到，一律走保守路径）。
  2. 默认只报告不拦；`--strict` 才把 fail/unknown 判失败（新判据不否定历史成片）。
  3. 数值只从 engine.json → media_qc_policy 取，禁止本文件写第二份（P88）。

用法：
    python scripts/media_qc.py <video> [--strict] [--out report.json]
    python scripts/media_qc.py <video> --from-plan <plan-N-v37.json> --shotmap <shotmap.json>
    python scripts/media_qc.py <video> --expected-still <[[start,end],...].json>
退出码：默认 0；--strict 且存在 fail/unknown → 1；输入不存在 → 2。
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import contracts                    # noqa: E402  配置契约唯一入口
import compliance                   # noqa: E402  AIGC 标识唯一判据
from policy import check_measured   # noqa: E402  时长唯一判据

CFG, _SRC = contracts.load()
P = CFG.get('media_qc_policy')
if not P:
    raise RuntimeError('[media_qc] media_qc_policy 段缺失 —— 读不到 ≠ 用内置默认值')
_REQUIRED = ('black_min_sec', 'black_pix_th', 'black_pic_th',
             'freeze_min_sec', 'freeze_noise_th', 'silence_min_sec',
             'silence_noise_db', 'max_av_drift_sec', 'require_aigc_tag',
             'cover_still_sec', 'still_tolerance_sec')
_MISSING = [k for k in _REQUIRED if k not in P]
if _MISSING:
    raise RuntimeError(f'[media_qc] media_qc_policy 缺字段 {_MISSING} —— 拒绝静默回退')

FF = CFG['runtime']['ffmpeg']
FFPROBE = CFG['runtime']['ffprobe']

_RE_BLACK = re.compile(
    r'black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)\s+black_duration:\s*([\d.]+)')
_RE_FREEZE_START = re.compile(r'freeze_start:\s*([\d.]+)')
_RE_FREEZE_END = re.compile(r'freeze_end:\s*([\d.]+)')
_RE_FREEZE_DUR = re.compile(r'freeze_duration:\s*([\d.]+)')
_RE_SILENCE_DUR = re.compile(r'silence_duration:\s*([\d.]+)')


def _item(name, status, detail, **extra):
    d = {'name': name, 'status': status, 'detail': detail}
    d.update(extra)
    return d


def _ff(args, timeout=900):
    """→ (stderr_text, err)。工具失败/超时 = err（unknown，绝不静默当通过）。"""
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
    except Exception as e:
        return '', f'{type(e).__name__}: {e}'
    if r.returncode != 0:
        return '', f'rc={r.returncode}: {(r.stderr or "")[-240:]}'
    return r.stderr or '', None


def classify_freezes(intervals, expected, tolerance):
    """→ (unexpected, expected_hits)：预期静止区间外的才算真异常。

    ★ 可离线双向验证的**纯函数**：不吞任何区间，只做归类。
      调用方必须同时保留 expected_hits 并写进报告。
    """
    def covered(a, b):
        return any(a >= lo - tolerance and b <= hi + tolerance
                   for lo, hi in expected)

    bad, ok = [], []
    for it in intervals:
        (ok if covered(it['start'], it['end']) else bad).append(it)
    return bad, ok


def expected_still_from_plan(plan_path, shotmap_path):
    """→ (intervals, note)：从 plan 的 gen 静态图段 + shotmap 成片时间轴推导预期静止区间。

    启发式：plan 段 `t` 与 shotmap 段 `source_start_sec` 同指源片时间点，按 1.0s
    容差对齐；另按 `event_id` 精确对齐。**推导结果会完整写进报告供人工核对**，
    推导失败返回空区间（调用方随即走保守 fail），绝不静默放宽。
    """
    try:
        plan = json.loads(Path(plan_path).read_text(encoding='utf-8'))
        sm = json.loads(Path(shotmap_path).read_text(encoding='utf-8'))
    except Exception as e:
        return [], f'上下文读不到（按保守处理）：{type(e).__name__}: {e}'
    ps = plan.get('segments', plan) if isinstance(plan, dict) else plan
    segs = sm.get('segments', [])
    gen_t, gen_events = [], set()
    for p in ps:
        if not p.get('gen'):
            continue
        try:
            gen_t.append(float(p.get('t', 0)))
        except (TypeError, ValueError):
            pass
        if p.get('event_id') is not None:
            gen_events.add(p.get('event_id'))
    intervals = [[0.0, float(P['cover_still_sec'])]]
    for s in segs:
        try:
            src = float(s.get('source_start_sec', -999))
            a, b = float(s['output_start_sec']), float(s['output_end_sec'])
        except (TypeError, ValueError, KeyError):
            continue
        if (s.get('event_id') in gen_events
                or any(abs(src - t) < 1.0 for t in gen_t)):
            intervals.append([a, b])
    return intervals, (f'封面首帧 + {len(intervals) - 1} 个 gen 静态图段'
                       f'（plan={Path(plan_path).name}）')


def check_visual(video, total_duration=0.0, expected_still=None):
    """黑帧 + 冻结帧，一次解码两条视频滤镜。

    ★ P95：本机 ffmpeg 片尾冻结只输出 freeze_start，不输出 end/duration
      → 有 start 无配对时用片长推算；片长读不到记 unknown，绝不静默当通过。
    ★ v6.0.1：冻结区间带时间点，并按预期静止区间分类（预期项仍完整列出）。
    """
    vf = (f"blackdetect=d={float(P['black_min_sec'])}:"
          f"pix_th={float(P['black_pix_th'])}:pic_th={float(P['black_pic_th'])},"
          f"freezedetect=n={float(P['freeze_noise_th'])}:d={float(P['freeze_min_sec'])}")
    log, err = _ff([FF, '-v', 'info', '-i', str(video), '-vf', vf,
                    '-an', '-f', 'null', '-'])
    if err:
        why = f'视觉滤镜执行失败（unknown，非通过）：{err}'
        return [_item('black_frames', 'unknown', why),
                _item('freeze_frames', 'unknown', why)]
    blacks = [(float(a), float(b), float(c)) for a, b, c in _RE_BLACK.findall(log)]
    black_item = _item(
        'black_frames', 'fail' if blacks else 'pass',
        (f'检出 {len(blacks)} 段黑帧（d≥{P["black_min_sec"]}s）' if blacks
         else f'未检出超 {P["black_min_sec"]}s 黑帧'),
        count=len(blacks), items=blacks[:8])

    starts = [float(x) for x in _RE_FREEZE_START.findall(log)]
    ends = [float(x) for x in _RE_FREEZE_END.findall(log)]
    durs = [float(x) for x in _RE_FREEZE_DUR.findall(log)]
    intervals = []
    for i, s in enumerate(starts):
        if i < len(ends):
            e, inferred = ends[i], False
        elif i < len(durs):
            e, inferred = s + durs[i], False
        elif total_duration > s:
            e, inferred = total_duration, True      # 片尾冻结，P95 推算
        else:
            return [black_item, _item(
                'freeze_frames', 'unknown',
                'freezedetect 报出 freeze_start 但片长读不到，无法判定冻结时长',
                freeze_starts=starts[:8])]
        intervals.append({'start': round(s, 2), 'end': round(e, 2),
                          'duration': round(e - s, 2), 'inferred': inferred})
    if durs and not starts:
        return [black_item, _item(
            'freeze_frames', 'unknown',
            '只解析到 freeze_duration 却没有起点，无法定位（拒绝静默放行）',
            durations=durs[:8])]

    expected = expected_still or []
    bad, ok = classify_freezes(intervals, expected, float(P['still_tolerance_sec']))
    status = 'fail' if bad else ('expected' if ok else 'pass')
    if bad:
        detail = (f'{len(bad)} 段非预期冻结（疑似卡帧），另有 '
                  f'{len(ok)} 段落在预期静止区间')
    elif ok:
        detail = (f'{len(ok)} 段冻结全部落在预期静止区间'
                  '（封面首帧 / 二创静态图），已记录但不判故障')
    else:
        detail = f'未检出超 {P["freeze_min_sec"]}s 冻结帧'
    return [black_item, _item(
        'freeze_frames', status, detail,
        count=len(intervals), unexpected_count=len(bad), expected_count=len(ok),
        intervals=intervals[:12], expected_intervals=expected[:12],
        inferred=any(x['inferred'] for x in intervals))]


def check_audio(video):
    """超长静音复检（记忆铁律：判静音用 silencedetect，不用 astats）。"""
    af = f"silencedetect=noise={int(P['silence_noise_db'])}dB:d={float(P['silence_min_sec'])}"
    log, err = _ff([FF, '-v', 'info', '-i', str(video), '-af', af,
                    '-vn', '-f', 'null', '-'])
    if err:
        return [_item('long_silence', 'unknown',
                      f'音频滤镜执行失败（unknown，非通过）：{err}')]
    runs = [float(x) for x in _RE_SILENCE_DUR.findall(log)]
    return [_item('long_silence', 'fail' if runs else 'pass',
                  (f'检出 {len(runs)} 段超长静音（d≥{P["silence_min_sec"]}s）' if runs
                   else f'未检出超 {P["silence_min_sec"]}s 静音'),
                  count=len(runs), durations=runs[:8])]


def check_probe(video):
    """→ (items, measured)：音画流时长差 + 成片时长。"""
    try:
        r = subprocess.run([FFPROBE, '-v', 'error', '-show_streams', '-show_format',
                            '-of', 'json', str(video)],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=120)
    except Exception as e:
        return [_item('av_drift', 'unknown', f'ffprobe 异常：{type(e).__name__}: {e}')], None
    if r.returncode != 0:
        return [_item('av_drift', 'unknown',
                      f'ffprobe rc={r.returncode}: {(r.stderr or "")[-160:]}')], None
    try:
        info = json.loads(r.stdout or '{}')
    except Exception as e:
        return [_item('av_drift', 'unknown',
                      f'ffprobe 输出不可解析：{type(e).__name__}')], None
    streams = info.get('streams') or []
    v = next((s for s in streams if s.get('codec_type') == 'video'), None)
    a = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    try:
        dur = float((info.get('format') or {}).get('duration') or 0)
    except (TypeError, ValueError):
        dur = 0.0
    vd = float(v['duration']) if v and v.get('duration') else None
    ad = float(a['duration']) if a and a.get('duration') else None
    items = []
    if not v or not a:
        items.append(_item('av_drift', 'unknown',
                           f'缺流：video={bool(v)} audio={bool(a)}'))
    elif vd is None or ad is None:
        items.append(_item('av_drift', 'unknown', '音/视频流时长字段读不到'))
    else:
        drift = abs(vd - ad)
        limit = float(P['max_av_drift_sec'])
        items.append(_item('av_drift', 'fail' if drift > limit else 'pass',
                           f'视频 {vd:.2f}s / 音频 {ad:.2f}s，差 {drift:.2f}s'
                           f'（上限 {limit}s）', drift_sec=round(drift, 3)))
    dur_ok, dur_msg = check_measured(dur)
    items.append(_item('duration_bounds', 'pass' if dur_ok else 'fail',
                       dur_msg, duration_sec=dur))
    return items, {'duration': dur, 'video_duration': vd, 'audio_duration': ad}


def check_aigc(video):
    """隐式标识实测 —— 唯一判据在 compliance.py，不在此拼第二份 tags 判断。"""
    ok, why = compliance.has_aigc_tag(video)
    if why.startswith('unknown:'):
        return [_item('aigc_implicit_tag', 'unknown',
                      f'标识状态读不到（保守处理）：{why}')]
    required = bool(P['require_aigc_tag'])
    status = 'pass' if ok else ('fail' if required else 'pass')
    return [_item('aigc_implicit_tag', status,
                  f'{why}；要求写入={required}',
                  note='用 compliance.py stamp --apply 补标（幂等，写后复核）')]


def run(video, expected_still=None, still_note=''):
    """→ 完整报告 dict。不写任何生产文件。"""
    probe_items, measured = check_probe(video)   # 先拿片长（P95 片尾冻结推算依赖它）
    total = float((measured or {}).get('duration') or 0.0)
    checks = []
    checks += check_visual(video, total, expected_still)
    checks += check_audio(video)
    checks += probe_items
    checks += check_aigc(video)
    n_fail = sum(c['status'] == 'fail' for c in checks)
    n_unknown = sum(c['status'] == 'unknown' for c in checks)
    n_pass = sum(c['status'] == 'pass' for c in checks)
    n_expected = sum(c['status'] == 'expected' for c in checks)
    return {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'engine_version': CFG.get('engine_version'),
        'video': str(video),
        'policy': P,
        'measured': measured,
        'expected_still_source': still_note,
        'checks': checks,
        'summary': {
            'pass': n_pass, 'fail': n_fail, 'unknown': n_unknown,
            'expected': n_expected,
            'ok': n_fail == 0 and n_unknown == 0,
            'note': ('unknown 一律不计入通过；expected 项完整留在报告里但不判故障；'
                     '未提供预期静止上下文时所有冻结仍判 fail'),
        },
        'caveat': ('客观媒体检查不等于人工审片；黑帧/冻结帧也可能是有意的转场或'
                   '二创静态图设计，命中后须抽帧确认（F12）'),
    }


def main():
    ap = argparse.ArgumentParser(description='成片客观媒体质检（纯本地、零联网）')
    ap.add_argument('video')
    ap.add_argument('--out', default='', help='报告 JSON 路径；不写则只打印')
    ap.add_argument('--strict', action='store_true',
                    help='存在 fail/unknown 时退出码 1（用于新片交付前复核）')
    ap.add_argument('--json', action='store_true', help='只输出 JSON，不打摘要')
    ap.add_argument('--expected-still', default='',
                    help='预期静止区间 JSON：[[start,end],...] 或 {"intervals": [...]}')
    ap.add_argument('--from-plan', default='', help='plan-<ep>-v37.json（推导 gen 静态图段）')
    ap.add_argument('--shotmap', default='', help='shotmap.json（成片时间轴）')
    a = ap.parse_args()
    v = Path(a.video)
    if not v.is_file():
        print(f'[FAIL] 视频不存在：{v}')
        return 2
    expected, note = [], '未提供预期静止上下文（保守：所有冻结判 fail）'
    if a.expected_still:
        try:
            data = json.loads(Path(a.expected_still).read_text(encoding='utf-8'))
            expected = data.get('intervals', []) if isinstance(data, dict) else data
            note = f'显式区间 {Path(a.expected_still).name}（{len(expected)} 段）'
        except Exception as e:
            print(f'[FAIL] 预期静止区间读不到（拒绝静默放宽）：{type(e).__name__}: {e}')
            return 2
    elif a.from_plan and a.shotmap:
        expected, note = expected_still_from_plan(a.from_plan, a.shotmap)
    rep = run(v, expected, note)
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f'── 成片客观媒体质检 · {v.name} ──')
        print(f'  预期静止上下文: {note}')
        mark = {'pass': '✓', 'fail': '✗', 'unknown': '?', 'expected': '·'}
        for c in rep['checks']:
            print(f'  {mark[c["status"]]} {c["name"]:20s} {c["detail"]}')
        s = rep['summary']
        print(f'\n  pass {s["pass"]} / fail {s["fail"]} / unknown {s["unknown"]}'
              f' / expected {s["expected"]} · ok={s["ok"]} · strict={a.strict}')
        if a.out:
            print(f'[out] {a.out}')
    if a.strict and not rep['summary']['ok']:
        print('[STRICT FAIL] 存在 fail/unknown —— 不放行')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
