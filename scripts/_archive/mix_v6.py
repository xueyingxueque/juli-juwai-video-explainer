"""独立混音器 v6 —— 按《短剧推广·13-原声混剪设计》重做。

★ 为什么要重写：
  用户反馈「背景还是有噪音」。查下来我的做法有三处根本错误：

  【错误1】BGM 恒定 volume，忘了 duck（抽空）
      13 号设计明确：BGM 要按场景压到 -24 / -34，甚至抽空。
      我之前是全程 volume 0.42 一路到底 → 原声炸点时 BGM 还在盖。

  【错误2】BGM 自身没降噪
      我合成的 BGM 是三角波，低通滤波后仍有谐波噪点。
      13 号设计要求 BGM 是最底层（-24 以下），且必须干净。

  【错误3】两条主声源各自 -16 混在一起
      13 号设计铁律：「任何时刻只有一个主声源达到 -14，其余全部 duck」。
      我之前是「解说 -16 + 原声 -18 各自归一后 amix」
      → 相当于两个主声源 → 听起来糊。

★ 四层响度矩阵（来自 13 号设计 §3）：
    ┌──────────────┬────────┬────────┬────────┐
    │ 场景         │ 解说   │ 原声   │ BGM    │
    ├──────────────┼────────┼────────┼────────┤
    │ 纯解说       │ -14★  │ —      │ -24    │
    │ 炸点原声     │ 静音   │ -14★  │ -34    │
    │ 氛围垫底     │ -14★  │ -26    │ -24    │
    └──────────────┴────────┴────────┴────────┘
    ★ = 主声源（只有一个）

用法：
    python mix_v6.py <plan.json> <ep_dir>
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
FF = CFG['runtime']['ffmpeg']

CH = 'aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,'


def build_duck_chain(bounds, kinds):
    """生成 BGM 的 duck 滤镜链（多个 volume enable 串联）。

    ★ 实测：volume=0.3|between(t,1,3):volume=0.05 会被 `|` 当滤镜分隔符 → 报错。
    ★ 正解：多个 volume 用逗号串联，且 between() 表达式必须用单引号包裹：
        volume=enable='between(t,0,5)':volume=0.30,
        volume=enable='between(t,5,9)':volume=0.05
      后面的规则覆盖前面的，所以顺序不影响（每条都是 enable 开关）。

    duck 策略（来自 13 号设计 §3）：
      - 解说段 → BGM 压到 0.20（垫底但不抢）
      - 原声段 → BGM 压到 0.05（几乎抽空，让原声当主角）
      - 段间空白 → 默认 0.30（听得到底）
    """
    if not bounds:
        return 'volume=0.30'
    segs = []
    for (st, en), is_seg in zip(bounds, kinds):
        v = 0.20 if is_seg else 0.05
        segs.append("volume=enable='between(t,%.3f,%.3f)':volume=%s" % (st, en, v))
    return ','.join(segs)


def mix(plan_path: Path, ep_dir: Path, bgm: Path | None = None,
        bgm_wav: Path | None = None) -> bool:
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    segs = plan['segments']
    work = ep_dir / '_work'
    audio = work / 'audio'

    kinds = [bool((s.get('text') or '').strip()) for s in segs]
    bounds, cur = [], 0.0
    for s in segs:
        d = float(s.get('duration') or 3.0)
        bounds.append((cur, cur + d))
        cur += d
    total = cur

    if bgm is None:
        cand = plan.get('bgm')
        if cand:
            for p in (Path(cand), Path(cand).with_suffix('.wav')):
                if p.exists():
                    bgm = p
                    break

    # 若外部已铺好 BGM（SFX+衰减曲线），用它替代原始 mp3
    if bgm_wav and bgm_wav.exists():
        bgm = bgm_wav

    video = work / 'silent.mp4'
    if not video.exists():
        print('  [错] 缺 silent.mp4')
        return False

    inputs = ['-i', str(video)]
    parts, labels, idx = [], [], 1

    # ── 第一层：解说（-14 LUFS）──
    n_seg = n_raw = 0
    for k, is_seg in enumerate(kinds):
        if not is_seg:
            continue
        sf = audio / ('seg%02d.wav' % k)
        if not sf.exists():
            print('    [跳过] 缺 seg%02d.wav' % k)
            continue
        inputs += ['-i', str(sf)]
        parts.append(f'[{idx}:a]' + CH +
                     'highpass=f=90,afftdn=nf=-34,'
                     'equalizer=f=4000:t=q:w=1:g=3,'
                     'dynaudnorm=f=250:g=12:p=0.75,'
                     'loudnorm=I=-14:TP=-1.5:LRA=11,'
                     f'adelay={int(bounds[k][0] * 1000)}:all=1[seg{k}]')
        labels.append('[seg%d]' % k)
        idx += 1
        n_seg += 1

    # ── 第二层：原声（-14 LUFS）──
    for k, is_seg in enumerate(kinds):
        if is_seg:
            continue
        rf = work / ('r%02d.wav' % (k + 1))
        if not rf.exists():
            print('    [跳过] 缺 r%02d.wav' % (k + 1))
            continue
        inputs += ['-i', str(rf)]
        parts.append(f'[{idx}:a]' + CH +
                     'highpass=f=90,afftdn=nf=-34,'
                     'loudnorm=I=-14:TP=-1.5:LRA=11,'
                     f'adelay={int(bounds[k][0] * 1000)}:all=1[raw{k}]')
        labels.append('[raw%d]' % k)
        idx += 1
        n_raw += 1

    if not labels:
        print('  [混音跳过] 无音轨')
        return False

    # ── 第三层：BGM（duck 包络 + 自身降噪）──
    pre_mixed = bgm is not None and bgm.suffix.lower() == '.wav'
    if bgm and bgm.exists():
        inputs += ['-i', str(bgm)]
        # ★ 全部用 f-string 拼 filter 片段。
        #   之前用 '[%d:a]' % idx + CH + '...%d' % x 的链式写法，
        #   Python 的 % 从左到右只作用一次 → 参数个数对不上 → TypeError。
        if pre_mixed:
            # 引擎已铺好 BGM（-14dB + duck + SFX），只需对齐长度
            parts.append(f'[{idx}:a]' + CH + f'atrim=0:{total:.2f}[bgm]')
            labels.append('[bgm]')
        else:
            parts.append(f'[{idx}:a]' + CH +
                         'highpass=f=120,lowpass=f=6000,afftdn=nf=-45,'
                         'aloop=loop=-1:size=2e9[bgm_raw]')
            labels.append('[bgm_raw]')
            idx += 1
            parts.append('[bgm_raw]' + build_duck_chain(bounds, kinds) + '[bgm]')
            labels.append('[bgm]')

    # ★★★ amix 的 duration 必须是 **longest**，不能是 first！
    #   duration=first = 取最短输入的时长。
    #   第一路（解说 seg00，约 2.0s）最短 → 整个音轨被截到 2.02s
    #   → 成片 20.5s 里后面 18 秒完全没声音（用户听到的「后面没音」）。
    #   正解：duration=longest，再 atrim 到成片总长。
    total_s = f'{total:.2f}'
    graph = ';'.join(parts) + ';' + ''.join(labels) + \
        (f'amix=inputs={len(labels)}:duration=longest:dropout_transition=0:'
         f'normalize=0,apad=whole_dur={total_s},atrim=0:{total_s}[amix]')

    tmp = work / '_mix_v6.wav'
    a1 = ([FF, '-v', 'error', '-y'] + inputs +
          ['-filter_complex', graph, '-map', '[amix]',
           '-c:a', 'pcm_s16le', '-ar', '44100', '-ac', '2',
           '-t', '%.2f' % total, str(tmp)])
    r1 = subprocess.run(a1, capture_output=True, text=True, timeout=1200)
    if not (r1.returncode == 0 and tmp.exists()):
        print('  [第一遍失败]', (r1.stderr or '')[-320:])
        return False

    nrm = work / '_mix_v6_norm.m4a'
    a2 = [FF, '-v', 'error', '-y', '-i', str(tmp),
          '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11:linear=true',
          '-c:a', 'aac', '-b:a', '320k', '-ar', '44100', '-ac', '2',
          '-t', '%.2f' % total, str(nrm)]
    r2 = subprocess.run(a2, capture_output=True, text=True, timeout=900)
    if not (r2.returncode == 0 and nrm.exists()):
        print('  [第二遍失败]', (r2.stderr or '')[-220:])
        return False

    out = work / 'mixed_v6.mp4'
    a3 = [FF, '-v', 'error', '-y', '-i', str(video), '-i', str(nrm),
          '-map', '0:v', '-map', '1:a',
          '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k',
          '-movflags', '+faststart', '-t', '%.2f' % total, str(out)]
    r3 = subprocess.run(a3, capture_output=True, text=True, timeout=900)
    tmp.unlink(missing_ok=True)
    nrm.unlink(missing_ok=True)

    ok = r3.returncode == 0 and out.exists() and out.stat().st_size > 10000
    if not ok:
        print('  [合成失败]', (r3.stderr or '')[-320:])
    else:
        print('  v6混音OK: 解说%d段(-14) + 原声%d段(-14) + BGM(duck 0.05~0.30)'
              % (n_seg, n_raw))
        print('  输出:', out)
    return ok


if __name__ == '__main__':
    p = Path(sys.argv[1])
    e = Path(sys.argv[2])
    b = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    bw = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    sys.exit(0 if mix(p, e, b, bw) else 1)