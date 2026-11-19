"""混剪装配 —— 按解说稿从 88 集挑片段拼成成片。

★ 用户要求：「本来为了原创度就要重新混剪原视频」
   所以本模块**不整集搬运**，而是按稿子逐句取对应时间点的片段，
   再按**叙事顺序**（非集号）重排拼接。

三条轨道：
  视频：原片片段（每句对应一个片段）
  配音：TTS 解说
  原声：原片真实对白（从素材切，音量压到 0.35）

用法：
  python series_assemble.py <plan.json> <out_dir> [--dry]
"""
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))

# 竖屏成片规格（抖音/快手）
W, H = 1080, 1920
FPS = 30


def find_source(ep: int) -> Path:
    """按集号找源文件 —— 兼容 `N.mp4` 与 `第N集.mp4` 两种命名，取更大的那份。"""
    d = Path(r'D:/剧里剧外/素材/短剧/快手/山藏万象/剧集')
    cands = [p for p in (d / f'{ep}.mp4', d / f'第{ep}集.mp4') if p.exists()]
    if not cands:
        raise FileNotFoundError(f'第{ep}集 源文件不存在')
    return max(cands, key=lambda p: p.stat().st_size)


def cut_clip(src: Path, start: float, dur: float, out: Path,
             with_audio: bool = True) -> bool:
    """切一个片段并统一规格（竖屏/同帧率/同分辨率），保证拼接不跳。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = (f'scale={W}:{H}:force_original_aspect_ratio=increase,'
          f'crop={W}:{H},setsar=1,fps={FPS},format=yuv420p')
    args = [CFG['runtime']['ffmpeg'], '-v', 'error', '-y',
            '-ss', f'{start:.2f}', '-t', f'{dur:.2f}', '-i', str(src),
            '-vf', vf]
    if with_audio:
        args += ['-ac', '2', '-ar', '44100', '-c:a', 'aac', '-b:a', '128k']
    else:
        args += ['-an']
    args += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22', str(out)]
    r = subprocess.run(args, capture_output=True, text=True, timeout=300)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0


def cut_raw_audio(src: Path, start: float, dur: float, out: Path) -> bool:
    """切原声片段（真实对白），归一响度后备用。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix('.raw.wav')
    r = subprocess.run(
        [CFG['runtime']['ffmpeg'], '-v', 'error', '-y',
         '-ss', f'{start:.2f}', '-t', f'{dur:.2f}', '-i', str(src),
         '-vn', '-ac', '1', '-ar', '44100', str(tmp)],
        capture_output=True, text=True, timeout=180)
    if not (r.returncode == 0 and tmp.exists()):
        return False
    # 响度归一：原片现场录音 vs TTS 合成音，响度不一致叠加会一高一低
    r2 = subprocess.run(
        [CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-i', str(tmp),
         '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11', '-ac', '1', '-ar', '44100', str(out)],
        capture_output=True, text=True, timeout=180)
    tmp.unlink(missing_ok=True)
    return r2.returncode == 0 and out.exists()


