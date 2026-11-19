# -*- coding: utf-8 -*-
r"""plan_est.py —— plan 时长预估的唯一正典（v3.6.0 提取，治「多处副本改一处漏一处」）。

为什么单独一个模块：assemble_plan.py（任务目录副本）与 bgm_select.py（技能 scripts）
原本各写一份估算（TTS 4.9 字/s + 0.45s 呼吸 + 原声段 2.5s 版权上限）——
同一逻辑两处副本，改阈值必漏一处。现收敛到此，两边都 import。
"""
import json
from pathlib import Path

# ★ 2026-11-04 实测校准：预估 316s → 成片 279s（偏差 -12%），旧值 4.9 系统性高估。
#   按实测反推 4.9/0.88 ≈ 5.6。估算仍只作预警，**硬门认实测**（铁律 F5）。
TTS_CPS = 5.6      # TTS 字速（实测校准值；童声偏慢时仍会高估，故留 12% 余量）
BREATH = 0.35      # 每段结尾呼吸留白（2026-11-08 与 build.py 同步，见其注释）
RAW_SLOT_MAX = 2.5 # 原声段槽位上限（版权门连续原画面阈值）

# ★ v3.7.1 冷开场钩子上限（与 engine.json compliance.cold_open.max_sec 同源）。
#   取不到配置时回落 5.0；cold_open 段按其实设 duration（≤ 此上限）预估，
#   不再套用普通原声段的 2.5s 槽位。
try:
    _CFG = json.loads((Path(__file__).resolve().parent.parent / 'config' / 'engine.json').read_text(encoding='utf-8'))
    COLD_OPEN_MAX = float((_CFG.get('compliance', {}).get('cold_open') or {}).get('max_sec', 5.0))
except Exception:
    COLD_OPEN_MAX = 5.0


def est_total(plan: dict) -> float:
    """预估成片时长：解说段按字数、原声段按版权上限、冷开场按实设 duration（保守口径，实测偏差 ±10%）。"""
    est = 0.0
    for s in plan.get('segments', []):
        if s.get('is_cold_open'):
            d = s.get('duration')
            est += min(float(d) if isinstance(d, (int, float)) else COLD_OPEN_MAX, COLD_OPEN_MAX)
        elif s.get('raw'):
            est += RAW_SLOT_MAX
        else:
            est += max(len((s.get('text') or '').strip()) / TTS_CPS, 0.8) + BREATH
    return est
