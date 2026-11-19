"""故事线重排 —— 从事件库推导系列结构。

★ 核心原则（用户明确要求）：
  「解说稿应该多集视频梳理出一个故事线进行写，而不是按视频顺序，
    应该是重新梳理故事线，本来为了原创度就要重新混剪原视频」

因此本模块**不按集号排序**，而是：
  1. 按张力挑出主线高潮点
  2. 用「因果链」把这些点串成故事线（不是时间轴）
  3. 高潮前置 —— 开场直接放张力最高的那一点（倒叙钩子）
  4. 拆成系列：每条聚焦一个主线高潮

产出：series-plan.json（系列结构 + 每条的混剪清单）
"""
import json
import sys
from pathlib import Path


def load_events(events_json: Path) -> list:
    d = json.loads(events_json.read_text(encoding='utf-8'))
    return d.get('events', [])


def pick_beats(events: list) -> list:
    """挑主线高潮点。

    规则（按优先级）：
    1. L3 高潮事件优先
    2. 同分时取张力分高的
    3. **同一集最多取 1 个**，避免整集照搬（保原创度）
    4. 相邻集（±1）不重复取，保证节奏
    """
    cands = [e for e in events if e['tension']['level'] >= 2]
    cands.sort(key=lambda x: -x['tension']['score'])

    beats, used_ep = [], set()
    for e in cands:
        ep = e['ep']
        if ep in used_ep:
            continue
        # 同集相邻 ±1 已有beat则跳过（保证跨集取样）
        if any(abs(ep - u) <= 1 for u in used_ep):
            continue
        beats.append(e)
        used_ep.add(ep)
    return beats


def infer_arc(beats: list, all_events: list) -> list:
    """推断故事线走向 —— 把零散高潮点串成有因果的叙事弧。

    做法：以集号为"时间坐标"判断剧情阶段，但**输出顺序按张力排**。
    这样既保留剧情逻辑（能看出这是同一条线），
    又实现「不按视频顺序」的要求。

    阶段划分（按集号在总集中的相对位置）：
    - 起（0-20%）   建立世界、人物、动机
    - 承（20-50%） 冲突升级、遇险
    - 转（50-80%） 反转、真相揭示
    - 合（80-100%） 决战、收束
    """
    eps = sorted({e['ep'] for e in all_events})
    if not eps:
        return []
    lo, hi = min(eps), max(eps)
    span = max(hi - lo, 1)

    out = []
    for b in beats:
        pos = (b['ep'] - lo) / span
        if pos < 0.2:
            phase = '起'
        elif pos < 0.5:
            phase = '承'
        elif pos < 0.8:
            phase = '转'
        else:
            phase = '合'
        out.append({**b, 'phase': phase, 'pos': round(pos, 3)})
    return out


def build_series(arc: list, n_series: int = 5) -> list:
    """拆成系列 —— 每条聚焦一个主线高潮。

    编排逻辑（★ 关键：高潮前置）：
    每条的叙事顺序 = [该条最高潮] → [铺垫] → [展开] → [收束]
    即**开场直接放最炸的那一刻**，这是真实爆款的倒叙钩子做法。
    """
    if not arc:
        return []
    # 按张力分组，每组取 1-2 个点作为一条
    groups = []
    for i in range(n_series):
        start = i * len(arc) // n_series
        end = (i + 1) * len(arc) // n_series
        g = arc[start:end] if end > start else []
        if g:
            groups.append(g)

    series = []
    for i, g in enumerate(groups, 1):
        # 高潮前置：组内张力最高的排第一
        g = sorted(g, key=lambda x: -x['tension']['score'])
        peak = g[0]
        series.append({
            'index': i,
            'title_hint': f"第{i}条 · 高潮在第{peak['ep']}集",
            'hook_source': {
                'event_id': peak.get('id'),
                'ep': peak['ep'], 't': peak['t'],
                'dialogue': peak['dialogue'],
                'event': peak['event'],
                'why': f"张力 {peak['tension']['score']:.2f}，全剧第 {peak['phase']} 阶段最强画面",
            },
            'phase': peak['phase'],
            # Keep the source event key: downstream curation must be able to
            # trace every planned beat back to events.json without matching
            # on dialogue text or approximate timestamps.
            'beats': [{'id': b.get('id'), 'ep': b['ep'], 't': b['t'], 'event': b['event'],
                       'dialogue': b['dialogue'], 'speaker': b['speaker'],
                       'tension': b['tension']} for b in g],
            'n_shots': len(g),
        })
    return series


def coverage(events: list) -> dict:
    """覆盖率统计 —— 分解完整度与张力分布。"""
    eps = sorted({e['ep'] for e in events})
    lv = {0: 0, 1: 0, 2: 0, 3: 0}
    for e in events:
        lv[e['tension']['level']] = lv.get(e['tension']['level'], 0) + 1
    return {
        'eps_covered': len(eps),
        'ep_range': [min(eps), max(eps)] if eps else [0, 0],
        'events': len(events),
        'levels': lv,
        'climax_ratio': round(lv.get(3, 0) / max(len(events), 1), 3),
    }


def main() -> int:
    events_json = Path(sys.argv[1])
    out_json = Path(sys.argv[2]) if len(sys.argv) > 2 else events_json.parent / 'series-plan.json'
    n_series = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    events = load_events(events_json)
    cov = coverage(events)
    print(f"事件库：{cov['events']} 个事件，覆盖第 {cov['ep_range'][0]}-{cov['ep_range'][1]} 集")
    print(f"张力分布：高潮{cov['levels'][3]} 冲突{cov['levels'][2]} 铺垫{cov['levels'][1]} 未评级{cov['levels'][0]}")

    beats = pick_beats(events)
    print(f'\n主线高潮点：{len(beats)} 个（同集去重 + 相邻集跳过）')
    arc = infer_arc(beats, events)
    series = build_series(arc, n_series)
    print(f'系列结构：{len(series)} 条')

    plan = {'coverage': cov, 'beats': arc, 'series': series,
            'note': '★ 叙事顺序按张力排，不按集号；每条开场即该条最高潮（倒叙钩子）'}
    out_json.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')

    print('\n=== 系列结构 ===')
    for s in series:
        h = s['hook_source']
        print(f"\n{s['title_hint']}  [{s['phase']}阶段]  {s['n_shots']} 个镜头")
        print(f"  开场钩子（第{h['ep']}集 {h['t']}s）: {h['event'][:46]}")
        if h['dialogue']:
            print(f"  原声候选：「{h['dialogue'][:26]}」")
    print(f'\n[out] {out_json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
