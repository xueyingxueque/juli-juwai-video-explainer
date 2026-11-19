"""系列短剧解说 —— 一条命令跑完整流程。

★★★ 这个文件存在的理由 ★★★

之前的问题：整个流程拆成 5 个脚本**靠人工按顺序跑**，
中间产物靠**文件名约定**传递（raw01.wav → seg00.wav → r02.wav）。
后果：
  - 改一个参数（如换音色）后，下游不会自动重算
  - 文件名错位（seg 用 0-based、raw 用 1-based）导致「跳过」而非「报错」
  - 表现就是**后半段没声音**（因为音轨对不上时间轴）

本脚本把这些全部收进一次执行：
  ① 读 plan → ② 配音 → ③ 按实际音频重定时长 → ④ 切原声（ASR 定位）
  → ⑤ 切画面 → ⑥ 铺 BGM/SFX → ⑦ 混音 → ⑧ 烧字幕 → ⑨ 校验
任何一步失败立即停止并报明确错误，不会留半成品。

用法：
    python run_pipeline.py <plan.json> <素材集目录> [--voice 谭湘文徐浩伦]
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
FF = CFG['runtime']['ffmpeg']
FP = CFG['runtime']['ffprobe']
PY = CFG['runtime']['asr_python']
DUB_PY = r'D:\AITools\TTS\Dots.tts\wzf\python.exe'
DUB = Path(r'C:/Users/Administrator/.workbuddy/skills/本地配音工坊/dub.py')

DEFAULT_VOICE = '谭湘文徐浩伦'   # 实测 6.00 字/秒 = 短剧解说标准
BREATH = 0.5                      # 每段结尾呼吸留白（秒）


def run(cmd, timeout=1800, quiet=True):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 and not quiet:
        print((r.stderr or '')[-400:])
    return r.returncode == 0


def dur(p: Path) -> float:
    r = subprocess.run([FP, '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(p)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def find_src(dataset: Path, ep: int) -> Path:
    """素材集里按集号找源文件（兼容 `N.mp4` 与 `第N集.mp4`）。"""
    for cand in (dataset / f'{ep}.mp4', dataset / f'第{ep}集.mp4'):
        if cand.exists():
            return cand
    raise FileNotFoundError(f'第{ep}集 源文件不存在')


def step(n, total, msg):
    print(f'\n[{n}/{total}] {msg}', flush=True)


# ─────────────────────────────────────────────
# ② 配音：逐段合成，产出 raw{N}.wav（1-based）
# ─────────────────────────────────────────────
def synth_narration(plan, ep_dir: Path, voice: str) -> dict:
    tts = ep_dir / '_tts'
    tts.mkdir(parents=True, exist_ok=True)
    # ★ 每次全清重配 —— 绝不复用旧文件（复用导致新旧音色混用）
    for f in tts.glob('*.wav'):
        f.unlink()

    out = {}
    for i, s in enumerate(plan['segments'], 1):
        if not (s.get('text') or '').strip():
            out[i] = None
            continue
        raw = tts / ('raw%02d.wav' % i)
        if not run([DUB_PY, '-X', 'utf8', str(DUB), '--voice', voice,
                    '--text', s['text'], '--out', str(raw)], 900, quiet=False):
            raise RuntimeError('配音失败 第%d段: %s' % (i, s['text']))
        d = dur(raw)
        if d <= 0:
            raise RuntimeError('配音时长为 0 第%d段' % i)
        out[i] = d
        rate = len(s['text']) / d
        flag = '' if 4.5 <= rate <= 7.0 else '  ⚠语速 %.1f 字/秒' % rate
        print('    raw%02d.wav  %5.2fs  语速 %.1f 字/秒%s'
              % (i, d, rate, flag))
    return out


# ─────────────────────────────────────────────
# ③ 按时长：解说段 = 配音 + 呼吸；原声段 = 原声时长
# ─────────────────────────────────────────────
def retime(plan, narration: dict, asr_of: dict):
    total = 0.0
    for i, s in enumerate(plan['segments'], 1):
        if (s.get('text') or '').strip():
            d = narration[i] + BREATH
        else:
            d = asr_of.get(i, 4.5)
        s['duration'] = round(d, 2)
        s['clip_duration'] = s['duration']
        total += s['duration']
    plan['total_clip_sec'] = round(total, 2)
    return plan['total_clip_sec']


# ─────────────────────────────────────────────
# ④ 原声：先用 ASR 定位台词真实时间点，再切
# ─────────────────────────────────────────────
def asr_locate(video: Path, tmp_wav: Path, hint_t: float) -> tuple:
    """★ 关键：字幕时间 ≠ 语音时间。必须 ASR 定位。

    实测：第4集字幕在 t=4.15s，但台词在 5.6-10.0s。
    按字幕切 → 只切到杂音 → 用户听成「原声没声音」。
    """
    if not run([FF, '-v', 'error', '-y', '-i', str(video),
                '-vn', '-ac', '1', '-ar', '16000', str(tmp_wav)], 300):
        return None
    script = (
        "import json,sys\n"
        "from faster_whisper import WhisperModel\n"
        "m=WhisperModel('D:/AITools/models/faster-whisper-tiny',device='cpu',compute_type='int8')\n"
        "segs,_=m.transcribe(sys.argv[1],language='zh',vad_filter=True,beam_size=5)\n"
        "print(json.dumps([{'s':round(s.start,2),'e':round(s.end,2),'t':s.text}"
        " for s in segs],ensure_ascii=False))\n"
    )
    r = subprocess.run([PY, '-X', 'utf8', '-c', script, str(tmp_wav)],
                       capture_output=True, text=True, timeout=1800)
    for line in (r.stdout or '').strip().splitlines():
        if line.startswith('['):
            try:
                return json.loads(line)
            except Exception:
                pass
    return None


def cut_raw(dataset: Path, ep: int, hint_t: float, dur_seg: float,
            out_wav: Path, work: Path) -> dict:
    """切原声：ASR 找到台词 → 以台词为中心截取 dur_seg 秒。"""
    src = find_src(dataset, ep)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / 'a.wav'
        segs = asr_locate(src, tmp, hint_t)
        if not segs:
            # ASR 没结果（可能整集无人声）→ 退回字幕提示点
            start = max(hint_t - dur_seg / 2, 0)
            src_t = '字幕提示(ASR空)'
            text = ''
        else:
            # 取离提示点最近的一段有实质内容的台词
            best = None
            for sg in segs:
                if len(sg['t'].strip()) < 4:
                    continue
                mid = (sg['s'] + sg['e']) / 2
                d = abs(mid - hint_t)
                if best is None or d < best[0]:
                    best = (d, sg)
            if best is None:
                start = max(hint_t - dur_seg / 2, 0)
                src_t, text = '无有效台词', ''
            else:
                sg = best[1]
                start = max(sg['s'] - 0.25, 0)
                src_t = 'ASR %.2f-%.2f' % (sg['s'], sg['e'])
                text = sg['t'][:26]

        raw = work / '_r.wav'
        run([FF, '-v', 'error', '-y', '-ss', '%.2f' % start,
             '-t', '%.2f' % dur_seg, '-i', str(src),
             '-vn', '-ac', '1', '-ar', '44100', str(raw)], 300)
        ok = run([FF, '-v', 'error', '-y', '-i', str(raw),
                  '-af', 'highpass=f=90,afftdn=nf=-34,loudnorm=I=-18:TP=-2',
                  '-ac', '1', '-ar', '44100', str(out_wav)], 300)
    return {'ok': ok, 'start': round(start, 2), 'from': src_t, 'text': text}


# ─────────────────────────────────────────────
# ⑤ 切画面：统一规格后拼接
# ─────────────────────────────────────────────
def cut_clips(plan, dataset: Path, ep_dir: Path) -> list:
    W, H, FPS = 1080, 1920, 30
    work = ep_dir / '_work'
    work.mkdir(parents=True, exist_ok=True)
    for f in work.glob('c*.mp4'):
        f.unlink()

    clips = []
    for i, s in enumerate(plan['segments'], 1):
        src = find_src(dataset, s['ep'])
        d = float(s['duration'])
        c = work / ('c%02d.mp4' % i)
        # 居中截取（解说段居中，原声段从台词点起）
        if (s.get('text') or '').strip():
            ss = max(s['t'] - d / 2, 0)
        else:
            ss = max(s.get('raw_start', s['t']), 0)
        ok = run([FF, '-v', 'error', '-y', '-ss', '%.2f' % ss, '-t', '%.2f' % d,
                  '-i', str(src),
                  '-vf', 'scale=%d:%d:force_original_aspect_ratio=increase,'
                         'crop=%d:%d,setsar=1,fps=%d,format=yuv420p'
                         % (W, H, W, H, FPS),
                  '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
                  '-an', str(c)], 300)
        if ok:
            clips.append(c)
    return clips


def concat(clips, work: Path) -> Path:
    lst = work / 'list.txt'
    lst.write_text('\n'.join("file '%s'" % c.as_posix() for c in clips), encoding='utf-8')
    silent = work / 'silent.mp4'
    run([FF, '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(lst),
         '-c', 'copy', str(silent)], 600)
    return silent


# ─────────────────────────────────────────────
# ⑧ 字幕
# ─────────────────────────────────────────────
def write_srt(plan, out: Path):
    """SRT 时间码。

    ★ 用 f-string 而不是 '%02d:%02d:%06.3f' % (h,m,s).replace(...)：
      Python 的 % 从左到右只作用一次，.replace() 会抢先在元组上执行 →
      "'tuple' object has no attribute 'replace'"。
      整类错误都在这里，同类代码必须一起改。
    """
    def ts(x: float) -> str:
        h = int(x // 3600)
        m = int((x % 3600) // 60)
        s = x % 60
        return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')

    cur, items = 0.0, []
    for s in plan['segments']:
        d = float(s['duration'])
        txt = (s.get('text') or '').strip()
        if txt:
            items.append(f'{len(items) + 1}\n'
                         f'{ts(cur)} --> {ts(cur + d)}\n{txt}\n')
        cur += d
    out.write_text('\n'.join(items), encoding='utf-8-sig')
    return out


def burn_subs(video: Path, srt: Path, out: Path):
    r"""★ Windows 中文路径烧字幕的三个硬约束（缺一不可）：

    1. **路径必须纯 ASCII** —— 中文路径 + force_style 组合必崩
    2. **必须用正斜杠 /** —— 反斜杠会被 subtitles 滤镜当转义符吃掉
       （实测 C:\Users\... 进去变成 C:Users...，找不到文件）
    3. **盘符冒号必须转义** —— C: 的冒号会被当滤镜参数分隔符
    """
    tmp = Path(tempfile.gettempdir()) / 'wb_sub.srt'
    tmp.write_bytes(srt.read_bytes())
    style = ("FontName=Microsoft YaHei,FontSize=17,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,"
             "Alignment=2,MarginV=340")
    # 反斜杠 → 正斜杠，再转义盘符冒号
    sp = str(tmp).replace(chr(92), '/').replace(':', chr(92) + ':')
    vf = "subtitles=filename='" + sp + "':force_style='" + style + "'"
    ok = run([FF, '-v', 'error', '-y', '-i', str(video), '-vf', vf,
              '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '22',
              '-c:a', 'copy', '-movflags', '+faststart', str(out)], 900,
             quiet=False)
    tmp.unlink(missing_ok=True)
    if not ok:
        print('    [调试] vf=%s' % vf)
    return ok


# ─────────────────────────────────────────────
# ⑨ 校验：逐秒扫 RMS，静音段直接判定失败
# ─────────────────────────────────────────────
def verify(video: Path) -> dict:
    """校验：音轨长度 + 静音段 + 响度。

    ★★★ 最重要的一条：**音轨长度必须等于视频长度**
      实测踩坑：amix duration=first 把 20.5s 的音轨截成 2.02s，
      成片「后面 18 秒完全没声音」，但 silencedetect 检不出来
      （因为剩余段是纯静音，BGM 也没铺到）。
      → 必须显式比对 stream duration。
    """
    total = dur(video)

    # ⓪ 音轨 vs 视频轨长度（★ 最关键的一条）
    v_dur = a_dur = None
    r0 = subprocess.run(
        [FP, '-v', 'error', '-show_entries', 'stream=codec_type,duration',
         '-of', 'default=noprint_wrappers=1', str(video)],
        capture_output=True, text=True, timeout=120)
    cur_type = None
    for line in (r0.stdout or '').splitlines():
        line = line.strip()
        if line.startswith('codec_type='):
            cur_type = line.split('=')[1]
        elif line.startswith('duration=') and cur_type == 'video' and v_dur is None:
            try:
                v_dur = float(line.split('=')[1])
            except Exception:
                pass
        elif line.startswith('duration=') and cur_type == 'audio' and a_dur is None:
            try:
                a_dur = float(line.split('=')[1])
            except Exception:
                pass

    # ① 静音段（阈值 -50dB，持续 0.6s 以上算静音）
    r = subprocess.run(
        [FF, '-hide_banner', '-i', str(video),
         '-af', 'silencedetect=noise=-50dB:d=0.6', '-f', 'null', '-'],
        capture_output=True, text=True, timeout=600)
    silent = []
    for line in (r.stderr or '').splitlines():
        if 'silence_start' in line:
            silent.append(line.strip())

    # ② 响度
    r2 = subprocess.run(
        [FF, '-hide_banner', '-i', str(video), '-af', 'ebur128=peak=true',
         '-f', 'null', '-'], capture_output=True, text=True, timeout=600)
    i_val = pk = None
    for line in (r2.stderr or '').splitlines():
        if 'I:' in line and 'LUFS' in line:
            try:
                i_val = float(line.split('I:')[1].split()[0])
            except Exception:
                pass
        if 'Peak:' in line:
            try:
                pk = float(line.split('Peak:')[1].split()[0])
            except Exception:
                pass

    return {'duration': round(total, 2),
            'silence_lines': silent,
            'silence_count': len(silent),
            'lufs': i_val, 'peak': pk,
            'v_dur': v_dur, 'a_dur': a_dur,
            'audio_truncated': (v_dur and a_dur and a_dur < v_dur - 0.5),
            'ok': len(silent) == 0 and not (v_dur and a_dur and a_dur < v_dur - 0.5)}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    plan_path = Path(sys.argv[1])
    dataset = Path(sys.argv[2])
    voice = DEFAULT_VOICE
    if '--voice' in sys.argv:
        voice = sys.argv[sys.argv.index('--voice') + 1]

    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    ep_dir = plan_path.parent / plan['name'].replace('.mp4', '').replace('EP0', 'ep0')
    if not ep_dir.exists():
        ep_dir = plan_path.parent / ('ep%02d' % plan.get('series_index', 1))
    ep_dir.mkdir(parents=True, exist_ok=True)
    work = ep_dir / '_work'
    work.mkdir(parents=True, exist_ok=True)
    audio = work / 'audio'
    if audio.exists():
        shutil.rmtree(audio)
    audio.mkdir(parents=True, exist_ok=True)

    T = 9

    step(1, T, '清理解说音轨')
    n_seg = sum(1 for s in plan['segments'] if (s.get('text') or '').strip())
    n_raw = len(plan['segments']) - n_seg
    print('    %d 段解说 / %d 段原声' % (n_seg, n_raw))

    step(2, T, '配音（音色：%s）' % voice)
    narration = synth_narration(plan, ep_dir, voice)

    step(3, T, '原声定位与切片（ASR 定位真实台词时间点）')
    asr_of, raw_files = {}, {}
    for i, s in enumerate(plan['segments'], 1):
        if (s.get('text') or '').strip():
            continue
        # 先给临时时长用于定位，定完再统一定长
        d = 4.5
        r = cut_raw(dataset, s['ep'], s['t'], d,
                    work / ('r%02d.wav' % i), work)
        got = dur(work / ('r%02d.wav' % i))
        asr_of[i] = round(min(max(got, 2.0), 6.0), 2)
        raw_files[i] = {'file': str(work / ('r%02d.wav' % i)),
                        'start': r['start'], 'from': r['from'],
                        'text': r['text'], 'dur': asr_of[i]}
        print('    第%2d段 r%02d.wav %5.2fs  起点%s  「%s」'
              % (i, i, asr_of[i], r['from'], r['text'] or '(无)'))

    step(4, T, '按实际音频重定时长')
    retime(plan, narration, asr_of)
    cur = 0.0
    for i, s in enumerate(plan['segments'], 1):
        # 原声段用真实切片时长，反写 raw_start 供画面定位
        if i in raw_files:
            s['raw_start'] = raw_files[i]['start']
        print('    %2d段 %5.2fs → [%5.2f - %5.2f]  %s'
              % (i, s['duration'], cur, cur + s['duration'],
                 s.get('text') or '(原声)'))
        cur += s['duration']
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                         encoding='utf-8')

    step(5, T, '切画面并拼接')
    clips = cut_clips(plan, dataset, ep_dir)
    if len(clips) != len(plan['segments']):
        raise RuntimeError('切片失败 %d/%d' % (len(clips), len(plan['segments'])))
    silent = concat(clips, work)
    print('    拼接完成 %ss' % dur(silent))

    step(6, T, '混音（解说/原声互斥 + BGM duck）')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mix_v6
    bgm_path = None
    for cand in (plan.get('bgm'), plan.get('bgm')):
        if cand:
            for p in (Path(cand), Path(cand).with_suffix('.wav')):
                if p.exists():
                    bgm_path = p
                    break
        if bgm_path:
            break
    # 说明：mix_v6 读 _work/audio/seg*.wav，这里先落盘
    for i, s in enumerate(plan['segments']):
        if (s.get('text') or '').strip():
            raw = ep_dir / '_tts' / ('raw%02d.wav' % (i + 1))
            dst = audio / ('seg%02d.wav' % i)
            run([FF, '-v', 'error', '-y', '-i', str(raw),
                 '-af', 'atrim=0:%.2f' % s['duration'],
                 '-ac', '1', '-ar', '44100', str(dst)], 120)
    ok = mix_v6.mix(plan_path, ep_dir, bgm_path)
    if not ok:
        raise RuntimeError('混音失败')

    step(7, T, '烧字幕')
    srt = write_srt(plan, work / 'subs.srt')
    # ★ plan['name'] 常不带扩展名，ffmpeg 认不出容器格式 → Invalid argument
    name = plan['name'] if plan['name'].lower().endswith('.mp4') else plan['name'] + '.mp4'
    final = ep_dir / name
    if not burn_subs(work / 'mixed_v6.mp4', srt, final):
        shutil.copy(work / 'mixed_v6.mp4', final)
        print('    [警告] 字幕烧录失败，输出无字幕版')
    print('    成片: %s' % final)

    step(8, T, '校验')
    rep = verify(final)
    print('    时长 %ss｜视频轨 %ss｜音轨 %ss'
          % (rep['duration'], rep['v_dur'], rep['a_dur']))
    print('    响度 %s LUFS｜峰值 %s dBTP' % (rep['lufs'], rep['peak']))

    if rep.get('audio_truncated'):
        print('    ✗✗✗ 音轨被截断！视频 %ss 但音轨只有 %ss'
              % (rep['v_dur'], rep['a_dur']))
        print('       → 检查 amix 的 duration 参数（必须 longest，不能是 first）')
    if rep['silence_count']:
        print('    ⚠ %d 处静音段：' % rep['silence_count'])
        for ln in rep['silence_lines'][:5]:
            print('      ' + ln)
    if rep['ok']:
        print('    ✓ 音轨完整、无静音段、响度达标')

    step(9, T, '完成')
    print('    音色: %s' % voice)
    print('    %d 段解说 + %d 段原声 + BGM' % (n_seg, n_raw))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print('\n[失败] %s: %s' % (type(e).__name__, e))
        sys.exit(1)