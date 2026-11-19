"""分段配音 + 按目标时长变速对齐（v2 —— 修正变速方向与根本解法）。

★ 两个实测教训：
1. **变速方向**：初版把 x = target/实际 算出来后夹错区间，
   12 段全部 ×1.0（总 96s vs 目标 36s）。
   第二版方向对了但没限住，做成 ×0.451（减速），更糟。
   → 修正：x > 1 才加速，且必须限 1.6；不够的部分用 apad 补静音。

2. ★★ 更根本：**靠变速对齐是错的**。
   实测 TTS 语速 3.22 字/秒，短剧解说标准是 5.5-6.0。
   25 字文案天然要 7.76 秒，而片段只有 3.5 秒 —— 需要加速到 2.2x，
   超过 1.6 上限，一加速人声就发飘。
   → **正解：先按目标时长反推该写多少字，再写稿。**
   字数 = 目标时长 × 5.0（留余量给停顿）。

本模块因此提供 `chars_for_duration()`，写稿前先算字数上限。
"""
import json
import subprocess
import sys
from pathlib import Path

CFG = json.loads((Path(__file__).resolve().parent.parent
                  / 'config' / 'engine.json').read_text(encoding='utf-8'))
DUB_ROOT = Path(r'C:/Users/Administrator/.workbuddy/skills/本地配音工坊')
FF = CFG['runtime']['ffmpeg']
FP = CFG['runtime']['ffprobe']
# dub.py 需用配好的解释器跑（直接执行会 WinError 193）
DUB_PY = r'D:\AITools\TTS\Dots.tts\wzf\python.exe'

# ★ 默认音色：谭湘文徐浩伦
#   来源：本地配音工坊 §0.1「默认建议 = breeze + 谭湘文徐浩伦」
#   实测语速 6.00 字/秒 = 短剧解说标准（5.0-6.0）
#   对比：纪录片 3.75（太慢）/ 小棠 4.69 / 子言 4.55
DEFAULT_VOICE = '谭湘文徐浩伦'

# 短剧解说标准语速（B 站/抖音）
SPEED_TARGET = 5.5          # 字/秒
SPEED_SAFE = 5.0            # 字/秒（写稿时的安全值，留余量）

SPEED_ACCEL_MAX = 1.6       # atempo 加速上限（再快人声发飘）
SPEED_DECEL_MIN = 0.75      # atempo 减速下限


def chars_for_duration(seconds: float, speed: float = SPEED_SAFE) -> int:
    """★ 写稿前先算：一段 duration 秒的解说该写多少字。

    这是「靠变速对齐」失败后的正解 —— 从源头让文案长度匹配时长。
    """
    return max(1, int(seconds * speed))


def atempo_chain(x: float) -> str:
    """atempo 单级只支持 0.5-2.0，超出要串联。"""
    parts, cur = [], x
    while cur > 2.0:
        parts.append('atempo=2.0')
        cur /= 2.0
    while cur < 0.5:
        parts.append('atempo=0.5')
        cur /= 0.5
    parts.append('atempo=%.4f' % cur)
    return ','.join(parts)


def run(cmd: list, timeout: int = 900) -> tuple:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or '') + (r.stderr or '')


