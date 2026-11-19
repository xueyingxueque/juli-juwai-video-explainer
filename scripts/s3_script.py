"""S3 解说成稿 —— 载入六流人格契约 + 三段式骨架 + 钩子候选。

人格正典在 ~/.workbuddy/skills/短剧解说*流灵魂/SKILL.md，本脚本只做**指针引用**，
不复制实现、不自创腔调描述（引擎 R14/R16：单一正典）。

硬约束：
1. 每句的 evidence_ids 覆盖句内可核验事实；primary_evidence_id 是其中一个剪辑主锚点；无证据的剧情句不允许存在。
2. 推断句必须显式标（推测）。
3. 钩子 <=20 字且含具体信息。
4. 字数按实测语速 4.5-6.0 字/秒反推（E4 铁律：最终时长由音频决定，稿子只是预算）。
5. 禁词零命中。
"""
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vlm  # noqa: E402

# 匹配中文/英文引号包裹的引文 —— 判断"叙述人称"时必须先剥离，引文里的"我"是角色台词
_QUOTED_RE = re.compile(r'「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’|"[^"]*"')

ROOT = Path(__file__).resolve().parent.parent
TASK = Path(sys.argv[1] if len(sys.argv) > 1 else '.')
TARGET_SEC = int(sys.argv[2]) if len(sys.argv) > 2 else 60
FORCE_SOUL = sys.argv[3] if len(sys.argv) > 3 else 'auto'
DIRECTION = sys.argv[4] if len(sys.argv) > 4 else ''


LAST_JSON_ERROR = None
LAST_JSON_PREVIEW = None


def ask_json_once(prompt: str, *, max_tokens: int, temperature: float,
                  timeout: int = 300, default=None,
                  frequency_penalty: float = 0.0, presence_penalty: float = 0.0):
    """一次本地推理并解析 JSON；长生成的重试统一由 S3 外层负责。"""
    global LAST_JSON_ERROR, LAST_JSON_PREVIEW
    LAST_JSON_ERROR = None
    LAST_JSON_PREVIEW = None
    try:
        raw = vlm.ask(prompt + '\n只输出合法 JSON，不要任何解释文字。',
                      max_tokens=max_tokens, temperature=temperature,
                      retries=0, timeout=timeout,
                      frequency_penalty=frequency_penalty,
                      presence_penalty=presence_penalty)
    except Exception as e:
        LAST_JSON_ERROR = f'{type(e).__name__}: {str(e)[:180]}'
        return default
    text = raw.strip()
    if text.startswith('```'):
        chunks = text.split('```')
        text = chunks[1] if len(chunks) > 1 else text
        if text.lstrip().startswith('json'):
            text = text.lstrip()[4:]
    text = text.strip().rstrip('，。')
    start = text.find('{')
    if start < 0:
        LAST_JSON_ERROR = 'invalid_json: no JSON object in response'
        LAST_JSON_PREVIEW = text[:180]
        return default
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
        if isinstance(parsed, dict):
            return parsed
        LAST_JSON_ERROR = 'invalid_json: expected JSON object'
        LAST_JSON_PREVIEW = text[start:start + 180]
        return default
    except json.JSONDecodeError as first_error:
        # Repair only a common safe formatting defect (trailing commas). Never
        # invent missing brackets or fields; truncated output still fails closed.
        candidate = text[start:]
        repaired = re.sub(r',\s*([}\]])', r'\1', candidate)
        try:
            parsed, _ = json.JSONDecoder().raw_decode(repaired)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as e:
            LAST_JSON_ERROR = f'invalid_json: JSONDecodeError:{e.msg[:60]} at {e.pos}'
            LAST_JSON_PREVIEW = repaired[max(0, e.pos - 70):e.pos + 110]
            return default
        LAST_JSON_ERROR = f'invalid_json: {type(first_error).__name__}'
        LAST_JSON_PREVIEW = candidate[:180]
        return default
    except TypeError as e:
        LAST_JSON_ERROR = f'invalid_json: {type(e).__name__}'
        LAST_JSON_PREVIEW = text[start:start + 180]
        return default

# ── 人格契约（正典指针，勿改腔调描述）────────────────────────
SOULS = {
    '悬念流': {
        'skill': '短剧解说悬念流灵魂',
        'persona': '用「我」直接说话，快狠勾人腔，抖音爆款解说风格',
        'rule': '每句至少留一个"想知道"，句间有悬念',
        'opening': '经典开头：注意看…／你敢信吗？／太气人了！',
        'tempo': '快', 'check': ['每句是否留了想知道', '句间是否有悬念', '是否有经典开头套路'],
    },
    '毒舌流': {
        'skill': '短剧解说毒舌流灵魂',
        'persona': '用「我」直接说话，犀利辛辣腔',
        'rule': '一针见血、站队感强、金句频出、骂得有水平',
        'opening': '',
        'tempo': '中速', 'check': ['有没有一针见血的判断', '是否站队明确', '金句够不够密'],
    },
    '吐槽流': {
        'skill': '短剧解说吐槽流灵魂',
        'persona': '用「我」直接说话，嘴快梗密腔',
        'rule': '每3句至少1个吐槽点，节奏快、有态度',
        'opening': '',
        'tempo': '快', 'check': ['每3句有吐槽点吗', '梗密吗', '态度明确吗'],
    },
    '感动流': {
        'skill': '短剧解说感动流灵魂',
        'persona': '用「我」直接说话，温柔细腻腔',
        'rule': '语速慢、有共情、细节放大、催泪但不刻意',
        'opening': '',
        'tempo': '慢', 'check': ['有细节放大吗（小动作胜过千言万语）', '观众能在角色里看到自己吗', '给情绪发酵时间了吗'],
    },
    '治愈流': {
        'skill': '短剧解说治愈流灵魂',
        'persona': '用「我」直接说话，温暖慢热腔',
        'rule': '语速慢、有留白、反高潮、温暖人心、反向情绪价值',
        'opening': '',
        'tempo': '慢热', 'check': ['不鸡汤不说教吗', '有留白吗', '是反高潮还是平铺直叙'],
    },
}

# 语气标注：真实爆款全篇标注，供 TTS 配音使用（engine_status: v2.2 已落地）
TONE_MARKS = {
    'exaggerated': '[语气夸张]', 'teasing': '[调侃地]', 'laugh': '[笑]',
    'tense': '[紧张地]', 'faster': '[语速加快]', 'slower': '[语速放缓]',
    'pause': '[停顿]', 'mocking': '[吐槽]', 'resonant': '[共鸣地]',
    'deep': '[语气转为深沉]', 'somber': '[节奏放缓]',
    'excited': '[高潮部分，语气激动]', 'warm': '[语气升温]',
}
# 段落角色 → 语气（对应真实稿的 10 段结构）
BLOCK_TONE = {
    'hook': 'exaggerated', 'setup': 'teasing', 'rising': 'faster',
    'twist': 'tense', 'core': 'slower', 'climax': 'excited',
    'ending': 'somber', 'aftertaste': 'deep',
}

TONE_RULES = """
原片证据是事实唯一来源。姓名、身份、人物关系、动机、因果、动作结果没有直接依据就删掉。
字幕中的打算或承诺只能写成“字幕/人物这样说”，不能当作已经发生的结果。
风格只影响句式节奏，不授权增加事实；不要写与上下文冲突的否定句。
"""

# ★ 视角定死铁律（来源：影视解说文案剪辑课 + 爆款解说拆解，见 references/narrative-craft.md）
POV_RULE = """
【叙述与证据】
- 不把 S2 的 POV 猜测当事实；每句按自己引用的证据确认人物与动作，不预设全片主角。
- 人名、昵称、亲属关系只在字幕或其他证据明确确认时使用；否则沿用画面描述中的称呼。
- 按时间戳组织素材。后段钩子可以倒叙，但之后要清楚回到较早镜头；其余顺序按原片推进。
- 先写清可见动作、原字幕和镜头变化。只有证据明确支持时才写因果、转折、结果、动机或人物内心。
- 不强求每句有悬念、反差、吐槽或连接词；不得用比喻、夸张和风格要求添加新事实。
- 口语感来自自然短句和节奏变化，不靠虚构情节。没拍到的结果就保留未完状态。
- hook、正文段落和 CTA 保持各自功能；高潮、反转、余味只在素材确实出现时使用。
"""

HOOK_FORMULAS = """五条分别从不同角度取材，不照搬固定句式：画面动作、单帧内冲突、字幕短引句、具体物件、人物反应。
每条只表达一个证据行可直接支持的事实；不跨镜头拼接，不根据故事摘要补写目标、结果、时间或动机。
如果 subtitle_text 的 speaker 是画外音/未知，必须写“字幕写着/字幕里出现”，不可写“字幕说”，也不可归到画中人物嘴里。
只写陈述句；悬念来自证据中的具体细节，不得承诺该帧之外的结果。"""


def name_and_tone(story: dict) -> tuple:
    """不造角色代号，只保留 TTS 可用的语气词表。"""
    return ('', '', ' / '.join(sorted(set(TONE_MARKS.values()))))


def pick_soul(story: dict, cfg_genre: str) -> str:
    """题材 → 流派映射。正典见 SKILL.md §4。"""
    if FORCE_SOUL in SOULS:
        return FORCE_SOUL
    # Story acts are advisory only; style selection reads the coarse genre label.
    if any(k in cfg_genre for k in ('轻喜剧', '喜剧', '搞笑', '幽默')):
        return '吐槽流'
    g = cfg_genre
    if any(k in g for k in ['逆袭', '打脸', '身份', '反转', '悬疑', '真相', '推理']):
        return '悬念流'
    if any(k in g for k in ['伦理', '婆媳', '憋屈', '的现实']):
        return '毒舌流'
    if any(k in g for k in ['甜宠', '霸总', '误会', '恋爱']):
        return '吐槽流'
    if any(k in g for k in ['亲情', '离散', '牺牲', '母亲', '父亲', '病']):
        return '感动流'
    if any(k in g for k in ['治愈', '乡村', '慢生活', '田园']):
        return '治愈流'
    return '悬念流'


def build_hooks(story: dict, ev: list, soul: dict, pov_desc: str, n: int = 5) -> list:
    gender = ''
    """生成 n 个钩子候选，各用不同公式。"""
    by_id = {e['evidence_id']: e for e in ev}
    ev_ids = [row.get('evidence_id') for row in ev if row.get('evidence_id')]
    ev_txt = '\n'.join(
        f"[{i} @ {by_id.get(i, {}).get('t', '?')}秒] {by_id.get(i, {}).get('frame_who', '')}｜"
        f"字幕：{by_id.get(i, {}).get('subtitle_text', '')}（说话者：{by_id.get(i, {}).get('speaker', '未知')}）｜"
        f"画面：{by_id.get(i, {}).get('desc', '')}"
        for i in ev_ids[:20])

    out = []
    for attempt in range(2):
        retry_rule = ('上次候选全部超过 20 字。本次必须重新压缩，逐字计数；每条 8-20 个汉字，'
                      '删除背景铺垫，只保留一个具体动作/冲突/悬念。') if attempt else ''
        data = ask_json_once(f"""一部{TARGET_SEC}秒竖屏短剧解说，要 {n} 个开场钩子候选。
主角性别：{gender or '未确认'}；不得生成未经证据确认的人名、昵称或亲属关系。
【可用画面证据】
{ev_txt}

{HOOK_FORMULAS}
【叙事焦点】{pov_desc}
{POV_RULE}

{retry_rule}
每个钩子必须：8-20字、句子中每项具体事实都能从它唯一引用的证据行直接核实；五条互不重复。evidence_ids 恰好包含一个 ID。
输出 JSON：{{"hooks":[{{"formula":"角度名","text":"钩子","evidence_ids":["E001"]}}]}}""",
        max_tokens=700, temperature=0.3)
        if not data or not data.get('hooks'):
            continue
        out = []
        for h in data['hooks'][:n]:
            t = (h.get('text') or '').strip().replace('字幕说', '字幕写着')
            if not t:
                continue
            out.append({
                'formula': h.get('formula', ''), 'text': t, 'chars': len(t),
                'evidence_ids': h.get('evidence_ids', []),
                'gate': 'OK' if len(t) <= 20 and len(t) >= 8 else 'BAD_LEN',
            })
        if any(h['gate'] == 'OK' for h in out):
            break
    return out


def evidence_hook_fallback(story: dict, ev: list, pov_name: str) -> dict | None:
    """模型钩子均不可信时，从主角在场的单帧字幕生成可核验的保底开头。"""
    prefix, suffix = (f'{pov_name}，字幕写着“' if pov_name else '画面字幕写着“'), '”'
    max_caption = 20 - len(prefix) - len(suffix)
    rows = sorted(ev, key=lambda row: row.get('t', float('inf'))
                  if isinstance(row.get('t'), (int, float)) else float('inf'))
    for row in rows:
        caption = (row.get('subtitle_text') or '').strip().strip('“”\" ')
        if not caption:
            continue
        caption = re.split(r'[，,。；;！？!?]', caption, maxsplit=1)[0].strip()
        if not caption:
            continue
        if len(caption) > max_caption:
            caption = caption[:max_caption - 1] + '…'
        text = prefix + caption + suffix
        if 8 <= len(text) <= 20:
            return {'formula': '单帧字幕保底', 'text': text, 'chars': len(text),
                    'evidence_ids': [row.get('evidence_id')], 'gate': 'OK',
                    'source': 'generated_from_timestamped_subtitle'}
    return None


def select_evidence_schedule(story: dict, ev: list, count: int, first_id: str = '') -> list[str]:
    """Choose distributed primary shots from the timestamped evidence, not S2 prose."""
    by_id = {row.get('evidence_id'): row for row in ev if isinstance(row, dict)}
    chronological = sorted(
        (row for row in ev if isinstance(row, dict)
         and row.get('evidence_id') in by_id
         and isinstance(row.get('t'), (int, float))
         and not isinstance(row.get('t'), bool)),
        key=lambda row: float(row['t']))
    first_id = first_id if first_id in by_id else ''
    body = [row for row in chronological if row.get('evidence_id') != first_id]
    need = max(0, count - bool(first_id))
    if need >= len(body):
        selected_body = body
    elif need <= 0:
        selected_body = []
    else:
        # Evenly cover the available timeline so the script does not inherit
        # arbitrary acts/shot rankings from the advisory S2 story summary.
        indices = sorted({round(i * (len(body) - 1) / max(need - 1, 1))
                          for i in range(need)})
        selected_body = [body[i] for i in indices[:need]]
    selected = ([first_id] if first_id else []) + [row['evidence_id'] for row in selected_body]
    if len(selected) < min(6, count):
        raise ValueError(f'逐句剪辑至少需要 {min(6, count)} 个唯一时间证据，当前仅有 {len(selected)} 个')
    return selected


