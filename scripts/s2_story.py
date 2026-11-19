"""S2 剧情解构 —— 用 ASR 台词 + VLM 画面证据整理可确认事实与事件顺序。

方法论来源：洋子 film-treatment 分场化改编（改编方法，非原创九阶段流程）。
硬约束（引擎铁律）：
- 原片是唯一事实源。人物姓名/身份/动机/因果必须有证据。
- 不明姓名用外观称谓；推断必须显式标注（推测）。
- 每句解说绑定 >=1 个证据 ID，无证据的句子不允许存在。
"""
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vlm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TASK = Path(sys.argv[1] if len(sys.argv) > 1 else '.')

RULES = """
硬性规则（违反即作废）：
1. 只依据给出的时间戳对白与画面证据，不要补充你不知道的剧情。
2. 人名若台词中未明确出现，用外观称谓（如「那个扎马尾的女人」），不要猜名字。
3. 动机、内心戏、因果若无法从证据确认，标为未确认；不要把推测塞进 logline 或 acts。
4. summary 可以点明关键画面动作或对白内容，但不要逐帧罗列；只写对应 evidence_ids 能直接证明的事实。
5. logline 必须是「本片段事实梗概」，不能补成完整故事；片段未展示的后续结果必须写「结果未显示」。
6. 字幕和 ASR 只证明对应文字/对白在相应时段出现，不证明说话者身份或对白描述的动作已发生；计划、愿望或承诺不能改写成已实现的结果。
"""


def frames_desc(frame_dir: Path, every: int = 1, frame_times: dict | None = None) -> list:
    """批量读帧描述。逐帧调用，保证每帧都有证据。

    密度经验（实测）：12 秒/帧只有 6 个证据点，解构出的段落无法与帧内容对应
    （关键词命中率 0%）。改为 4 秒/帧，147 秒素材约 36 个证据点，可支撑逐段绑定。
    """
    fs = sorted(Path(frame_dir).glob('*.jpg'))[::every]
    times = frame_times or {}
    out, failed = [], 0
    for i, f in enumerate(fs, 1):
        try:
            # ★ 关键：必须区分「画面里的人」与「说话的人」。
            #   实测教训：短剧字幕大量是画外音/内心独白/对白延续，
            #   出镜者不一定是说话人。若把两者混为一谈，
            #   下游所有「动作主体」判据都建立在错误前提上。
            data = vlm.ask_json(
                '''读这张短剧截图，只描述静态画面可直接看见的内容。
1) frame_who：画面里能看到的人（外貌+性别+动作），没有就写「无」
2) subtitle_text：画面上的字幕文字原文，没有就写空
3) speaker：静态图片不能证明说话人，固定写「无法判断」；不得根据画中人物或字幕推断
4) desc：一句话概括这帧在做什么
只描述能看到的，不推测剧情。''',
                images=[str(f)], max_tokens=320, temperature=0.2,
                default={'frame_who': '', 'subtitle_text': '', 'speaker': '无法判断', 'desc': ''})
            item = {'evidence_id': f'E{i:03d}', 'frame': f.name,
                    'frame_who': data.get('frame_who', ''),
                    'subtitle_text': data.get('subtitle_text', ''),
                    'speaker': data.get('speaker', '无法判断'),
                    'desc': data.get('desc', '')}
            if f.name in times:
                item['ep'], item['t'] = times[f.name]
            out.append(item)
        except Exception as e:
            failed += 1
            item = {'evidence_id': f'E{i:03d}', 'frame': f.name, 'desc': f'[读取失败: {e}]'}
            if f.name in times:
                item['ep'], item['t'] = times[f.name]
            out.append(item)
        if i % 5 == 0 or i == len(fs):
            print(f'  frame {i}/{len(fs)}  失败{failed}', flush=True)

    # 熔断：过半失败说明 VLM 掉了，继续跑只会产出空证据
    if fs and failed > len(fs) / 2:
        raise RuntimeError(
            f'VLM 读帧失败率过高（{failed}/{len(fs)}），证据不可用。'
            f'先跑 ensure_vlm.ps1 确认服务在线，再重跑本脚本。')
    return out


