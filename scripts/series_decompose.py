"""全剧分解 —— 把 N 集短剧拆成带张力评分的事件库。

★ 为什么全量：
之前用单集写解说，等于给别人的解说稿再解一遍，没有信息增量。
用户要求：88 集全部分解 → 建事件库 → 按故事线重排（不按集号）→ 高潮前置 → 混剪原片。

★ 复用 s2_story 的结构化读帧（frame_who / subtitle_text / speaker），
  避免再次踩「字幕主体≠画面主体」的坑。

断点续跑：已完成的集跳过，中断可继续。
输出：<out>/events.json（全剧事件库）
      <out>/episodes/E{集号}.json（单集分解，含 ASR 时间戳）
"""
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import vlm  # noqa: E402
from plan_story import ep_from_name  # noqa: E402  ★ 集号解析唯一实现（build.py 共用）

CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))

# 张力分档 —— 决定这个事件能不能进系列、属于第几条
#
# ★ 实测教训：初版只看字幕关键词（决战/秒杀/碾压），结果全判 L0。
#   原因：题材是「乡村萌宠 + 修仙」，最强画面是「黑熊张开大嘴扑来」，
#   根本没有那种关键词。**张力必须同时看画面冲击。**

# 画面冲击（这类题材的高潮主要在这里）
VISUAL_CLIMAX = ['扑', '撕', '撞', '砸', '砍', '刺', '冲', '扑倒', '张口', '大嘴',
                 '锋利', '獠牙', '攻击', '搏斗', '缠斗', '翻滚', '坠落', '爆开',
                 '光芒', '光柱', '剑气', '雷', '电', '火', '爆炸', '变身', '现出',
                 '腾空', '飞起', '崩塌', '碎裂', '巨兽', '咆哮']
VISUAL_CONFLICT = ['对峙', '怒视', '瞪', '争执', '阻拦', '推', '拉扯', '摔', '追',
                   '躲', '闪', '警惕', '紧张', '紧张地', '犹豫', '迟疑']
VISUAL_SETUP = ['田', '屋', '山门', '院子', '村', '赶集', '走路', '背影', '远望',
                '特写', '静', '坐着', '发呆']
# 台词张力（仍保留，但权重降低）
DIALOG_CLIMAX = ['决战', '大战', '杀', '斩', '反杀', '打脸', '逆袭', '觉醒', '突破',
                 '真相', '身份', '暴露', '认亲', '原来', '竟是', '居然']
DIALOG_CONFLICT = ['骂', '怒', '嘲讽', '轻蔑', '挑衅', '威胁', '抢', '夺', '背叛',
                   '陷害', '算计', '骗', '羞辱', '压制', '围攻', '逼']


def _hit(text, words):
    return sum(1 for w in words if w in (text or ''))


# ★ 补：词表漏掉的题材化画面（持刀/发光/异常）
VISUAL_CLIMAX += ['握刀', '持刀', '大刀', '刀', '剑', '发狂', '狂', '异', '神',
                   '仙', '法', '术', '诀', '符', '阵', '灵', '气', '龙', '凤',
                   '巨大', '庞大', '袭击', '袭', '伤', '血', '倒', '亡', '魂',
                   '幼', '崽', '幼崽', '崽们', '叼', '咬', '扑倒', '围']
VISUAL_CONFLICT += ['盯', '看', '拦', '围', '逼', '赶', '追', '拦', '护', '抱',
                    '举起', '指', '摇', '拽', '抓', '抬', '举']