def format_audio_segments(row: dict) -> str:
    parts = []
    for segment in row.get('audio_segments', []):
        if not isinstance(segment, dict) or not segment.get('eligible_for_narrative'):
            continue
        parts.append('{}-{}秒 {}'.format(
            segment.get('start', '?'), segment.get('end', '?'), segment.get('text', '')))
    return '；'.join(parts)


def evidence_speaker(row: dict) -> str:
    """Only expose diarized speaker labels; frame/VLM guesses are not attribution evidence."""
    if row.get('speaker_source') != 'audio_diarization':
        return '未知'
    return str(row.get('speaker') or '未知')


def write_script(story: dict, ev: list, soul_name: str, hook: dict, budget: int,
                 pov_desc: str, evidence_schedule: list[str],
                 previous_lines: list | None = None, line_count: int | None = None,
                 min_line_chars: int | None = None, max_line_chars: int | None = None,
                 caption_capacity: int = 36) -> dict:
    _, _, tone_vocab = name_and_tone(story)
    gender = (story.get('pov') or {}).get('gender', '')
    hook_evidence_id = (hook.get('evidence_ids') or [''])[0]
    by_id = {e['evidence_id']: e['desc'] for e in ev}
    outline_txt = json.dumps([
        {'event': row.get('event', ''), 'evidence_ids': row.get('evidence_ids') or []}
        for row in story.get('timeline_events', []) if isinstance(row, dict)
    ], ensure_ascii=False)
    ev_txt = '\n'.join(
        f"[{e.get('evidence_id')} @ {e.get('t', '?')}秒] 画面人物：{e.get('frame_who', '')}｜"
        f"画面描述：{e.get('desc', '')}｜字幕：{e.get('subtitle_text', '')}｜"
        f"说话者：{evidence_speaker(e)}｜"
        f"带时间戳对白：{format_audio_segments(e)}"
        for e in ev[:24])
    scheduled_ids = list(dict.fromkeys(evidence_schedule))
    row_by_id = {row.get('evidence_id'): row for row in ev if isinstance(row, dict)}
    coverage_txt = '、'.join(
        f"{eid}@{float(row_by_id[eid].get('t')):g}s"
        for eid in scheduled_ids
        if eid in row_by_id and isinstance(row_by_id[eid].get('t'), (int, float)))
    line_count = line_count or max(8, round(budget / 24))
    min_line_chars = min_line_chars or max(8, math.floor((TARGET_SEC * 5.25) / line_count))
    max_line_chars = max_line_chars or min(36, math.ceil((TARGET_SEC * 6.0) / line_count))
    lo, hi = TARGET_SEC * 4.5, TARGET_SEC * 6.0
    previous_txt = (json.dumps([
                        {'line': i, 'text': line.get('text', ''),
                         'primary_evidence_id': line.get('primary_evidence_id', ''),
                         'evidence_ids': line.get('evidence_ids') or [],
                         'block': line.get('block', '')}
                        for i, line in enumerate(previous_lines, 1)], ensure_ascii=False)
                    if previous_lines else '无上一轮草稿；这是首稿。')

    data = ask_json_once(f"""写一条 {TARGET_SEC} 秒竖屏短剧解说的口播稿。

【表达风格】{soul_name}（只影响口语节奏；不得为了悬念、吐槽或情绪加写事实）
【开头】必须用这个钩子：{hook['text']}
【钩子证据】第 1 句必须引用 {hook_evidence_id}；该 ID 已通过单帧事实核验，不得换成其他镜头。
【字数预算】全文目标 {TARGET_SEC * 4.5:.0f}-{TARGET_SEC * 6.0:.0f} 字，中心约 {budget} 字；剧情句按 {min_line_chars}-{max_line_chars} 字分配，句数已按有效事件量和字幕容量计算。不能把短句堆成逐帧复述，也不能用重复回顾填满字数。
【分句硬约束】输出恰好 {line_count} 行；不得靠减少句数写出低于字数预算的短稿。全文预算 {lo:.0f}-{hi:.0f} 字是硬要求。剧情句每行约 {min_line_chars}-{max_line_chars} 字是平均节奏参考，单句硬上限为 {caption_capacity} 字（平台字幕容量）；首句 hook 不超过 20 字，末尾 CTA 可短于下限。一行只讲一个信息推进，不堆多个动作、字幕和转折。必须使用 JSON 字段 block 标记句子任务；CTA 恰好一行且放最后；aftertaste 若有，必须引用能直接支撑该收束句的已有证据，并在剧情结果/高潮之后作为一个连续收束段、紧接 CTA；没有直接证据时省略 aftertaste，不得写无证据感悟。较长稿件可让一个叙事任务跨多个短句，但每句必须推进新信息，不得复制填充。
【配音连贯度】每个句对象会单独合成配音。优先把同一主锚点、同一推进任务下可连读的短句写成完整口播；当证据或镜头边界要求短句时允许保留，不得跨无关镜头合并或补写证据外内容。短句比例用于诊断，不得为了变长而改动事实。
【逐句证据约束】除 CTA 外，包括 hook、aftertaste 在内的每个句子都必须在 evidence_ids 列出直接支持句中事实/画面收束的已有证据，primary_evidence_id 必须是其中最能代表该句的剪辑主锚点；无证据的剧情句不允许存在。aftertaste 可以复用确实支持其收束内容的已有证据；若没有合适证据，就删除该段，不得留空引用或补写推断。主镜头只决定剪辑定位，不代表它单独支持整句。只在句子确实概括多个画面/字幕事实时才加多个证据，不可借此拼出无证据的因果。CTA 不引用事实，evidence_ids 用 []、primary_evidence_id 用空字符串。剧情句至少有 6 个不同主镜头；任一主镜头最多对应 2 句；主镜头按原片时间顺序覆盖素材，不要求按预设 ID 逐行映射。
【字幕精确引用】若本句证据的 speaker 为未知/画外，绝不能写“她/他/某人说、问、回答”；逐字引用必须写“画面字幕写着‘原文’”且引号内容能在本句所引字幕中逐字找到。不确定原字时不可加引号；如需转述字幕，必须标成“画面字幕大意是”，且语义要受该字幕直接支持。不得把口音、近义改写或前后缀伪装成引文。
【S2 事件提纲（仅作导航）】只帮助理解时间顺序；提纲本身不是证据。每个事实必须由本句引用的逐帧/对白证据直接支持：
{outline_txt}
【时间线覆盖参考】以下是按原片时间分布的候选镜头，只用于选择真实支撑句子的证据与剪辑锚点，不是逐行分配清单；不能为了覆盖时间点把无关 ID 填入事实依据：
{coverage_txt}
【衔接】剧情句按证据时间推进；可通过重复出现的人物、物件、原话或自然时间顺序承接。不要为了满足连接词比例而添加“但/所以/没想到”等关系；因果、反转只在证据明确时写。若钩子来自后段，第二句回到较早镜头即可，不得把先后关系说成因果。
【逐句事实核验硬约束】只用该句所引证据行的画面人物、画面描述、字幕/对白。ASR对白只支持对应时间段听到的文字，不支持说话者身份、画面动作或未说出的事实；说话人未做音频分离确认时，使用“原片对白提到”，不得归给画中人物。静帧不能证明未显示的连续动作；未显示的物品、数量、人物身份/关系、动机、因果或结果不得写成事实。字幕支持文字内容，但不自动证明说话者身份或内容已经发生。纯感受若无法直接核验就删除，不能用“像是/仿佛”掩护具体新事实。
【推断专项】动机、心理、因果、比喻或人物关系若证据不能直接证明，一律删除；不要用“也许/像是/仿佛”绕过事实核验。
【主体一致硬约束】主角动作要与对应画面人物一致；「他人」「其他」是泛指，不代表男性角色。

【带时间戳的逐帧/对白事实证据】逐句引用只能选其中 ID。对白文本只支持转写内容，不带说话人身份；视觉描述只支持该帧可见事实。
{ev_txt}

【主角 POV】{pov_desc}
{POV_RULE}
{TONE_RULES}

【上一轮草稿】
{previous_txt}
如果这是返修：优先保留没有被反馈指出问题的句子；只改有问题的行。绝不能复制同一句填满多行。若句子事实与证据冲突，改写句子或换成真正支撑它的证据 ID，不要自动照建议表换号。

【主人指定方向】{DIRECTION or '无额外方向；按证据时间与可确认事件自主写作。'}
方向只能影响叙述重点和表达，不能补造画面或剧情事实。

要求：
- 第一句必须就是上面的钩子。
- 以证据中明确的人物和事件保持叙述焦点；不必每句硬塞同一个主角或亲属关系。
- 按证据能确认的时间/事件推进；只有证据支持时才写「冲突、选择、结果」。没有明确结果就如实说明片段停在未完处。
- **写成一个人在说话，不是写大纲**。禁止把每句写成"X？Y？"这种罗列式短语。
- 问句只允许放在最后一行 CTA，且最多一句；其余用陈述句制造悬念。
- 必须包含 hook、至少两类不同的正文推进任务（setup/rising/core/climax/ending 等）和 cta；CTA 是互动提问或评论引导，必须为最后一行。climax2、twist、aftertaste 仅在证据确实支持时使用，不为结构凑戏。aftertaste 若使用，只能在最后一个高潮/结果段之后连续出现，并作为 CTA 前最后一个剧情段。
- 每行必须使用且仅使用以下 block 之一：hook、setup、setup2、rising、core、climax、climax2、twist、ending、aftertaste、cta。
- 各句推进不同信息；不得复制上一句或重复同一事实。
{hook.get('retry_hint','')}
输出 JSON（每行必须有 block 与 tone）：
剧情句的 evidence_ids 覆盖句内全部可核验事实；primary_evidence_id 是其中一个最能代表该句的主镜头。主镜头按原片时间推进且至少覆盖 6 个不同时间点。CTA 用空 evidence_ids 和空 primary_evidence_id。
返回体必须是一个 JSON 对象，唯一顶层键为 `lines`，且 `lines` 数组恰好包含 {line_count} 个完整句对象；只返回单句对象、`text` 顶层对象、代码块或说明文字都视为错误。
{{"lines":[{{"text":"剧情口播句","evidence_ids":["E001"],"primary_evidence_id":"E001","inference":false,"block":"hook|setup|setup2|rising|core|climax|climax2|twist|ending|aftertaste","tone":"tense"}},{{"text":"你觉得她接下来会遇到什么？评论区聊聊。","evidence_ids":[],"primary_evidence_id":"","inference":false,"block":"cta","tone":"deep"}}]}}""",
        max_tokens=2400, temperature=0.4 if hook.get('retry_hint') else 0.3,
        frequency_penalty=0.6, presence_penalty=0.3)
    return data or {}


def revise_script(previous_lines: list, ev: list, soul_name: str, hook: dict,
                  pov_desc: str, evidence_schedule: list[str], budget: int,
                  line_count: int, min_line_chars: int, max_line_chars: int,
                  retry_hint: str, caption_capacity: int = 36) -> dict:
    """Focused repair: keep the accepted draft context and ask only for a complete corrected lines array."""
    used_ids = {eid for line in previous_lines for eid in (line.get('evidence_ids') or [])}
    requested_ids = set(evidence_schedule) | used_ids
    evidence_rows = [
        {'evidence_id': row.get('evidence_id'), 't': row.get('t'),
         'frame_who': row.get('frame_who', ''), 'desc': row.get('desc', ''),
         'subtitle_text': row.get('subtitle_text', ''),
         'speaker': evidence_speaker(row),
         'audio_segments': [
             {key: segment.get(key) for key in ('segment_id', 'start', 'end', 'text',
                                                'avg_logprob', 'no_speech_prob')}
             for segment in row.get('audio_segments', [])
             if isinstance(segment, dict) and segment.get('eligible_for_narrative')
         ]}
        for row in ev if row.get('evidence_id') in requested_ids
    ]
    schedule_text = '、'.join(evidence_schedule)
    lo, hi = TARGET_SEC * 4.5, TARGET_SEC * 6.0
    prompt = f"""你正在修订一份本地短剧解说稿。保留可用内容，只解决下方失败项；但输出必须是完整稿，不能只返回单句。

【目标】{TARGET_SEC}秒；全文 {lo:.0f}-{hi:.0f} 字，中心约 {budget} 字；必须输出恰好 {line_count} 句，不得通过减少句数写出短稿。剧情句约 {min_line_chars}-{max_line_chars} 字是平均节奏参考，单句硬上限为 {caption_capacity} 字（平台字幕容量）；CTA 可短于下限。首句必须逐字采用钩子：{hook.get('text','')}。
【修订保稿与字数】不要把全文压缩成剧情梗概，也不要删掉整句来逃避事实核验；只修改失败句中未被证据支持的部分，保留其余可用信息和原句结构。删除无依据对白、说话人归属、心理或因果后，优先用同一句已引用证据里可确认的人物、动作、物件、位置或字幕信息替换。修订稿仍必须达到 {lo:.0f}-{hi:.0f} 字；不足时只从已引用证据补充可核验细节，不能编造或用重复句凑字数。
【配音连贯度】每个句对象会单独合成配音。优先把同一主锚点、同一推进任务下可连读的短句写成完整口播；当证据或镜头边界要求短句时允许保留，不得跨无关镜头合并或补写证据外内容。短句比例用于诊断，不得为了变长而改动事实。
【修订反馈】{retry_hint or '按下面规则完整校对当前稿。'}
【事实与剪辑规则】每句 evidence_ids 列出直接支持句内事实的一个或多个证据；primary_evidence_id 必须是本句 evidence_ids 中最能代表画面的一个主镜头。主镜头用于剪辑，不能替代其他事实依据。主镜头时间整体顺序覆盖原片；至少用 6 个不同主镜头，任一主镜头最多对应 2 句；不要为覆盖时间而引用无关证据。CTA 不引用事实，evidence_ids=[]、primary_evidence_id=""。说话人未由音频分离确认时，不得写“她/他说、问、回答”，应使用“原片对白提到”并逐字匹配引用证据中的 ASR；字幕逐字引用须匹配原文，转述须标成“画面字幕大意是”，且只能表达字幕直接支持的含义。ASR只证明对应时间段的转写文字，不证明说话者身份或画面动作。字幕/对白中的计划、愿望或承诺不能写成已发生结果。没有直接依据的姓名、亲属关系、连续动作、物品、动机、因果或结果必须删除。
【时间线覆盖参考】以下 ID 是按原片时间挑出的候选，不是逐行映射；只选择直接支持句子的 ID：{schedule_text}
【结构规则】保持唯一 hook、至少两类不同的剧情正文任务和唯一末尾 CTA。高光、反转、余味仅在证据支持时使用；aftertaste 若使用，必须作为有证据引用的剧情句，在剧情结果/高潮之后形成一个连续收束段并紧接 CTA；没有支持它的证据就省略。CTA 是唯一允许 evidence_ids=[] 且 primary_evidence_id="" 的句子。剧情句按时间戳推进；人物、物件或原话可自然承接，不要凑连接词，更不能补造因果。风格只影响口语节奏。
【视角】{pov_desc}
【风格】{soul_name}（只影响口语节奏，不添加悬念、吐槽或情绪事实）
【原钩子】{hook.get('text','')}
【按时间排序的可用证据】
{json.dumps(evidence_rows, ensure_ascii=False)}
【当前完整稿】
{json.dumps(previous_lines, ensure_ascii=False)}

只输出合法 JSON 对象，唯一顶层键是 `lines`。`lines` 必须恰好包含 {line_count} 个句对象，每个对象字段为 text、evidence_ids、primary_evidence_id、inference、block、tone。单句对象或缺 `lines` 一律错误。
示例结构：{{"lines":[{{"text":"口播句","evidence_ids":["E001"],"primary_evidence_id":"E001","inference":false,"block":"hook","tone":"tense"}}]}}"""
    return ask_json_once(prompt, max_tokens=2400, temperature=0.2,
                          frequency_penalty=0.6, presence_penalty=0.3)