def attach_audio_segments(ev: list, segments: list) -> dict:
    """Attach each timestamped ASR segment to its nearest sampled visual frame.

    The frame remains the edit anchor; transcript text is a separate audio fact
    attached by time. It never supplies a speaker identity.
    """
    timed_rows = [row for row in ev if isinstance(row, dict)
                  and isinstance(row.get('t'), (int, float))
                  and not isinstance(row.get('t'), bool)]
    report = {'input_segments': len(segments), 'attached_segments': 0,
              'eligible_attached': 0, 'unmatched_segments': 0}
    for row in timed_rows:
        row['audio_segments'] = []
    if not timed_rows:
        report['unmatched_segments'] = len(segments)
        return report
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            report['unmatched_segments'] += 1
            continue
        start, end = segment.get('start'), segment.get('end')
        text = str(segment.get('text') or '').strip()
        if (not text or isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(float(start)) or not math.isfinite(float(end))
                or start < 0 or end < start):
            report['unmatched_segments'] += 1
            continue
        midpoint = (float(start) + float(end)) / 2
        anchor = min(timed_rows, key=lambda row: abs(float(row['t']) - midpoint))
        attached = {
            'segment_id': str(segment.get('segment_id') or f'A{index:04d}'),
            'start': round(float(start), 3), 'end': round(float(end), 3),
            'text': text, 'avg_logprob': segment.get('avg_logprob'),
            'no_speech_prob': segment.get('no_speech_prob'),
            'eligible_for_narrative': segment.get('eligible_for_narrative') is True,
            'anchor_delta_sec': round(abs(float(anchor['t']) - midpoint), 3),
        }
        anchor['audio_segments'].append(attached)
        report['attached_segments'] += 1
        if attached['eligible_for_narrative']:
            report['eligible_attached'] += 1
    return report


def pov_cross_check(story: dict, ev: list) -> dict:
    """POV 交叉校验 —— 抓「主角性别/身份判定与实际台词主体不符」。

    ★ 实测致命问题：S2 判 POV 为「厨师服男性」（厨师服帧 10 / 哭泣女性帧 3），
    但把「我要退学去打工」（画面里是**女性**）这个核心动作写成了男主的。
    说明：帧数最多的角色不一定是叙事主角，**要按台词主体判定**。

    本函数统计：谁说了最多「第一人称关键台词」→ 才是真主角。
    """
    import re as _re
    # 画面里明确说「我…」的字幕 = 该帧人物在自述
    first = {'男': [], '女': []}
    for e in ev:
        t = e.get('desc', '')
        if '字幕' not in t and '文字' not in t:
            continue
        male = any(k in t for k in ['男性', '男子', '男人', '厨师服'])
        female = any(k in t for k in ['女性', '女子', '女人'])
        # 只统计第一人称自述（我…）
        m = _re.search(r'[“"]([^”"]*我[^”"]*)[”"]', t)
        if not m:
            continue
        if male and not female:
            first['男'].append((e['evidence_id'], m.group(1)))
        elif female and not male:
            first['女'].append((e['evidence_id'], m.group(1)))

    rep = {'male_firstperson': len(first['男']), 'female_firstperson': len(first['女']),
           'male_samples': first['男'][:3], 'female_samples': first['女'][:3]}
    pov_g = (story.get('pov') or {}).get('gender', '')
    if not rep['male_firstperson'] and not rep['female_firstperson']:
        rep['verdict'] = 'unknown'
        rep['why'] = '画面字幕无第一人称自述，无法交叉校验'
        return rep

    speaker = '男' if rep['male_firstperson'] >= rep['female_firstperson'] else '女'
    rep['speaker'] = speaker
    rep['pov_gender'] = pov_g
    if pov_g and speaker != pov_g and rep[f'{speaker}_firstperson'] > 0:
        rep['verdict'] = 'mismatch'
        rep['why'] = (f'POV 判为{pov_g}性，但画面里第一人称自述最多的是{speaker}性'
                      f'（男{rep["male_firstperson"]}条/女{rep["female_firstperson"]}条）—— '
                      f'核心动作的主体可能被安错了人')
    else:
        rep['verdict'] = 'ok'
        rep['why'] = f'POV 与自述主体一致（{speaker}性）'
    return rep


def extract_beats(ev: list, asr_text: str) -> dict:
    """从证据里抽取高信息量镜头候选，供 S3 选择；候选不代表必须写成高潮或反转。"""
    import re as _re
    # 高冲击度帧 = 名场面候选
    ranked = rank_shots(ev)
    shots = [{'evidence_id': r['evidence_id'], 'score': r['score'], 'dim': r['dim'],
              'desc': next((e['desc'] for e in ev if e['evidence_id'] == r['evidence_id']), '')}
             for r in ranked[:6]]

    # 画面字幕里的「目标类」词：可当外部目标的具体物
    anchors = extract_onscreen_text(ev)
    # 实测教训：初版词表只收了「合伙人/股东/合同」这类书面词，
    # 结果漏掉「十几万手术费」这种短剧里最常见的具体目标 —— 空数组。
    # 改为按短剧实际出现的具体物/事来匹配。
    GOAL_HINT = ['合伙人', '股东', '合同', '比赛', '录取', 'offer', '资格', '名额',
                 '手术', '学费', '欠', '账', '婚', '孩子', '职位', '项目',
                 '主厨', '称号', '食客', '高管', '食客', '大堂', '后厨',
                 '金牌', '证书', '辞职', '退学', '打工', '工地', '外卖', '中暑',
                 '车', '包', '酒', '宴', '面试', '体检', '房子', '钱', '奖金']
    goals = [w for w in GOAL_HINT if w in anchors]

    # 反转线索：画面字幕里的转折词
    TWIST_HINT = ['才知', '原来', '没想到', '竟然', '其实是', '早就', '从不', '一直',
                  '反倒', '把柄', '这辈子', '那是']
    twists = [w for w in TWIST_HINT if w in anchors]

    return {'shot_candidates': shots, 'goal_keywords': goals,
            'twist_keywords': twists, 'anchor_text': anchors}


