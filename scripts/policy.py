# -*- coding: utf-8 -*-
r"""policy.py —— **规范数值的唯一真相源**（时长 / 跨集去重）。

★ 为什么必须有这个模块（2026-11-06 重构，坑典 P83/P84 同族病）：
  重构前「成片时长合规」这一条规则被写了 **5 份**：
    · config/engine.json      四平台 platform_profiles.*.duration = [60,900]
    · spec_precheck.py:35      TIER_RANGE = {"hook":..,"mid":..,"long":..}
    · deliver_check.py:36      与上一行**逐字相同**的第二份 TIER_RANGE
    · quality_gate.py:99       `if dur > 15*60`（写死 900）
    · _task_template/assemble_plan.py:251-262  `est<60 / est>900`
  五份口径彼此还不一致（文档说 hook≥60/mid≥300/long<900，代码却是
  mid=(300,600)/long=(300,900)）→ 改一处必漏四处。
  用户令「60s 以上、300s 以下」后，这种结构**不可能改对**。

  → 数值只存 engine.json 一处；本模块是**唯一读取器**，并在导入时
    断言四处平台值一致（漂移立刻报错，而不是静默放行）。
  调用方一律 `from policy import ...`，**禁止再写第二份常量**。

用法：
    from policy import duration_bounds, check_measured, check_estimate, dedupe_policy
"""
import json
import os
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
# 2026-11-09：评估脚本可用 POLICY_CONFIG 指向临时副本做漂移测试，
# 绝不改写 config/engine.json 正典（进程被杀留残值会让全链 import 失败）。
_CFG_PATH = Path(os.environ.get('POLICY_CONFIG') or (SKILL / 'config' / 'engine.json'))


def _load():
    return json.loads(_CFG_PATH.read_text(encoding='utf-8'))


CFG = _load()

# ── 一、成片时长（用户 2026-11-04 令：1 分钟以上、5 分钟以下）──────────
_dp = CFG.get('duration_policy')
if not _dp or 'bounds' not in _dp or len(_dp['bounds']) != 2:
    raise RuntimeError('[policy] duration_policy.bounds 读不到 —— 未知 ≠ 可以回退旧默认值'
                       '（旧兜底 0.15/2.0 会让跨集零重叠被静默放宽，见 P88）')
_BOUNDS = tuple(_dp['bounds'])
MIN_SEC, MAX_SEC = float(_BOUNDS[0]), float(_BOUNDS[1])

# ★ 一致性断言：quality_gate.py 直接读 platform_profiles.*.duration，
#   若有人只改了那一侧，这里立刻炸 —— 不允许「实切」与「度量」漂移。
for _name, _prof in CFG.get('platform_profiles', {}).items():
    if not isinstance(_prof, dict) or 'duration' not in _prof:
        continue
    _d = _prof['duration']
    if tuple(_d) != (MIN_SEC, MAX_SEC):
        raise RuntimeError(
            f'[policy] 时长口径漂移：duration_policy.bounds={[MIN_SEC, MAX_SEC]} '
            f'但 platform_profiles.{_name}.duration={_d}。'
            f'两处必须同时改，禁止只改一侧。')


def duration_bounds():
    """→ (min_sec, max_sec)：全平台统一的成片时长硬区间。"""
    return MIN_SEC, MAX_SEC


def format_range():
    """→ 人类可读区间，用于日志/文档，不参与判定。"""
    return f'{MIN_SEC:.0f}–{MAX_SEC:.0f}s'


def check_measured(dur):
    """**实测**成片时长是否合规（唯一硬门判据）。

    返回 (ok: bool, msg: str)。实测值来自 ffprobe/verify，估算值不许走这里。
    """
    try:
        d = float(dur)
    except (TypeError, ValueError):
        return False, '时长读不到（未知 ≠ 合规，保守拒绝）'
    if d <= 0:
        return False, '时长为 0 或不可读（拒绝）'
    if not (MIN_SEC < d < MAX_SEC):
        return False, f'实测 {d:.1f}s 越界（须严格大于 {MIN_SEC:.0f}s 且小于 {MAX_SEC:.0f}s）'
    return True, f'实测 {d:.1f}s ✓ 在 {format_range()} 内'


def check_estimate(est):
    """**预估**时长是否落在区间（仅预警，不阻断）。

    ★ 为什么估算不能当硬门：plan_est 按字速估算，实测 TTS 语速更快
      （2026-11-04 实测：预估 316s → 成片 279s，偏差 -12%）。
      用估算 REJECT 会误杀，用实测才是铁律 F5「实测音频时长是唯一时间基准」。
    """
    try:
        e = float(est)
    except (TypeError, ValueError):
        return True, '预估读不到（跳过预警）'
    margin = float(CFG.get('duration_policy', {}).get('estimate_margin', 0.12))
    lo, hi = MIN_SEC * (1 + margin), MAX_SEC * (1 - margin)
    if not (lo <= e <= hi):
        return False, (f'预估 {e:.0f}s 未落在安全区 {lo:.0f}–{hi:.0f}s'
                       f'（估算有 ±{margin:.0%} 偏差，最终以实测为准）')
    return True, f'预估 {e:.0f}s ✓（安全区 {lo:.0f}–{hi:.0f}s，实测为准）'


# ── 二、跨集去重策略（用户 2026-11-04 令：每集内容不能一样）────────────
_D = CFG.get('dedupe_policy', {})


_DEDUPE_REQUIRED = ('max_shot_overlap_ratio', 'max_text_overlap_ratio',
                    'min_text_novelty', 'require_unique_cold_open',
                    'require_unique_lead', 'require_unique_gen',
                    'ngram', 'shot_time_bucket')


def dedupe_policy():
    """→ 跨集去重判据字典。**唯一数值源**，episode_dedupe.py 专用。

    ★ 2026-11-09 修（审计发现）：旧版每个键都有 `.get(k, 旧默认值)`，
      配置一旦缺字段就**静默回退**到 0.15 / 2.0 —— 用户令「画面零重复」会被
      悄悄放宽而无人发现（P84「读不到≠没有」的又一种形态）。
      → 缺任何必需键直接抛错，宁可不跑也不静默降级。
    """
    missing = [k for k in _DEDUPE_REQUIRED if k not in _D]
    if missing:
        raise RuntimeError(f'[policy] dedupe_policy 缺字段 {missing} —— 拒绝静默回退旧口径')
    return {k: _D[k] for k in _DEDUPE_REQUIRED}


if __name__ == '__main__':
    print(f'时长规范: {format_range()}（严格区间，实测判定）')
    print(f'平台一致性: {len(CFG.get("platform_profiles", {}))} 个 profile 已断言一致 ✓')
    print('去重策略:', json.dumps(dedupe_policy(), ensure_ascii=False))
    print('实测校验:', check_measured(97.1), '|', check_measured(301.0))
    print('预估校验:', check_estimate(200.0), '|', check_estimate(295.0))
