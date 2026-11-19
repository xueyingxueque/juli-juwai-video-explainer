"""镜头证据解析器 —— 把 (ep, t) 对到 VLM 读帧证据。

用途：写 plan 时每个解说段都必须挂 `event_id`，而 series_decompose 只均匀抽 8 帧/集，
我选的镜头大概率不在采样点上。本脚本负责：
  1. 已有事件命中（±match_window 秒）→ 直接复用，id 用原事件 id；
  2. 没命中 → ffmpeg 抽这一帧 + vlm.ask 读一帧，生成新事件；
  3. 输出统一事件列表（可并入 events.json）+ 每个镜头的规范 t。

★ 约定（2026-10-21）：
  - plan 里解说段的 `t` **必须等于证据帧的 t**（validate_plan 的 time_tolerance=0.75s）；
  - 原声段的 `t` 用 ASR 行中点（见 references/00-pitfalls.md ㉛），
    本脚本同时接受 `raw=True` 的请求，此时只回填 ASR 行信息、不读帧。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL = Path(r"C:/Users/Administrator/.workbuddy/skills/剧里剧外-视频解说引擎")
sys.path.insert(0, str(SKILL / 'scripts'))
import vlm  # noqa: E402
import asr  # noqa: E402

HERE_PARENT = Path(__file__).resolve().parent.parent  # 任务目录（_plan 的上一级）

CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
FF = CFG['runtime']['ffmpeg']
# ★ v3.7.1 冷开场钩子上限（与 compliance.cold_open.max_sec 同源）
COLD_OPEN_MAX = float((CFG.get('compliance', {}).get('cold_open') or {}).get('max_sec', 5.0))
# ★ 素材路径从环境变量读（E2E_DATASET），换剧不用改代码；缺省回落到 gq6。
DATASET = Path(os.environ.get('E2E_DATASET') or
               r"D:/BaiduNetdiskDownload/国庆归家惊变，我撕碎五年错付婚姻 卡点6")
# ★ 事件复用库必须跟随本任务目录（2026-10-22 修串剧隐患）：旧版三副本都硬编码
#   指向 gq6-e2e/episodes，同集数±1.2s 命中会复用错剧事件。改为 HERE.parent/episodes，
#   gq6 解析结果不变；新任务目录无 episodes/ → load_ep 返回空 → 全部新读帧（正确）。
EPS_DIR = Path(os.environ.get('E2E_EPS_DIR') or (HERE_PARENT / 'episodes'))

READ_PROMPT = '''读这张短剧截图，**严格区分画面里的人**和**说话的人**，不要混为一谈。
1) frame_who：画面里能看到的人（外貌+性别+年龄+动作），没有就写「无」
2) subtitle_text：画面上的字幕文字原文，没有就写空
3) speaker：那句字幕是谁说的？只能从以下选一个——
   「画面内男性」/「画面内女性」/「画外音」/「内心独白」/「无法判断」
   ★ 若字幕是画外音或他人对白，出镜者不是说话人，speaker 不要填「画面内…」。
4) desc：一句话概括这帧在做什么
只描述能看到的，不推测剧情。'''


def load_ep(ep: int) -> dict:
    p = EPS_DIR / f'E{ep:03d}.json'
    if not p.is_file():
        return {'frames': []}
    return json.loads(p.read_text(encoding='utf-8'))


def find_src(ep: int) -> Path:
    from plan_story import find_ep_file
    return find_ep_file(DATASET, ep)


def grab(video: Path, t: float) -> Path:
    tmp = Path(tempfile.gettempdir()) / f'ev_{video.stem[:12]}_{t:.2f}.jpg'
    subprocess.run([FF, '-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', str(video),
                    '-frames:v', '1', '-q:v', '3', str(tmp)],
                   capture_output=True, text=True, encoding='utf-8',
                   errors='replace', timeout=180)
    return tmp


def resolve(shots: list, match_window: float = 1.2) -> list:
    """shots: [{ep, t, raw?, want?}] → 每项补 id/desc/dialogue/speaker/frame_t"""
    asr_cache: dict[int, list] = {}
    out = []
    for i, s in enumerate(shots):
        ep = int(s['ep'])
        t = float(s['t'])
        # ★ v3.7.1 冷开场钩子：直接透传，不跑 VLM 也不跑 ASR。
        #   cold_open 是人工选定的高潮片段（ep/t/duration/keep_audio/caption），
        #   不需要读帧证据，也不需要台词定位——build 端 cut_cold_open 直接按
        #   时长切连续原片。保留 is_cold_open/duration/keep_audio/caption 供 assemble。
        if s.get('is_cold_open') or s.get('cold_open') or s.get('block') == 'cold_open':
            cd = float(s.get('duration') or COLD_OPEN_MAX)
            cd = min(max(cd, 0.1), COLD_OPEN_MAX)
            out.append({**s, 'id': s.get('id') or f'CO{ep:03d}-{t:.2f}',
                        'frame_t': t, 'desc': '(冷开场原片钩子)', 'dialogue': '',
                        'speaker': '', 'is_cold_open': True,
                        'duration': cd, 'keep_audio': bool(s.get('keep_audio', True)),
                        'caption': (s.get('caption') or '').strip(),
                        'note': '冷开场钩子(透传,无VLM/ASR)'})
            continue
        if s.get('raw'):
            if ep not in asr_cache:
                wav = Path(tempfile.gettempdir()) / f'ar_{ep}.wav'
                subprocess.run([FF, '-v', 'error', '-y', '-i', str(find_src(ep)), '-vn',
                                '-ac', '1', '-ar', '16000', str(wav)],
                               capture_output=True, timeout=300)
                asr_cache[ep] = asr.timed_lines(str(wav), CFG)
                wav.unlink(missing_ok=True)
            best = None
            for ln in asr_cache[ep]:
                mid = (float(ln['s']) + float(ln['e'])) / 2
                if best is None or abs(mid - t) < abs((best['s'] + best['e']) / 2 - t):
                    best = ln
            if best is None:
                raise SystemExit(f'shot#{i} ep{ep} t={t}: 该集没有 ASR 行')
            span = float(best['e']) - float(best['s'])
            out.append({**s, 'id': f'R{ep:03d}-{best["s"]:.2f}', 'frame_t': float(best['s']),
                        'desc': '(原声引用)', 'dialogue': (best['t'] or '').strip(),
                        'speaker': '角色对白', 'asr': best, 'span': span,
                        'note': 'ASR 行' if span <= 2.5 else 'ASR 行(超2.5s，retime 会截断)'})
            continue

        rec = load_ep(ep)
        hit = None
        for f in rec.get('frames', []):
            if abs(float(f.get('t', -99)) - t) <= match_window:
                hit = f
                break
        if hit is not None:
            out.append({**s, 'id': f'{ep:03d}-{hit["frame"][:3]}',
                        'frame_t': float(hit['t']), 'desc': hit.get('desc', ''),
                        'dialogue': hit.get('subtitle_text', ''),
                        'speaker': hit.get('speaker', ''), 'note': '复用已有事件'})
            continue
        d = vlm.ask_json(READ_PROMPT, images=[str(grab(find_src(ep), t))],
                         max_tokens=320, temperature=0.2,
                         default={'frame_who': '', 'subtitle_text': '',
                                  'speaker': '无法判断', 'desc': ''})
        out.append({**s, 'id': f'X{ep:03d}-{t:.2f}', 'frame_t': t,
                    'desc': d.get('desc', ''), 'dialogue': d.get('subtitle_text', ''),
                    'speaker': d.get('speaker', ''), 'frame_who': d.get('frame_who', ''),
                    'note': '新读帧'})
    return out


if __name__ == '__main__':
    spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    res = resolve(spec['shots'])
    Path(spec['out']).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                 encoding='utf-8')
    for r in res:
        print(f"ep{r['ep']:>3} t={r['frame_t']:6.2f} [{r['id']}] {r['note']}")
        print(f"      desc={r['desc'][:64]}")
        if r['dialogue']:
            print(f"      sub ={r['dialogue'][:40]}  ({r['speaker']})")