def build_story(asr_text: str, ev: list, retry_hint: str = '') -> dict:
    _anchors = extract_onscreen_text(ev)
    _rk = rank_shots(ev)
    top_shots = chr(10).join(
        f"  {r['evidence_id']} (冲击度 {r['score']}): {r['dim']}" for r in _rk[:5])
    """让 VLM 基于全部证据整理事件顺序和阶段状态。"""
    def format_evidence(row):
        t = row.get('t')
        timing = f" @{float(t):g}s" if isinstance(t, (int, float)) and not isinstance(t, bool) else ''
        parts = [f"[{row['evidence_id']}{timing}] 画面人物：{row.get('frame_who', '')}；"
                 f"画面：{row.get('desc', '')}；可见字幕：{row.get('subtitle_text', '')}"]
        for seg in row.get('audio_segments') or []:
            if seg.get('eligible_for_narrative') is True:
                parts.append(f"  ASR对白 {seg.get('segment_id')} "
                             f"[{float(seg.get('start', 0)):g}-{float(seg.get('end', 0)):g}s]："
                             f"{seg.get('text', '')}（不含说话者判定）")
        return '\n'.join(parts)

    ev_txt = '\n'.join(format_evidence(e) for e in ev)
    dialogue = re.sub(r'<\|[^|]*\|>', '', asr_text)
    dialogue = re.sub(r'(.{0,60}[。！？])', r'\1\n', dialogue).strip()
    has_timed_audio = any(seg.get('eligible_for_narrative') is True
                          for row in ev for seg in (row.get('audio_segments') or []))
    dialogue_block = ('可用的时间戳对白已逐段附在对应 E 证据行中；请引用父级 E ID。'
                      if has_timed_audio else
                      f'无可用时间戳分段；以下整段转写仅供理解，不可单独绑定镜头：\n{dialogue[:2600]}')
    hint = f"\n【上次被核验打回的原因，必须避免】\n{retry_hint}\n" if retry_hint else ""

    anchor_len = len([x for x in _anchors.split("｜") if x.strip()])
    prompt = f"""你是短剧解说编剧。下面是一部短剧的证据材料。

【对白材料】
{dialogue_block}

【时间戳画面与对白证据】
{ev_txt}

【可见字幕 OCR（只证明画面显示了这些字，共 {anchor_len} 条）】
{_anchors}

证据来源边界：画面描述只支持可见事实；OCR 只支持画面显示的文字；合格 ASR 分段只支持对应时段的对白内容，不支持说话者身份或对白中描述的行动已经发生。来源冲突、听不清或没有对应 E ID 时，标未确认并从解说中省略。

{RULES}
{hint}
任务：依据可核验的证据提炼剧情顺序，供一条 60 秒竖屏解说使用。可以标记冲突、选择、结果；证据没有显示的阶段写「片段未显示」或留空，不补齐成完整故事。

关键：每段 summary 只能陈述所引 E ID 中的画面事实或合格 ASR 对白事实；不要把对白说过的计划写成已发生的画面动作。

输出 JSON，格式：
{{
  "genre": "题材类型",
  "pov": {{"who": "主角是谁，用外观描述（如'那个穿厨师服的男人'）", "gender": "男/女",
           "why": "选他做主角的理由（画面多/贯穿全片/冲突集中）"}},
  "logline": "一句话事实梗概（只总结当前证据片段；未显示的后续结果明确写结果未显示）",
   "acts": [
     {{"name":"冲突","summary":"证据直接显示的处境；未显示就写片段未显示","evidence_ids":["E001"]}},
     {{"name":"选择","summary":"证据直接显示的动作/台词；未显示明确选择就写未确认","evidence_ids":["E002"]}},
     {{"name":"结果","summary":"证据直接显示的结果；若片段未结束故事，就明确写结果未显示","evidence_ids":["E003"]}}
   ],
  "timeline_events": [
     {{"event":"按原片时间顺序陈述的一个可核验事件，不写未经证明的因果","evidence_ids":["E001"]}}
   ],
  "key_turns": [{{"text":"转折点描述","evidence_ids":["E004"]}}],
  "usable_hook_material": "哪句台词/哪个画面最有悬念潜力"
}}

★ pov 是必填项。选画面最多、贯穿全片、冲突最集中的那个人作为主角。
其他人物一律通过主角引出，不单独成为叙述对象。

timeline_events 是 S3 的编辑提纲，不是事实来源。按时间顺序输出 3-8 个事件；每个事件引用能支撑它的 E ID。不要强行补齐因果或片段外结果。

★ 已算出「信息量候选帧」（仅供查找，不是事实或高潮标签）：
{top_shots}

★★ 必须区分「证据中的事实」与「分析推断」：
   - 动机、代价、难处若无直接证据，标成未确认，不可写成事实；S3 默认不使用这些推断。
   - 画面动作可以作为事件事实；不要为了避免复述而虚构画面之外的意义或因果。"""

    data = vlm.ask_json(prompt, max_tokens=1800, temperature=0.3)
    if not data:
        return {'error': 'VLM 未返回可解析 JSON', 'evidence': ev}

    # 事件提纲可缺省，但不得自动补造因果链或证据引用。
    data['timeline_events'] = fix_chain_ids(data.get('timeline_events') or [], ev)[:8]
    data['pivotal_events'] = fix_chain_ids(data.get('pivotal_events') or [], ev)[:3]
    data['chain_reorder'] = {
        'reordered': False,
        'reason': '保留证据顺序；不自动重写因果关系',
    }

    data['evidence_count'] = len(ev)
    data['chain_check'] = check_chain(data, ev)
    data['onscreen_grounding'] = check_onscreen_grounding(data, ev)
    data['onscreen_text'] = extract_onscreen_text(ev)   # 供 S3 写稿时约束
    data['beats'] = extract_beats(ev, asr_text)      # 段落任务素材
    data['pov_cross_check'] = pov_cross_check(data, ev)  # POV 与自述主体是否一致

    # 镜头重排序：按冲击度给帧打分，供 S3「最炸的镜头前置」
    ranked = rank_shots(ev)
    data['shot_ranking'] = {
        'method': 'conflict/twist/emotion/info weighted, 位置仅作微弱加权',
        'top_shots': ranked[:8],
        'note': '真实爆款惯例：把最有冲击力的片段提到最前，不按原片顺序',
        'top_evidence_ids': [r['evidence_id'] for r in ranked[:5]],
    }
    return data


