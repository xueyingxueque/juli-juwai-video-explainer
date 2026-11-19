"""素材预检门 v3.0 —— 素材性质检测。

★ 为什么必须在源头：
用户发现素材库其实是「解说成片」，但引擎此前毫无察觉地跑完了 S1-S3。
所有下游门（锚点门、主体门、POV 门）的判据都建立在「这是原片」这个错误前提上。
**根因不是某个门失灵，是没有门去问"这到底是什么素材"。**

★ v3.0 判据重写（2026-10-20 实测，旧判据会误杀真素材）：
  旧判据「音频动态范围 <20dB 且静音 <5%」+「时长 <240s」对**短剧**完全没有区分力 ——
  实测 4 部真实短剧原片：动态范围 12.7~18.9dB、静音 0%~2%、时长 65~145s。
  其中「返乡那天」已经用它做出过合格成片，却被这两条判成"解说成片"。
  原因很简单：短剧原片本来就是全程有对白+BGM、单集 1~3 分钟。
  这些阈值大概是按"电影/长视频"的直觉定的，套到短剧上等于 100% 误杀。

  真正有区分力的是**文本人称**：
  - 原片：角色对话（"谢老公，就知道你最疼我"）、第一人称自述（"我叫林峰…"）
  - 解说成片：第三人称旁白（"注意看，这个男人叫小帅…"）
  实测 6 部样本（4 原片 + 2 解说成片）：解说模板词命中数 原片全为 0、解说成片全为 3。
  → **只有这一条能否决**；音频统计与时长只作信息记录，不再具有否决权。

用法（每次处理素材前必跑）：
    python material_precheck.py <文件或目录>
退出码：0 = 疑似原片，可继续；2 = 解说成片，源头上拒绝；1 = 检测异常。
"""
import json
import math
import re
import subprocess
import sys
import tempfile
import wave
import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))

# 解说成片的画面特征词（人物标签、装饰文字、解说专用元素）
TEMPLATE_CUES = [
    '人物标签', '标签牌', '角色名', '人设', '解说', '旁白',
    '第一集', '第1集', '全集', '精彩', '看点', '注意看',
]
# 解说片的调色/构图痕迹（VLM 抽帧描述时用）
TEMPLATE_FRAME_CUES = ['标签', '牌', '花字', '标题栏', '进度条']

# ── 文本人称判据 ────────────────────────────────
# 强特征：解说号专属话术，正常剧情对白里几乎不会出现
NARR_CUES_STRONG = [
    '注意看', '这个男人', '这个女人', '这个男孩', '这个女孩', '这个小伙',
    '小帅', '小美', '全集', '大结局', '开局', '本期', '小编', '咱们接着看',
]
# 弱特征：书面连接词，角色口语里罕见但偶有，单独出现不算
NARR_CUES_WEAK = [
    '不料', '谁料', '殊不知', '下一秒', '紧接着', '随后', '于是他', '于是她',
    '没想到', '然而', '就在这时', '原来',
]


def probe_basic(video: Path) -> dict:
    r = subprocess.run(
        [CFG['runtime']['ffprobe'], '-v', 'error',
         '-show_entries', 'format=duration,bit_rate',
         '-show_entries', 'stream=codec_type,width,height',
         '-of', 'json', str(video)],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f'ffprobe 失败: {r.stderr[:200]}')
    info = json.loads(r.stdout)
    fmt = info.get('format', {})
    streams = info.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), {})
    return {
        'duration': round(float(fmt.get('duration', 0)), 2),
        'bitrate': int(fmt.get('bit_rate', 0) or 0),
        'width': v.get('width', 0), 'height': v.get('height', 0),
        'has_audio': any(s.get('codec_type') == 'audio' for s in streams),
        'has_subtitle_stream': any(s.get('codec_type') == 'subtitle' for s in streams),
    }


