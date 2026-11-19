"""plan 组装器 —— 把 shot_evidence 的 ev-N.json 组装成 build.py 可用的 plan.json。

★ 为什么单独一个脚本（2026-10-21 源头优化）：
  plan 的每个段都必须挂 event_id，而事件来源有三处：
    1. series_decompose 的事件库（id `001-f02`）
    2. shot_evidence 新读帧（id `X001-0.30`）
    3. shot_evidence ASR 原声行（id `R001-13.84`）
  手工拼 plan 必然漏挂或挂错 → validate_plan 报错才发现。
  本脚本一次做齐：合并事件 → 生成 segments → 挂 CTA → 跑校验，任何一步失败直接退出。

用法：
  python assemble_plan.py <ev-N.json> --index 1 --name <片名> --cta <结尾文案>
输出（与 ev 同目录）：
  plan-N.json        —— build.py 的输入
  plan-events-N.json —— 合并后的本片事件库（build --events 用）
"""
import argparse
import json
import re
import sys
from pathlib import Path

SKILL = Path(r"C:/Users/Administrator/.workbuddy/skills/剧里剧外-视频解说引擎")
sys.path.insert(0, str(SKILL / 'scripts'))
from plan_story import validate_plan  # noqa: E402
from policy import check_estimate, MIN_SEC, MAX_SEC  # noqa: E402  时长规范唯一真相源

# ★ v3.7.1 冷开场钩子上限（与 engine.json compliance.cold_open.max_sec 同源）
try:
    _CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
    COLD_OPEN_MAX = float((_CFG.get('compliance', {}).get('cold_open') or {}).get('max_sec', 5.0))
except Exception:
    COLD_OPEN_MAX = 5.0

# ★ 时长预估口径唯一正典在 scripts/plan_est.py（v3.6.0 治「两处副本改一处漏一处」）
try:
    from plan_est import est_total as _est_total
except ImportError:  # 副本在任务目录跑时按技能绝对路径兜底
    sys.path.insert(0, str(SKILL / 'scripts'))
    from plan_est import est_total as _est_total

HERE = Path(__file__).resolve().parent
SERIES_EVENTS = HERE.parent / 'events.json'

# ── block 曲线：按位置给叙事角色（lint/shotmap 消费，不影响成片参数）──
def block_for(i: int, n: int) -> str:
    if i == 0:
        return 'hook'
    r = i / max(n - 1, 1)
    if r < 0.18:
        return 'setup'
    if r < 0.38:
        return 'rising'
    if r < 0.62:
        return 'core'
    if r < 0.78:
        return 'twist'
    if r < 0.92:
        return 'climax'
    return 'ending'


TONE_TENSE = ['撞破', '证据', '抵押', '冻结', '伪造', '转账', '账单', '念', '摊牌',
              '撤销', '保全', '拦截', '签字', '签名', '合同', '流水', '报警', '律师']
TONE_SOMBER = ['哭', '雨', '病', '委屈', '可怜', '失踪', '淋', '住院', '养老钱']
TONE_DEEP = ['凭什么', '五年', '结婚', '人生', '选择', '加倍', '还回去', '纪念日',
             '信任', '分居']
TONE_EXCITED = ['当众', '所有人', '主动权', '解气', '照单全收', '见证', '吐出来']


def tone_for(text: str) -> str:
    for w in TONE_TENSE:
        if w in text:
            return 'tense'
    for w in TONE_SOMBER:
        if w in text:
            return 'somber'
    for w in TONE_DEEP:
        if w in text:
            return 'deep'
    for w in TONE_EXCITED:
        if w in text:
            return 'excited'
    return 'tense'


# 角色条幅（如「林薇 | 女主」）不是台词，不能当 dialogue 灌进事件库
_LABEL = re.compile(r'^.{1,12}\s*\|\s*.{1,12}$')


def clean_dialogue(s: str) -> str:
    s = (s or '').strip()
    return '' if _LABEL.match(s) else s