def fix_chain_ids(pes: list, ev: list) -> list:
    """修正事件链里的证据 ID —— 8B 模型常写成 E1 而非 E001。"""
    valid = {e['evidence_id'] for e in ev}
    for e in pes:
        ids = []
        for i in (e.get('evidence_ids') or []):
            s = str(i).strip().upper().replace('E-', 'E')
            if not re.fullmatch(r'E\d{1,3}', s):
                continue
            norm = f'E{int(s[1:]):03d}'
            if norm in valid:
                ids.append(norm)
        e['evidence_ids'] = ids
    return pes


def build_chain(asr_text: str, ev: list, story: dict) -> list:
    """独立生成 3 个枢纽事件链。分步问，降低 8B 模型漏字段的概率。"""
    by_id = {e['evidence_id']: e['desc'] for e in ev}
    ev_txt = '\n'.join(f"[{i}] {by_id[i]}" for i in list(by_id)[:24])
    dialogue = re.sub(r'<\|[^|]*\|>', '', asr_text)[:1800]

    ranked0 = rank_shots(ev)
    top0 = chr(10).join(f"  {r['evidence_id']} (impact {r['score']}): {by_id[r['evidence_id']][:46]}"
                        for r in ranked0[:5])

    ranked0 = rank_shots(ev)
    top0 = chr(10).join(f"  {r['evidence_id']} (impact {r['score']}): {by_id[r['evidence_id']][:46]}"
                     for r in ranked0[:5])

    d = vlm.ask_json(f"""一部短剧，已知下面的对白与画面证据。

【对白】
{dialogue}

【画面证据】
{ev_txt}

【已知剧情梗概】{story.get('logline', '')}
【主角】{story.get('pov', {}).get('who', '主角')}

任务：提炼 **3 个枢纽事件**，串成一条「因—果—再因」的因果链。

★★ 叙事顺序 ≠ 时间顺序（真实爆款惯例：最炸的镜头放最前，不按原片顺序）：
   第一个事件必须选**冲击力最强**的那个（冲突最烈/反转最狠/情绪最炸），
   不一定是原片最早发生的那件事。

★ 冲击度 TOP5 帧（第一个事件优先引用这些）：
{top0}

硬要求：
1. 只挑 3 个，不要多。每个用不超过 12 字写成一个动宾短语。
2. cause 写「为什么会发生这件事」，effect 写「这件事又导致了什么」——
   **不要写画面动作，要写动机与后果**。
3. linkage 从这六个里选一个：递进 / 联系点 / 因果 / 反差 / 悬念 / 人物
4. 后一个事件的 cause 必须接得住前一个的 effect。

输出 JSON（严格这个结构）：
{{"pivotal_events":[{{"id":"PE1","event":"动作短语","cause":"为什么","effect":"导致什么","linkage":"因果","evidence_ids":["E001"]}}]}}""",
        max_tokens=900, temperature=0.4)
    if not d or not d.get('pivotal_events'):
        return []
    return fix_chain_ids(d['pivotal_events'], ev)


