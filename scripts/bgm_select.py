# -*- coding: utf-8 -*-
r"""bgm_select.py —— 解说成片 BGM 自动选曲（v3.6.0 原创度增强第一层）

用户 2026-10-24 令：解说视频默认配 BGM（废除「无 BGM」惯例），
用 BGM音效引擎 的 pick_bgm.py 按 plan 情绪基调选曲，回写 plan.bgm。

用法：
  python bgm_select.py <plan-N.json> [--mood 昂扬|紧张|温暖|史诗|轻快|氛围]
输出（回写 plan-N.json 的 bgm 字段）：
  {"bgm": "<绝对路径>", "bgm_intent": "...", "bgm_reason": "..."}

情绪推导（可解释规则，不搞黑盒）：
  tone 是「叙事张力」不是「剧种情绪」——用它推 BGM 会把治愈萌娃剧推成
  紧张悬疑（F1 实测踩坑：tense 87% → 紧张，实际该轻快）。正确口径：
  **片名/plan.name 关键词为主**（剧种信号），tone 的 excited 占比作提权：
    - name 含 悬疑/复仇/谜/凶/杀/局/计   → 紧张
    - name 含 逆袭/翻身/打脸/赢/爆/神/豪 → 昂扬
    - name 含 甜/趣/萌/笑/治愈/暖/福    → 轻快
    - 未命中                               → 轻快（短剧解说明快向是安全默认）
    - excited tone 占比 > 40%              → 昂扬（提权覆盖）
选曲铁律：有口播必须 --no-vocal（BGM 带唱会打架）；Top1 直接采用，
理由写进 plan 备查；选曲失败不阻塞——plan.bgm 缺省时 build 走无 BGM。
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
BGM_ENGINE = Path(r'C:/Users/Administrator/.workbuddy/skills/BGM音效引擎/scripts/pick_bgm.py')
# ★ library_root 与 BGM音效引擎 的 library_index.json 保持一致；索引缺失时
#   用此兜底（两处必须同步改，v3.6.0 注）
LIB_INDEX = Path(r'D:/Agent引擎/音频素材库/library_index.json')
LIB_ROOT_FALLBACK = 'D:/Agent引擎/音频素材库'
PY = sys.executable

# ★ 时长预估口径唯一正典（与 assemble_plan 共享，治「两处副本改一处漏一处」）
sys.path.insert(0, str(SKILL / 'scripts'))
from plan_est import est_total as _est_total  # noqa: E402

MOOD_TENSE = ['悬疑', '复仇', '谜', '凶', '杀', '局', '计', '暗',
              # ★ v3.6.2 补恐怖志怪类（长白山实测：缺关键词 → 恐怖剧选成轻快，
              #   完全违和）。山村怪谈/精怪/吃人/应名/雪夜失踪等信号
              '人面', '大马猴', '志异', '怪谈', '精怪', '吃人', '应名',
              '山村', '雪夜', '失踪', '闹鬼', '阴', '邪', '诡异', '猛兽']
MOOD_UPBEAT = ['逆袭', '翻身', '打脸', '赢', '爆', '神', '豪', '王']
MOOD_LIGHT = ['甜', '趣', '萌', '笑', '治愈', '暖', '福', '娃']


def est_total(plan: dict) -> float:
    """预估成片时长（正典在 scripts/plan_est.py，两边共享同一口径）。"""
    return _est_total(plan)


def derive_mood(plan: dict) -> tuple[str, str]:
    """片名+hook/CTA 文本关键词为主 + excited tone 提权。返回 (mood, reason)。"""
    name = str(plan.get('name') or '')
    segs = plan.get('segments', [])
    # 片名常无情绪词（如「星落庭深万物生」）——把首段（hook）与 CTA 文本并入扫描
    edge_text = ''
    if segs:
        edge_text = (segs[0].get('text') or '') + ' ' + (segs[-1].get('text') or '')
    scan = name + ' ' + edge_text
    tones = [s.get('tone') for s in segs if (s.get('text') or '').strip()]
    c = Counter(tones) if tones else {}
    excited_ratio = (c['excited'] / len(tones)) if tones else 0.0
    # excited 提权优先（爽点密度高就是逆袭爽剧，标题未必带关键词）
    if excited_ratio > 0.4:
        return '昂扬', f'excited tone 占比 {excited_ratio:.0%} > 40% → 昂扬'
    if any(w in scan for w in MOOD_TENSE):
        return '紧张', f'标题/hook 命中悬疑/复仇类关键词 → 紧张'
    if any(w in scan for w in MOOD_UPBEAT):
        return '昂扬', f'标题/hook 命中逆袭/打脸类关键词 → 昂扬'
    if any(w in scan for w in MOOD_LIGHT):
        return '轻快', f'标题/hook 命中甜趣/萌娃类关键词 → 轻快'
    return '轻快', '未命中关键词，短剧解说明快向安全默认 → 轻快'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('plan')
    ap.add_argument('--mood', default='', help='强制情绪目录（默认按 tone 推导）')
    ap.add_argument('--top', type=int, default=3)
    a = ap.parse_args()

    p = Path(a.plan)
    if not p.is_file():
        print(f'[警告] plan 文件不存在: {p} —— 跳过选曲（不阻塞）')
        return 0
    try:
        plan = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'[警告] plan JSON 解析失败: {type(e).__name__} {e} —— 跳过选曲（不阻塞）')
        return 0
    if not isinstance(plan, dict):
        print('[警告] plan 不是对象 —— 跳过选曲')
        return 0
    mood, reason = (a.mood, f'手动指定 --mood {a.mood}') if a.mood else derive_mood(plan)
    dur = est_total(plan)
    print(f'情绪推导：{mood}（{reason}）')
    print(f'预估时长：{dur:.0f}s')

    if not BGM_ENGINE.is_file():
        print(f'[警告] BGM音效引擎 缺失: {BGM_ENGINE} —— 跳过选曲（build 走无 BGM）')
        return 0

    try:
        r = subprocess.run(
            [PY, str(BGM_ENGINE), '--kind', 'bgm', '--mood', mood,
             '--dur', f'{dur:.0f}', '--no-vocal', '--top', str(a.top), '--json'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    except subprocess.TimeoutExpired:
        print('[警告] 选曲子进程 120s 超时 —— 跳过（不阻塞）')
        return 0
    except Exception as e:
        print(f'[警告] 选曲子进程异常: {type(e).__name__} {e} —— 跳过（不阻塞）')
        return 0
    if r.returncode:
        print(f'[警告] 选曲失败 rc={r.returncode}: {(r.stderr or "")[-200:]} —— 跳过（不阻塞）')
        return 0
    try:
        cands = json.loads(r.stdout)
    except Exception as e:
        print(f'[警告] 选曲输出解析失败: {type(e).__name__} —— 跳过')
        return 0
    if not isinstance(cands, list) or not cands:
        print(f'[警告] {mood} 目录无候选 —— 跳过')
        return 0

    top1 = cands[0]
    rel = top1.get('rel')
    if not rel:
        print('[警告] Top1 无 rel 字段 —— 跳过')
        return 0
    # rel 是相对 library_root 的路径（pick_bgm --json 的字段口径）
    root = LIB_ROOT_FALLBACK
    if LIB_INDEX.is_file():
        try:
            root = (json.loads(LIB_INDEX.read_text(encoding='utf-8')).get('library_root')
                    or root).replace('\\', '/')
        except Exception:
            pass
    bgm_path = str(Path(root) / rel)
    if not Path(bgm_path).is_file():
        print(f'[警告] Top1 路径无效: {bgm_path} —— 跳过')
        return 0
    plan['bgm'] = bgm_path
    plan['bgm_intent'] = mood
    plan['bgm_reason'] = (top1.get('why') and ' / '.join(top1['why']) or reason)[:200]
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'[OK] BGM → {Path(bgm_path).name}（{top1.get("dur")}s, {top1.get("label")}）')
    print(f'     理由: {plan["bgm_reason"][:120]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
