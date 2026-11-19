# -*- coding: utf-8 -*-
r"""shot_rank.py —— **选镜候选打分**（本地 ffmpeg 代理指标，零联网、零 GPU）。

★ 为什么新增（2026-11-09 全网调研，对标 Mixpeek「四阶段高光检测」与
  clipforge 的 RMS 能量选段）：本技能此前由 Agent 人肉读字幕定镜头，
  没有「先召回候选、再人工定稿」的底座；VLM 逐镜采证据是全量扫，成本高。
  本模块用本机 ffmpeg 的三个**客观信号**先给出候选排序，缩小选择范围。

  分解 → 打分 →（可选的区间合并）→ 排序：
    ① ffmpeg silencedetect 找非静音区间（对白/冲突密度代理）
    ② ffmpeg scene 变化点（画面切换密度代理，可选 --scene）
    ③ 已入库 ASR 台词密度（若 _s1/epN/asr_full.json 存在）
  打分 = 加权合成，全部为**代理指标**，不替代人工/VLM 对表演强度的判断。

用法：
    python scripts/shot_rank.py <素材目录> --ep 1 --top 20 [--scene]
    python scripts/shot_rank.py <素材目录> --ep 1 --out <候选.json>
输出：`_plan/shot_candidates-ep<N>.json`（默认写到任务根 _tools 下，可 --out 指定）

纪律：不引新 Python 依赖；ffmpeg 只做分析不写视频；失败优雅降级并标注 signals。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import contracts                   # noqa: E402
sys.path.insert(0, str(ROOT / 'scripts'))
from plan_story import find_ep_file, ep_from_name   # noqa: E402

CFG, _ = contracts.load()
FF = CFG['runtime']['ffmpeg']
FFPROBE = CFG['runtime']['ffprobe']
TASK_ROOT = Path(CFG['paths']['task_root'])


def probe_dur(video):
    r = subprocess.run([FFPROBE, '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'csv=p=0', str(video)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def silence_windows(video):
    """非静音区间 [(start,end)] —— 用 silencedetect，不用 astats（Windows 实测不稳）。"""
    r = subprocess.run(
        [FF, '-v', 'info', '-i', str(video), '-af',
         'silencedetect=noise=-35dB:d=0.5', '-f', 'null', '-'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=900)
    txt = r.stderr or ''
    ends = [float(x) for x in re.findall(r'silence_end:\s*([\d.]+)', txt)]
    starts = [float(x) for x in re.findall(r'silence_start:\s*([\d.]+)', txt)]
    silences = []
    for i, st in enumerate(starts):
        en = next((e for e in ends if e > st), None)
        silences.append((st, en or probe_dur(video)))
    dur = probe_dur(video)
    loud, cur = [], 0.0
    for st, en in silences:
        if st > cur:
            loud.append((cur, st))
        cur = max(cur, en)
    if dur > cur:
        loud.append((cur, dur))
    return [w for w in loud if w[1] - w[0] >= 1.0], dur


def scene_points(video):
    r = subprocess.run(
        [FF, '-v', 'info', '-i', str(video),
         '-vf', "select='gt(scene,0.35)',showinfo", '-f', 'null', '-'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=900)
    pts = [float(x) for x in re.findall(r'pts_time:\s*([\d.]+)', r.stderr or '')]
    return sorted(set(pts))


def asr_density(task, ep):
    """台词时间段（若有已入库 ASR），用于给候选点加台词密度分。"""
    out = []
    for p in sorted((TASK_ROOT / task / '_s1' / f'ep{ep}').glob('*.json')):
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        for s in d.get('segments', []):
            try:
                out.append((float(s.get('start', 0)), float(s.get('end', 0))))
            except Exception:
                pass
    return out


def rank(dataset, ep, top, use_scene, out):
    ds = Path(dataset)
    video = find_ep_file(ds, int(ep))
    loud, dur = silence_windows(video)
    scenes = scene_points(video) if use_scene else []
    lines = asr_density(ds.name, ep)
    cands = []
    step = 6.0
    for st, en in loud:
        t = st
        while t < en - 0.5:
            # 信号①：离静音边界越远越稳；②：附近 3s 有镜头切换加分；③：台词密度加分
            inner = min(t - st, en - t) / max(1.0, (en - st) / 2)
            near_scene = any(abs(s - t) <= 3.0 for s in scenes)
            near_line = sum(1 for a, b in lines if a <= t <= b)
            score = (0.35 * inner + (0.30 if near_scene else 0.0)
                     + min(0.35, 0.12 * near_line))
            cands.append({'ep': int(ep), 't': round(t, 2),
                          'score': round(score, 3),
                          'signals': {'non_silent_window': [round(st, 2), round(en, 2)],
                                      'scene_change_nearby': near_scene,
                                      'asr_lines_covering': near_line}})
            t += step
    cands.sort(key=lambda x: -x['score'])
    cands = cands[:top]
    d = {
        'source': str(video), 'ep': int(ep), 'duration_sec': round(dur, 2),
        'candidate_count': len(cands),
        'method': 'ffmpeg silencedetect + 可选 scene + 已入库 ASR 台词密度',
        'caveat': ('全部为客观代理指标：能召回「对白密集/切镜频繁」的片段，'
                   '但**不能判断表演张力与戏眼**，最终选镜仍需 Agent 结合 transcript 与抽帧定稿。'),
        'candidates': cands,
    }
    Path(out).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    return d


def main():
    ap = argparse.ArgumentParser(description='本地镜头候选打分（不生成视频）')
    ap.add_argument('dataset'); ap.add_argument('--ep', required=True)
    ap.add_argument('--top', type=int, default=20)
    ap.add_argument('--scene', action='store_true', help='额外跑场景变化检测（更慢）')
    ap.add_argument('--out', default='')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    out = a.out or str(TASK_ROOT / '_tools' / f'shot_candidates-ep{a.ep}.json')
    d = rank(a.dataset, a.ep, a.top, a.scene, out)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        print(f'  {Path(d["source"]).name}  {d["duration_sec"]}s  → {d["candidate_count"]} 个候选')
        for c in d['candidates'][:6]:
            print(f'    t={c["t"]:7.2f}s  score={c["score"]:.3f}  '
                  f'切镜={c["signals"]["scene_change_nearby"]} 台词={c["signals"]["asr_lines_covering"]}')
        print(f'  ⚠ {d["caveat"][:60]}…')
        print(f'  → {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