def probe_audio(video: Path) -> dict:
    """音频动态范围与静音占比 —— 区分 TTS 旁白与原片音轨。

    注意：Windows 下 TemporaryDirectory 返回短路径(ADMINI~1)，
    ffmpeg 写入前需转成完整反斜杠路径，否则报 WinError 267。
    """
    if not probe_basic(video)['has_audio']:
        return {'available': False, 'reason': 'no_audio_track'}
    td = Path(tempfile.gettempdir()).resolve() / ('mp_' + next(tempfile._get_candidate_names()))
    td.mkdir(parents=True, exist_ok=True)
    try:
        wav = td / 'a.wav'
        r = subprocess.run(
            [CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-i', str(video),
             '-vn', '-ac', '1', '-ar', '16000', str(wav)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
        if r.returncode != 0 or not wav.exists():
            return {'available': False, 'error': (r.stderr or '')[:120]}
        w = wave.open(str(wav))
        n, sr = w.getnframes(), w.getframerate()
        a = array.array('h', w.readframes(n))
        if not a:
            return {'available': False, 'reason': 'empty_audio'}
        frame = max(sr // 20, 1)
        usable = len(a) // frame * frame
        db = []
        for i in range(0, usable, frame):
            seg = a[i:i + frame]
            s = sum(x * x for x in seg) / len(seg)
            db.append(20 * math.log10(max((s ** 0.5) / 32768.0, 1e-8)))
        srt = sorted(db)
        out = {
            'available': True,
            'dynamic_range_db': round(srt[int(len(srt) * 0.95)] - srt[int(len(srt) * 0.10)], 1),
            'silence_ratio': round(sum(1 for x in db if x < -40) / len(db), 3),
            'avg_db': round(sum(db) / len(db), 1),
        }
        return out
    except Exception as e:
        return {'available': False, 'error': f'{type(e).__name__}: {e}'}
    finally:
        try:
            for f in td.glob('*'):
                f.unlink()
            td.rmdir()
        except Exception:
            pass


def check_template_text(video: Path, evidence: dict | None) -> dict:
    """VLM 抽帧找解说模板元素（人物标签牌等）。无 VLM 时跳过。"""
    if not evidence:
        return {'checked': False, 'reason': '无 evidence，跳过'}
    hits = []
    for e in evidence:
        t = e.get('desc', '')
        for cue in TEMPLATE_FRAME_CUES:
            if cue in t:
                hits.append({'evidence_id': e.get('evidence_id'), 'cue': cue})
    return {'checked': True, 'hits': hits[:5], 'hit': bool(hits)}


def check_narration_text(video: Path) -> dict:
    """用 ASR 文本判断"第三人称旁白解说"还是"角色对白"。

    实测依据见模块 docstring：原片解说模板词命中 0，解说成片命中 3。
    失败时返回 checked=False，**不否决素材**（不能因为 ASR 挂了就把真素材判死）。
    """
    try:
        if not probe_basic(video)['has_audio']:
            return {'checked': False, 'reason': '无音轨'}
    except Exception as e:
        return {'checked': False, 'reason': f'探测失败 {type(e).__name__}'}

    td = Path(tempfile.gettempdir()).resolve() / ('asr_' + next(tempfile._get_candidate_names()))
    td.mkdir(parents=True, exist_ok=True)
    wav = td / 'a.wav'
    try:
        subprocess.run([CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-i', str(video),
                        '-vn', '-ac', '1', '-ar', '16000', str(wav)],
                       capture_output=True, timeout=600, check=True)
        sys.path.insert(0, str(ROOT / 'scripts'))
        import asr
        text = re.sub(r'<\|[^|]*\|>', '', asr.transcribe(str(wav)).get('text') or '')
    except Exception as e:
        return {'checked': False, 'reason': f'ASR 不可用 {type(e).__name__}: {str(e)[:80]}'}
    finally:
        try:
            for f in td.glob('*'):
                f.unlink()
            td.rmdir()
        except Exception:
            pass

    strong = [c for c in NARR_CUES_STRONG if c in text]
    weak = [c for c in NARR_CUES_WEAK if c in text]
    hit = bool(strong) or len(weak) >= 3
    return {
        'checked': True, 'hit': hit, 'strong': strong, 'weak': weak,
        'chars': len(text), 'excerpt': text[:80],
        'detail': (f"第三人称旁白特征 强{strong} 弱{weak} → 解说成片"
                   if hit else
                   f"未检出旁白话术（强0 弱{len(weak)}）→ 角色对白/自述，符合原片"),
    }


def classify(video: Path, evidence: list | None = None) -> dict:
    basic = probe_basic(video)
    audio = probe_audio(video)          # 仅作信息记录，不再有否决权（见 docstring）
    nar = check_narration_text(video)
    tmpl = check_template_text(video, evidence)

    signals = []

    # ★ 信号 1（唯一能否决的）：文本人称 = 第三人称旁白解说
    signals.append({'name': 'narration_text',
                    'hit': bool(nar.get('hit')),
                    'detail': nar.get('detail') or f"未检查（{nar.get('reason')}）"})

    # 信号 2：画面有解说模板元素（需 evidence，precheck 阶段通常跳过）
    if tmpl.get('checked'):
        signals.append({'name': 'frame_template', 'hit': tmpl['hit'],
                        'detail': (f"命中模板元素: {tmpl['hits']}" if tmpl['hit']
                                   else '未检出人物标签等模板元素')})

    # 信号 3：音轨统计（只记录）
    if audio.get('available'):
        signals.append({'name': 'audio_stats', 'hit': False,
                        'detail': f"动态范围 {audio['dynamic_range_db']}dB、"
                                  f"静音占比 {audio['silence_ratio']:.0%}"
                                  f"（短剧原片同样全程有声，此项不参与判定）"})
    else:
        signals.append({'name': 'audio_stats', 'hit': False,
                        'detail': f"无音轨或读取失败（{audio.get('reason', '未知')}）"})

    # 信号 4：时长（<30s 连一集短剧都不够，只提示不否决）
    dur = basic['duration']
    signals.append({'name': 'too_short', 'hit': bool(dur and dur < 30),
                    'detail': (f'时长 {dur}s < 30s，短于一集短剧的合理下限'
                               if (dur and dur < 30) else f'时长 {dur}s（短剧单集 60~180s 属正常）')})

    hits = sum(1 for s in signals if s['hit'])
    # ★ 判定只看 narration_text：其余信号全中也不否决（它们对短剧没有区分力）
    verdict = '解说成片' if nar.get('hit') else '疑似原片'
    return {
        'path': str(video),
        'basic': basic,
        'audio': audio,
        'narration_text': nar,
        'frame_template': tmpl,
        'signals': signals,
        'hit_count': hits,
        'verdict': verdict,
        'can_extract_original_audio': verdict == '疑似原片',
        'can_restore_true_plot': verdict == '疑似原片',
        'usable_as': ('原片素材' if verdict == '疑似原片' else '爆款参考库（只能学写法，不能提取原片事实/原声）'),
    }


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.')
    vids = ([target] if target.is_file()
            else sorted(target.rglob('*.mp4')))[:20]
    if not vids:
        print('未找到 mp4')
        return 1

    results = []
    for v in vids:
        try:
            r = classify(v)
        except Exception as e:
            r = {'path': str(v), 'error': f'{type(e).__name__}: {e}'}
        results.append(r)
        if 'error' not in r:
            print(f"[{r['verdict']:6s}] {v.name}  {r['hit_count']} 项命中")
            for s in r['signals']:
                print(f"    {'✔' if s['hit'] else '✗'} {s['name']}: {s['detail']}")
        else:
            print(f"[错误] {v.name}: {r['error']}")

    out = ROOT / 'references' / 'material-precheck-report.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n[out] {out}')

    bad = sum(1 for r in results if r.get('verdict') == '解说成片')
    if bad:
        print(f'\n[源头上拒绝] {bad}/{len(results)} 条为解说成片。')
        print('这些素材只能当爆款参考库用；提取原片事实/角色原声的前提不成立。')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