def rank_shots(ev: list) -> list:
    """给每一帧打「冲击度」—— 用于镜头重排序。

    用户认知一（已实证）：真实爆款「把最有冲击力的片段提到最前」，
    **不按原片顺序**。本函数给出可排序的量化依据。

    维度（各 0-1，加权求和）：
    - 冲突度：文本中是否有对抗/羞辱/撕/逼 等冲突词
    - 反转度：是否含 没想到/竟然/原来/才知 等反转词
    - 情绪强度：是否含 愤怒/崩溃/哭/笑/狠 等情绪词
    - 画面信息量：字幕越长通常信息越强
    - 时间位置惩罚：原片靠后的镜头天然更可能是高潮（弱权重）
    """
    # 冲突：只算「对主角产生实质压力」的动作，剔除纯表情词
    CONFLICT = ['撕', '逼', '抢', '打', '骂', '羞辱', '拒绝', '甩', '跪', '顶替', '背叛',
                '离婚', '刁难', '不配当', '配不上', '辞退', '开除', '侮辱', '逼他', '逼她']
    TWIST = ['没想到', '竟然', '原来', '才知', '居然', '不料', '谁知', '突然', '竟然']
    # 情绪：哭泣/崩溃/绝望等强情绪应显著加权（真实爆款最爱用这类镜头）
    EMOTION = ['崩溃', '哭', '痛哭', '流泪', '绝望', '心碎', '颤抖', '痛哭流涕',
               '笑容', '大笑', '激动', '震惊', '痛苦', '暖', '甜', '狠', '怒']
    EMOTION_WEIGHT = {'崩溃': 3, '痛哭': 3, '流泪': 2.5, '哭': 2, '绝望': 2.5, '心碎': 2.5,
                      '颤抖': 2, '笑容': 1, '大笑': 1.5, '激动': 1.5, '震惊': 2}
    n = len(ev) or 1
    out = []
    for idx, e in enumerate(ev):
        t = e.get('desc', '')
        conflict = min(1.0, sum(t.count(w) for w in CONFLICT) / 3)
        twist = min(1.0, sum(t.count(w) for w in TWIST) / 2)
        emotion = min(1.0, sum(EMOTION_WEIGHT.get(w, 1) * t.count(w) for w in EMOTION) / 3.5)
        # 字幕文字量作为信息量代理
        info = min(1.0, len(t) / 120)
        # 位置：越靠后越可能是高潮，但只作微弱加权
        pos = (idx + 1) / n * 0.15
        score = 0.32 * conflict + 0.28 * twist + 0.2 * emotion + 0.12 * info + pos
        out.append({'evidence_id': e['evidence_id'], 'idx': idx, 'score': round(score, 4),
                    'dim': {'conflict': round(conflict, 2), 'twist': round(twist, 2),
                            'emotion': round(emotion, 2), 'info': round(info, 2)}})
    return sorted(out, key=lambda x: -x['score'])


def reorder_hook_candidates(hooks: list, ranked: list, top: int = 6) -> list:
    """按冲击度给钩子排序 —— 「最炸的镜头配最炸的开场」。

    钩子声明的证据 ID 若命中高冲击度帧，则该钩子优先。
    """
    score = {r['evidence_id']: r['score'] for r in ranked}
    for h in hooks:
        ids = h.get('evidence_ids') or []
        h['impact'] = round(max((score.get(i, 0) for i in ids), default=0), 4)
    return sorted(hooks, key=lambda x: -x.get('impact', 0))[:top]


