"""对白时间戳清单 —— 原声定位的基础数据（v2.6 第 2 步）。

为什么需要它：解说要放原片原声，必须知道「哪句台词在第几秒」。
faster-whisper 已带 word_timestamps，实测 147s 素材产出 106 段带时间戳的对白。

输出 dialogue.json：
{
  "audio": "full.wav",
  "duration": 147.19,
  "segments": [
    {"id": "D001", "start": 0.0, "end": 2.4, "text": "...",
     "words": [{"w": "...", "s": 0.0, "e": 0.3}],
     "quoteable": true,      // 是否适合做原声（台词有信息量）
     "quote_score": 0.72}
  ]
}

quote_score 判据（用于「哪里放原声」）：
- 台词长度适中（5-25 字最适合单段原声）
- 含第一人称（叙事性强）
- 含张力词（顶替/羞辱/退学/金牌…）
- 太短（<4 字）不够做独立原声段
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TENSE = ['顶替', '羞辱', '退学', '打工', '金牌', '辞职', '车祸', '离婚', '跪',
         '甩', '撕', '打', '抢', '骂', '冷笑', '哭', '崩', '救', '钱', '命']


def cfg():
    return json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))


def build(audio: str, out_json: str) -> dict:
    c = cfg()
    py = c['runtime']['asr_python']
    a = c['asr']
    script = (
        "import json,sys\n"
        "from faster_whisper import WhisperModel\n"
        f"m=WhisperModel({c['runtime'].get('asr_model_dir', 'D:/AITools/models/faster-whisper-tiny')!r},"
        f"device={a['device']!r},compute_type={a['compute_type']!r})\n"
        "segs,info=m.transcribe(sys.argv[1],language='zh',word_timestamps=True,"
        f"beam_size={a['beam_size']!r},vad_filter=True)\n"
        "segs=list(segs)\n"
        "out={'language':info.language,'duration':info.duration,'segments':[]}\n"
        "for i,s in enumerate(segs,1):\n"
        "    ws=[{'w':(w.word or '').strip(),'s':round(w.start,2),'e':round(w.end,2)}"
        " for w in (s.words or [])]\n"
        "    out['segments'].append({'id':f'D{i:03d}','start':round(s.start,2),"
        "'end':round(s.end,2),'text':(s.text or '').strip(),'words':ws})\n"
        "print(json.dumps(out,ensure_ascii=False))\n"
    )
    r = subprocess.run([py, '-X', 'utf8', '-c', script, audio],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=3600)
    line = [x for x in r.stdout.strip().splitlines() if x.startswith('{')]
    if not line:
        raise RuntimeError(f'ASR 无输出: {(r.stderr or r.stdout)[-300:]}')
    data = json.loads(line[-1])

    for s in data['segments']:
        s['quote_score'] = quote_score(s['text'])
        s['quoteable'] = s['quote_score'] >= 0.4

    data['audio'] = audio
    Path(out_json).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def quote_score(text: str) -> float:
    """这段台词适不适合单独做原声段。"""
    t = text.strip()
    n = len(t)
    if n < 4 or n > 40:
        return 0.0
    score = 0.3
    # 长度适中加分
    if 5 <= n <= 22:
        score += 0.25
    # 第一人称叙事
    if '我' in t:
        score += 0.2
    # 张力词
    hits = sum(1 for w in TENSE if w in t)
    score += min(0.3, hits * 0.12)
    # 过长的扣分
    if n > 28:
        score -= 0.2
    return round(min(score, 1.0), 3)


def top_segments(dialogue: dict, n: int = 8) -> list:
    """挑出最适合做原声的片段，供 S3 参考。"""
    ok = [s for s in dialogue['segments'] if s['quoteable']]
    ok.sort(key=lambda x: -x['quote_score'])
    return ok[:n]


if __name__ == '__main__':
    audio = sys.argv[1] if len(sys.argv) > 1 else 'full.wav'
    out = sys.argv[2] if len(sys.argv) > 2 else 'dialogue.json'
    d = build(audio, out)
    print(f"片段 {len(d['segments'])} 条，时长 {d['duration']:.1f}s")
    q = [s for s in d['segments'] if s['quoteable']]
    print(f"可做原声 {len(q)} 条（占比 {len(q)/len(d['segments']):.0%}）")
    print('\n最适合放原声的 TOP6：')
    for s in top_segments(d, 6):
        print(f"  {s['id']} [{s['start']:6.2f}-{s['end']:6.2f}] "
              f"score={s['quote_score']:.2f}  {s['text'][:30]}")
    print(f'\n[out] {out}')
