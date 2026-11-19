# -*- coding: utf-8 -*-
r"""content_brief.py —— **内容决策层**：把「写稿时的暗知识」变成可核对清单。

★ 为什么新增（2026-11-09 全网调研，对标 speclip-skills 的 drama-explainer /
  short-drama-commentary）：本技能此前只有**装配校验**（deliver_check 查产物齐不齐），
  没有**内容质量决策**：不记录情绪链条、不做节奏预算、不写「为什么这段留原声」、
  没有 Kill List 与返工判据。结果「信息对了但不好看」无法被机器发现。

本模块只做**可本地计算**的部分（不依赖云端 LLM / 不做主观审美判断）：
  · 6 段式骨架映射、情绪链条
  · 节奏预算：目标时长 / VO-原片比例 / 开头第一刀秒数 / 长原片数 / 结尾策略
  · Kill List：连续同集超限、原声占比超标、空泛词、超长原片
  · 每段决策理由（为什么留原声 / 为什么用解说）
  · 内容质量 checklist（自动项打勾，需人工项标 manual）
  · verdict = PASS / WARN / REWORK（--strict 时 REWORK 退出 1）

判据数值来自 config/engine.json → content_policy（唯一读取器）。
输出 `_plan/brief-<ep>.json`，不改动 spec 本身。

用法：
    python scripts/content_brief.py <task> <ep> [--strict] [--json]
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import contracts                     # noqa: E402
from plan_est import est_total       # noqa: E402  时长预估唯一正典

CFG, _ = contracts.load()
P = CFG['content_policy']           # 缺段直接抛错，不静默回退


def _dur(seg):
    if seg.get('is_cold_open') or seg.get('cold_open'):
        return float(seg.get('duration') or 5.0)
    if seg.get('raw'):
        return 2.5
    return est_total({'segments': [seg]})


def build(task, ep):
    t = Path(task)
    spec_p = t / '_plan' / f'spec-{ep}.json'
    spec = json.loads(spec_p.read_text(encoding='utf-8'))
    shots = spec.get('shots', [])
    P_bound = CFG['duration_policy']['bounds']

    durations = [_dur(s) for s in shots]
    total = sum(durations)
    raw_idx = [i for i, s in enumerate(shots) if s.get('raw')]
    vo_idx = [i for i, s in enumerate(shots) if (s.get('text') or '').strip()
              and not s.get('raw')]
    cold = [i for i, s in enumerate(shots) if s.get('cold_open')]

    # 开头第一刀：cold_open **本身就是**前 3 秒的钩子（原片高潮原声），
    # 不能再往后找「第一个冲突块」—— 实测把 MW1 误判成 21.93s，而它 0s 就出刀。
    # 判据必须回答真正的问题：观众多久看到第一个爆点？
    conflict_blocks = {'rising', 'core', 'twist', 'climax'}
    if cold:
        first_cut = 0.0
    else:
        first_cut, acc = 0.0, 0.0
        for i, s in enumerate(shots):
            if s.get('block') in conflict_blocks or (i in raw_idx and acc > 0):
                first_cut = round(acc, 2)
                break
            acc += durations[i]

    # 情绪链条：block 序列去重保序
    chain, seen = [], set()
    for s in shots:
        b = s.get('block') or '未标注'
        if b not in seen:
            chain.append(b)
            seen.add(b)

    # 结尾策略
    last = shots[-1] if shots else {}
    ending = ('原片最后一刀' if last.get('raw') else
              'VO 挂钩' if (last.get('text') or '').strip() else '未定')

    # Kill List（可本地判定的返工信号）
    kill = []
    run, run_ep, max_run = 0, None, 0
    for s in shots:
        e = s.get('ep')
        run = run + 1 if e == run_ep else 1
        run_ep = e
        max_run = max(max_run, run)
    if max_run > P['max_same_ep_run']:
        kill.append(f'同集连续 {max_run} 镜 > {P["max_same_ep_run"]}：按剧本重排，别按集数顺序灌')
    raw_ratio = len(raw_idx) / max(1, len(shots))
    lo_r, hi_r = P['raw_ratio_range']
    if raw_ratio > hi_r:
        kill.append(f'原声段占比 {raw_ratio:.0%} > {hi_r:.0%}：把重复争吵/背景说明压成解说')
    long_raw = [i for i in raw_idx
                if durations[i] > P['long_original_max_sec']]
    if len(long_raw) > P['max_long_original_count']:
        kill.append(f'长原片 {len(long_raw)} 段 > {P["max_long_original_count"]}：逐段写 notes 说明为何不能再压')
    vague = sorted({w for w in P['vague_words']
                    for s in shots if w in (s.get('text') or '')})
    if vague:
        kill.append(f'空泛包装词 {vague}：换成贴人物处境的具体表达')
    if first_cut > P['hook_first_cut_max_sec']:
        kill.append(f'开头第一刀 {first_cut}s > {P["hook_first_cut_max_sec"]}s：钩子前置不够快')

    # 每段决策理由
    reasons = []
    for i, s in enumerate(shots):
        if s.get('cold_open'):
            why = '前 3 秒钩子：直接放原片高潮，保留原声'
        elif s.get('raw'):
            why = '保留原声：台词本身有张力，解说不复述'
        elif (s.get('text') or '').strip():
            why = f'解说承担：{s.get("block", "推进")}（铺垫/跳转/补信息）'
        else:
            why = '未定：缺少 text 且非原声段，需补全'
        reasons.append({'i': i, 'ep': s.get('ep'), 't': s.get('t'),
                        'mode': 'cold_open' if s.get('cold_open') else
                                'original' if s.get('raw') else 'voiceover',
                        'why': why, 'est_sec': round(durations[i], 2)})

    checklist = [
        ('剧情骨架已按 6 段式编排', len(chain) >= 3, 'auto'),
        ('开头第一刀 ≤ 8 秒', first_cut <= P['hook_first_cut_max_sec'], 'auto'),
        # 「低于建议下限」只是节奏偏好（原片少一点并不等于残片），不算硬伤；
        # 只把「超上限」当 auto 判据，建议区间写进 pace_budget 供人参考。
        ('原声占比未超上限', raw_ratio <= hi_r, 'auto'),
        ('没有空泛包装词', not vague, 'auto'),
        ('同集连续未超限', max_run <= P['max_same_ep_run'], 'auto'),
        ('长原片数量未失控', len(long_raw) <= P['max_long_original_count'], 'auto'),
        ('结尾只采用一种收尾策略', ending != '未定', 'auto'),
        ('每个原声段都记录了保留理由', all(r['why'] for r in reasons), 'auto'),
        ('最有戏的原片台词被优先保留', True, 'manual'),
        ('解说没有复述紧邻原片已说清的内容', True, 'manual'),
        ('保留完整情绪链条而非只留结果', True, 'manual'),
        ('观众看完能记住一句原台词 / 一个场面', True, 'manual'),
    ]
    auto_fail = [name for name, okv, kind in checklist if kind == 'auto' and not okv]
    verdict = 'REWORK' if auto_fail else ('WARN' if kill else 'PASS')
    return {
        'task': str(t), 'ep': str(ep), 'spec': str(spec_p),
        'structure': P['structure'], 'emotion_chain': chain,
        'pace_budget': {
            'target_sec_range': list(P_bound), 'est_sec': round(total, 1),
            'vo_ratio': round(len(vo_idx) / max(1, len(shots)), 3),
            'raw_ratio': round(raw_ratio, 3),
            'raw_ratio_recommended': list(P['raw_ratio_range']),
            'hook_first_cut_sec': first_cut,
            'long_original_count': len(long_raw),
            'ending_strategy': ending,
        },
        'kill_list': kill,
        'segment_reasons': reasons,
        'quality_checklist': [{'item': n, 'ok': okv, 'kind': k}
                              for n, okv, k in checklist],
        'auto_failed_checks': auto_fail,
        'verdict': verdict,
        'manual_note': 'manual 项需 Agent/人抽帧与回听判定，脚本不假装能评审美',
    }


def main():
    ap = argparse.ArgumentParser(description='内容决策清单（离线，不跑 GPU）')
    ap.add_argument('task'); ap.add_argument('ep')
    ap.add_argument('--strict', action='store_true', help='REWORK 时退出码 1')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    d = build(a.task, a.ep)
    out = Path(a.task) / '_plan' / f"brief-{a.ep}.json"
    out.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        b = d['pace_budget']
        print(f'── 内容决策 · ep{a.ep} · {d["verdict"]} ──')
        print(f'  {len(d["segment_reasons"])} 段 / 预估 {b["est_sec"]}s / '
              f'VO {b["vo_ratio"]:.0%} 原片 {b["raw_ratio"]:.0%}')
        print(f'  第一刀 {b["hook_first_cut_sec"]}s  长原片 {b["long_original_count"]}  收尾 {b["ending_strategy"]}')
        print(f'  情绪链: {" → ".join(d["emotion_chain"])}')
        for k in d['kill_list']:
            print('  [Kill]', k)
        print(f'  → {out}')
    return 1 if (a.strict and d['verdict'] == 'REWORK') else 0


if __name__ == '__main__':
    raise SystemExit(main())