def load_series_events() -> dict:
    # ★ 无 events.json 时不报错：ev 自身每个镜头都会进 extra 分支，
    #   小剧（<10 集）不必先跑 series_decompose，ev 即事件库。
    #   （2026-10-21 回归修复：同步副本时勿覆盖此降级逻辑）
    if not SERIES_EVENTS.is_file():
        return {}
    data = json.loads(SERIES_EVENTS.read_text(encoding='utf-8'))
    out = {}
    for row in data.get('events', []):
        if isinstance(row, dict) and isinstance(row.get('id'), str):
            out[row['id']] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('ev', help='shot_evidence 输出的 ev-N.json')
    ap.add_argument('--index', type=int, required=True, help='第几部成片（series_index）')
    ap.add_argument('--name', required=True, help='成片文件名（plan name）')
    ap.add_argument('--cta', required=True, help='结尾 CTA 文案')
    args = ap.parse_args()

    ev = json.loads(Path(args.ev).read_text(encoding='utf-8'))
    # ★ 入口校验（v3.6.0 修）：ev 空列表/字段缺失原来裸 IndexError/KeyError，
    #   现在给可读错误——「拒绝组装」必须说清为什么
    if not isinstance(ev, list) or not ev:
        print(f'[FAIL] ev 文件为空或不是数组: {args.ev}'
              f'（先跑 shot_evidence.py 生成 ev-N.json）')
        return 2
    for i, s in enumerate(ev):
        if not isinstance(s, dict) or 'ep' not in s or 'frame_t' not in s:
            print(f'[FAIL] ev[{i}] 缺 ep/frame_t 字段: {json.dumps(s, ensure_ascii=False)[:120]}')
            return 2
    events = load_series_events()
    extra: list[dict] = []

    segments = []
    cold_segs = []   # ★ v3.7.1 冷开场钩子统一收集，循环后前置到成片第 1 段
    for i, s in enumerate(ev):
        ep, t = int(s['ep']), float(s['frame_t'])
        eid = s['id']
        # ★ v3.7.1 冷开场钩子：原片高潮片段，无解说，强制位于成片第 1 段。
        #   复用 raw=1 的「无解说→铺原声」结构，但 duration 由人工设定（≤COLD_OPEN_MAX），
        #   豁免普通原声段的「必须带 dialogue」硬要求（validate_plan 同步豁免）。
        if s.get('is_cold_open') or s.get('cold_open') or s.get('block') == 'cold_open':
            cd = float(s.get('duration') or COLD_OPEN_MAX)
            if cd <= 0 or cd > COLD_OPEN_MAX:
                raise SystemExit(f'[FAIL] cold_open 段时长必须 ∈(0, {COLD_OPEN_MAX}]s，'
                                 f'收到 {cd}（spec 的 duration 字段）')
            cid = eid if eid else f'CO{ep:03d}-{t:.2f}'
            if cid not in events:
                extra.append({'id': cid, 'ep': ep, 't': t, 'event': '(冷开场原片钩子)',
                              'dialogue': '', 'speaker': '', 'frame_who': ''})
            cold_segs.append({'ep': ep, 't': t, 'text': '', 'raw': 1,
                             'event_id': cid, 'primary_evidence_id': cid,
                             'evidence_ids': [cid],
                             'block': 'cold_open', 'tone': 'excited', 'inference': False,
                             'is_cold_open': True, 'duration': round(cd, 2),
                             'keep_audio': bool(s.get('keep_audio', True)),
                             'caption': (s.get('caption') or '').strip()})
            continue
        if s.get('raw'):
            # 原声段：事件必须带 dialogue（validate_plan 对 raw 段的硬要求）
            if eid not in events:
                extra.append({'id': eid, 'ep': ep, 't': t,
                              'event': '(原声引用)',
                              'dialogue': (s.get('dialogue') or '').strip(),
                              'speaker': '角色对白'})
            segments.append({'ep': ep, 't': t, 'text': '', 'raw': 1,
                             'event_id': eid, 'primary_evidence_id': eid,
                             'evidence_ids': [eid],
                             'block': block_for(i, len(ev)),
                             'tone': 'tense', 'inference': False})
            continue
        if eid not in events:
            extra.append({'id': eid, 'ep': ep, 't': t,
                          'event': (s.get('desc') or '')[:60],
                          'dialogue': clean_dialogue(s.get('dialogue')),
                          'speaker': s.get('speaker', ''),
                          'frame_who': (s.get('frame_who') or '')[:50]})
        text = (s.get('text') or '').strip()
        if not text:
            raise SystemExit(f'segments[{i}] ({eid}) 解说文案为空 —— 拒绝组装空段')
        segments.append({'ep': ep, 't': t, 'text': text, 'raw': 0,
                         'event_id': eid, 'primary_evidence_id': eid,
                         'evidence_ids': [eid],
                         'block': block_for(i, len(ev)),
                         'tone': tone_for(text), 'inference': False})

    # ── 冷开场钩子统一前置（无论 spec 里顺序如何，都放成片第 1 段）──
    # ★ v3.7.1：cold_open 必须位于成片开头才起「前5秒吸引」作用；
    #   若用户把 cold_open 写在 spec 中间，这里强制搬到 segments[0]。
    if cold_segs:
        segments = cold_segs + segments

    # ── 剧本驱动重排校验（用户 2026-10-24 铁律：不许按集数顺序播放） ──
    #   按集数顺序灌素材的特征：ep 序列单调不回跳 + 单 ep 连续 ≥5 段。
    #   剧本重排（block 曲线驱动）的特征：ep 跳跃/回跳，每集只取 2-4 个镜头。
    #   F1 实测 ep=[1×4,2×4,4×3,5×4] 单调但 max_run=4 → 合规放行。
    import itertools
    eps = [s['ep'] for s in segments]
    max_run = max(len(list(g)) for _, g in itertools.groupby(eps))
    monotonic = all(b >= a for a, b in zip(eps, eps[1:]))
    if monotonic and max_run >= 5:
        print(f'[FAIL] ep 序列单调不回跳且单集连续 {max_run} 段 —— 疑似按集数顺序播放。'
              f'铁律（用户 2026-10-24）：镜头序必须由解说剧本（hook→…→cta block 曲线）'
              f'驱动跨集跳选，每集只取最有张力的 2-4 个镜头。重排 ev 顺序再 assemble。')
        return 5

    # ── CTA：不挂事实证据，只借用上一个主镜头做画面锚（script_to_plan 同款约定）──
    # ★ CTA 去重硬闸（2026-10-22 实测踩坑）：F1/F2/F3 三集的 ev ending 文案都
    #   自带「点个关注」尾巴，与 --cta 重复 → 成片「一句话说两遍」（用户铁律）。
    #   CTA 挂上前先剥掉关注引导比主干：ev 末段与 CTA 主干互相包含即拒绝。
    last_text = (segments[-1].get('text') or '').strip()
    if last_text:
        # ★ 关注引导变体全覆盖（v3.6.0 修）：点[赞个些]?关注/给个关注/关注我；
        #   提取「钩子主干」= 移除关注词后最长的连续文本段（split 方向坑：
        #   关注词在开头时 split[0] 是空串）；主干≥3字才判重，防「点关注」
        #   单独成句时误报
        def _cta_core(t):
            t = re.sub(r'点[赞个些]?关[注汪]|给[个些]?关[注汪]|关注我', '|', t)
            parts = [p.strip('。，,！! ') for p in t.split('|') if p.strip('。，,！! ')]
            return max(parts, key=len) if parts else ''
        cta_core, ev_core = _cta_core(args.cta), _cta_core(last_text)
        if len(cta_core) >= 3 and len(ev_core) >= 3 and (cta_core in ev_core or ev_core in cta_core):
            print(f'[FAIL] CTA 与 ending 段文案重复：ev 末段「{ev_core}」vs CTA「{cta_core}」。'
                  f'关注引导只留一处——改 ev 的 text（去掉关注尾巴）或改 --cta，二选一。')
            return 3
    last_primary = segments[-1]['event_id']
    last = segments[-1]
    segments.append({'ep': last['ep'], 't': last['t'], 'text': args.cta, 'raw': 0,
                     'event_id': last_primary, 'primary_evidence_id': '',
                     'evidence_ids': [], 'visual_anchor_only': True,
                     'block': 'cta', 'tone': 'deep', 'inference': False})

    plan = {'name': args.name, 'series_index': args.index, 'segments': segments}
    merged = {**events, **{e['id']: e for e in extra}}
    # ★ 版权门预算预警（2026-10-21 实测踩坑）：原声段数 >25% 时大概率
    #   original_audio_ratio>0.2 FAIL（D1 15/31→0.37 / D2 16/27→0.53）。
    #   段数比是上限代理指标，超了先削原声再 build，别等质检。
    n_raw = sum(1 for s in segments if s['raw'])
    if n_raw / len(segments) > 0.25:
        print(f'[警告] 原声段 {n_raw}/{len(segments)}（{n_raw/len(segments):.0%}）'
              f' > 25% 预算 —— 版权门 original_audio_ratio≤20% 大概率 FAIL，建议削原声')
    normalized, errors = validate_plan(plan, merged, 0.75)
    if errors:
        print('[FAIL] plan 校验未过:')
        for e in errors:
            print('  -', e)
        return 2

    n_raw = sum(1 for s in normalized['segments'] if s['raw'])
    print(f'[OK] {len(normalized["segments"])} 段（解说 '
          f'{len(normalized["segments"]) - n_raw} / 原声 {n_raw}）'
          f'  新增事件 {len(extra)} 个，复用 {len(normalized["segments"]) - len(extra)} 个')

    # ★ 时长规范（用户 2026-11-04 令 1-5 分钟）：数值正典 = policy.py。
    #   plan 层用**预估**拦明显跑偏的稿（差太远就别白采证据/白配音）；
    #   真正的硬门在成片**实测**（quality_gate / deliver_check），
    #   因为估算有 ±12% 偏差，用估算 REJECT 会误杀（铁律 F5）。
    est = _est_total(normalized)
    if est < MIN_SEC * 0.5 or est > MAX_SEC * 1.5:
        print(f'[FAIL] 预估 {est:.0f}s 严重偏离 {MIN_SEC:.0f}-{MAX_SEC:.0f}s 规范'
              f'（允许 ±50% 估算误差）——先改稿再往下走。')
        return 4
    ok, msg = check_estimate(est)
    print(f'[时长] {msg}' + ('' if ok else '  [warn] 接近边界，实测可能越界'))

    plan_path = HERE / f'plan-{args.index}.json'
    ev_path = HERE / f'plan-events-{args.index}.json'
    plan_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2),
                         encoding='utf-8')
    ev_path.write_text(json.dumps({'events': list(merged.values())},
                                  ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'  plan  → {plan_path}')
    print(f'  events→ {ev_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