Q = '？?'


GENDER_HINT = {
    '她': ['女性', '女子', '女人', '女孩', '女', '母', '妻', '她'],
    '他': ['男性', '男子', '男人', '男孩', '男', '父', '丈夫', '他'],
}


def check_gender(text: str, ids: list, by_id: dict, pov_gender: str = '',
                 pov_name: str = '') -> dict:
    """人物一致性校验 —— 以 POV 为准（视角定死铁律）。

    ★★★ 判据修正（2026-10-15 实测发现旧判据完全反了）

    旧判据：「句中是否出现配角人称」——
        who = '他' if ('他' in text and '她' not in text) else '她'
    它假设「一句里只能有一种人称」，于是：
      - 「他哄她」被判成 who='她' → FAIL   ❌ 误拦（主角作主语是正确写法）
      - 「妻子嫌他学历低」who='他' → PASS   ❌ 放过（配角作主语才是真问题）

    新判据：**句首主语是谁**（中文解说的主语基本落在句首）
      - 第一人称「我」自述 → PASS
      - 句首是配角代词/称呼 → FAIL（叙述重心真偏了）
      - 其余（主角起句、省略主语、场景起句）→ PASS
    """
    pronoun_text = text.replace('他人', '').replace('其他', '')
    if '我' in pronoun_text:
        return {'ok': True, 'why': '第一人称自述，主角主导叙述'}
    if not ids:
        return {'ok': True, 'why': '未绑定证据，跳过'}
    if '他' not in pronoun_text and '她' not in pronoun_text:
        return {'ok': True, 'why': '无第三人称人称，跳过'}
    if not pov_gender:
        return {'ok': True, 'why': '未指定 POV 性别，跳过'}

    want = '他' if pov_gender in ('男', '男性', 'male') else '她'
    other = '她' if want == '他' else '他'

    # ★★ 判据（2026-10-15 第三版，与 check_subject_match 统一）：
    #   **主角是否在场推进**，不是「必须主角起句」。
    #
    #   演进：
    #   ① 旧：「句中是否出现配角人称」→ 判据完全反了（见函数头）
    #   ② 中：「句首主语是谁」→ 把「她炫耀海归同事，他冷笑反制」也判 FAIL。
    #      但这是短剧解说的常见手法：配角行为作引子，主角反应作主体，
    #      POV 仍在主角（后半"他冷笑"才是重点）。
    #   ③ 现：句中**出现主角人称（或第一人称）即算在场** → PASS；
    #      整句只有配角、主角完全不出现 → 才是真偏离 POV。
    head = re.sub(r'^[，。、；：\s]*', '', text)
    head = re.sub(r'^(注意看|可谁|没想到|谁知|殊不知)[，,!！]\s*', '', head)[:10]

    # 整句只有配角、主角（POV 人称/代号/第一人称）完全不出现 → 真偏离
    main_any = [want, '我'] + ([pov_name] if pov_name else [])
    if not any(s in text for s in main_any):
        side_hint = next((s for s in [other, '妻子', '媳妇', '老婆', '丈夫', '老公',
                                      '情敌', '海归', '同事', '邻居', '村长',
                                      '掌门', '长老'] if s in head), other)
        return {'ok': False,
                'why': f'整句只有配角「{side_hint}」在推进，主角（{want}）'
                       f'完全未出现，叙述重心偏离 POV；应让主角在场'
                       f'（如「{side_hint}…，{want}却…」）或改成主角第一人称自述'}

    return {'ok': True, 'why': '主角在场推进（POV 一致）'}


LINKAGE_MARKERS = {
    '递进': ['可还没完', '但更糟', '更糟糕', '但这还不', '不料', '谁知', '更过分',
             '接着', '随后', '结果', '反倒', '反而', '甚至', '愈发', '更是'],
    '联系点': ['偏偏', '本以为', '没想到', '不曾想', '孰料', '万万没想到',
               '谁曾想', '殊不知', '原来', '起初', '起先', '回到开头'],
    '因果': ['因为', '所以', '因此', '于是', '正因为', '才', '结果', '只为',
             '就是为了', '正因', '这才', '以至于', '害得'],
    '反差': ['但', '却', '不过', '然而', '别人', '而他', '而她', '偏偏只有',
             '倒是', '只有他', '只有她', '而非', '而是'],
    '悬念': ['可他', '可她', '但他不知道', '但她不知道', '接下来', '后面',
             '谁料', '竟'],
    '人物引导': ['门开了', '这时', '这时候', '门口', '身后', '走来'],
    '时间': ['第二天', '隔天', '下午', '晚上', '从此', '那时', '当晚', '那天',
             '此时', '转眼'],
}


def evidence_value_text(value) -> str:
    """Normalize mixed S2 scalar/list values before text-only heuristics."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ' '.join(evidence_value_text(item) for item in value if item is not None)
    return '' if value is None else str(value)


def check_cohesion(lines: list, evidence_by_id: dict | None = None) -> dict:
    """衔接门：衡量相邻句是否承接，不把连接词比例误当逻辑质量。

    连接词配额会诱导模型给无关事实强加“但是/所以”。实际门槛只看
    相邻内容承接和机械平铺比例；因果与事实由 grounding 门核验。
    """
    story_lines = [ln for ln in lines
                   if (ln.get('block') or '').strip().lower() not in ('cta', 'ending_cta')]
    texts = [(ln.get('text') or '').strip() for ln in story_lines]
    evidence_by_id = evidence_by_id or {}
    n = len(texts)
    if n < 3:
        return {'verdict': 'PASS', 'reason': '句数过少，跳过'}

    with_link = 0
    kinds = []
    for t in texts:
        hit = [k for k, ws in LINKAGE_MARKERS.items() if any(w in t for w in ws)]
        if hit:
            with_link += 1
            kinds.append(hit[0])
    ratio = with_link / n

    def context_ngrams(line: dict, size: int = 3) -> set[str]:
        primary = line.get('primary_evidence_id')
        row = evidence_by_id.get(primary, {}) if primary else {}
        context = ' '.join(evidence_value_text(value) for value in (
            line.get('text'), row.get('frame_who'), row.get('desc'), row.get('subtitle_text')))
        compact = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', context)
        return {compact[i:i + size] for i in range(max(0, len(compact) - size + 1))}

    connected = 0
    connected_pairs = []
    disconnected_pairs = []
    for pair_no, (a, b) in enumerate(zip(texts, texts[1:]), 1):
        has_link = any(w in b for ws in LINKAGE_MARKERS.values() for w in ws)
        frags = [x for x in re.split(r'[，。、；：\s「」？！]', a) if len(x) >= 3]
        has_echo = (any(f[:3] in b for f in frags[:4])
                    or bool(context_ngrams(story_lines[pair_no - 1])
                             & context_ngrams(story_lines[pair_no], 3)))
        if has_link or has_echo:
            connected += 1
            connected_pairs.append({'from_line': pair_no, 'to_line': pair_no + 1,
                                    'kind': 'marker' if has_link else 'echo'})
        else:
            disconnected_pairs.append({'from_line': pair_no, 'to_line': pair_no + 1,
                                       'from': a[:36], 'to': b[:36]})
    pair_ratio = connected / max(n - 1, 1)

    # 翻译画面特征：句子全是「谁 + 动作」结构且无心理/因果词
    flat = sum(1 for t in texts
               if re.match(r'^[他她我].{0,4}[被把用让]', t)
               and not re.search(r'(因为|所以|但|却|才|为了|没想到|不肯|不敢)', t))

    # 相邻内容承接阈值随稿件长度缩放；词语标记率只记录，不参与通过判定。
    avg_len = sum(len(t) for t in texts) / max(len(texts), 1)
    need = 0.5 if n < 10 else (0.6 if avg_len >= 14 else 0.4)
    verdict = 'PASS' if (pair_ratio >= need and flat / n <= 0.4) else 'FAIL'
    return {
        'verdict': verdict, 'linkage_ratio': round(ratio, 3),
        'pair_connected_ratio': round(pair_ratio, 3),
        'flat_describe_ratio': round(flat / n, 3),
        'linkage_threshold': need,
        'linkage_kinds': kinds[:10],
        'linkage_lines': [i for i, t in enumerate(texts, 1)
                          if any(any(w in t for w in ws) for ws in LINKAGE_MARKERS.values())],
        'connected_pairs': connected_pairs,
        'disconnected_pairs': disconnected_pairs,
        'why': '' if verdict == 'PASS' else
               f'相邻承接 {pair_ratio:.0%}（需≥{need:.0%}）／机械平铺 {flat/n:.0%}（需≤40%）',
    }


def check_semantic_dup(lines: list) -> dict:
    """语义去重门 —— 衔接门测不出「同义重复」。

    实测教训：词法 3 字片段重合度全为 0，但第 2 句「可她笑得比金牌还亮」
    与第 6 句「她低头认错」讲的是同一件事 —— **字面不重合，语义重复**。
    故必须交给 VLM 判「这两句是不是同一件事」。
    """
    texts = [(ln.get('text') or '').strip() for ln in lines]
    n = len(texts)
    if n < 3:
        return {'verdict': 'PASS', 'pairs_checked': 0, 'dups': []}

    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = texts[i], texts[j]
            if not a or not b:
                continue
            # 先用词法粗筛，减少 VLM 调用
            fa = {a[k:k + 3] for k in range(max(len(a) - 2, 1))}
            fb = {b[k:k + 3] for k in range(max(len(b) - 2, 1))}
            if fa & fb:
                pairs.append((i, j, 'lexical'))

    if not pairs:
        # 词法无交集时，只取相邻窗口做语义抽查（VLM 成本高）
        for i in range(n - 1):
            pairs.append((i, i + 1, 'adjacent'))

    dups = []
    for i, j, why in pairs:
        try:
            v = vlm.ask(f"""下面两句口播，判断它们是否在讲**同一件事**。

A：{texts[i]}
B：{texts[j]}