def tension_llm(frame_desc, dialogue, speaker) -> dict:
    """★ VLM 语义张力判定 —— 关键词打分最大的问题是「词表有题材偏置」。

    实测：纯关键词打分下，「小女孩双手握着一把大刀」判 L0、「黑熊张开嘴」判 L2，
    明显不合理。**必须让语义模型来判断，而不是词表。**

    判据（不问「有没有关键词」，只问「这画面张力多大」）：
    - 0.0-0.3 平静（日常、闲聊、静态）
    - 0.3-0.5 有点紧张（对峙、怀疑、观察）
    - 0.5-0.7 明显冲突（追逐、对抗、威胁）
    - 0.7-1.0 高潮爆发（攻击、变身、真相揭示、死亡、震惊）
    """
    try:
        r = vlm.ask_json(
            """判断这个短剧**单个镜头**的张力大小。

画面：""" + (frame_desc or '（无）') + """
台词：""" + (dialogue or '（无）') + """
说话人：""" + (speaker or '不明') + """

★★★ 三条硬规则（违反即判错）★★★

【规则一：字幕与画面不一定同步，不一致≠异常】
字幕是对白，可能说到一半。这一帧抓到的是瞬间画面，
**两者对不上是正常的**（比如台词说"六"，画面只举了三根手指——可能"六"指六根，
画面只拍了三根而已）。
**绝对不要因为「数字对不上」「动作对不上」就判为异常/伏笔/冲突。**

【规则二：只往下调，不往上加】
- 画面本身明确没有冲击（日常/静态）→ 压到 0.3 以下。
- 画面本身看不出冲突 → 给 0.4，**不要脑补后续一定有大事发生**。
- ★ 但如果**画面或台词里明确出现了下面这些**，就按实际强度给高分，**不要因为"只看单帧"就打折**。
  一旦看到「攻击力/受伤/异象/真相」这类具体事件，0.7 以上是合理的。

【规则三：以下情况一律 ≤0.3（日常，不是高潮）】
- 数数、比手指、报数（「六」「三根」这类）
- 吃饭、走路、坐着、站着发呆
- 日常对话、家长里短
- 动物卖萌、宠物打闹
- 表情夸张但无实际事件的摆拍

★ 什么才算高张力（0.7-1.0）：
- 明确的攻击/受伤/死亡（刀、扑、咬、砍、流血）
- 变身/异象显现（发光轮廓、巨大化、符阵法术）
- 真相揭示（身份暴露、秘密曝光）
- 强烈震惊（瞪眼、张嘴、倒地的反应镜头）
- 明确的威胁对抗（举刀对峙、追逐、围堵）

★ 中等（0.4-0.6）：对峙、怀疑、警惕、阻拦、争执。

★ 平静（0-0.3）：日常、静态、无事件。

判断依据是**这个镜头本身对观众造成的情绪冲击**，不是台词里有没有吓人的词。

★ 最后提醒：
- 「保守」只针对**看不出所以然的日常画面**；
- 「明确事件」（攻击/异象/真相/震惊）**该给多高给多高，不要压低**。
  例：某人震惊到瞪眼 + 台词揭露「一个人屠了整个万鬼谷」→ 这是 0.9 级别，不是 0.6。

只输出 JSON：{"score": 0.0-1.0, "reason": "一句话"}""",
            max_tokens=180, temperature=0.2,
            default={'score': 0.0, 'reason': ''})
        s = float(r.get('score', 0) or 0)
        s = max(0.0, min(1.0, s))
        if s >= 0.7:
            lv = 3
        elif s >= 0.5:
            lv = 2
        elif s >= 0.3:
            lv = 1
        else:
            lv = 0
        return {'level': lv, 'score': round(s, 3),
                'why': [f"语义判定 {s:.2f}", (r.get('reason') or '')[:24]],
                'src': 'llm'}
    except Exception:
        return None


def tension(frame_desc, dialogue) -> dict:
    """张力评分 —— 画面与台词双维。画面权重更高（实测：高潮多在画面）。"""
    f = frame_desc or ''
    t = dialogue or ''
    vf, vc, vs = _hit(f, VISUAL_CLIMAX), _hit(f, VISUAL_CONFLICT), _hit(f, VISUAL_SETUP)
    dt, dc = _hit(t, DIALOG_CLIMAX), _hit(t, DIALOG_CONFLICT)

    score = min(0.62, vf * 0.26) + min(0.30, vc * 0.10) + min(0.10, vs * 0.05)
    score += min(0.24, dt * 0.12) + min(0.14, dc * 0.07)

    if vf >= 2:
        lv = 3
    elif vf or dt or vc >= 2:
        lv = 2
    elif vc or dc or vs:
        lv = 1
    else:
        lv = 0

    why = []
    if vf:
        why.append(f'画面冲击x{vf}')
    if dt:
        why.append(f'台词高潮x{dt}')
    if vc or dc:
        why.append(f'对抗x{vc + dc}')
    if vs and lv == 0:
        why.append('铺垫镜头')
    return {'level': lv, 'score': round(min(score, 1.0), 3), 'why': why}


def ep_key(v: Path) -> int:
    """集号解析 —— 唯一实现在 plan_story.ep_from_name（build.py 也用它找源文件）。

    兼容两种命名：`1.mp4` 与 `第88集.mp4`，以及 `001_剧名-第1集.mp4`。
    """
    return ep_from_name(v.stem)


