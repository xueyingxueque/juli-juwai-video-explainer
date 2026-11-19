# -*- coding: utf-8 -*-
r"""learn_from_runs.py —— **学习闭环**：把已出片的质检结论变成可复盘的改进输入。

★ 为什么新增（2026-11-09 审计 + 项目准入标准「内置自我迭代/学习机制」）：
  `config/engine.json` 里 `rework` 段写了 max_rounds / strategies 却**零实现**；
  已交付各集的 qc.json 里反复出现同一条 risk（soft/speech 未校准，CER 0.2685），
  但没有任何机制把它抽出来 → 每次都靠人重新发现。
  → 本脚本扫描全部已交付集的 qc.json / 生产日志，统计**反复出现的风险**，
    生成坑典候选与 rework 建议（只报告、不自动写坑典，避免噪声污染正典）。

输出：`evals/learning_report.json`
  · episodes_scanned / verdict 分布 / hard_failures 汇总
  · recurring_risks（≥2 次）→ pitfall_candidates（带证据文件 + 建议判据）
  · rework_suggestions（把 config.rework.strategies 映射到具体集）
  · eval_trend（读 eval_history.jsonl 的同 id 趋势）

用法：
    python scripts/learn_from_runs.py                # 扫描并写报告
    python scripts/learn_from_runs.py --json         # 同时打印完整 JSON
纪律：只读产物、不跑 GPU、不改已交付成片、不自动写坑典。
"""
import argparse
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVALS = ROOT / 'evals'
CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
TASK_ROOT = Path(CFG['paths']['task_root'])


def norm_risk(s):
    """把同一类风险的不同数值实例归一，才能统计频次。"""
    s = re.sub(r'\d+\.\d+|\d+', '#', str(s))
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:120]


def scan():
    reports, risks, hard = [], Counter(), Counter()
    for qc in sorted(TASK_ROOT.glob('*/out/*/*.qc.json')):
        try:
            d = json.loads(qc.read_text(encoding='utf-8'))
        except Exception:
            continue
        prefix = qc.parent.name
        item = {'prefix': prefix, 'file': str(qc),
                'verdict': d.get('verdict'), 'hard_passed': d.get('hard_passed'),
                'soft_grade': d.get('soft_grade'),
                'soft_unscored': d.get('soft_unscored') or [],
                'risks': d.get('risks') or [],
                'hard_failures': d.get('hard_failures') or []}
        reports.append(item)
        for r in item['risks']:
            risks[norm_risk(r)] += 1
        for h in item['hard_failures']:
            hard[norm_risk(h)] += 1
        for u in item['soft_unscored']:
            risks['未打分软门: ' + norm_risk(u)] += 1
    recurring = [{'risk': r, 'count': n,
                  'candidate_judgement': '同类问题反复出现，应抽成单一判据/脚本并记入坑典'}
                 for r, n in risks.most_common() if n >= 2]
    strategies = (CFG.get('rework') or {}).get('strategies', [])
    suggestions = []
    for it in reports:
        if it['hard_passed'] is False or it['risks']:
            suggestions.append({
                'prefix': it['prefix'],
                'max_rounds': (CFG.get('rework') or {}).get('max_rounds', 3),
                'strategies': strategies,
                'reason': ('hard_failures=' + str(len(it['hard_failures']))
                           if it['hard_passed'] is False else
                           'risks=' + str(len(it['risks']))),
            })
    return reports, recurring, hard, suggestions


def eval_trend():
    p = EVALS / 'eval_history.jsonl'
    if not p.is_file():
        return {'note': '尚无评估历史'}
    rows = []
    for line in p.read_text(encoding='utf-8').splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if len(rows) < 2:
        return {'runs': len(rows), 'note': '至少两轮才有趋势'}
    last, prev = rows[-1], rows[-2]
    common = sorted(set(last.get('by_id', {})) & set(prev.get('by_id', {})), key=int)
    flipped = [int(k) for k in common
               if last['by_id'][k] != prev['by_id'][k]]
    return {'runs': len(rows), 'common_ids': common, 'flipped_ids': flipped,
            'last_pass_rate': last.get('pass_rate'),
            'note': 'flipped_ids 为空 = 同口径下无回归'}


def main():
    ap = argparse.ArgumentParser(description='从已出片质检结论生成学习报告')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--root', default='', help='覆盖任务根（调试用）')
    a = ap.parse_args()
    global TASK_ROOT
    if a.root:
        TASK_ROOT = Path(a.root)

    reports, recurring, hard, suggestions = scan()
    report = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'episodes_scanned': len(reports),
        'verdict_distribution': dict(Counter(r['verdict'] for r in reports)),
        'hard_failures': [{'issue': k, 'count': n} for k, n in hard.most_common(10)],
        'recurring_risks': recurring,
        'pitfall_candidates': [
            {'title': x['risk'], 'occurrences': x['count'],
             'evidence': [r['file'] for r in reports
                          if any(norm_risk(z) == x['risk'] for z in r['risks'])][:5],
             'suggested_judgement': x['candidate_judgement']}
            for x in recurring
        ],
        'rework_suggestions': suggestions,
        'eval_trend': eval_trend(),
        'note': '本报告只作改进输入；坑典条目须经人工/Agent 复核后写入 references/00-pitfalls.md',
    }
    EVALS.mkdir(exist_ok=True)
    (EVALS / 'learning_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f'── 学习报告 · {report["ts"]} ──')
        print(f'  扫描 {report["episodes_scanned"]} 集  verdict={report["verdict_distribution"]}')
        print(f'  反复风险 {len(recurring)} 个：')
        for x in recurring[:6]:
            print(f'    ×{x["count"]}  {x["risk"][:88]}')
        print(f'  rework 建议 {len(suggestions)} 集  eval 趋势 {report["eval_trend"]["note"]}')
        print(f'  → {EVALS / "learning_report.json"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