规则：
- 若两句传递的是同一个信息/同一个转折/同一种情绪 → 输出 {{"dup": true}}
- 若两句讲不同的推进、不同的层面 → 输出 {{"dup": false}}
只输出 JSON。""", max_tokens=120, temperature=0.1)
            import json as _json
            mm, end = v.find('{'), v.rfind('}')
            r = _json.loads(v[mm:end + 1]) if mm >= 0 and end > mm else {}
        except Exception:
            r = {}
        if r.get('dup'):
            dups.append({'a_line': i + 1, 'b_line': j + 1, 'a': texts[i][:26], 'b': texts[j][:26]})

    ratio = len(dups) / max(n - 1, 1)
    return {
        'verdict': 'PASS' if ratio <= 0.15 else 'FAIL',
        'pairs_checked': len(pairs),
        'dup_pairs': len(dups),
        'dup_ratio': round(ratio, 3),
        'dups': dups,
        'why': '' if ratio <= 0.15 else f'{len(dups)} 对句子在讲同一件事（去重率须≤15%）',
    }


# 段落任务链（照真实爆款定，见 references/directing.md §5.6）
BLOCK_ROLES = {
    'hook': '用一个可核验的事实开场',
    'setup': '交代已证实的背景或目标',
    'setup2': '补充另一条有证据的背景',
    'rising': '推进已发生的冲突或变化',
    'core': '交代有证据的选择或应对',
    'climax': '最强的已证实场面（可选）',
    'climax2': '第二个独立强场面（可选）',
    'twist': '有明确证据的反转（可选）',
    'ending': '交代片段实际结果或未完状态',
    'aftertaste': '有证据支撑的余味（可选）',
    'cta': '互动引导',
}
REQUIRED_BLOCKS = {'hook', 'cta'}
STORY_BODY_BLOCKS = {'setup', 'setup2', 'rising', 'core', 'climax', 'climax2', 'twist', 'ending'}


def check_block_structure(lines: list) -> dict:
    """句级叙事任务门 —— 检查任务多样性、收束顺序和单类任务占比。

    实测教训（用户两次点破「毫无逻辑性」）：衔接词达标但逻辑依然崩。
    根因是**段落没有独立任务**：第 3/4/5 句都在讲「他打工受苦」，
    第 6/7 句都在讲「金牌反转」—— 5 段在做 2 件事。

    block 标注落在句子级，一段任务可以跨多个字幕/口播句；不能把相邻同标签句
    直接当成冗余。重复信息由语义去重门处理，本门检查两类以上正文任务、余味
    是否只在剧情收束后出现，以及单一任务是否占据正文的大多数。
    """
    blocks = [(ln.get('block') or '').strip() for ln in lines]
    texts = [(ln.get('text') or '').strip() for ln in lines]
    n = len(lines)
    rep = {'total': n, 'blocks': blocks}

    missing = [b for b in REQUIRED_BLOCKS if b not in blocks]
    rep['missing_required'] = missing
    hook_positions = [i for i, block in enumerate(blocks) if block == 'hook']
    story_block_kinds = sorted({b for b in blocks if b in STORY_BODY_BLOCKS})
    rep['story_block_kinds'] = story_block_kinds
    rep['has_story_body'] = bool(story_block_kinds)
    counts = {b: blocks.count(b) for b in set(blocks) if b}
    rep['block_counts'] = counts

    # 相邻同标签句记录为诊断信息；短稿才可能据此判冗余。
    dup_pairs = []
    for i in range(len(blocks) - 1):
        if blocks[i] and blocks[i] == blocks[i + 1]:
            dup_pairs.append({'i': i + 1, 'block': blocks[i],
                              'why': f'第{i+1}、{i+2}段都是 {blocks[i]}（{BLOCK_ROLES.get(blocks[i], "")}）'})
    rep['consecutive_dup_blocks'] = dup_pairs

    # 结尾只有问句 = 缺升华与互动
    tail = texts[-1] if texts else ''
    rep['ending_is_question_only'] = bool(tail) and tail[-1] in '？?' and len(tail) <= 24
    rep['has_aftertaste'] = 'aftertaste' in blocks
    rep['aftertaste_order_warnings'] = []
    rep['has_cta'] = 'cta' in blocks
    cta_positions = [i for i, b in enumerate(blocks) if b == 'cta']
    rep['cta_positions'] = cta_positions

    # ★ 问句只允许出现在 cta 段
    # 实测教训：提示词里写了「想知道…？」做示例，模型把它当模板滥用，
    # 结果 9/9 行都拖一个「想知道…？」—— 观感是审问式，非常糟。
    # 真实爆款只在结尾 cta 用一次问句做互动。
    Q = '？?'
    misplaced_q = []
    for i, (b2, t) in enumerate(zip(blocks, texts), 1):
        if b2 != 'cta' and any(q in t for q in Q):
            misplaced_q.append({'line': i, 'block': b2, 'text': t[:26]})
    rep['misplaced_questions'] = misplaced_q
    rep['question_lines'] = sum(1 for t in texts if any(q in t for q in Q))

    # 汇总
    # 行数随时长变化，标签落在句子级；用任务多样性和占比识别结构失衡，
    # 不按“每个任务标签最多几句”误拦正常的多句任务段。
    problems = []
    if missing:
        problems.append(f'缺必需段落 {missing}')
    if len(hook_positions) != 1 or (hook_positions and hook_positions[0] != 0):
        problems.append('hook 必须且只能出现一次，并位于第一行')
    if not rep['has_story_body']:
        problems.append('正文没有可识别的剧情推进段落')
    invalid = sorted({b for b in blocks if b and b not in BLOCK_ROLES})
    if invalid:
        problems.append(f'出现未知段落标签 {invalid}')
    if len(cta_positions) != 1 or (cta_positions and cta_positions[0] != n - 1):
        problems.append('cta 必须且只能出现一次，并位于最后一行')
    if len(story_block_kinds) < 2:
        problems.append('正文至少要推进两类不同的剧情任务')
    aftertaste_positions = [i for i, block in enumerate(blocks) if block == 'aftertaste']
    if aftertaste_positions:
        terminal_positions = [i for i, block in enumerate(blocks)
                              if block in {'climax', 'climax2', 'twist', 'ending'}]
        contiguous = aftertaste_positions == list(range(aftertaste_positions[0],
                                                        aftertaste_positions[-1] + 1))
        follows_terminal = bool(terminal_positions) and max(terminal_positions) < aftertaste_positions[0]
        before_cta = bool(cta_positions) and aftertaste_positions[-1] == cta_positions[0] - 1
        if not (contiguous and follows_terminal and before_cta):
            rep['aftertaste_order_warnings'].append(
                'aftertaste 标签未形成高潮/结果后的 CTA 前连续收束段')
    narrative_lines = [b for b in blocks if b not in {'hook', 'cta'}]
    max_block_count = max(4, math.ceil(len(narrative_lines) * 0.6))
    overfull = {b: count for b, count in counts.items()
                if b not in {'hook', 'cta'} and count > max_block_count}
    rep['task_share_warnings'] = ([{'max_lines': max_block_count,
                                    'narrative_lines': len(narrative_lines),
                                    'overfull': overfull}]
                                  if overfull else [])
    n_block_kinds = len({b for b in blocks if b})
    if dup_pairs and n <= n_block_kinds:
        problems.append(f'{len(dup_pairs)} 处相邻段落任务重复')
    if misplaced_q:
        problems.append(f'{len(misplaced_q)} 段在非 cta 处用问句（问句只许出现在结尾互动）')
    rep['verdict'] = 'PASS' if not problems else 'FAIL'
    rep['why'] = '；'.join(problems)
    return rep


GENDER_CUES = {'男': ['男性', '男子', '男人', '男孩', '男', '父亲', '丈夫', '厨师服', '工装'],
              '女': ['女性', '女子', '女人', '女孩', '女', '母亲', '妻子', '丝巾', '耳环']}


def check_subject_match(lines: list, by_id: dict) -> dict:
    """主体一致性门 —— 抓「画面是 A 做的事，叙述安给 B」。

    ★ 这是一类全新的错误，现有门全部抓不到：
      - 锚点门只查「动作是否在画面里」→ 主体错配不影响动作存在性，过
      - 人物门只查「人称是否与 POV 一致」→ 整篇人称一致，但配错了画面，照过
      - 证据绑定只查「叙述与画面是否矛盾」→ 「他退学打工」与画面不矛盾，过

    实测致命案例：画面里说「我要退学去打工」的是**女性**（E015），
    但稿子写成「小帅被妻子逼退学打工」——女方的行为安到了男方身上。

    判据：句子的动作主体人称 → 与所引证据画面人物性别 → 是否矛盾。
    """
    rep = {'total': len(lines), 'checked': 0, 'issues': [], 'verdict': 'PASS'}

    for i, ln in enumerate(lines, 1):
        text = (ln.get('text') or '').strip()
        primary = ln.get('primary_evidence_id')
        ids = [primary] if primary else []
        if not ids or not text:
            continue
        desc = by_id.get(primary, '')
        if not desc:
            continue

        # 证据画面里出现的人物性别（只统计明确描述人物的帧）
        male = sum(desc.count(k) for k in GENDER_CUES['男'])
        female = sum(desc.count(k) for k in GENDER_CUES['女'])
        if male == 0 and female == 0:
            continue
        # ★ 男女同框（对手戏是短剧常态）无法判定唯一主体 → 不做主体校验。
        #   旧写法 `'男' if male > female else '女'` 在打平时落到"女"，
        #   于是"他重生归来"配一张男女同框的帧就被判"主体不符"（实测 6 处 FAIL 里 5 处如此）。
        #   判据目的是抓"张冠李戴"，不是处罚同框。
        if male == female:
            continue
        scene_gender = '男' if male > female else '女'

        # 句子的动作主体 = **句中所有人称**（不是单一主语）
        # ★★ 判据演进（2026-10-15，实测三轮才调对）：
        #   ① 旧：按优先级 (小帅→他→她→我) 找第一个 → 混合句「她…他…」被判
        #      actor=他，而句首是她 → 误判（4 处里 2 处错）
        #   ② 中：改成「句首主语」→ 纠正了混合句，但把「她炫耀，他冷笑」
        #      这类后半主语才是画面主体的句子也判 FAIL（又误报 3 处）
        #   ③ 现：**句中出现的任一人称与画面性别一致，即算匹配**。
        #      混合句两个主语都真实出现，不是「把 A 的行为安到 B 身上」；
        #      只有「全句只有一个人称、且与画面性别相反」才是真错配。
        # 泛指「他人」不代表男性角色，先剔除。
        # ★ 必须**先剥离引号内的字幕原文**再判断叙述人称（2026-10-20 实测）：
        #   像「随后字幕打出「等我放出来后」」这种句子，引号里的"我"是**角色台词**，
        #   不是叙述者的人称。旧写法把它算进 present={'我'}，与男性画面无交集 →
        #   误判"叙述人称（我）与画面男性人物不符"（实测 5 处全由此产生）。
        actor_text = _QUOTED_RE.sub('', text).replace('他人', '').replace('其他', '')
        # ★ present 必须同时覆盖**代词**和**社会称呼**——
        #   只查「她」会漏掉「妻子/媳妇/老婆」等写法。
        #   实测第 1 句 hook「小帅被妻子逼退学打工」用「妻子」不用「她」，
        #   present 只剩 {男}，与女性画面 E015 误判为不符（2026-10-15）。
        present = set()
        if '我' in actor_text:
            present.add('我')
        FEMALE_TITLES = ['妻子', '媳妇', '老婆', '女孩', '女人', '女性',
                         '姑娘', '母亲', '妈妈', '女生', '女方']
        MALE_TITLES = ['丈夫', '老公', '男孩', '男人', '男性', '父亲',
                       '爸爸', '男生', '男方']
        if ('她' in actor_text or '小美' in actor_text
                or any(t in actor_text for t in FEMALE_TITLES)):
            present.add('女')
        if ('他' in actor_text or '小帅' in actor_text
                or any(t in actor_text for t in MALE_TITLES)):
            present.add('男')
        if not present:
            continue
        rep['checked'] += 1

        # 矛盾判定：句中没有任何人称与画面性别一致
        if scene_gender not in present:
            rep['issues'].append({
                'line': i, 'actor': scene_gender, 'actor_gender': scene_gender,
                'scene_gender': scene_gender, 'ids': ids,
                'text': text[:30],
                'why': f'叙述人称（{"".join(sorted(present))}）与所引画面'
                       f'{scene_gender}性人物不符（男线索{male}/女线索{female}）',
            })

    rep['failed'] = len(rep['issues'])
    rep['verdict'] = 'PASS' if rep['failed'] == 0 else 'FAIL'
    return rep


def check_evidence_diversity(lines: list) -> dict:
    """检查主镜头覆盖，事实支持证据不计作镜头数量。"""
    cnt = {}
    for ln in lines:
        if (ln.get('block') or '').strip().lower() in ('cta', 'ending_cta'):
            continue
        primary = ln.get('primary_evidence_id')
        if primary:
            cnt[primary] = cnt.get(primary, 0) + 1
    over = {k: v for k, v in cnt.items() if v > 2}
    uniq = len(cnt)
    rep = {'unique_evidence': uniq, 'total_refs': sum(cnt.values()),
           'max_repeat': max(cnt.values()) if cnt else 0,
           'over_reused': over}
    problems = []
    if over:
        problems.append(f'{len(over)} 个证据被引用超过 2 次')
    story_lines = sum(1 for ln in lines
                     if (ln.get('block') or '').strip().lower() not in ('cta', 'ending_cta'))
    if story_lines >= 6 and uniq < 6:
        problems.append(f'{story_lines} 个剧情句只用了 {uniq} 个不同主镜头（应≥6）')
    rep['verdict'] = 'PASS' if not problems else 'FAIL'
    rep['why'] = '；'.join(problems)
    return rep


def schedule_problems(lines: list, min_unique: int = 6) -> list:
    """验证事实证据集合与剪辑主镜头分工，不要求二者是一对一。"""
    probs = []
    for i, ln in enumerate(lines, 1):
        if (ln.get('block') or '').strip().lower() in ('cta', 'ending_cta'):
            if ln.get('evidence_ids') or ln.get('primary_evidence_id'):
                probs.append({'line': i, 'why': 'CTA 不应绑定剧情证据',
                              'evidence_ids': ln.get('evidence_ids'),
                              'primary_evidence_id': ln.get('primary_evidence_id')})
            continue
        refs = ln.get('evidence_ids') or []
        pri = ln.get('primary_evidence_id')
        bad = []
        if not refs:
            bad.append('事实支持 evidence_ids 为空')
        if len(refs) != len(set(refs)):
            bad.append('evidence_ids 有重复项')
        if not pri or pri not in refs:
            bad.append('primary_evidence_id 缺失或不在事实支持 evidence_ids 内')
        if bad:
            probs.append({'line': i, 'why': '；'.join(bad),
                          'evidence_ids': refs, 'primary_evidence_id': pri})
    uniq = {ln.get('primary_evidence_id') for ln in lines
            if (ln.get('primary_evidence_id') and
                (ln.get('block') or '').strip().lower() not in ('cta', 'ending_cta'))}
    if len(uniq) < min_unique:
        probs.append({'why': f'全稿仅 {len(uniq)} 个不同证据（需 ≥{min_unique}）'})
    return probs


def narration_line_count_range(target_count: int, lo: float, hi: float,
                               min_chars: int, max_chars: int) -> list[int]:
    """Derive an advisory sentence-count band from budget and per-line pacing."""
    lower = max(8, math.ceil(lo / max(max_chars, 1)) - 1)
    upper = min(target_count + 4, math.floor(hi / max(min_chars, 1)))
    return [lower, max(lower, upper)]


def check_evidence_order(lines: list, evidence_by_id: dict) -> dict:
    """Require factual lines to follow their source timestamps, with a late hook allowed."""
    story_lines = [ln for ln in lines
                   if (ln.get('block') or '').strip().lower() not in ('cta', 'ending_cta')]
    timed = []
    issues = []
    for i, line in enumerate(story_lines, 1):
        refs = line.get('evidence_ids') or []
        primary = line.get('primary_evidence_id')
        row = evidence_by_id.get(primary, {})
        t = row.get('t')
        if not isinstance(t, (int, float)):
            issues.append({'line': i, 'evidence_id': primary or '',
                           'why': '证据缺少来源时间戳'})
            continue
        timed.append((i, float(t), primary))
    if len(timed) >= 2:
        body = timed[1:]
        if timed[0][1] <= body[0][1]:
            sequence = timed
        else:
            # A late hook is permitted; the remaining narration must rewind and
            # continue chronologically from earlier source evidence.
            sequence = body
            if body[0][1] >= timed[0][1]:
                issues.append({'line': body[0][0], 'evidence_id': body[0][2],
                               'why': '后段钩子后未回到更早的原片时间'})
        for prev, cur in zip(sequence, sequence[1:]):
            if cur[1] < prev[1]:
                issues.append({'line': cur[0], 'evidence_id': cur[2],
                               'why': f'原片时间倒退：第{prev[0]}句 {prev[1]:g}s → 第{cur[0]}句 {cur[1]:g}s'})
    return {'verdict': 'PASS' if not issues else 'FAIL', 'issues': issues,
            'source_times': [{'line': i, 't': t, 'evidence_id': eid} for i, t, eid in timed]}


SUBTITLE_CLAIM_RE = re.compile(
    r'(?:画外字幕|画面字幕|屏幕字幕|字幕(?:里|中)?)(?:内容)?'
    r'(?:显示|写着|写出|出现|是|为)?\s*'
    r"[“「\"‘'](?P<quote>[^”」\"’']+)[”」\"’']"
)
SUBTITLE_PLAIN_CLAIM_RE = re.compile(
    r'(?:画外字幕|画面字幕|屏幕字幕|字幕(?:里|中)?)(?:上)?(?:内容)?'
    r'(?:显示|写着|写出|出现)\s*'
    r'(?P<claim>[^，,。！？?；;：:“「\"‘’”」]+)'
)
DIRECT_SPEECH_RE = re.compile(
    r'(?:小美|她|他|蓝衣女性|蓝衣女人|小女孩|女孩|女人|娘|娘亲|母亲|妈妈)'
    r'[^，。！？?；;]{0,14}(?:说|讲|喊|问|回答|告诉|叮嘱|念道|叫道)(?:话|道|着)?'
)
QUOTED_TEXT_RE = re.compile(r'[“「"‘](?P<quote>[^”」"’]+)[”」"’]')
AUDIO_CLAIM_RE = re.compile(
    r'(?:原片对白|原声对白|对白(?:里|中)?)(?:内容)?(?:提到|说着|说的是|是)?\s*'
    r'[“「"‘](?P<quote>[^”」"’]+)[”」"’]'
)


def eligible_audio_segments(row: dict) -> list[dict]:
    return [segment for segment in row.get('audio_segments', [])
            if isinstance(segment, dict) and segment.get('eligible_for_narrative')
            and str(segment.get('text') or '').strip()]


def strip_verified_audio_claim(text: str, transcript: str) -> tuple[str, list[str]]:
    verified = []

    def replace(match):
        quote = match.group('quote').strip()
        if quote and quote in transcript:
            verified.append(quote)
            return ''
        return match.group(0)

    remainder = AUDIO_CLAIM_RE.sub(replace, text)
    remainder = re.sub(r'[，,、；;：:\s]+([。！？?!])', r'\1', remainder)
    return remainder.strip().strip('，,、；;：:'), verified


def strip_verified_subtitle_claim(text: str, subtitle: str) -> tuple[str, list[str]]:
    """Strip only an explicitly introduced, verbatim subtitle quote.

    The quoted words are independently checked against S2 OCR. Other claims in
    the same sentence remain for VLM grounding, so this cannot bless a whole
    sentence merely because one subtitle fragment matches.
    """
    verified = []

    def replace(match):
        quote = (match.group('quote') or '').strip()
        if quote and quote in (subtitle or ''):
            verified.append(quote)
            return ''
        return match.group(0)

    remainder = SUBTITLE_CLAIM_RE.sub(replace, text)

    def replace_plain(match):
        claim = (match.group('claim') or '').strip()
        if claim and claim in (subtitle or ''):
            verified.append(claim)
            return ''
        return match.group(0)

    remainder = SUBTITLE_PLAIN_CLAIM_RE.sub(replace_plain, remainder)
    remainder = re.sub(r'[，,、；;：:\s]+([。！？?!])', r'\1', remainder)
    return remainder.strip().strip('，,、；;：:'), verified


def strip_linkage_words(text: str) -> str:
    """剥离叙述衔接词 —— 它们是修辞连接，不是需要证据支撑的事实主张。

    ★ 实测冲突（2026-10-20）：cohesion 门**要求**句子带衔接词（≥60%），
      而 grounding 门把「接着」「偏偏」当成 narration claim 送审，
      VLM 必然回答"该证据未支持'接着'这一时间顺序" → FAIL。
      两个门直接打架，任何带衔接词的句子都过不了。
      剥掉衔接词后再判断剩余是否为空：只剩衔接词 + 已核验引文 = 无独立事实主张。
    """
    t = text
    for words in LINKAGE_MARKERS.values():
        for w in sorted(words, key=len, reverse=True):
            t = t.replace(w, '')
    return t


def audit_grounding(lines: list, ev: list, pov_name: str = '', pov_desc: str = '') -> dict:
    """核对句内事实是否由所列证据集合支持；主镜头只负责剪辑定位。"""
    by_id = {row.get('evidence_id'): row for row in ev if isinstance(row, dict)}
    items = []
    local_errors = []
    auto_supported_lines = []
    subtitle_supported_lines = []
    audio_supported_lines = []
    for i, line in enumerate(lines, 1):
        text = (line.get('text') or '').strip()
        cta_markers = ('你们觉得', '你觉得', '你们认为', '你认为', '评论区', '说说你的看法', '聊聊')
        cta_pos = min((text.find(mark) for mark in cta_markers if text.find(mark) >= 0), default=-1)
        cta_prefix = text[:cta_pos].rstrip('，,。；;：: ') if cta_pos >= 0 else text
        open_question = cta_prefix.endswith(('？', '?'))
        is_open_cta = (
            (line.get('block') or '').strip().lower() in ('cta', 'ending_cta')
            and cta_pos >= 0
            and (not cta_prefix or open_question
                 or re.match(r'^(你们觉得|你觉得|你们认为|你认为)', text))
        )
        if is_open_cta:
            auto_supported_lines.append(i)
            continue
        ids = line.get('evidence_ids') or []
        primary = line.get('primary_evidence_id')
        if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
                or not primary or primary not in ids or any(eid not in by_id for eid in ids)):
            local_errors.append({'line': i, 'claim': '事实证据集合缺失/重复/无效，或主镜头不在证据集合内'})
            continue
        rows = [by_id[eid] for eid in ids]
        subtitles = [(row.get('subtitle_text') or '').strip() for row in rows]
        subtitles = [value for value in subtitles if value]
        audio_segments = [segment for row in rows for segment in eligible_audio_segments(row)]
        audio_texts = [str(segment.get('text') or '').strip() for segment in audio_segments]
        mismatches = [m.group('quote').strip()
                      for m in SUBTITLE_CLAIM_RE.finditer(text)
                      if m.group('quote').strip()
                      and not any(m.group('quote').strip() in subtitle for subtitle in subtitles)]
        mismatches.extend(
            m.group('claim').strip() for m in SUBTITLE_PLAIN_CLAIM_RE.finditer(text)
            if m.group('claim').strip()
            and not any(m.group('claim').strip() in subtitle for subtitle in subtitles))
        mismatches.extend(
            m.group('quote').strip() for m in AUDIO_CLAIM_RE.finditer(text)
            if m.group('quote').strip()
            and not any(m.group('quote').strip() in transcript for transcript in audio_texts))
        if mismatches:
            local_errors.append({
                'line': i, 'claim': mismatches[0][:36],
                'reason': '字幕/对白引文必须与本句引用证据中的 OCR/ASR 文本逐字匹配',
            })
            continue
        remainder = text
        verified_quotes = []
        for subtitle in subtitles:
            remainder, quotes = strip_verified_subtitle_claim(remainder, subtitle)
            verified_quotes.extend(quotes)
        verified_audio_quotes = []
        for transcript in audio_texts:
            remainder, quotes = strip_verified_audio_claim(remainder, transcript)
            verified_audio_quotes.extend(quotes)
        if verified_quotes:
            subtitle_supported_lines.append(i)
            # The stable narrator alias is a POV label from the S2 task, not a
            # claim that the original frame displays that name.
            if pov_name:
                alias_free = remainder.replace(pov_name, '')
                if not re.sub(r'[\W_]', '', alias_free, flags=re.UNICODE):
                    remainder = ''
        if verified_audio_quotes:
            audio_supported_lines.append(i)
        # If a line only states an OCR-verified subtitle quote, no semantic
        # model call is needed. Any remaining scene/action claim is still audited.
        # ★ 衔接词先剥掉再判空：只剩「接着」「偏偏」这类连接词时不算事实主张。
        remainder_wo_link = strip_linkage_words(remainder)
        if (verified_quotes or verified_audio_quotes) and not re.sub(
                r'[，,。！？?；;：:\s]', '', remainder_wo_link):
            continue
        speaker_relevant = bool(DIRECT_SPEECH_RE.search(remainder or text))
        primary_row = by_id[primary]
        items.append({
            'line': i,
            'narration_claims': (remainder_wo_link if (verified_quotes or verified_audio_quotes)
                                 else strip_linkage_words(text)),
            'verified_subtitle_quotes': verified_quotes,
            'verified_audio_quotes': verified_audio_quotes,
            'block': line.get('block', ''),
            'inference': bool(line.get('inference', False)),
            'evidence_ids': ids,
            'primary_evidence_id': primary,
            'primary_time_sec': primary_row.get('t'),
            'evidence': [
                {'evidence_id': row.get('evidence_id'), 'time_sec': row.get('t'),
                 'frame_who': row.get('frame_who', ''), 'scene_desc': row.get('desc', ''),
                 'subtitle_text': (row.get('subtitle_text', '') or '')
                                  if speaker_relevant or not verified_quotes else '',
                 'speaker': evidence_speaker(row) if speaker_relevant else '',
                 'speaker_source': row.get('speaker_source', ''),
                 'audio_segments': [
                     {key: segment.get(key) for key in
                      ('segment_id', 'start', 'end', 'text', 'avg_logprob', 'no_speech_prob')}
                     for segment in eligible_audio_segments(row)
                 ]}
                for row in rows
            ],
        })
    if not items and (auto_supported_lines or subtitle_supported_lines or audio_supported_lines):
        return {'verdict': 'FAIL' if local_errors else 'PASS', 'issues': local_errors,
                'supported_lines': sorted(set(auto_supported_lines + subtitle_supported_lines
                                              + audio_supported_lines)),
                'checked': len(lines), 'error': 'evidence binding invalid' if local_errors else None}
    if not items:
        return {'verdict': 'FAIL', 'issues': local_errors, 'supported_lines': [],
                'checked': len(lines), 'error': 'no evidence-bound narration lines'}

    prompt = f"""你是严格的逐句事实核验员。只核对每句文字与它自己引用的 S2 事实证据集合，不得借用其他句、全片剧情简介或常识补足。