def reorder_chain(pes: list) -> list:
    """事件链重排 —— 解决「cause/effect 接不上」。

    实测：模型给的链条 pairs_linked=0/2，PE1.effect 是「被妻子羞辱」，
    PE2.cause 是「嫉妒丈夫无学历」，两者无交集 —— 顺序或表述有问题。

    本函数做两件事：
    1. **按因果重排**：找 A.effect 与 B.cause 词面重叠的连接，重排成一条链；
    2. **接不上就重写衔接**：把后一个事件的 cause 改写成能接住前一个 effect 的表述。
    排不动的保持原序并标记，交由人工判断（不硬改）。

    返回 (新链, 重排报告)
    """
    if len(pes) < 2:
        return pes, {'reordered': False, 'reason': 'chain too short'}

    def kw(s):
        s = str(s or '')
        # 去虚词，留实词 2 字以上
        for stop in ('了', '的', '被', '却', '就', '也', '还', '只', '在', '是', '而', '与', '和'):
            s = s.replace(stop, '')
        return {s[k:k + 2] for k in range(max(len(s) - 1, 1))}

    def linkable(a, b, thresh=0.34):
        ka, kb = kw(a.get('effect')), kw(b.get('cause'))
        if not ka or not kb:
            return False, 0.0
        r = len(ka & kb) / min(len(ka), len(kb))
        return r >= thresh, r

    # 贪心重排：每次找当前链尾 effect 能接住的最优后继
    n = len(pes)
    used = [False] * n
    order = [0]
    used[0] = True
    while len(order) < n:
        tail = order[-1]
        best, best_r = None, 0.0
        for j in range(n):
            if used[j]:
                continue
            ok, r = linkable(pes[tail], pes[j])
            if ok and r > best_r:
                best, best_r = j, r
        if best is None:
            rest = [k for k in range(n) if not used[k]]
            order.extend(rest)
            break
        order.append(best)
        used[best] = True

    new = [pes[i] for i in order]
    changed = order != list(range(n))

    # 重排后仍接不上的，重写后一个 cause
    rewrites = []
    for a, b in zip(new, new[1:]):
        ok, r = linkable(a, b)
        if not ok:
            old_c = b.get('cause', '')
            b['cause'] = f"承接「{str(a.get('effect',''))[:14]}」，" + (old_c or '继续往下走')
            b['_rewritten'] = True
            rewrites.append({'event': b.get('event', ''), 'old_cause': old_c,
                             'new_cause': b['cause']})

    return new, {'reordered': changed, 'order': order,
                 'cause_rewrites': len(rewrites), 'rewrites': rewrites}


def check_chain(story: dict, ev: list | None = None) -> dict:
    """核对事件候选是否引用真实证据；不声称已经证明因果链。"""
    pes = story.get('timeline_events') or story.get('pivotal_events') or []
    valid = {row.get('evidence_id') for row in (ev or []) if isinstance(row, dict)}
    r = {'total': len(pes), 'passed': 0, 'failed': 0, 'items': [],
         'causal_chain_verdict': 'not_certified'}
    for i, e in enumerate(pes, 1):
        ids = e.get('evidence_ids') or []
        bad_ids = [eid for eid in ids if valid and eid not in valid]
        why = []
        event_text = e.get('event') or e.get('summary') or e.get('text')
        if not str(event_text or '').strip():
            why.append('缺事件描述')
        if not ids:
            why.append('没有证据引用')
        if bad_ids:
            why.append(f'无效 evidence_id: {bad_ids}')
        ok = not why
        r['items'].append({'id': e.get('id', f'EV{i}'),
                           'event': str(event_text or ''), 'evidence_ids': ids,
                           'ok': ok, 'why': '；'.join(why)})
        r['passed' if ok else 'failed'] += 1
    r['verdict'] = 'grounded_beats' if not r['failed'] else 'weak'
    r['note'] = '该检查只核对事件候选的证据引用，不代表不同事件之间的因果已经证实。'
    return r


def extract_onscreen_text(ev: list) -> str:
    """抽取画面内可见文字（字幕/文字）—— 作为「事实锚点」全集。

    诊断依据：实测 37 帧共 34 条画面字幕，涵盖「十几万手术费」「我要退学去打工」
    「手上的血泡挑了又长」等大量具体事实；而模型叙述里的「撕辞职报告」
    「妻子羞愧低头」**在画面文字中根本不存在** —— 它是从台词推的。

    因此：画面文字 = 可引用的事实边界。叙述超出这个边界即为幻觉。
    """
    import re as _re
    out = []
    for e in ev:
        # 优先用结构化的 subtitle_text，回退到 desc 里的引号内容
        st = (e.get('subtitle_text') or '').strip()
        if st:
            out.append(st)
            continue
        t = e.get('desc', '')
        for m in _re.findall(r'[“"]([^”"]{2,24})[”"]', t):
            out.append(m)
    return ' ｜ '.join(dict.fromkeys(out))


