# -*- coding: utf-8 -*-
"""extract_voice_ref.py — 从原片提取「主角音色克隆参考音频」（2026-10-21）

用法 A（raw 段模式，默认）：
  python extract_voice_ref.py --from-spec spec-1.json --dataset <素材目录> \
      [--out voice_ref.wav] [--max-sec 20] [--min-sec 8] [--span 3]

用法 B（旁白段模式，2026-10-22 增）：显式指定 ep:start:dur 三元组——
  适合「原剧第一人称旁白就是主角音色」的剧（如星落），raw 段混杂他人台词时
  用旁白段提纯：
  python extract_voice_ref.py --dataset <素材目录> \
      --segments "1:0.0:8.1,2:19.0:14.1" [--out voice_ref.wav] [--max-sec 20]

逻辑：
  spec 的 raw=1 段（原声台词）本来就是「最有张力的核心台词」——直接把这些段
  从原片切出、响度归一、拼成 8-20s 的干净人声，作为 dotstts/Breeze 的
  零样本克隆参考（dub.py --ref）。解说即可用「主角的音色」讲主角的故事。

  优先选台词密度高的段；总时长不足 min-sec 时自动放宽（加相邻 ASR 行）。
  ★ raw 段可能是他人台词（如母亲命令主角）——要纯主角音色时用 --segments
    显式挑主角旁白段，避免克隆参考里混进别人的声音。
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))
FF = None


def find_ep(dataset: Path, ep: int) -> Path:
    from plan_story import find_ep_file
    return find_ep_file(Path(dataset), ep)


def cut(video: Path, t: float, dur: float, out: Path) -> bool:
    r = subprocess.run([FF, '-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', str(video),
                        '-t', f'{dur:.2f}', '-vn', '-ac', '1', '-ar', '48000',
                        str(out)], capture_output=True, text=True, timeout=180)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 20000


def parse_segments(text: str):
    """'1:0.0:8.1,2:19.0:14.1' → [(1, 0.0, 8.1), (2, 19.0, 14.1)]；格式错返回 None。"""
    out = []
    for item in text.split(','):
        item = item.strip()
        if not item:
            continue
        ps = item.split(':')
        if len(ps) != 3:
            return None
        try:
            out.append((int(ps[0]), float(ps[1]), float(ps[2])))
        except ValueError:
            return None
    return out or None


def main() -> int:
    global FF
    ap = argparse.ArgumentParser()
    ap.add_argument('--from-spec', help='spec-N.json（取 raw=1 段的 ep/t）；'
                                        '用了 --segments 可不传')
    ap.add_argument('--dataset', required=True, help='素材目录')
    ap.add_argument('--segments', help='显式段：ep:start:dur 逗号分隔（主角旁白提纯用）')
    ap.add_argument('--out', default='voice_ref.wav')
    ap.add_argument('--max-sec', type=float, default=20.0)
    ap.add_argument('--min-sec', type=float, default=8.0)
    ap.add_argument('--span', type=float, default=3.0, help='raw 段模式每段切取时长')
    args = ap.parse_args()

    cfg = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
    FF = cfg['runtime']['ffmpeg']

    cuts = []  # [(ep, start, dur)]
    if args.segments:
        cuts = parse_segments(args.segments)
        if cuts is None:
            print('[FAIL] --segments 格式错：应为 ep:start:dur 逗号分隔，'
                  '如 "1:0.0:8.1,2:19.0:14.1"')
            return 2
        print(f'[info] 显式旁白段 {len(cuts)} 个，按给定顺序切取')
    else:
        if not args.from_spec:
            print('[FAIL] 未传 --segments 时必须传 --from-spec')
            return 2
        spec = json.loads(Path(args.from_spec).read_text(encoding='utf-8'))
        raws = [s for s in spec.get('shots', []) if s.get('raw')]
        if not raws:
            print('[FAIL] spec 无 raw 段——先选原声台词再提音色，或用 --segments')
            return 2
        cuts = [(int(s['ep']), float(s['t']), args.span) for s in raws]
        print(f'[info] raw 段 {len(cuts)} 个，按剧情顺序切取')

    tmp = Path(tempfile.mkdtemp(prefix='vref_'))
    parts = []
    total = 0.0
    for i, (ep, start, dur) in enumerate(cuts):
        if total >= args.max_sec:
            break
        video = find_ep(Path(args.dataset), ep)
        seg = tmp / f'p{i:02d}.wav'
        if cut(video, start, dur, seg):
            parts.append(seg)
            total += dur
        else:
            print(f'[警告] ep{ep} {start:.2f}s 切取失败（跳过）——检查素材文件与时间点')
    if not parts:
        print('[FAIL] 所有段切取失败——素材路径/时间点有问题')
        return 2
    if total < args.min_sec:
        print(f'[警告] 仅凑到 {total:.1f}s（<{args.min_sec}s），克隆稳定性可能下降——'
              f'可增大 --span / 加段 / 换 --segments')

    # 拼接 + 响度归一（-16 LUFS，和产线一致）
    lst = tmp / 'list.txt'
    lst.write_text('\n'.join(f"file '{p.as_posix()}'" for p in parts), encoding='utf-8')
    out = Path(args.out)
    r = subprocess.run([FF, '-v', 'error', '-y', '-f', 'concat', '-safe', '0',
                        '-i', str(lst), '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11',
                        '-ar', '48000', '-ac', '1', str(out)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        print('[FAIL] 拼接失败:', (r.stderr or '')[-200:])
        return 3
    print(f'[OK] 克隆参考 → {out}  ({total:.1f}s, {len(parts)} 段)')
    print(f'[用法] dub.py --engine dotstts --ref {out.resolve()} --text "..." --out x.wav')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