事实证据集合里的 frame_who、scene_desc 和字幕支持可见事实；带时间戳的 ASR 片段只支持对应区间的转写文字，不证明说话者身份、画面动作、人物关系或话外事实；primary_evidence_id 只是该句剪辑时显示的代表镜头。不同证据只能支持它们各自实际显示/听到的事实，不能借不同时间点拼造未发生的连续动作或因果。已核实字幕/对白只能证明文字出现/被听到；“要去找/等着带回”等计划不能写成已经实现的结果。speaker 未经音频分离确认时，不得把对白归给画中人物。程序先检查字幕/对白引文是否能在本句引用的证据集合中逐字找到；你只判断 narration_claims 中剩余的事实。不得借用未引用证据、全片梗概或常识补足。

该任务的 POV 主体是「{pov_desc or '未额外描述'}」。以所引证据的画面人物和字幕为准；若稿件称呼与证据中人物不符，判 unsupported；不要要求画面出现 POV 标签。

逐句拆出原子主张：人物、动作、物品、地点、关系、台词归属、计划/结果。每项必须能从该句引用的某一条证据直接确认；多条证据可以分别支持不同事实，但不能以“集合整体看起来合理”替代逐项核对。任何动作、关系、台词归属、因果或结果缺乏直接依据/与证据矛盾，整句判 unsupported。画面/字幕/对白跨时间只可支持并列事实或明确时间顺序，不证明因果或连续动作。ASR 只能支持原片中听到的文字，不能确认说话者身份；开放 CTA 不算剧情断言。情绪解释只有明确标记 inference 且使用弱化表达时才可接受；不能用 inference 掩护具体新事实。

必须对每一句都给出一个判定，不可因稿件整体合理就全部默认通过。输出格式：{{"results":[{{"line":整数,"supported":true或false,"reason":"不支持时简述原因，支持时留空"}}]}}。results 必须恰好覆盖输入的全部行号，每行一次；无法确定就判 false。