def check_onscreen_grounding(story: dict, ev: list) -> dict:
    """画面事实锚点校验 —— 拦「台词当画面事实」的幻觉。

    判据：叙述中的关键动名词，必须能在画面字幕全集中找到近义依据。
    找不到 → 该叙述是模型从台词/常识补的，不是画面能证明的。
    """
    anchors = extract_onscreen_text(ev)
    if not anchors:
        return {'verdict': 'no_anchor', 'reason': '画面无可读文字，跳过（纯画面片适用）',
                'anchor_count': 0}

    claims = []
    for a in story.get('acts', []):
        claims.append((f"acts/{a.get('name')}", a.get('summary', ''), a.get('evidence_ids', [])))
    for i, k in enumerate(story.get('key_turns', []) or []):
        claims.append((f"key_turns[{i}]", k.get('text', ''), k.get('evidence_ids', [])))

    # 叙述里的关键动名词 —— 这些必须由画面证明，不能靠台词补
    # 只收「屏幕上能看见的实体动作/状态」。
    # 实测教训：不能收「低头」「羞愧」这类词 —— 「不向妻子低头」是态度表达，
    # 画面并没有低头这个动作，误报率极高。
    KEY = ['撕', '摔', '扇', '泼', '推倒', '抢', '砸', '扔', '递', '抱住',
           '跪', '痛哭', '崩溃', '下跪', '握手', '拥抱', '转身',
           '摔门', '摔碎', '撕碎', '撕掉', '辞职', '开除', '婚', '退学', '打工',
           '工地', '外卖', '金牌', '账单', '手术', '证书', '通知书']

    report = {'anchor_count': len(anchors.split('｜')), 'total': len(claims),
              'passed': 0, 'failed': 0, 'items': []}
    for where, text, ids in claims:
        found = [k for k in KEY if k in text]
        missing = [k for k in found if k not in anchors]
        ok = not missing
        report['items'].append({
            'where': where, 'ok': ok, 'ids': ids,
            'key_actions_in_text': found,
            'unsupported': missing,
            'why': '' if ok else f'叙述中的「{"、".join(missing)}」在画面字幕里找不到依据（属台词推演或虚构）',
        })
        report['passed' if ok else 'failed'] += 1

    report['verdict'] = 'grounded' if report['failed'] == 0 else 'ungrounded'
    return report


def verify_binding(story: dict, ev: list) -> dict:
    """核验证据绑定真伪 —— 防止 VLM 编造证据 ID。

    实测教训（两轮）：
    1. VLM 会返回「存在但语义不匹配」的 ID（把"女性捂脸"当"医院走廊痛哭"的证据），
       所以必须校验 ID 确实对应画面内容。
    2. 但**不能用关键词逐字匹配**来校验——叙述是对证据的概括，天然不会逐字出现。
       实测 37 帧时关键词命中率恒为 0%，而证据本身是正确的
       （E001 帧内字幕就是"竞争餐饮集团合伙人"，与叙述完全对应）。

    正解：把「该段叙述 + 所引证据描述」一起交给 VLM 判一致性，人工只看结论。
    词法重叠率仅作参考记录，不作为判定依据。
    """
    by_id = {e['evidence_id']: e['desc'] for e in ev}
    claims = []
    for a in story.get('acts', []):
        claims.append((f"acts/{a.get('name')}", a.get('summary', ''), a.get('evidence_ids', [])))
    for i, k in enumerate(story.get('key_turns', []) or []):
        claims.append((f"key_turns[{i}]", k.get('text', ''), k.get('evidence_ids', [])))

    report = {'total': len(claims), 'passed': 0, 'failed': 0, 'method': 'vlm_cross_check', 'items': []}
    for where, text, ids in claims:
        if not ids:
            report['items'].append({'where': where, 'ok': False, 'why': '未引用任何证据 ID'})
            report['failed'] += 1
            continue
        missing = [i for i in ids if i not in by_id]
        if missing:
            report['items'].append({'where': where, 'ok': False, 'why': f'证据 ID 不存在: {missing}'})
            report['failed'] += 1
            continue

        quoted = '\n'.join(f"[{i}] {by_id[i]}" for i in ids)
        v = vlm.ask(f"""下面是一条剧情叙述，以及它引用的几帧画面描述。

【叙述】{text}

【引用的画面】
{quoted}

问题：这条叙述是否有这些画面作为依据？
- 若画面能支撑叙述的主要事实 → 输出 {{"ok": true, "reason": "支撑点"}}
- 若画面与叙述明显无关或矛盾 → 输出 {{"ok": false, "reason": "矛盾点"}}
只输出 JSON。""", max_tokens=200, temperature=0.1)
        import json as _json
        mm, end = v.find('{'), v.rfind('}')
        verdict = None
        if mm >= 0 and end > mm:
            try:
                verdict = _json.loads(v[mm:end + 1])
            except Exception:
                verdict = None
        ok = bool(verdict and verdict.get('ok'))
        report['items'].append({
            'where': where, 'ok': ok, 'ids': ids,
            'reason': (verdict or {}).get('reason', 'VLM 未返回可解析结论'),
        })
        report['passed' if ok else 'failed'] += 1

    report['verdict'] = 'verified' if report['failed'] == 0 else 'unverified'
    return report


