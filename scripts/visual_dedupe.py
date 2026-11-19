# -*- coding: utf-8 -*-
r"""visual_dedupe.py —— 跨集**成片画面**感知去重实证（v6.0.0 第六层，2026-11-13）。

★ 为什么新增（F13 的最后一公里）：
  episode_dedupe.py 是 **spec 元数据级**硬闸：比对 (源片集号, 时间桶) 与解说词 n-gram。
  它验证的是「写稿时声明不重复」，**没有验证成片真实画面**。
  而 anti_dup.py 只负责「打散帧指纹」（hue/eq/noise 后处理），**不做检测**。
  → 「画面到底重复没重复」全链零实证，只能靠人抽帧（F12）。
  这与「实切 X 与度量 X 必须同一真相源」是同一类问题：
  声明层有闸门、实证层没有，改一处必漏另一处。

做什么（纯本地 ffmpeg + 标准库，**不依赖 Pillow / 不联网 / 不写生产素材**）：
  ffmpeg 按固定间隔抽帧 → scale 到 (hash_size+1)×hash_size 灰度 rawvideo →
  Python 直接读字节算 dHash（64bit 整数）→ 跨集两两算汉明距离 →
  输出「哪些帧在不同集里重复出现」的可核对报告。

边界（勿混为一谈）：
  · dedupe_policy / episode_dedupe.py = 写稿时的**硬闸**（超阈值直接 FAIL）
  · visual_dedupe_policy / 本脚本     = 出片后的**实证报告**（只报告不拦）
  原片不同集本就可能有相似镜头，一律硬拦会误杀；两者互补、不可互相替代。

★ 纪律：
  1. 抽帧字节数不能被帧大小整除 = **unknown**（读不到 ≠ 没有，P84）。
  2. 分母取**较小集**（与 episode_dedupe 对称判据同源，避免长集被自己规模稀释）。
  3. 数值只从 engine.json → visual_dedupe_policy 取，禁止本文件写第二份（P88）。
  4. `--pilot` 从 episodes.json 清单取集（清单是唯一参数源，不手打路径）。

用法：
    python scripts/visual_dedupe.py --pilot <episodes.json> [--out report.json]
    python scripts/visual_dedupe.py <a.mp4> <b.mp4> [c.mp4...] [--out report.json]
退出码：0 = 报告完成（含 unknown / 超阈值也只报告）；2 = 输入不可用。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import contracts                     # noqa: E402  配置契约唯一入口
import pipeline_state as ps          # noqa: E402  清单/已出片判定唯一真相源

CFG, _SRC = contracts.load()
P = CFG.get('visual_dedupe_policy')
if not P:
    raise RuntimeError('[visual_dedupe] visual_dedupe_policy 段缺失 —— 读不到 ≠ 用内置默认值')
_REQUIRED = ('sample_interval_sec', 'hash_size', 'max_hamming_distance',
             'max_duplicate_ratio', 'max_frames_per_video')
_MISSING = [k for k in _REQUIRED if k not in P]
if _MISSING:
    raise RuntimeError(f'[visual_dedupe] visual_dedupe_policy 缺字段 {_MISSING}'
                       ' —— 拒绝静默回退')
FF = CFG['runtime']['ffmpeg']


def frame_hashes(video):
    """→ (hashes: list[int], err: str|None)。ffmpeg 抽帧 → dHash 列表。

    ★ 不依赖 Pillow：直接读 `format=gray` 的 rawvideo 字节，
      每帧固定 (hash_size+1)*hash_size 字节，逐行比较相邻像素得到 64bit 指纹。
    """
    h = int(P['hash_size'])
    w = h + 1
    fps = 1.0 / float(P['sample_interval_sec'])
    vf = f"fps={fps},scale={w}:{h}:flags=area,format=gray"
    cmd = [FF, '-v', 'error', '-i', str(video), '-vf', vf,
           '-f', 'rawvideo', '-pix_fmt', 'gray', '-']
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=900)
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'
    if r.returncode != 0:
        tail = (r.stderr or b'').decode('utf-8', errors='replace')[-240:]
        return None, f'ffmpeg rc={r.returncode}: {tail}'
    data = r.stdout or b''
    frame_size = w * h
    if not data or len(data) % frame_size != 0:
        return None, (f'抽帧字节数 {len(data)} 不能被帧大小 {frame_size} 整除'
                      '（unknown，非画面干净的证据）')
    cap = int(P['max_frames_per_video'])
    hashes, truncated = [], False
    for off in range(0, len(data), frame_size):
        block = data[off:off + frame_size]
        bits = 0
        for row in range(h):
            base = row * w
            for col in range(w - 1):
                bits = (bits << 1) | (1 if block[base + col] > block[base + col + 1] else 0)
        hashes.append(bits)
        if len(hashes) >= cap:
            truncated = True
            break
    return hashes, (f'达到帧数上限 {cap}，后续帧未采样' if truncated else None)


def compare(ha, hb):
    """→ (重复帧数, 样例)。limit 内汉明距离视为同一画面。"""
    limit = int(P['max_hamming_distance'])
    interval = float(P['sample_interval_sec'])
    hits, examples = 0, []
    set_b = set(hb)
    for ia, x in enumerate(ha):
        best = None
        if x in set_b:
            best = (0, hb.index(x))
        else:
            for ib, y in enumerate(hb):
                d = (x ^ y).bit_count()
                if d <= limit and (best is None or d < best[0]):
                    best = (d, ib)
        if best is None:
            continue
        hits += 1
        if len(examples) < 5:
            examples.append({'a_frame': ia, 'b_frame': best[1],
                             'hamming': best[0],
                             'a_sec': round(ia * interval, 1),
                             'b_sec': round(best[1] * interval, 1)})
    return hits, examples


def collect_from_pilot(pilot_file):
    """清单驱动：只取**已交付**封面版（ps.final_path 是唯一出片判据）。"""
    eps = ps.load_pilot(pilot_file)
    if eps is None:
        return None, '清单读不到（未知 ≠ 没有），拒绝用空集合假装「无重复」'
    out = []
    for e in eps:
        f = ps.final_path(e)
        if f.is_file():
            out.append((str(e.get('prefix') or f.parent.name), f))
    return out, None


def _unique_labels(videos):
    """★ 保证 label 唯一（v6.0.0 离线验证实测修复，坑典 P94）。

    实测：命令行传入的三支测试片都在同一临时目录，旧版 label 取
    `Path(p).parent.name` → 三个 label 完全相同 → `hashes` 字典被覆盖成
    只剩最后一支 → pairs 为空 → 报「重复率 0.0% / ok=True」**假阴性**。
    这与 P84「状态文件必须按自身记录的归属归位」同源：
    **容器名 ≠ 内容标识**。清单驱动时 prefix 天然唯一，命令行必须兜底。
    """
    labels = [str(lb or p.stem) for lb, p in videos]
    if len(set(labels)) != len(labels):
        labels = [p.stem for _, p in videos]        # 回退到文件名
    if len(set(labels)) != len(labels):
        labels = [f'{s}#{i + 1}' for i, s in enumerate(labels)]
    return [(lb, p) for lb, (_, p) in zip(labels, videos)]


def run(videos):
    """videos: [(label, Path)] → 完整报告 dict。"""
    videos = _unique_labels(videos)
    hashes, unknown, info = {}, [], []
    for label, path in videos:
        hs, err = frame_hashes(path)
        if hs is None:
            unknown.append({'label': label, 'video': str(path), 'reason': err})
            info.append({'label': label, 'video': str(path), 'frames': 0,
                         'status': 'unknown'})
        else:
            hashes[label] = hs
            info.append({'label': label, 'video': str(path), 'frames': len(hs),
                         'status': 'ok', 'note': err or ''})
    labels = sorted(hashes)
    pairs = []
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            ha, hb = hashes[a], hashes[b]
            hits, examples = compare(ha, hb)
            denom = max(1, min(len(ha), len(hb)))
            ratio = hits / denom
            exceeds = ratio > float(P['max_duplicate_ratio'])
            pairs.append({
                'a': a, 'b': b, 'duplicate_frames': hits,
                'denom_smaller_video': denom,
                'ratio': round(ratio, 4),
                'max_allowed_ratio': float(P['max_duplicate_ratio']),
                'exceeds': exceeds,
                'examples': examples,
            })
    worst = max((p['ratio'] for p in pairs), default=0.0)
    return {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'engine_version': CFG.get('engine_version'),
        'policy': P,
        'videos': info,
        'pairs': pairs,
        'unknown': unknown,
        'summary': {
            'videos_compared': len(hashes), 'pairs_compared': len(pairs),
            'worst_ratio': round(worst, 4),
            'exceeded_pairs': [f"{p['a']}↔{p['b']}" for p in pairs if p['exceeds']],
            'ok': not unknown and not any(p['exceeds'] for p in pairs),
            'note': '只报告不拦；ok=false 须人工抽帧确认，不自动判不合格',
        },
        'caveat': ('本层是**成片画面实证**，与 episode_dedupe.py 的 spec 元数据硬闸互补；'
                   '相似镜头不等于违规搬运，命中后须人工复核'),
    }


def main():
    ap = argparse.ArgumentParser(description='跨集成片画面感知去重实证（纯本地）')
    ap.add_argument('videos', nargs='*', help='成片路径（可多个）')
    ap.add_argument('--pilot', default='', help='episodes.json 清单（唯一参数源）')
    ap.add_argument('--out', default='', help='报告 JSON 路径')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    if a.pilot:
        videos, err = collect_from_pilot(a.pilot)
        if err:
            print(f'[FAIL] {err}')
            return 2
    else:
        videos = [(Path(p).parent.name, Path(p)) for p in a.videos]
    videos = [(lb, p) for lb, p in videos if p.is_file()]
    if len(videos) < 1:
        print('[FAIL] 没有可读取的成片（至少 1 个）')
        return 2
    rep = run(videos)
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print('── 跨集成片画面实证 ──')
        for v in rep['videos']:
            print(f'  {v["label"]:8s} {v["frames"]:4d} 帧  {v["status"]}'
                  + (f'  {v.get("note", "")}' if v.get('note') else ''))
        for u in rep['unknown']:
            print(f'  [unknown] {u["label"]}: {u["reason"]}')
        for p in rep['pairs']:
            print(f'  {"✗" if p["exceeds"] else "✓"} {p["a"]}↔{p["b"]}: '
                  f'{p["duplicate_frames"]}/{p["denom_smaller_video"]} = '
                  f'{p["ratio"]:.1%}（上限 {p["max_allowed_ratio"]:.0%}）')
        s = rep['summary']
        print(f'\n  最差重复率 {s["worst_ratio"]:.1%} · ok={s["ok"]}'
              f' · 超阈值对={s["exceeded_pairs"] or "无"}')
        if a.out:
            print(f'[out] {a.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