def dur_of(path: Path) -> float:
    r = subprocess.run([CFG['runtime']['ffprobe'], '-v', 'error', '-show_entries',
                        'format=duration', '-of', 'csv=p=0', str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def concat(clips: list, out: Path, workdir: Path) -> bool:
    """ concat demuxer 拼接 —— 不重编码，快且无损。"""
    lst = workdir / 'list.txt'
    lst.write_text('\n'.join(f"file '{c.as_posix()}'" for c in clips), encoding='utf-8')
    r = subprocess.run(
        [CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-f', 'concat', '-safe', '0',
         '-i', str(lst), '-c', 'copy', str(out)],
        capture_output=True, text=True, timeout=600)
    return r.returncode == 0 and out.exists()


def build_audio_timeline(segments: list, narration: Path,
                         out_dir: Path) -> tuple:
    """★ 按「说话权」构建音轨 —— 这才对，不是三轨同时响。

    用户指正：「放解说的时候原片静音，放原片的时候解说静音」。

    所以音轨要按时间轴分段铺：
      - 该段是解说段（text 非空）→ 只放解说，原片静音
      - 该段是原声段（raw=1）→ 只放原声，解说静音
      - 全程铺 BGM，压在下面当底
    最后用 adelay 把每段放到它在成片里的绝对时间点。
    """
    work = out_dir / '_work' / 'audio'
    work.mkdir(parents=True, exist_ok=True)

    # 读每段在成片里的起始时间
    starts, cur = [], 0.0
    for s in segments:
        starts.append(cur)
        cur += float(s.get('duration') or s.get('clip_duration') or 3.0)
    total = cur

    tracks = []   # (标签, 文件, 音量, 起始秒)
    for k, (s, st) in enumerate(zip(segments, starts)):
        dur = float(s.get('duration') or 3.0)
        if s.get('raw') and s.get('raw_audio_file'):
            rf = Path(s['raw_audio_file'])
            if rf.exists():
                # 原声段：只放原声
                tracks.append(('raw%d' % k, rf, 1.0, st))
        # 解说段：有口播才放
        seg_audio = work / ('seg%02d.wav' % k)
        if seg_audio.exists():
            tracks.append(('seg%d' % k, seg_audio, 1.0, st))

    return tracks, starts, total


def mux_audio(video: Path, segments: list, narration: Path | None,
              bgm: Path | None, raw_dir: Path, out: Path) -> bool:
    """★ 混音 v3：整段独占（真正的互斥）+ BGM 全程 + 两遍母带。

    ★★★ 用户两次点出的问题，这次的根因不是音量，是**内容冗余**：
        我写的口播在「翻译」原声台词。
        例：原声「你敢你动俺的狗试试」→ 口播「她张臂就喊：你敢动俺的狗试试」
        同一句话在 5 秒内说了两遍，听感就是「解说和原音一起出现」。

    ★ 正解（结构上互斥，不靠音量）：
        - 段落有 text  → 只铺 seg 音轨（纯解说）
        - 段落无 text  → 只铺 raw 音轨（纯原声）
        - 两路永远不铺到同一段 → 天然互斥，不需要 volume=enable
          （实测 volume=enable 需单引号包裹，易踩坑，不必要就不用）
    """
    work = raw_dir / 'audio' if (raw_dir / 'audio').exists() else raw_dir
    raw_dir_ok = raw_dir.exists()

    # 段的类型：True=解说段 False=原声段
    kinds = [bool((s.get('text') or '').strip()) for s in segments]

    bounds, cur = [], 0.0
    for s in segments:
        d = float(s.get('duration') or 3.0)
        bounds.append((cur, cur + d))
        cur += d
    total = cur

    inputs = ['-i', str(video)]        # 0 视频
    inputs += ['-i', str(video)]       # 1 原片音
    parts, out_labels, idx = [], [], 2

    CH = 'aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,'

    # ── 解说段：只铺 seg（且只在解说段铺）──
    n_seg = n_raw = 0
    for k, is_seg in enumerate(kinds):
        if not is_seg:
            continue
        sf = work / ('seg%02d.wav' % k)
        if not sf.exists():
            print('    [跳过] 缺 seg%02d.wav' % k)
            continue
        st = bounds[k][0]
        inputs += ['-i', str(sf)]
        parts.append('[%d:a]' % idx + CH +
                     'afftdn=nf=-30,'
                     'equalizer=f=4000:t=q:w=1:g=3,dynaudnorm=f=250:g=12:p=0.75,'
                     'volume=1,adelay=%d:all=1[seg%d]' % (int(st * 1000), k))
        out_labels.append('[seg%d]' % k)
        idx += 1
        n_seg += 1

    # ── 原声段：只铺 raw ──
    for k, is_seg in enumerate(kinds):
        if is_seg:
            continue
        rf = raw_dir / ('r%02d.wav' % (k + 1))
        if not rf.exists():
            print('    [跳过] 缺 r%02d.wav' % (k + 1))
            continue
        st = bounds[k][0]
        inputs += ['-i', str(rf)]
        parts.append('[%d:a]' % idx + CH +
                     'afftdn=nf=-28,'
                     'loudnorm=I=-18:TP=-2,volume=1,'
                     'adelay=%d:all=1[raw%d]' % (int(st * 1000), k))
        out_labels.append('[raw%d]' % k)
        idx += 1
        n_raw += 1

    if n_seg + n_raw == 0:
        print('  [混音跳过] 没有任何音轨')
        return False

    # ── BGM 全程铺底 ──
    if bgm and bgm.exists():
        inputs += ['-i', str(bgm)]
        parts.append('[%d:a]' % idx + CH +
                     'highpass=f=80,lowpass=f=8000,volume=0.42,'
                     'aloop=loop=-1:size=2e9[bgm]')
        out_labels.append('[bgm]')
        idx += 1

    # ★ 原片环境音不再混入！
    #   实测原片底噪 -4.3 dB（很响的现场收录噪声），
    #   即使压到 0.05 也能被听见「沙沙」声。
    #   解说片要的是干净，BGM + 解说/原声 已足够。

    graph = ';'.join(parts) + ';' + ''.join(out_labels) +         ('amix=inputs=%d:duration=first:normalize=0[amix]' % len(out_labels))

    ff = CFG['runtime']['ffmpeg']
    tmp = out.parent / '_mix_raw.wav'
    a1 = ([ff, '-v', 'error', '-y'] + inputs +
          ['-filter_complex', graph, '-map', '[amix]', '-c:a', 'pcm_s16le',
           '-ar', '44100', '-ac', '2', '-t', '%.2f' % total, str(tmp)])
    r1 = subprocess.run(a1, capture_output=True, text=True, timeout=1200)
    if not (r1.returncode == 0 and tmp.exists()):
        print('  [第一遍失败]', (r1.stderr or '')[-300:])
        return False

    nrm = tmp.with_suffix('.norm.m4a')
    a2 = [ff, '-v', 'error', '-y', '-i', str(tmp),
          '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11:linear=true',
          '-c:a', 'aac', '-b:a', '320k', '-ar', '44100', '-ac', '2',
          '-t', '%.2f' % total, str(nrm)]
    r2 = subprocess.run(a2, capture_output=True, text=True, timeout=900)
    if not (r2.returncode == 0 and nrm.exists()):
        print('  [第二遍失败]', (r2.stderr or '')[-200:])
        return False

    a3 = [ff, '-v', 'error', '-y', '-i', str(video), '-i', str(nrm),
          '-map', '0:v', '-map', '1:a',
          '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k',
          '-movflags', '+faststart', '-t', '%.2f' % total, str(out)]
    r3 = subprocess.run(a3, capture_output=True, text=True, timeout=900)
    tmp.unlink(missing_ok=True)
    nrm.unlink(missing_ok=True)

    ok = r3.returncode == 0 and out.exists() and out.stat().st_size > 10000
    if not ok:
        print('  [合成失败]', (r3.stderr or '')[-300:])
    else:
        print('  混音OK: 解说%d段 + 原声%d段 + BGM | ★整段互斥(结构保证，不靠音量)'
              % (n_seg, n_raw))
    return ok


def burn_subs(video: Path, srt: Path, out: Path) -> bool:
    """烧字幕 —— 竖屏底部安全区。

    ★ 实测坑（Windows 中文路径）：
      - `subtitles=<path>` 里的盘符冒号会被当滤镜参数分隔 → Invalid argument
      - 即使转义成 `\:`，中文路径 + force_style 组合仍会解析失败
      - 正解：**先把 srt 复制到纯 ASCII 临时路径**（如 C:\tmp_sub.srt），
        再用简单路径调用，避免所有转义问题
    """
    if not srt.exists():
        return False
    tmp_srt = Path(tempfile.gettempdir()) / 'wb_sub.srt'
    tmp_srt.write_bytes(srt.read_bytes())          # 原样复制（SRT 内容不变）

    style = ("FontName=Microsoft YaHei,FontSize=17,"
             "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
             "BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=340")
    # 用 filename= 形式 + 单引号，盘符冒号仍需转义
    sp = str(tmp_srt).replace(':', chr(92) + ':')
    vf = "subtitles=filename='" + sp + "':force_style='" + style + "'"
    r = subprocess.run(
        [CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-i', str(video),
         '-vf', vf, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
         '-c:a', 'copy', '-movflags', '+faststart', str(out)],
        capture_output=True, text=True, timeout=900)
    ok = r.returncode == 0 and out.exists() and out.stat().st_size > 10000
    if not ok:
        print('  [字幕失败]', (r.stderr or '')[-200:])
    tmp_srt.unlink(missing_ok=True)
    return ok

def write_srt(lines: list, out: Path) -> Path:
    """按每句对应片段的时长生成 SRT（时间轴对齐混剪后的成片）。"""
    def ts(x: float) -> str:
        h = int(x // 3600); m = int((x % 3600) // 60); s = x % 60
        return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')
    cur, items = 0.0, []
    for ln in lines:
        d = ln.get('clip_duration', 0) or ln.get('duration', 3.0)
        txt = (ln.get('subtitle') or ln.get('text') or '').strip()
        if not txt:
            cur += d
            continue
        items.append(f'{len(items)+1}\n{ts(cur)} --> {ts(cur+d)}\n{txt}\n')
        cur += d
    out.write_text('\n'.join(items), encoding='utf-8-sig')
    return out


def assemble(plan: dict, out_dir: Path, dry: bool = False) -> dict:
    """主流程：按 plan.shots 混剪成片。"""
    # 兼容两种字段：segments（含口播+时长）是新格式，shots 是旧格式
    shots = plan.get('segments') or plan.get('shots') or []
    if not shots:
        raise ValueError('plan 里没有 shots')
    work = out_dir / '_work'
    work.mkdir(parents=True, exist_ok=True)

    clips, raws, log = [], [], []
    for i, sh in enumerate(shots, 1):
        ep, t = sh['ep'], float(sh.get('t') or 0)
        dur = float(sh.get('clip_duration') or sh.get('duration') or 3.0)
        src = find_source(ep)
        c = work / f'c{i:02d}.mp4'
        ok = cut_clip(src, max(t - dur / 2, 0), dur, c)
        log.append({'i': i, 'ep': ep, 't': t, 'dur': dur, 'ok': ok,
                    'text': sh.get('text', '')[:26]})
        if not ok:
            print(f'  [失败] 第{ep}集 t={t}s')
            continue
        clips.append(c)
        if sh.get('raw'):
            r = work / f'r{i:02d}.wav'
            if cut_raw_audio(src, t, dur, r):
                raws.append(r)
                sh['raw_audio_file'] = str(r)
                log[-1]['raw_cut'] = True

    total = sum(dur_of(c) for c in clips)
    print(f'片段 {len(clips)}/{len(shots)} 个，总时长 {total:.1f}s')
    if dry:
        return {'clips': [str(c) for c in clips], 'total': total, 'log': log}

    silent = work / 'silent.mp4'
    if not concat(clips, silent, work):
        raise RuntimeError('拼接失败')
    print(f'拼接完成 {dur_of(silent):.1f}s')

    nar = plan.get('narration_audio')
    bgm = plan.get('bgm')
    bgm_p = Path(bgm) if bgm else None
    if bgm_p and not bgm_p.exists():
        alt = bgm_p.with_suffix('.wav')
        bgm_p = alt if alt.exists() else None
    mixed = work / 'mixed.mp4'
    if mux_audio(silent, shots, Path(nar) if nar else None,
                 bgm_p, work, mixed):
        pass
    else:
        print('  [警告] 混音失败，输出仅原片音')
        mixed = silent

    srt = write_srt(shots, work / 'subs.srt')
    name = plan.get('name') or 'final'
    if not name.lower().endswith('.mp4'):
        name += '.mp4'
    final = out_dir / name
    if burn_subs(mixed, srt, final):
        print(f'字幕烧录完成 -> {final.name}')
    else:
        final = mixed
        print('字幕烧录失败，输出无字幕版')
    return {'final': str(final), 'duration': dur_of(final), 'log': log}


def main() -> int:
    plan_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else plan_path.parent
    dry = '--dry' in sys.argv
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    out_dir.mkdir(parents=True, exist_ok=True)
    r = assemble(plan, out_dir, dry)
    if dry:
        for x in r['log']:
            print(f"  {'OK' if x['ok'] else 'NG'} 第{x['ep']}集 t={x['t']}s d={x['dur']} {x['text']}")
        print(f"预估时长 {r['total']:.1f}s")
    else:
        print(f"\n[成片] {r['final']}  {r['duration']:.1f}s")
    return 0


if __name__ == '__main__':
    sys.exit(main())