# -*- coding: utf-8 -*-
r"""episode_dedupe.py —— **跨成片内容去重闸门**（用户 2026-11-04 令「每集内容不能一样」）。

★ 为什么是新建而不是复用（2026-11-06 三方审计确认）：
  重构前技能里所有「去重」都是**集内**的，跨成片复用比对 = **0 处实现**：
    · `assemble_plan.py` F8 → 单集内不许按集数顺序播放（镜头序纪律）
    · `spec_precheck.py`    → 单集内 CTA 与末段不许重复
    · `quality_gate.py`    → 单集内 cold_open 唯一
    · `anti_dup.py`        → 成片后像素级变换（防平台 pHash 查重），**可选、默认关、主链零调用**
  而「第 2 集不能把第 1 集拉长注水」只写在文档里（F11 / 说明文档.md:185），
  是**人工纪律、无代码闸门** → 人一忙就会重复。

  → 本模块把这条人工纪律变成**可自动判定的硬闸**：
    以磁盘上已写好的历史 `spec-*.json` / gencfg 为账本（不另建第二份账本），
    产第 N 集时与前 N-1 集逐维度比对，超阈值直接 FAIL，不许进证据/配音。

★ 三层去重分层（勿混为一谈）：
  ① **剪辑序列层**（已有，集内）：F8 / series_plan.used_ep —— 管「这一集选哪些镜头、什么顺序」
  ② **帧变换层**（已有，默认关）：anti_dup.py —— 管「选中的帧怎么改像素指纹」
  ③ **跨成片复用层**（本模块，新建）：管「这一集与上一集是不是同一个内容」
  三层互补，不能互相替代；本模块只负责 ③。

判据数值一律来自 `policy.dedupe_policy()`，**禁止在本文件写第二份常量**。

用法：
    python episode_dedupe.py <任务目录> <ep> [--gencfg <当前集.json>] [--pilot <清单.json>]
    python episode_dedupe.py <任务目录> <ep> --json     # 机器可读
退出码：0 = 通过；1 = 内容重复超阈值（逐项列出）；2 = 输入缺失/读不到（保守拒绝）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from policy import dedupe_policy  # noqa: E402  唯一数值源

_PUNCT = re.compile(r'[\s，。！？、；：,.!?;:"“”‘’（）()【】\[\]—…\-]+')
_CJK = re.compile(r'[一-鿿]')


def norm_text(s):
    """规范化文本：去标点空白，只留中日韩文字与字母数字。"""
    return _PUNCT.sub('', s or '').lower()


def shot_keys(spec, bucket):
    """镜头指纹： (源片集号, 时间桶) —— 同一镜头换个 t 小数点不该算新镜头。"""
    keys = set()
    for s in spec.get('shots', []):
        ep, t = s.get('ep'), s.get('t')
        if ep is None or t is None:
            continue
        try:
            keys.add((int(ep), round(float(t) / bucket)))
        except (TypeError, ValueError):
            continue
    return keys


def ngrams(spec, n):
    """解说词 n-gram 集合（跨段拼接，能抓到「整段照搬只改几个字」）。"""
    blob = norm_text(''.join((s.get('text') or '') for s in spec.get('shots', [])))
    if len(blob) < n:
        return {blob} if blob else set()
    return {blob[i:i + n] for i in range(len(blob) - n + 1)}


def cold_open_key(spec):
    """冷开场标识：(ep,t,caption)。无冷开场返回 None。"""
    for s in spec.get('shots', []):
        if s.get('cold_open'):
            return (str(s.get('ep')), str(s.get('t')),
                    norm_text(s.get('caption') or ''))
    return None


def lead_keys(cfg, entry=None):
    """对外可见文案：标题 / 封面主标 / 引导语 / CTA。"""
    if not cfg:
        cfg = {}
    keys = {
        'title': norm_text(cfg.get('title') or ''),
        'cover_main': norm_text(cfg.get('cover_main') or ''),
        'cover_lead': norm_text(cfg.get('cover_lead') or ''),
    }
    if entry:
        keys['cta'] = norm_text(entry.get('cta') or '')
        keys['title'] = keys['title'] or norm_text(entry.get('title') or '')
    return {k: v for k, v in keys.items() if v}


def gen_keys(cfg):
    """二创图标识：prompt + seed。"""
    out = set()
    for g in (cfg or {}).get('gen', []):
        p = norm_text(g.get('prompt') or '')
        seed = str(g.get('seed') or '')
        if p or seed:
            out.add((p, seed))
    return out


def _read_json(p):
    """返回 (data, err)。区分「读不到」与「不存在」，由调用方决定。"""
    try:
        return json.loads(Path(p).read_text(encoding='utf-8')), None
    except Exception as e:
        return None, f'{p}: {e}'


def load_history(task, cur_ep, pilot=None):
    """历史集 = 同 _plan/ 下**集号更早**的 spec-*.json（磁盘即账本）。

    ★ 为什么只比「更早的集」（2026-11-06 实测修正）：
      第一版与「所有其他 spec」双向比对，结果重跑已交付的第 1 集时，
      被第 2 集判成「内容雷同」→ **旧集永远无法重做**。
      按产出顺序单向比对才自洽：第 N 集不得与 1..N-1 集雷同
      （等价于任意两集不雷同），且重跑旧集不受后面新集影响。
      这与「告警/闸门的成立条件必须包含『这件事还需要被做』」同一判据。

    返回 (history, unknown)。unknown 非空表示「有文件但读不到」→ 保守 FAIL。
    """
    plan = Path(task) / '_plan'
    history, unknown = [], []
    try:
        cur_n = int(str(cur_ep))
    except ValueError:
        cur_n = None
    for f in sorted(plan.glob('spec-*.json')):
        m = re.fullmatch(r'spec-(.+)\.json', f.name)
        if not m or m.group(1) == str(cur_ep):
            continue
        label = m.group(1)
        # 非数字集号无法判先后 → 保守纳入（宁可多查，不可漏查）
        if cur_n is not None:
            try:
                if int(label) >= cur_n:
                    continue
            except ValueError:
                pass
        d, err = _read_json(f)
        if err:
            unknown.append(err)          # 读不到 ≠ 没有
        else:
            history.append((label, d, f))
    return history, unknown


def history_gencfg(task, ep_label, pilot):
    """找历史集的 gencfg：优先清单（单一真相源），否则按 prefix 在 _gencfg/ 找。"""
    cands = []
    if pilot:
        for e in pilot:
            if str(e.get('ep')) == str(ep_label) or str(e.get('prefix')) == str(ep_label):
                if e.get('gen'):
                    cands.append(Path(e['gen']))
    if not cands:
        root = Path(task).parent / '_gencfg'
        cands = [root / f'{ep_label}.json', root / f'EP{ep_label}.json']
    for c in cands:
        if c.is_file():
            d, err = _read_json(c)
            return d, err, c
    return None, None, None          # 确实没有 → 跳过该维度（不算失败）


def check(task, ep, cur_cfg, pilot=None):
    """→ (passed: bool, report: dict)。"""
    P = dedupe_policy()
    cur_path = Path(task) / '_plan' / f'spec-{ep}.json'
    cur, err = _read_json(cur_path)
    if err:
        return False, {'fatal': f'当前 spec 读不到：{err}', 'checks': [], 'warnings': []}

    hist, unknown = load_history(task, ep, pilot)
    report = {'current': str(ep), 'history': [h[0] for h in hist],
              'policy': P, 'checks': [], 'warnings': [], 'fatal': None}
    if unknown:
        # ★ 读不到历史文件 = 未知，不能当成「没有历史」放行
        report['fatal'] = '历史 spec 存在但读不到（未知 ≠ 无重复，保守拒绝）：' + '; '.join(unknown)
        return False, report
    if not hist:
        report['warnings'].append('首集（无历史 spec）→ 跨集比对不适用，直接通过')
        return True, report

    cur_shots = shot_keys(cur, P['shot_time_bucket'])
    cur_ng = ngrams(cur, P['ngram'])
    cur_co = cold_open_key(cur)
    cur_lead = lead_keys(cur_cfg, next((e for e in (pilot or [])
                                        if str(e.get('ep')) == str(ep)), None))
    cur_gen = gen_keys(cur_cfg)

    def add(name, ok, detail, level='hard'):
        report['checks'].append({'name': name, 'ok': ok, 'detail': detail, 'level': level})

    for label, old, old_path in hist:
        # ★ 分母取**较小集**（对称判据）：否则「长集把短集整段搬过来再注水」
        #   会被自己的规模稀释掉（实测 T2 98 镜抄 T1 31 镜里的 11 个，
        #   按 98 算只有 11.2% 就放行了，按 31 算是 35.5% 才抓得住）。
        #   这与「实切与度量必须同一判据」同源：换个方向看结果不该变。
        # ① 镜头重叠
        old_shots = shot_keys(old, P['shot_time_bucket'])
        inter = cur_shots & old_shots
        denom = max(1, min(len(cur_shots), len(old_shots)))
        ratio = len(inter) / denom
        limit = P['max_shot_overlap_ratio']
        ok = ratio <= limit
        detail = (f'{len(inter)}/{denom} = {ratio:.1%}'
                  f'（上限 {limit:.0%}，分母取较小集）')
        if not ok:
            # ★ 零容忍时把**具体重复镜头**报出来，否则写稿的人不知道该换哪几段
            if limit == 0 and inter:
                sample = sorted(inter)[:6]
                detail += ' 重复镜头(源集@秒桶): ' + ', '.join(
                    f'{e}@{t}s' for e, t in sample)
            detail += '  → 这些画面已在前面集用过，须换源片集号或时段'
        add(f'镜头重叠 vs {label}', ok, detail)

        # ② 解说词重叠（n-gram）
        old_ng = ngrams(old, P['ngram'])
        ti = cur_ng & old_ng
        tdenom = max(1, min(len(cur_ng), len(old_ng)))
        tr = len(ti) / tdenom
        ok = tr <= P['max_text_overlap_ratio']
        add(f'解说词重叠 vs {label}', ok,
            f'{len(ti)}/{tdenom} = {tr:.1%}'
            f'（上限 {P["max_text_overlap_ratio"]:.0%}，分母取较小集）'
            + ('' if ok else '  → 重写雷同段落，换叙事切口/因果链'))

        # ③ 冷开场不得复用
        if P['require_unique_cold_open'] and cur_co:
            ok = cold_open_key(old) != cur_co
            add(f'冷开场唯一 vs {label}', ok,
                '未复用 ✓' if ok else f'与历史集用了同一个名场面 {cur_co[:2]} → 换高潮片段')

        # ④ 标题/封面主标/引导语/CTA 不得复用
        if P['require_unique_lead']:
            old_cfg, cfg_err, cfg_path = history_gencfg(task, label, pilot)
            if cfg_err:
                add(f'文案唯一 vs {label}', False, f'历史 gencfg 读不到：{cfg_err}')
            elif old_cfg is None:
                report['warnings'].append(f'{label}: 无历史 gencfg → 标题/CTA 维度未检查')
            else:
                old_lead = lead_keys(old_cfg, next((e for e in (pilot or [])
                                                    if str(e.get('ep')) == str(label)
                                                    or str(e.get('prefix')) == str(label)), None))
                dup = sorted(k for k, v in cur_lead.items()
                             if v and old_lead.get(k) == v)
                add(f'文案唯一 vs {label}', not dup,
                    '未复用 ✓' if not dup else f'重复字段 {dup} → 换标题/主标/CTA')

        # ⑤ 二创图 prompt/seed 不得复用
        if P['require_unique_gen'] and cur_gen:
            old_cfg, cfg_err, _ = history_gencfg(task, label, pilot)
            if old_cfg is not None and not cfg_err:
                dup = cur_gen & gen_keys(old_cfg)
                add(f'二创图唯一 vs {label}', not dup,
                    '未复用 ✓' if not dup else f'{len(dup)} 张图用了相同 prompt/seed → 换画面描述')

    failed = [c for c in report['checks'] if not c['ok']]
    report['passed'] = not failed and not report['fatal']
    report['failed_count'] = len(failed)
    return report['passed'], report


def main():
    ap = argparse.ArgumentParser(description='跨成片内容去重闸门（每集内容不能一样）')
    ap.add_argument('task')
    ap.add_argument('ep')
    ap.add_argument('--gencfg', default='', help='当前集 gencfg json')
    ap.add_argument('--pilot', default='', help='待产清单 episodes.json（提供历史 gencfg 路径）')
    ap.add_argument('--json', action='store_true', help='输出机器可读 JSON')
    a = ap.parse_args()

    cur_cfg, err = (None, None)
    if a.gencfg:
        cur_cfg, err = _read_json(a.gencfg)
    pilot = None
    if a.pilot:
        pilot, err2 = _read_json(a.pilot)
        if err2:
            print(f'[FAIL] 清单读不到（未知 ≠ 没有）：{err2}')
            return 2

    if err:
        print(f'[FAIL] 当前 gencfg 读不到：{err}')
        return 2
    if not Path(a.task, '_plan', f'spec-{a.ep}.json').is_file():
        print(f'[FAIL] 找不到 spec-{a.ep}.json（写稿是第一步，别跳过）')
        return 2

    passed, rep = check(a.task, a.ep, cur_cfg, pilot)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if passed else 1

    print(f'── 跨集去重 · ep{a.ep} ──')
    print(f'  历史集: {rep["history"] or "无（首集）"}')
    for w in rep['warnings']:
        print(f'  [warn] {w}')
    if rep['fatal']:
        print(f'  [FAIL] {rep["fatal"]}')
    for c in rep['checks']:
        print(f'  {"✓" if c["ok"] else "✗"} {c["name"]}: {c["detail"]}')
    if passed:
        print('\n[OK] 每集内容互不重复 ✓')
        return 0
    print(f'\n[FAIL] {rep["failed_count"]} 项重复超阈值 —— 本集内容与历史集雷同，禁止进生产')
    return 1


if __name__ == '__main__':
    sys.exit(main())