def dur_of(p: Path) -> float:
    rc, r = run([FP, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', str(p)], 60)
    try:
        return float(r.strip().splitlines()[-1])
    except Exception:
        return 0.0


def synth(text: str, voice: str, out: Path) -> float:
    rc, log = run([DUB_PY, '-X', 'utf8', str(DUB_ROOT / 'dub.py'),
                   '--text', text, '--voice', voice, '--out', str(out)], 900)
    if rc != 0 or not out.exists():
        print('  [TTS失败] %s' % log[-160:])
        return 0.0
    return dur_of(out)


def fit_to_target(src: Path, dst: Path, target: float) -> dict:
    """把音频对齐到目标时长。加速限 1.6，不够的用静音补（宁停勿飘）。"""
    d0 = dur_of(src)
    if d0 <= 0:
        return {'ok': False, 'reason': 'src duration 0'}

    need = target / d0
    filters = []
    if need >= 1.0:
        used = min(need, SPEED_ACCEL_MAX)
        if used > 1.001:
            filters.append(atempo_chain(used))
        gap = d0 * used
        if target - gap > 0.05:
            filters.append('apad=pad_dur=%.2f' % (target - gap))
    else:
        used = max(need, SPEED_DECEL_MIN)
        if used < 0.999:
            filters.append(atempo_chain(used))
        gap = d0 * used
        if target - gap > 0.05:
            filters.append('apad=pad_dur=%.2f' % (target - gap))
    if not filters:
        used = 1.0
        filters = ['anull']

    rc, log = run([FF, '-v', 'error', '-y', '-i', str(src),
                   '-af', ','.join(filters), '-ac', '1', '-ar', '44100',
                   str(dst)], 300)
    ok = rc == 0 and dst.exists()
    return {'ok': ok, 'src_dur': round(d0, 2), 'target': round(target, 2),
            'speed': round(used, 3),
            'need': round(need, 3),
            'over_speed': need > SPEED_ACCEL_MAX,
            'final': round(dur_of(dst), 2) if ok else 0}


def build(plan: dict, out_dir: Path, voice: str = DEFAULT_VOICE,
          reuse_raw: bool = True) -> dict:
    segs = plan['segments']
    tts_dir = out_dir / '_tts'
    tts_dir.mkdir(parents=True, exist_ok=True)

    rows, aligned = [], []
    for i, s in enumerate(segs, 1):
        target = float(s.get('duration') or s.get('clip_duration') or 3.0)
        raw = tts_dir / ('raw%02d.wav' % i)
        fin = tts_dir / ('S%02d.wav' % i)

        if reuse_raw and raw.exists():
            d0 = dur_of(raw)
        else:
            d0 = synth(s['text'], voice, raw)
        if d0 <= 0:
            rows.append({'i': i, 'ok': False, 'text': s['text'][:18]})
            continue

        fit = fit_to_target(raw, fin, target)
        fit['i'] = i
        fit['text'] = s['text'][:18]
        fit['budget_chars'] = chars_for_duration(target)
        fit['actual_chars'] = len(s['text'])
        rows.append(fit)
        if fit['ok']:
            aligned.append(fin)

    lst = tts_dir / 'list.txt'
    lst.write_text('\n'.join("file '%s'" % c.as_posix() for c in aligned), encoding='utf-8')
    narration = out_dir / 'narration.wav'
    run([FF, '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', str(lst),
         '-c:a', 'pcm_s16le', '-ar', '44100', '-ac', '1', str(narration)], 600)
    return {'rows': rows, 'narration': str(narration),
            'duration': round(dur_of(narration), 2) if narration.exists() else 0,
            'segments': len(aligned)}


if __name__ == '__main__':
    plan_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    voice = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_VOICE
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    r = build(plan, out_dir, voice)
    print('\n配音 %d/%d 段，总时长 %ss' % (r['segments'], len(plan['segments']), r['duration']))
    over = [x for x in r['rows'] if x.get('over_speed')]
    for x in r['rows']:
        if not x.get('ok'):
            print('  %2d 失败' % x['i'])
            continue
        flag = ' ⚠超速' if x.get('over_speed') else ''
        print('  %2d %5.2fs->%.1fs x%-5.2f 得%.2fs 字%2d(预算%d)%s'
              % (x['i'], x['src_dur'], x['target'], x['speed'], x['final'],
                 x['actual_chars'], x['budget_chars'], flag))
    if over:
        print('\n注意：%d 段需要的加速超过 %.1fx，文案偏长。' % (len(over), SPEED_ACCEL_MAX))
        print('建议按「字数 = 目标秒数 x %.1f」精简这些句子。' % SPEED_SAFE)