逐句稿件与所引证据：
{json.dumps(items, ensure_ascii=False)}"""
    try:
        result = ask_json_once(prompt, max_tokens=1000, temperature=0.1, timeout=300)
    except Exception as e:
        return {'verdict': 'FAIL', 'issues': local_errors,
                'supported_lines': sorted(set(auto_supported_lines + subtitle_supported_lines
                                              + audio_supported_lines)),
                'checked': len(lines),
                'error': f'grounding audit call failed: {type(e).__name__}: {e}'}
    if not isinstance(result, dict) or not isinstance(result.get('results'), list):
        return {'verdict': 'FAIL', 'issues': local_errors,
                'supported_lines': sorted(set(auto_supported_lines + subtitle_supported_lines
                                              + audio_supported_lines)),
                'checked': len(lines),
                'error': 'grounding audit returned invalid JSON'}

    issues = []
    expected_lines = {item['line'] for item in items}
    item_by_line = {item['line']: item for item in items}
    results_by_line = {}
    for row in result['results']:
        if not isinstance(row, dict):
            continue
        line_no = row.get('line')
        if isinstance(line_no, int) and line_no in expected_lines and line_no not in results_by_line:
            results_by_line[line_no] = row
    if set(results_by_line) != expected_lines:
        return {'verdict': 'FAIL', 'issues': local_errors,
                'supported_lines': sorted(set(auto_supported_lines + subtitle_supported_lines
                                              + audio_supported_lines)),
                'checked': len(lines),
                'error': 'grounding audit did not classify every line'}
    issues = list(local_errors)
    supported_lines = list(auto_supported_lines) + list(subtitle_supported_lines)
    for line_no in sorted(expected_lines):
        row = results_by_line[line_no]
        if row.get('supported') is not True:
            text = item_by_line[line_no]['narration_claims']
            issues.append({'line': line_no, 'claim': text[:36],
                           'reason': str(row.get('reason') or '证据不足')[:96]})
        else:
            supported_lines.append(line_no)
    return {'verdict': 'PASS' if not issues else 'FAIL', 'issues': issues,
            'supported_lines': sorted(set(supported_lines)),
            'checked': len(lines),
            'error': 'evidence binding invalid' if local_errors else None}


INFERENCE_MARKERS = ('像', '仿佛', '似乎', '好像', '也许', '可能', '显得',
                     '藏着', '命运', '喧嚣', '风霜', '扛住', '光', '温度', '委屈得',
                     '本以为', '以为', '不知', '才懂', '只为', '盼着', '希望', '想要',
                     '心里', '牵挂', '替爹', '替娘', '扛家', '重担', '押在', '换回', '最笨')
INFERENCE_HEDGES = ('像是', '仿佛', '似乎', '好像', '也许', '可能')


def check_inference_labels(lines: list) -> dict:
    """主观比喻/心理判断必须标 inference=true 并在口播中显式弱化。"""
    issues = []
    for i, line in enumerate(lines, 1):
        text = (line.get('text') or '').strip()
        is_cta = (line.get('block') or '').strip().lower() in ('cta', 'ending_cta')
        if is_cta:
            continue
        has_trigger = any(m in text for m in INFERENCE_MARKERS)
        if has_trigger and line.get('inference') is not True:
            issues.append({'line': i, 'text': text[:48],
                           'why': '含主观比喻/心理判断，但 inference 未标 true'})
        elif line.get('inference') is True and not any(m in text for m in INFERENCE_HEDGES):
            issues.append({'line': i, 'text': text[:48],
                           'why': 'inference=true 但口播没有“像是/仿佛/也许”等明确弱化词'})
    return {'verdict': 'FAIL' if issues else 'PASS', 'issues': issues}


def check_subtitle_claims(lines: list, evidence_by_id: dict) -> dict:
    """Catch subtitle/frame crossovers, inaccurate quotes and unverified speech attribution."""
    issues = []
    for i, line in enumerate(lines, 1):
        text = (line.get('text') or '').strip()
        if (line.get('block') or '').strip().lower() in ('cta', 'ending_cta'):
            continue
        ids = line.get('evidence_ids') or []
        if (not isinstance(ids, list) or not ids
                or any(eid not in evidence_by_id for eid in ids)):
            continue
        rows = [evidence_by_id[eid] or {} for eid in ids]
        subtitles = [str(row.get('subtitle_text') or '').strip() for row in rows]
        subtitles = [subtitle for subtitle in subtitles if subtitle]
        audio_segments = [segment for row in rows for segment in eligible_audio_segments(row)]
        audio_texts = [str(segment.get('text') or '').strip() for segment in audio_segments]
        claims = [m.group('quote').strip() for m in SUBTITLE_CLAIM_RE.finditer(text)]
        claims.extend(m.group('claim').strip() for m in SUBTITLE_PLAIN_CLAIM_RE.finditer(text))
        for claim in claims:
            if claim and not any(claim in subtitle for subtitle in subtitles):
                issues.append({'line': i, 'evidence_ids': ids, 'claim': claim[:40],
                               'actual_subtitle': subtitles[:3],
                               'why': '字幕引用必须逐字匹配本句引用证据中的字幕'})
                break
        audio_claims = [m.group('quote').strip() for m in AUDIO_CLAIM_RE.finditer(text)]
        if not any(x.get('line') == i for x in issues):
            for claim in audio_claims:
                if claim and not any(claim in transcript for transcript in audio_texts):
                    issues.append({'line': i, 'evidence_ids': ids, 'claim': claim[:40],
                                   'actual_audio_segments': len(audio_texts),
                                   'why': '对白引用必须逐字匹配本句引用证据中的 ASR 文本'})
                    break
        if any(x.get('line') == i for x in issues):
            continue
        for other_id, other in evidence_by_id.items():
            if other_id in ids:
                continue
            other_subtitle = str((other or {}).get('subtitle_text') or '').strip()
            if len(other_subtitle) >= 5 and other_subtitle in text:
                issues.append({'line': i, 'evidence_ids': ids, 'claim': other_subtitle[:40],
                               'actual_subtitle': subtitles[:3],
                               'why': f'该文字出现在其他证据 {other_id}，但未列为本句事实依据'})
                break
        if any(x.get('line') == i for x in issues):
            continue
        speech_claim = DIRECT_SPEECH_RE.search(text)
        if speech_claim and not re.match(r'^(?:画面|屏幕|画外)?字幕', text):
            quoted = [m.group('quote').strip() for m in SUBTITLE_CLAIM_RE.finditer(text)]
            speech_rows = [row for row in rows
                           if not quoted or any(q and q in str(row.get('subtitle_text') or '')
                                                for q in quoted)]
            known_speakers = [str(row.get('speaker') or '').strip() for row in speech_rows
                              if str(row.get('speaker') or '').strip()
                              and row.get('speaker_source') == 'audio_diarization'
                              and not any(token in str(row.get('speaker') or '')
                                          for token in ('未知', '画外', '不确定', '无法确认',
                                                        '未识别', 'unknown'))]
            if not known_speakers:
                issues.append({'line': i, 'evidence_ids': ids, 'claim': speech_claim.group(0)[:40],
                               'actual_subtitle': [row.get('speaker', '未知') for row in speech_rows],
                               'why': '所引字幕证据没有确认说话者，不得归给画中人物'})
    return {'verdict': 'FAIL' if issues else 'PASS', 'issues': issues}


def normalize_unknown_speaker_attributions(lines: list, evidence_by_id: dict) -> list[int]:
    """Turn a verbatim on-screen quote into neutral subtitle narration when its speaker is unknown.

    This removes only an unsupported attribution. It uses a verbatim fragment
    when available; otherwise it labels the short suffix as a subtitle gist.
    The exact-quote and semantic-grounding gates still run on the result.
    """
    repaired = []
    unknown_markers = ('未知', '画外', '不确定', '无法确认', '未识别', 'unknown')
    for index, line in enumerate(lines, 1):
        if not isinstance(line, dict) or (line.get('block') or '').strip().lower() in ('cta', 'ending_cta'):
            continue
        text = str(line.get('text') or '').strip()
        attribution = DIRECT_SPEECH_RE.search(text)
        if not attribution:
            continue
        rows = [evidence_by_id.get(eid) or {} for eid in (line.get('evidence_ids') or [])]
        quotes = [m.group('quote').strip() for m in QUOTED_TEXT_RE.finditer(text)]
        suffix = re.sub(r'^[，,：:]\s*', '', text[attribution.end():])
        suffix = suffix.strip().strip('“”「」"‘’\'，,。！？?：: \t')
        candidates = quotes or ([suffix] if 1 <= len(suffix) <= 32 else [])
        subtitle_quote_rows = [row for row in rows if any(
            phrase and phrase in str(row.get('subtitle_text') or '') for phrase in candidates)]
        audio_quote_rows = [row for row in rows if any(
            phrase and any(phrase in str(segment.get('text') or '')
                           for segment in eligible_audio_segments(row))
            for phrase in candidates)]
        quote_rows = subtitle_quote_rows or audio_quote_rows
        subtitle_rows = [row for row in rows if str(row.get('subtitle_text') or '').strip()]
        audio_rows = [row for row in rows if eligible_audio_segments(row)]
        if not subtitle_rows and not audio_rows:
            continue
        has_confirmed_speaker = any(
            str(row.get('speaker') or '').strip()
            and row.get('speaker_source') == 'audio_diarization'
            and not any(marker in str(row.get('speaker') or '').lower() for marker in unknown_markers)
            for row in (quote_rows or subtitle_rows))
        if has_confirmed_speaker:
            continue
        # Prefer a verbatim subtitle fragment. If S2 OCR differs, make the
        # claim explicitly a paraphrase so the downstream semantic grounding
        # audit (not the exact-quote matcher) must verify it.
        if subtitle_quote_rows and candidates:
            normalized = f'画面字幕写着“{candidates[0]}”'
        elif audio_quote_rows and candidates:
            normalized = f'原片对白提到“{candidates[0]}”'
        elif 1 <= len(suffix) <= 32:
            if audio_rows:
                normalized = f'原片对白大意是：{suffix}'
            elif subtitle_rows:
                normalized = f'画面字幕大意是：{suffix}'
            else:
                continue
        elif audio_rows:
            normalized = '原片出现一段对白'
        elif subtitle_rows:
            normalized = '画面出现字幕'
        else:
            continue
        if normalized != text and not DIRECT_SPEECH_RE.search(normalized):
            line['text'] = normalized
            repaired.append(index)
    return repaired


def normalize_primary_timeline(lines: list, evidence_by_id: dict) -> dict:
    """Choose a chronological editorial anchor from each line's already-cited facts.

    This never adds or removes a factual citation. It only changes which cited
    frame represents a line in the edit, and only when the writer's primary
    anchors would move backwards in source time.
    """
    story_indices = [i for i, line in enumerate(lines)
                     if isinstance(line, dict)
                     and (line.get('block') or '').strip().lower() not in ('cta', 'ending_cta')]
    repaired, unresolved = [], []

    def get_time(eid):
        row = evidence_by_id.get(eid) or {}
        value = row.get('t')
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    if len(story_indices) < 2:
        return {'repaired_lines': repaired, 'unresolved_lines': unresolved}
    first_line = lines[story_indices[0]]
    first_t = get_time(first_line.get('primary_evidence_id'))
    second_t = get_time(lines[story_indices[1]].get('primary_evidence_id'))
    late_hook = ((first_line.get('block') or '').strip().lower() == 'hook'
                 and first_t is not None and second_t is not None and first_t > second_t)
    previous_t = None
    for position, index in enumerate(story_indices):
        line = lines[index]
        primary = line.get('primary_evidence_id')
        current_t = get_time(primary)
        if position == 0:
            previous_t = None if late_hook else current_t
            continue
        refs = line.get('evidence_ids') or []
        candidates = [(get_time(eid), eid) for eid in refs if get_time(eid) is not None]
        legal = [(t, eid) for t, eid in candidates
                 if (previous_t is None or t >= previous_t)
                 and not (late_hook and position == 1 and first_t is not None and t >= first_t)]
        if current_t is not None and (previous_t is None or current_t >= previous_t) \
                and not (late_hook and position == 1 and first_t is not None and current_t >= first_t):
            previous_t = current_t
            continue
        if not legal:
            unresolved.append(index + 1)
            previous_t = current_t
            continue
        # Prefer the earliest still-legal cited frame to maintain a natural cut progression.
        chosen_t, chosen_id = min(legal, key=lambda pair: (pair[0], pair[1]))
        line['primary_evidence_id'] = chosen_id
        repaired.append(index + 1)
        previous_t = chosen_t
    return {'repaired_lines': repaired, 'unresolved_lines': unresolved}


def normalize_story_line_order(lines: list, evidence_by_id: dict) -> dict:
    """Keep hook/CTA fixed and order self-contained narration lines by source time.

    Each line object moves intact, so its wording, evidence set, edit anchor,
    block and tone remain attached. No factual citation is created or changed.
    """
    hook_positions = [i for i, line in enumerate(lines)
                      if isinstance(line, dict) and (line.get('block') or '').strip().lower() == 'hook']
    cta_positions = [i for i, line in enumerate(lines)
                     if isinstance(line, dict) and (line.get('block') or '').strip().lower() in ('cta', 'ending_cta')]
    if len(hook_positions) != 1 or hook_positions[0] != 0 or len(cta_positions) != 1 \
            or cta_positions[0] != len(lines) - 1:
        return {'verdict': 'NOT_RUN', 'reordered_from_lines': [], 'unresolved_lines': []}
    hook, cta = lines[0], lines[-1]
    body = lines[1:-1]
    timed = []
    unresolved = []
    for i, line in enumerate(body, 2):
        row = evidence_by_id.get(line.get('primary_evidence_id')) or {}
        value = row.get('t')
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            timed.append((float(value), i, line))
        else:
            unresolved.append(i)
    if unresolved:
        return {'verdict': 'FAIL', 'reordered_from_lines': [], 'unresolved_lines': unresolved}
    original_line = {id(line): i for i, line in enumerate(lines, 1)}
    ordered = [item[2] for item in sorted(timed, key=lambda item: (item[0], item[1]))]
    reordered_from = [original_line[id(line)] for new_i, line in enumerate(ordered, 2)
                      if original_line[id(line)] != new_i]
    if reordered_from:
        lines[:] = [hook, *ordered, cta]
    return {'verdict': 'PASS', 'reordered_from_lines': reordered_from, 'unresolved_lines': []}


def normalize_cta_to_end(lines: list) -> dict:
    """Move one evidence-free CTA to the end without changing the line itself."""
    cta_positions = [i for i, line in enumerate(lines)
                     if isinstance(line, dict)
                     and (line.get('block') or '').strip().lower() in ('cta', 'ending_cta')]
    if len(cta_positions) != 1:
        return {'verdict': 'NOT_RUN', 'moved_from_lines': [],
                'reason': 'CTA count is not exactly one'}
    index = cta_positions[0]
    cta = lines[index]
    if (cta.get('evidence_ids') or []) or cta.get('primary_evidence_id'):
        return {'verdict': 'NOT_RUN', 'moved_from_lines': [],
                'reason': 'CTA contains factual citations'}
    if index == len(lines) - 1:
        return {'verdict': 'PASS', 'moved_from_lines': []}
    lines.append(lines.pop(index))
    return {'verdict': 'PASS', 'moved_from_lines': [index + 1]}


def lint_lines(lines: list, by_id: dict, lo: float, hi: float, pov_gender: str = '',
               pov_name: str = '', caption_capacity: int = 36,
               evidence_by_id: dict | None = None,
               short_line_max_chars: int = 12,
               max_short_story_line_ratio: float = 0.4) -> dict:
    """口播稿硬校验：证据绑定 / 字数预算 / 口播可读性。

    可读性门来自实测教训：VLM 默认写"大纲体"——每句都是罗列式短语；
    问句占比≤40%，平均句长按目标字数/句数动态缩放。
    """
    rep = {'total': len(lines), 'no_evidence': 0, 'long_lines': [],
           'pacing_overages': [], 'chars': 0}
    texts = []
    for i, ln in enumerate(lines, 1):
        t = (ln.get('text') or '').strip()
        texts.append(t)
        rep['chars'] += len(t)
        ids = ln.get('evidence_ids') or []
        # ★ CTA 段是互动引导（"评论区说说你的看法"），逻辑上不绑定画面证据，
        #   不应计入 no_evidence —— 旧逻辑把它当缺证据，是误判（2026-10-15 实测）。
        is_cta = (ln.get('block') or '').strip().lower() in ('cta', 'ending_cta')
        if not is_cta and (not ids or any(x not in by_id for x in ids)):
            rep['no_evidence'] += 1
        primary = ln.get('primary_evidence_id')
        if not is_cta and ((len(ids) > 1 and not primary)
                           or (primary and primary not in ids)):
            rep['no_evidence'] += 1
        pacing_limit = min(caption_capacity,
                           max(18, math.ceil(hi / max(len(lines), 1)) + 2))
        if len(t) > caption_capacity:
            rep['long_lines'].append({'line': i, 'chars': len(t),
                                      'max_chars': caption_capacity})
        elif len(t) > pacing_limit:
            # The average-based line length is a pacing target; only the actual
            # platform subtitle capacity is a hard limit for one TTS segment.
            rep['pacing_overages'].append({'line': i, 'chars': len(t),
                                           'target_chars': pacing_limit})
    rep['estimated_sec'] = round(rep['chars'] / 5.25, 1)
    lower_slack = max(1, math.ceil(lo * 0.025))
    rep['nominal_in_budget'] = lo <= rep['chars'] <= hi
    rep['budget_slack_chars'] = lower_slack
    rep['in_budget'] = lo - lower_slack <= rep['chars'] <= hi

    # 人物一致性：以 POV 为准（视角定死铁律）
    rep['gender_issues'] = []
    for i, ln in enumerate(lines, 1):
        primary = ln.get('primary_evidence_id')
        ids = [primary] if primary else []
        r = check_gender((ln.get('text') or ''), ids, by_id, pov_gender, pov_name)
        if not r['ok']:
            rep['gender_issues'].append({'line': i, 'text': (ln.get('text') or '')[:30],
                                         'ids': ids, 'why': r['why']})
    rep['gender_ok'] = len(rep['gender_issues']) == 0
    rep['inference_labels'] = check_inference_labels(lines)
    rep['subtitle_claims'] = check_subtitle_claims(lines, evidence_by_id or {})

    # 衔接门：句子之间是否用上衔接法、有无故事推进（防"翻译画面"）
    rep['cohesion'] = check_cohesion(lines, evidence_by_id or {})
    rep['evidence_order'] = check_evidence_order(lines, evidence_by_id or {})
    rep['semantic_dup'] = check_semantic_dup(lines)
    rep['block_structure'] = check_block_structure(lines)
    rep['subject_match'] = check_subject_match(lines, by_id)
    rep['evidence_diversity'] = check_evidence_diversity(lines)
    n = len(texts) or 1
    q = sum(1 for t in texts if t and t[-1] in Q)
    short = sum(1 for t in texts if len(t) <= 12)
    avg = rep['chars'] / n
    # ★ 均长门按**目标时长缩放**（2026-10-15 第九个判据缺陷）
    #   旧的写死 14-30 是 60s 口径。20s 短片：预算 90-120 字 / 10 句
    #   = 均长 9-12 字，**与 14 的下限数学上矛盾**，导致 20s 目标永远 FAIL。
    #   缩放依据：语速 5.25 字/秒，一句约占 1.6-2.4 秒 →
    #   20s→8-12 字/句，60s→14-28 字/句。
    #   用 hi（字数上限）/ n 推出实际上限，下限取 min(14, 上限*0.75)。
    len_hi = max(12.0, hi / n)
    len_lo = min(14.0, len_hi * 0.75)
    avg_ok = len_lo <= avg <= len_hi
    story_texts = [
        (ln.get('text') or '').strip() for ln in lines
        if (ln.get('block') or '').strip().lower() not in ('hook', 'cta', 'ending_cta')
    ]
    story_short = sum(
        len(re.sub(r'[\s\W_]+', '', text)) <= short_line_max_chars
        for text in story_texts
    )
    story_short_ratio = story_short / max(len(story_texts), 1)
    story_short_ok = story_short_ratio <= max_short_story_line_ratio
    rep['readability'] = {
        'question_ratio': round(q / n, 3), 'question_ratio_ok': q / n <= 0.4,
        'short_line_ratio': round(short / n, 3),
        'story_short_line_max_chars': short_line_max_chars,
        'story_short_line_ratio': round(story_short_ratio, 3),
        'max_short_story_line_ratio': max_short_story_line_ratio,
        'story_short_line_ratio_ok': story_short_ok,
        'avg_len': round(avg, 1),
        'avg_len_range': [round(len_lo, 1), round(len_hi, 1)],
        'avg_len_ok': avg_ok,
        'verdict': 'PASS' if (q / n <= 0.4 and avg_ok) else 'FAIL',
    }
    rep['texts'] = texts
    return rep


def main() -> int:
    global LAST_JSON_PREVIEW
    existing = [name for name in ('script.json', 'narration.txt') if (TASK / name).exists()]
    if existing:
        print('[ERR] 任务目录已有可用稿件产物: ' + ', '.join(existing) +
              '；为避免旧稿与本轮失败结果混淆，请使用新任务目录', file=sys.stderr)
        return 2
    story_p, ev_p = TASK / 'story.json', TASK / 'evidence.json'
    if not story_p.exists() or not ev_p.exists():
        print('[ERR] 缺 story.json / evidence.json，先跑 S2', file=sys.stderr)
        return 1
    story = json.loads(story_p.read_text(encoding='utf-8'))
    ev = json.loads(ev_p.read_text(encoding='utf-8'))

    # S2 的 POV / 关系推断只用于编辑参考，不作为事实。人物称呼逐句由证据决定。
    pov_gender = ''
    pov_desc = '不预设全片主角；按各句引用证据确认人物、动作与关系'
    pov_name = ''
    print(f'[S3] 叙事焦点: {pov_desc}', flush=True)

    soul_name = pick_soul(story, story.get('genre', ''))
    soul = SOULS[soul_name]
    print(f'[S3] 流派选型: {soul_name}（正典 {soul["skill"]}）', flush=True)

    lo, hi = TARGET_SEC * 4.5, TARGET_SEC * 6.0
    budget = int((lo + hi) / 2)
    request = json.loads((TASK / 'request.json').read_text(encoding='utf-8')) \
        if (TASK / 'request.json').exists() else {}
    config = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
    platform = request.get('platform', 'generic')
    profile = config.get('platform_profiles', {}).get(
        platform, config['platform_profiles']['generic'])
    caption_capacity = int(profile.get('chars_per_line', 14)) * int(
        profile.get('subtitle_max_lines', 2))
    # TTS is generated per line; 18 characters target a complete spoken beat
    # without exceeding the two-line Douyin caption capacity.
    script_quality = config.get('script_quality', {})
    narration_chars_per_line = min(
        max(1, int(script_quality.get('target_chars_per_tts_segment', 18))),
        max(caption_capacity, 1))
    short_line_max_chars = max(1, int(script_quality.get('short_story_line_max_chars', 12)))
    max_short_story_line_ratio = min(1.0, max(0.0, float(
        script_quality.get('max_short_story_line_ratio', 0.4))))
    line_count = max(8, math.ceil(budget / narration_chars_per_line))
    timeline_events = [row for row in story.get('timeline_events', [])
                       if isinstance(row, dict) and row.get('evidence_ids')]
    if timeline_events:
        line_count = min(line_count, max(8, len(timeline_events) + 4))
    line_count = min(line_count, max(8, len(ev) + 1))
    min_line_chars = max(8, math.floor(budget / line_count))
    max_line_chars = min(caption_capacity, math.ceil(hi / line_count))

    print('[S3] 生成钩子候选...', flush=True)
    hooks = build_hooks(story, ev, soul, pov_desc)
    fallback_hook = evidence_hook_fallback(story, ev, pov_name)
    if fallback_hook and all(h.get('text') != fallback_hook['text'] for h in hooks):
        hooks.append(fallback_hook)
    length_good = [h for h in hooks if h['gate'] == 'OK']
    hook_lines, hook_map = [], {}
    for h in length_good:
        ids = h.get('evidence_ids') or []
        if len(ids) != 1 or ids[0] not in {row.get('evidence_id') for row in ev}:
            h['grounding'] = {'verdict': 'FAIL', 'reason': '钩子缺少唯一有效证据'}
            continue
        hook_lines.append({'text': h['text'], 'evidence_ids': ids,
                           'primary_evidence_id': ids[0], 'inference': False,
                           'block': 'hook'})
        hook_map[len(hook_lines)] = h
    if hook_lines:
        hook_audit = audit_grounding(hook_lines, ev, pov_name, pov_desc)
        supported = set(hook_audit.get('supported_lines', []))
        issue_by_line = {x.get('line'): x for x in hook_audit.get('issues', [])}
        for line_no, h in hook_map.items():
            issue = issue_by_line.get(line_no, {})
            h['grounding'] = {
                'verdict': 'PASS' if line_no in supported else 'FAIL',
                'reason': issue.get('reason') or hook_audit.get('error', ''),
            }
    else:
        hook_audit = {'verdict': 'FAIL', 'issues': [], 'supported_lines': [],
                      'checked': 0, 'error': 'no hook candidate had one valid evidence ID'}
    (TASK / 'hook_grounding.json').write_text(
        json.dumps(hook_audit, ensure_ascii=False, indent=2), encoding='utf-8')
    (TASK / 'hooks.json').write_text(json.dumps(hooks, ensure_ascii=False, indent=2), encoding='utf-8')
    good = [h for h in length_good if h.get('grounding', {}).get('verdict') == 'PASS']
    print(f'      长度通过 {len(length_good)}/{len(hooks)}；逐帧事实通过 {len(good)} 个', flush=True)
    if not good:
        print('[ERR] 没有同时通过长度与逐帧事实核验的钩子，终止（不硬写）', file=sys.stderr)
        return 2

    chosen = good[0]
    print(f'[S3] 采用钩子（{chosen["formula"]}）: {chosen["text"]}', flush=True)

    # 口播、证据绑定或事实核验不过则重生（最多 3 次），把具体失败回灌给模型。
    by_id = {e['evidence_id']: e['desc'] for e in ev}
    evidence_rows_by_id = {e['evidence_id']: e for e in ev if isinstance(e, dict)}
    script = None
    accepted = False
    last_report = None
    attempt_history = []
    chosen_index = 0
    previous_lines = None
    generation_errors = []
    last_nonempty_attempt = 0
    for attempt in range(3):
        print(f'[S3] 生成口播稿（第 {attempt + 1} 次）...', flush=True)
        try:
            scheduled_ids = select_evidence_schedule(story, ev, max(6, line_count - 1),
                                                     (chosen.get('evidence_ids') or [''])[0])
        except ValueError as e:
            print(f'[ERR] {e}', file=sys.stderr)
            return 2
        if previous_lines and last_report is not None:
            data = revise_script(previous_lines, ev, soul_name, chosen, pov_desc,
                                 scheduled_ids, budget, line_count,
                                 min_line_chars, max_line_chars,
                                 chosen.get('retry_hint', ''),
                                 caption_capacity=caption_capacity)
        else:
            data = write_script(story, ev, soul_name, chosen, budget, pov_desc,
                                scheduled_ids, previous_lines, line_count,
                                min_line_chars, max_line_chars,
                                caption_capacity=caption_capacity)
        lines = data.get('lines', []) if isinstance(data, dict) else []
        if not lines:
            error = LAST_JSON_ERROR or 'valid JSON contains no lines'
            retry_shape_hint = ' 上次没有返回合法完整稿件。请只返回顶层为 lines 的 JSON 对象，lines 必须含全部目标句数；不要只返回一个句对象。'
            if isinstance(data, dict):
                keys = ','.join(sorted(str(k) for k in data.keys())[:8])
                error += '; top_level_keys=' + keys
                LAST_JSON_PREVIEW = json.dumps(data, ensure_ascii=False)[:180]
                if 'text' in data and 'evidence_ids' in data:
                    retry_shape_hint += f' 上次只返回了单句对象（顶层字段 {keys}），格式错误。'
            generation_errors.append({
                'attempt': attempt + 1, 'error': error,
                **({'preview': LAST_JSON_PREVIEW[:180]} if LAST_JSON_PREVIEW else {}),
            })
            attempt_history.append({'attempt': attempt + 1, 'status': 'invalid_output',
                                    'error': error,
                                    **({'preview': LAST_JSON_PREVIEW[:180]} if LAST_JSON_PREVIEW else {})})
            print(f'      VLM 未返回可解析口播稿（{error}），重试', flush=True)
            chosen = dict(chosen)
            chosen['retry_hint'] = (chosen.get('retry_hint', '') + retry_shape_hint)
            continue
        script = data
        last_nonempty_attempt = attempt + 1
        speaker_repairs = normalize_unknown_speaker_attributions(lines, evidence_rows_by_id)
        cta_position = normalize_cta_to_end(lines)
        timeline_repairs = normalize_primary_timeline(lines, evidence_rows_by_id)
        line_order = normalize_story_line_order(lines, evidence_rows_by_id)
        if line_order['verdict'] == 'PASS':
            post_order_repairs = normalize_primary_timeline(lines, evidence_rows_by_id)
            timeline_repairs['repaired_lines'] = list(dict.fromkeys(
                timeline_repairs['repaired_lines'] + post_order_repairs['repaired_lines']))
            timeline_repairs['unresolved_lines'] = post_order_repairs['unresolved_lines']
        if (speaker_repairs or cta_position['moved_from_lines']
                or timeline_repairs['repaired_lines'] or line_order['reordered_from_lines']):
            script['normalizations'] = {
                **(script.get('normalizations') or {}),
                'unknown_speaker_quotes_to_subtitle': speaker_repairs,
                'evidence_free_cta_to_end_from_lines': cta_position['moved_from_lines'],
                'chronological_primary_anchor_lines': timeline_repairs['repaired_lines'],
                'chronological_narration_order_from_lines': line_order['reordered_from_lines'],
            }
        previous_lines = lines
        # 不自动把无效/重复证据替换成预排 ID；那会静默制造“句子—镜头”错配。
        # 保留原引用，让结构门/事实门指出问题并交给重写解决。
        r = lint_lines(lines, by_id, lo, hi, pov_gender, pov_name, caption_capacity,
                       evidence_rows_by_id, short_line_max_chars,
                       max_short_story_line_ratio)
        r['speaker_attribution_normalization'] = {
            'verdict': 'PASS', 'repaired_lines': speaker_repairs,
            'method': 'exact_quote_or_explicit_paraphrase_of_cited_subtitle',
        }
        r['primary_anchor_normalization'] = {
            'verdict': 'PASS' if not timeline_repairs['unresolved_lines'] else 'FAIL',
            **timeline_repairs,
            'method': 'reselect_from_existing_citations_only',
        }
        r['narrative_order_normalization'] = line_order
        r['cta_position_normalization'] = cta_position
        r['caption_budget'] = {'platform': platform,
                               'chars_per_line': int(profile.get('chars_per_line', 14)),
                               'max_lines': int(profile.get('subtitle_max_lines', 2)),
                               'total_chars_per_cue': caption_capacity}

        # 事实证据集与剪辑主镜头各自校验；CTA 是无事实证据的互动句。
        story_lines = [ln for ln in lines
                       if (ln.get('block') or '').strip().lower() not in ('cta', 'ending_cta')]
        schedule_mismatches = schedule_problems(lines, min(6, len(story_lines)))
        line_count_range = narration_line_count_range(
            line_count, lo, hi, min_line_chars, max_line_chars)
        line_count_ok = line_count_range[0] <= len(lines) <= line_count_range[1]
        if not line_count_ok:
            schedule_mismatches.append({
                'why': f'实际句数 {len(lines)} 不在预算范围 {line_count_range[0]}-{line_count_range[1]}'})
        r['evidence_schedule'] = {'verdict': 'PASS' if not schedule_mismatches else 'FAIL',
                                  'mismatches': schedule_mismatches,
                                  'expected_line_count': line_count,
                                  'accepted_line_range': line_count_range,
                                  'actual_line_count': len(lines),
                                  'line_count_within_accepted_range': line_count_ok,
                                  'line_count_is_advisory': False,
                                  'expected_ids': [(ln.get('primary_evidence_id') or '') for ln in lines],
                                  'scheduled_story_ids': scheduled_ids,
                                  'contract': 'primary_is_cited_evidence; chronology_and_diversity_are_checked_separately'}
        print(f"      字数 {r['chars']}（预算 {lo:.0f}-{hi:.0f}，低端容差 {r['budget_slack_chars']}）  "
              f"问句 {r['readability']['question_ratio']:.0%}  "
              f"正文短句 {r['readability']['story_short_line_ratio']:.0%}  "
              f"可读性 {r['readability']['verdict']}  "
              f"承接 {r['cohesion']['verdict']}({r['cohesion']['pair_connected_ratio']:.0%})  "
              f"去重 {r['semantic_dup']['verdict']}({r['semantic_dup']['dup_ratio']:.0%})  "
              f"段落 {r['block_structure']['verdict']}  "
              f"主体 {r['subject_match']['verdict']}  "
              f"字幕 {r['subtitle_claims']['verdict']}  "
              f"推断 {r['inference_labels']['verdict']}  "
              f"多样 {r['evidence_diversity']['verdict']}  "
              f"镜头表 {r['evidence_schedule']['verdict']}  "
              f"口播节奏超线 {len(r['pacing_overages'])}（仅诊断）", flush=True)
        basic_pass = (r['in_budget'] and r['readability']['verdict'] == 'PASS' and r['no_evidence'] == 0
                and not r['long_lines']
                and r['gender_ok'] and r['cohesion']['verdict'] == 'PASS'
                and r['evidence_order']['verdict'] == 'PASS'
                and r['inference_labels']['verdict'] == 'PASS'
                and r['subtitle_claims']['verdict'] == 'PASS'
                and r['semantic_dup']['verdict'] == 'PASS'
                and r['block_structure']['verdict'] == 'PASS'
                and r['subject_match']['verdict'] == 'PASS'
                and r['evidence_diversity']['verdict'] == 'PASS'
                and r['evidence_schedule']['verdict'] == 'PASS')
        candidate_groundable = (
            len(lines) >= 6 and r['no_evidence'] == 0 and r['gender_ok']
            and r['subject_match']['verdict'] == 'PASS'
            and r['evidence_diversity']['verdict'] == 'PASS'
            and r['subtitle_claims']['verdict'] == 'PASS'
        )
        if candidate_groundable:
            print('      逐句核验画面、字幕、说话者与具体事实...', flush=True)
            r['grounding'] = audit_grounding(lines, ev, pov_name, pov_desc)
            print(f"      事实绑定 {r['grounding']['verdict']}（核验 {r['grounding']['checked']} 句，"
                  f"问题 {len(r['grounding']['issues'])} 条）", flush=True)
        else:
            r['grounding'] = {'verdict': 'NOT_RUN', 'issues': [], 'checked': 0,
                              'reason': '证据绑定或主体结构未通过，未调用事实核验'}
        attempt_history.append({
            'attempt': attempt + 1,
            'status': 'accepted' if (basic_pass and r['grounding']['verdict'] == 'PASS') else 'rejected',
            'lines': lines,
            'lint': json.loads(json.dumps(r, ensure_ascii=False)),
        })
        if basic_pass and r['grounding']['verdict'] == 'PASS':
            accepted = True
            last_report = r
            break
        last_report = r
        hint = (f"上次未通过：全文 {r['chars']} 字（必须 {lo:.0f}-{hi:.0f}，目标 {budget}），"
                f"目标 {line_count} 行（允许 {line_count_range[0]}-{line_count_range[1]} 行）、"
                f"每行 {min_line_chars}-{max_line_chars} 字；"
                f"问句占比 {r['readability']['question_ratio']:.0%}（需≤40%）、"
                f"平均句长 {r['readability']['avg_len']}（需 {r['readability']['avg_len_range'][0]}-"
                f"{r['readability']['avg_len_range'][1]}）。")
        if not line_count_ok:
            hint += (f" 当前有 {len(lines)} 句，预算允许 {line_count_range[0]}-{line_count_range[1]} 句；"
                     '优先合并同一主锚点下可连读的短句或删除重复推进，不得把无关镜头事实拼成一句。')
        if not r['readability']['story_short_line_ratio_ok']:
            hint += (f" 正文≤{short_line_max_chars}字短句占比 "
                     f"{r['readability']['story_short_line_ratio']:.0%}，高于诊断参考 "
                     f"{max_short_story_line_ratio:.0%}；仅在同一主锚点下能自然连读时合并，"
                     '不因此跨证据合并或补写内容。')
        if r['chars'] < lo:
            hint += (f" 当前比最低字数少至少 {math.ceil(lo - r['chars'])} 字；"
                     '请从尚未使用且有直接证据支持的时间点补充不同事件信息，不要靠形容词、重复句、心理推测或无依据因果凑字数。')
        elif r['chars'] > hi:
            hint += ' 当前超过最高字数，请删去重复信息，保留每句的新事件推进和事实证据。'
        if r['long_lines']:
            overlong = '；'.join(
                f"第{x['line']}句 {x['chars']} 字，必须≤{x['max_chars']} 字"
                for x in r['long_lines'][:8])
            hint += (f' 字幕容量超限：{overlong}。仅压缩这些句子，保留其原有事实与证据绑定；'
                     '不能删掉唯一事实、改动人物归属或把一句拆成两句重复同一主镜头。')
        _sm = r.get('subject_match', {}).get('issues', [])
        if _sm:
            hint += (f" 第{_sm[0]['line']}句主体错配：{_sm[0]['why']}。"
                     f'动作主体必须与所引画面里实际在做这件事的人一致，'
                     f'不要把一方的行为安到另一方头上。')
        _co = r.get('cohesion', {})
        if _co.get('verdict') == 'FAIL':
            hint += (f" 剧情衔接失败：{_co.get('why','')}。优先改写被指出的相邻句，" 
                     '用证据中确实重复的人物、物件、原话或时间顺序承接；不要为了连贯添加未经证实的原因、转折或结果。')
            _pairs = _co.get('disconnected_pairs', [])
            if _pairs:
                pair_txt = '；'.join(
                    f"第{x['from_line']}→{x['to_line']}句（{x.get('from','')} → {x.get('to','')}）"
                    for x in _pairs[:6])
                hint += f' 以下相邻句没有可见承接：{pair_txt}。优先重写箭头右侧的句子，以原句已有证据中的人物/物件或“原来/接着”等时间词接住前句；不要为了衔接添加未经证实的因果或动作。'
        _order = r.get('evidence_order', {})
        if _order.get('verdict') == 'FAIL':
            detail = '；'.join(f"第{x['line']}句：{x['why']}" for x in _order.get('issues', [])[:4])
            body_chain = []
            for line_no, line in enumerate(lines, 1):
                if (line.get('block') or '').strip().lower() in ('hook', 'cta', 'ending_cta'):
                    continue
                primary = line.get('primary_evidence_id')
                source_t = (evidence_rows_by_id.get(primary) or {}).get('t') if primary else None
                if isinstance(source_t, (int, float)) and not isinstance(source_t, bool):
                    body_chain.append((float(source_t), line_no, primary))
            body_chain.sort(key=lambda item: (item[0], item[1]))
            required_order = ' → '.join(f'第{line_no}句({eid}@{source_t:g}s)'
                                        for source_t, line_no, eid in body_chain)
            hint += (f' 镜头时间顺序失败：{detail}。后段钩子可以倒叙，但正文必须回到更早证据后顺时间推进；'
                     f'本次按时间顺序重排正文句对象，保持每句文字、block、evidence_ids、primary_evidence_id 绑定不拆散；'
                     f'钩子仍放首句、CTA 仍放末句。当前可用顺序参考：{required_order}。'
                     '若本句引用了多个事实 ID，可从本句已有 evidence_ids 中改选更合适的 primary_evidence_id，不能新增或挪用事实证据。')
        _dv = r.get('evidence_diversity', {})
        if _dv.get('verdict') == 'FAIL':
            reused = '、'.join(f'{eid}×{count}' for eid, count in _dv.get('over_reused', {}).items())
            reused_lines = []
            for eid, count in _dv.get('over_reused', {}).items():
                rows = [str(i) for i, ln in enumerate(lines, 1) if eid in (ln.get('evidence_ids') or [])]
                if rows:
                    reused_lines.append(f'{eid}（第{"、".join(rows)}句，共{count}次）')
            already_used = {eid for ln in lines for eid in (ln.get('evidence_ids') or [])}
            alternatives = [e for e in ev if e.get('evidence_id') not in already_used]
            alt_text = '；'.join(f"{e.get('evidence_id')}: {str(e.get('desc') or '')[:28]}"
                                 for e in alternatives[:10]) or '（无未使用证据；可改选本稿中仅用过一次的相关证据）'
            hint += (f" 证据重复：{_dv['why']}。超额证据 {reused or '无'}；出现句位："
                     f"{'；'.join(reused_lines) or '见重复证据'}。可替换为尚未使用的证据：{alt_text}。"
                     '重写重复句并换成真正支撑该句的其他证据；全稿至少 6 个不同 ID，任一 ID 最多两次，'
                     '不要为了多样而挂不相关证据。')
        _ground = r.get('grounding', {})
        if _ground.get('verdict') == 'FAIL':
            bad = _ground.get('issues', [])
            if bad:
                issue_txt = '；'.join(
                    f"第{x['line']}句「{x.get('claim','')}」：{x.get('reason','')}"
                    for x in bad[:8])
                hint += (f' 逐句事实核验失败：{issue_txt}。请只保留本句所引帧的人物、动作、字幕与物品；'
                         '删掉证据没有的连续动作、数量、台词归属、动机和因果。')
                hint += (' 事实返修时不得删掉整句或把全文改写成梗概；只替换不支持的局部内容。对白/字幕无法逐字核实时，'
                         '改写为同一句已引用画面证据直接支持的可见细节；若修订后低于字数下限，从这些已引用证据补充不同的新细节，'
                         '同时保证总字数仍在预算内。')
                if any(x.get('line') == 1 for x in bad) and chosen_index + 1 < len(good):
                    chosen_index += 1
                    chosen = good[chosen_index]
                    print(f"      钩子证据不充分，改用候选 {chosen_index + 1}: {chosen['text']}", flush=True)
            elif _ground.get('error'):
                hint += f" 逐句事实核验未完成（{_ground['error']}）；本轮不能交付，重试后仍不可用则停止。"
        _sub = r.get('subtitle_claims', {}).get('issues', [])
        if _sub:
            detail = '；'.join(
                f"第{x['line']}句：{x.get('why','证据不匹配')}；文本「{x.get('claim','')}」；"
                f"引用证据 {','.join(x.get('evidence_ids') or [])} 对应字幕为「{' / '.join(x.get('actual_subtitle') or [])}」"
                for x in _sub[:6])
            hint += f' 字幕/说话者/跨帧证据校验失败：{detail}。只写当前行分配证据直接支持的内容；说话者为画外音或未知时，不要归给画中人物。'
        _sched = r.get('evidence_schedule', {}).get('mismatches', [])
        if _sched:
            m = _sched[0]
            if 'expected_lines' in m:
                hint += (f" 稿件句数目标为 {m['expected_lines']}，允许 {m.get('accepted_line_range')}，"
                         f"当前 {m.get('actual_lines')}；按该范围与字数预算重写，不得挪用不相关镜头。")
            else:
                hint += f" 事实集合与剪辑主镜头分工错误：{m.get('why', '修正 evidence_ids 与 primary_evidence_id')}; 保证 primary_evidence_id 属于本句事实证据，CTA 不带事实引用，并覆盖至少六个真实镜头。"
                hint += ' CTA 是唯一允许不带证据引用的行；aftertaste 属于剧情行，必须引用能直接支持其收束内容的已有证据，不能支持时删除 aftertaste。'
        _mq = r['block_structure'].get('misplaced_questions', [])
        if _mq:
            hint += f" 有 {len(_mq)} 段在非结尾处用了问句（如第{_mq[0]['line']}段）。"
            hint += '问句只能出现在最后一段 cta，其余全用陈述句，悬念靠说一半留一半。'
        if r['block_structure']['verdict'] == 'FAIL':
            bs = r['block_structure']
            hint += f" 段落结构问题：{bs['why']}。"
            hint += (' 必须含唯一 hook、至少两类不同正文推进任务和唯一末尾 cta。'
                     'climax2、twist、aftertaste 仅在证据支持时使用；aftertaste 若出现，必须在最后的高潮/结果后连续收束并紧接 cta。')
        if r['semantic_dup']['dups']:
            dup_lines = sorted({i for dp in r['semantic_dup']['dups']
                                for i in (dp['a_line'], dp['b_line'])})
            hint += (f" 重复句涉及第{'、'.join(map(str, dup_lines))}句；逐句重写成不同的新信息，"
                     '不能把上一句复制到后续行，也不能用重复的 aftertaste 凑字数。')
        if r['gender_issues']:
            g = r['gender_issues'][0]
            hint += (f" 另外第{g['line']}句「{g['text']}」人称用错了：{g['why']}。"
                     f"请先确定人物性别再写，人称必须与所引画面一致。")
        _inf = r.get('inference_labels', {}).get('issues', [])
        if _inf:
            detail = '；'.join(f"第{x['line']}句：{x['why']}（{x.get('text','')}）" for x in _inf[:6])
            hint += (f" 推断门失败：{detail}。优先删掉无证据的动机、心理和因果；确需保留时设 inference=true，并在句中加“也许/像是/仿佛/似乎”，不能把推测写成事实。")
        hint += " 请写成连贯的口语独白，不要罗列式短语。"
        chosen = dict(chosen)
        chosen['retry_hint'] = hint

    if script is None or not accepted:
        failed_lines = (script or {}).get('lines', [])
        failed_report = last_report
        if failed_report is not None:
            failed_report.pop('texts', None)
            failed_report['generation_errors'] = generation_errors
        (TASK / 'script.failed.json').write_text(json.dumps(
            {'status': 'rejected_after_lint', 'attempts': 3,
             'last_nonempty_attempt': last_nonempty_attempt,
             'generation_errors': generation_errors, 'attempt_history': attempt_history,
             'lines': failed_lines,
             'last_lint': failed_report}, ensure_ascii=False, indent=2), encoding='utf-8')
        print('[ERR] 三次生成均未通过质量门；仅保存 script.failed.json，不产生可供剪辑的 script.json', file=sys.stderr)
        return 2

    lines = script['lines']
    report = last_report
    texts = report.pop('texts', [])

    pov = {'gender': pov_gender, 'name': pov_name, 'description': pov_desc}
    out = {
        'soul': soul_name, 'soul_source': SOULS[soul_name]['skill'],
        'pov': pov,
        'hook': chosen, 'budget_chars': budget, 'budget_range': [lo, hi],
        'lines': lines, 'lint': report,
        'generation_errors': generation_errors,
        'soul_check_list': SOULS[soul_name]['check'],
        'caveat': '口播稿由本地 VLM 生成；逐句画面/字幕事实核验已执行，但需人工审叙事质量与修辞。',
    }
    (TASK / 'script.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    (TASK / 'narration.txt').write_text('\n'.join(texts), encoding='utf-8')

    print(json.dumps({'soul': soul_name, 'hook': chosen['text'],
                      'lines': len(lines), 'chars': report['chars'],
                      'estimated_sec': report['estimated_sec'],
                      'in_budget': report['in_budget'],
                      'no_evidence_lines': report['no_evidence']},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)
        safe_report = {
            'error_type': type(exc).__name__,
            'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno,
                        'function': frame.name} for frame in frames],
        }
        try:
            (TASK / 's3_runtime_error.json').write_text(
                json.dumps(safe_report, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
        last = frames[-1].lineno if frames else 0
        print(f'[ERR] S3 未处理异常：{type(exc).__name__}，代码行 {last}；'
              '仅记录脱敏类型和位置，详细内容保留在本机日志。', file=sys.stderr)
        sys.exit(1)