def pick_unique(vids: list) -> tuple:
    """按集号去重 —— 同集号只留一个（优先带「第N集」命名的，其次文件更大）。"""
    by_ep = {}
    for v in vids:
        e = ep_key(v)
        if e not in by_ep:
            by_ep[e] = v
        else:
            # 已有一份：若新的更大，认为是正片
            try:
                if v.stat().st_size > by_ep[e].stat().st_size:
                    by_ep[e] = v
            except OSError:
                pass
    out = [by_ep[k] for k in sorted(by_ep)]
    return out, len(vids) - len(out)


def extract_audio(video: Path, out_wav: Path) -> bool:
    r = subprocess.run([CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-i', str(video),
                        '-vn', '-ac', '1', '-ar', '16000', str(out_wav)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    return r.returncode == 0 and out_wav.exists()


def transcribe(audio: Path) -> dict:
    """整集 ASR 文本（诊断用，不参与事件库打分）。

    ★ 2026-10-21 源头优化：本函数原来是**第三份 ASR 实现**，
      且硬编码 `faster-whisper-tiny` —— 该模型在 engine.json 里被标为
      `ready_but_unusable`（实测 CER 0.963，约 80% 字错）。
      等于用一份已知不可用的引擎产出"看起来很完整的台词"，
      而 build_events / series_plan 都不消费它 → 纯静默污染。
    → 改为走统一 ASR 层 asr.transcribe()（ SenseVoice 主引擎，实测 CER 0.03）。
      事件库的台词ground truth 是 VLM 读到的硬字幕，本字段只作人工速读辅助。
    """
    import asr as _asr
    r = _asr.transcribe(str(audio), CFG)
    text = str(r.get('text') or '').strip()
    if not text:
        return []
    return [{'start': 0.0, 'end': 0.0, 'text': text, 'words': [],
             'engine': r.get('engine'), 'model': r.get('model')}]


def sample_frames(video: Path, out_dir: Path, n: int = 8) -> list:
    """均匀抽 n 帧（每集 60-90s，约 8-10s 一帧）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = float(subprocess.run(
        [CFG['runtime']['ffprobe'], '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', str(video)], capture_output=True, text=True,
        encoding='utf-8', errors='replace'
    ).stdout.strip() or 60)
    step = max(dur / n, 0.5)
    frames = []
    for i in range(n):
        t = min(step * i + step * 0.5, max(dur - 0.3, 0.3))
        f = out_dir / f'f{i:02d}.jpg'
        r = subprocess.run(
            [CFG['runtime']['ffmpeg'], '-v', 'error', '-y', '-ss', f'{t:.2f}',
             '-i', str(video), '-frames:v', '1', '-q:v', '3', str(f)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        if f.exists() and f.stat().st_size > 0:
            frames.append((f, round(t, 2)))
    return frames


def vlm_alive() -> bool:
    """VLM 探活 —— 实测它会掉线（会话回收），且掉线后 ASR/抽帧照跑，
    结果跑几小时才发现全部失败。必须在每集开始前探活。"""
    import urllib.request
    try:
        url = CFG['runtime']['vlm_endpoint'].rstrip('/') + '/health'
        urllib.request.urlopen(url, timeout=8)
        return True
    except Exception:
        return False


def describe(f: Path, t: float, ep: int) -> dict:
    """结构化读帧 —— 严格区分画面里的人与说话的人。"""
    return vlm.ask_json(
        '''读这张短剧截图，**严格区分画面里的人**和**说话的人**，不要混为一谈。
1) frame_who：画面里能看到的人（外貌+性别+年龄+动作），没有就写「无」
2) subtitle_text：画面上的字幕文字原文，没有就写空
3) speaker：那句字幕是谁说的？只能从以下选一个——
   「画面内男性」/「画面内女性」/「画外音」/「内心独白」/「无法判断」
   ★ 若字幕是画外音或他人对白，出镜者不是说话人，speaker 不要填「画面内…」。
4) desc：一句话概括这帧在做什么
只描述能看到的，不推测剧情。''',
        images=[str(f)], max_tokens=320, temperature=0.2,
        default={'frame_who': '', 'subtitle_text': '', 'speaker': '无法判断', 'desc': ''})


def analyze_ep(video: Path, ep: int, out: Path) -> dict:
    ep_dir = out / 'episodes'
    ep_dir.mkdir(parents=True, exist_ok=True)
    dest = ep_dir / f'E{ep:03d}.json'
    if dest.exists():
        return json.loads(dest.read_text(encoding='utf-8'))

    # VLM 探活：掉线时立刻中止，别让 ASR/抽帧空跑几小时
    if not vlm_alive():
        raise RuntimeError('VLM 不在线。先跑 ensure_vlm.ps1 再重试（断点会续，不会重做已完成集）')

    tmp = Path(tempfile.gettempdir()).resolve() / f'sq_{ep}_{os.getpid()}'
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        wav = tmp / 'a.wav'
        asr = transcribe(wav) if extract_audio(video, wav) else []
        frames = sample_frames(video, tmp / 'f')
        fdesc = []
        for f, t in frames:
            d = describe(f, t, ep)
            d['t'] = t
            d['frame'] = f.name
            fdesc.append(d)
        rec = {'ep': ep, 'video': str(video), 'asr': asr, 'frames': fdesc}
        dest.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding='utf-8')
        return rec
    finally:
        try:
            for f in tmp.rglob('*'):
                if f.is_file():
                    f.unlink()
            for d in sorted(tmp.rglob('*'), reverse=True):
                if d.is_dir():
                    d.rmdir()
            tmp.rmdir()
        except Exception:
            pass


def build_events(eps: list) -> list:
    """从单集分解里提取事件，带张力分。"""
    evs = []
    for rec in eps:
        for f in rec.get('frames', []):
            txt = (f.get('subtitle_text') or '') + ' ' + (f.get('desc') or '')
            if len(txt.strip()) < 4:
                continue
            tg = tension_llm(f.get('desc') or '',
                             (f.get('subtitle_text') or '')[:40],
                             f.get('speaker'))
            if tg is None:
                tg = tension(f.get('desc') or '', f.get('subtitle_text') or '')
            evs.append({
                'id': f"{rec['ep']:03d}-{f.get('frame','')[:3]}",
                'ep': rec['ep'], 't': f.get('t'),
                'event': (f.get('desc') or '')[:60],
                'dialogue': (f.get('subtitle_text') or ''),
                'speaker': f.get('speaker', ''),
                'frame_who': (f.get('frame_who') or '')[:50],
                'tension': tg,
            })
    evs.sort(key=lambda x: -x['tension']['score'])
    return evs


def main() -> int:
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('D:/剧里剧外/视频解说/新引擎/series')
    raw = sorted(src.glob('*.mp4'), key=ep_key)
    vids, dup = pick_unique(raw)
    if not vids:
        print('未找到 mp4')
        return 1
    out.mkdir(parents=True, exist_ok=True)

    print(f'扫描 {len(raw)} 个文件，去重后 {len(vids)} 集（丢弃 {dup} 个重复副本）')
    print(f'全量分解中（可断点续跑）')
    done, failed = [], []
    for i, v in enumerate(vids, 1):
        ep = ep_key(v)
        try:
            analyze_ep(v, ep, out)
            done.append(ep)
        except Exception as e:
            msg = str(e)
            failed.append({'ep': ep, 'error': f'{type(e).__name__}: {msg}'})
            print(f'  [{i}/{len(vids)}] 第{ep}集 失败: {type(e).__name__}: {msg[:40]}', flush=True)
            # 连续失败 3 次 → 多半是服务掉线，不是素材问题，直接中止
            if len(failed) >= 3:
                print('[中止] 连续失败 3 次，判定为 VLM 掉线而非素材问题。')
                print('       请执行 ensure_vlm.ps1 拉起服务，再重跑本脚本（会自动从断点续）。')
                break
            continue
        if i % 5 == 0 or i == len(vids):
            print(f'  [{i}/{len(vids)}] 成功 {len(done)} 失败 {len(failed)}', flush=True)

    eps = [json.loads((out / 'episodes' / f'E{e:03d}.json').read_text(encoding='utf-8'))
           for e in done if (out / 'episodes' / f'E{e:03d}.json').exists()]
    evs = build_events(eps)
    (out / 'events.json').write_text(
        json.dumps({'total_eps': len(vids), 'ok_eps': len(done), 'failed': failed,
                    'events': evs}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n事件库完成：{len(evs)} 个事件')
    for lv, name in ((3, '高潮'), (2, '冲突'), (1, '铺垫')):
        sel = [e for e in evs if e['tension']['level'] == lv]
        print(f'  {name}级 {len(sel)} 个')
    return 0


if __name__ == '__main__':
    sys.exit(main())