def main() -> int:
    asr_p = TASK / 'asr_full.json'
    if not asr_p.exists():
        print('[ERR] 缺 asr_full.json，先跑 ASR', file=sys.stderr)
        return 1
    asr_data = json.loads(asr_p.read_text(encoding='utf-8'))
    asr_text = asr_data.get('text', '')
    asr_segments = asr_data.get('segments') or []

    print('[S2] 读帧描述...', flush=True)
    time_file = TASK / 'frame_times.json'
    frame_times = {}
    if time_file.exists():
        try:
            index = json.loads(time_file.read_text(encoding='utf-8'))
            ep = index.get('episode')
            rows = index.get('frames')
            if isinstance(ep, bool) or not isinstance(ep, int) or ep < 1 or not isinstance(rows, list):
                raise ValueError('frame_times.json requires positive episode and frames array')
            for row in rows:
                name, t = row.get('frame'), row.get('t')
                if (not isinstance(name, str) or isinstance(t, bool)
                        or not isinstance(t, (int, float)) or not math.isfinite(float(t)) or t < 0):
                    raise ValueError(f'invalid timestamp row: {row!r}')
                if name in frame_times:
                    raise ValueError(f'duplicate frame timestamp: {name}')
                frame_times[name] = (ep, float(t))
            actual = {p.name for p in (TASK / 'frames').glob('*.jpg')}
            missing = sorted(actual - set(frame_times))
            if missing:
                raise ValueError(f'frames missing timestamp metadata: {missing[:5]}')
        except Exception as e:
            print(f'[ERR] 无效的 frame_times.json: {e}', file=sys.stderr)
            return 2
    else:
        print('[WARN] 无 frame_times.json；本次证据不会包含时间戳，不能自动生成剪辑计划', flush=True)
    ev = frames_desc(TASK / 'frames', every=1, frame_times=frame_times)
    audio_binding = attach_audio_segments(ev, asr_segments)
    (TASK / 'evidence.json').write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding='utf-8')

    print('[S2] 证据驱动剧情解构...', flush=True)
    story = build_story(asr_text, ev)

    # S2 的自由文本只提供编辑参考。VLM 自检可能把“字幕提到”误判为
    # “角色说了/事情发生了”，因此不再把它标成事实已核验，也不据此返工重写。
    if not isinstance(story, dict) or story.get('error'):
        story = {
            'genre': '未分类', 'pov': {'who': '', 'gender': '', 'why': 'S2 摘要不可用'},
            'logline': '', 'acts': [], 'key_turns': [],
            'story_status': 'advisory_unavailable',
        }
    else:
        story['story_status'] = 'advisory_only'
    story['schema_version'] = 2
    story['fact_authority'] = 'evidence.json'
    story['audio_binding'] = audio_binding
    story['timeline_event_count'] = len(story.get('timeline_events') or [])
    story['binding_check'] = {
        'verdict': 'not_authoritative', 'method': 'disabled_vlm_self_verification',
        'note': 'S2 自由文本不作为事实门；逐句事实核验在 S3 对引用证据集合执行。',
    }

    story['evidence_count'] = len(ev)
    story['asr_chars'] = len(re.sub(r'<\|[^|]*\|>', '', asr_text))
    (TASK / 'story.json').write_text(json.dumps(story, ensure_ascii=False, indent=2), encoding='utf-8')

    print(json.dumps({k: v for k, v in story.items() if k != 'evidence'},
                     ensure_ascii=False, indent=2)[:2000])
    og = story.get('onscreen_grounding', {})
    print('[S2] 画面锚点:', og.get('verdict'), og.get('passed', 0), '/', og.get('total', 0),
          '| 画面字幕条数', og.get('anchor_count', 0))
    for _it in og.get('items', []):
        if isinstance(_it, dict) and _it.get('ok') is False:
            print('   !', _it.get('where', '?'), str(_it.get('why', ''))[:70])
    bc = story['binding_check']
    print(f"\n[S2] 事实权限状态: {bc['verdict']}（{bc['method']}）；{bc.get('note', '')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
