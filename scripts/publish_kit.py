# -*- coding: utf-8 -*-
r"""publish_kit.py —— 发布物料包 + 复核队列（v6.0.0 第六层，2026-11-13）。

★ 为什么新增（全网调研 + §4.2 已知缺口 ⑨ / P90）：
  1. `compliance.py check` 对「AI 显式标识三处」只产出 P2 提示，靠人自行填写，
     没有可直接复制的物料；而《标识办法》第四条对**视频起始画面 + 播放周边**、
     发布页必选标签都有明确要求 → 人工填写 = 最容易漏的一环。
  2. 学习报告实测 51 集中 **39 集** soft/semantic、soft/speech 未打分，
     长期只躺在 qc.json.risks 里，没有「下一步做什么」的可推进队列（P90）。
  3. 隐式标识有没有**真的写进这支成片**，此前只有评估第 18 条抽查历史成片；
     单集交付时没有实测核对。

做什么（纯本地、只读生产素材、不发布、不改画面）：
  ① 按 compliance_policy 生成三处显式标识文案 + 强标参数提示（≥5% 短边 / ≥2s）
  ② 生成发布页 6 类必选标签的推荐项与候选，附发布侧三处落位
  ③ 实测这支成片的 AIGC 隐式标识（唯一判据 compliance.has_aigc_tag），未标识给补标命令
  ④ 汇总 provenance 生成链路留痕状态
  ⑤ 扫 qc.json 的 soft_unscored → 分成「可自动补测」与「必须人工回听」两类行动队列
  ⑥ 输出可直接复制的 Markdown 物料 + 结构化 JSON

★ 纪律：
  - **F7 不自动发布**：本脚本只产出物料，任何平台勾选、上传、发布由人执行。
  - **不把未打分伪装成通过**：soft/speech 的 narration_qc 仍是 provisional
    （allow_auto_pass=false），队列里一律标「必须人工回听」，绝不自动放行。
  - 判据/文案/标签全部来自 engine.json → compliance_policy，禁止本文件写第二份（P88）。
  - 强标参数、6 类标签来自官方媒体转述，**标注存疑且不进硬门**
    （见 references/06-research-2026-11.md 存疑清单）。

用法：
    python scripts/publish_kit.py --task <任务目录> --ep <集号> --video <封面版.mp4> \
        [--gencfg <gencfg.json>] [--prefix <前缀>] [--out <目录>]
默认写到 <task>/_plan/publish_kit-<ep>.json 与 .md。退出码：0 成功；2 输入不可用。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import contracts                    # noqa: E402  配置契约唯一入口
import compliance                   # noqa: E402  AIGC 标识唯一判据

CFG, _SRC = contracts.load()
POL = CFG.get('compliance_policy')
if not POL:
    raise RuntimeError('[publish_kit] compliance_policy 段缺失 —— 读不到 ≠ 用内置默认值')

_LABELS = POL.get('publish_labels') or {}
_EXPLICIT = POL.get('explicit_label_spec') or {}
_IMPLICIT = POL.get('implicit_metadata_spec') or {}
_REQUIRED_KIT = ('ai_label', 'ai_label_positions', 'publish_side_positions',
                 'ai_participation', 'publish_labels', 'explicit_label_spec',
                 'implicit_metadata_spec')
_MISSING = [k for k in _REQUIRED_KIT if k not in POL]
if _MISSING:
    raise RuntimeError(f'[publish_kit] compliance_policy 缺字段 {_MISSING}'
                       ' —— 拒绝静默回退旧口径')


def _aigc_state(video):
    """→ dict：隐式标识实测（唯一判据在 compliance.py）。"""
    ok, why = compliance.has_aigc_tag(video)
    if why.startswith('unknown:'):
        return {'status': 'unknown', 'detail': why,
                'action': 'ffprobe 读不到，按「未知」保守处理，先排查文件与 ffprobe 路径'}
    state = {'status': 'verified' if ok else 'missing', 'detail': why}
    if not ok:
        state['action'] = (f'python scripts/compliance.py stamp "{video}" '
                           f'--task <任务目录> --ep <集号> --apply')
    state['required_ingredients'] = _IMPLICIT.get('production_ingredients', [])
    state['standard_keys'] = _IMPLICIT.get('standard_keys', [])
    state['gap'] = ('哈希校验值当前引擎未写入 —— 如实声明，不伪装已合规'
                    '（见 references/06-research-2026-11.md）')
    return state


def _provenance(task, ep):
    p = Path(task) / '_plan' / f'provenance-{ep}.json'
    if not p.is_file():
        return {'status': 'missing', 'path': str(p),
                'action': f'python scripts/compliance.py provenance "{task}" {ep}',
                'note': '溯源清单记录各中间件 sha256 + 模型版本，交付时随片留存'}
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        return {'status': 'unreadable', 'path': str(p),
                'detail': f'{type(e).__name__}: {e}',
                'note': '读不到 ≠ 没有，按未留痕处理'}
    return {'status': 'verified', 'path': str(p),
            'files': len(d.get('files') or []),
            'models': d.get('models') or {},
            'engine_version': d.get('engine_version')}


def _review_queue(task):
    """P90 落地：把「未打分软门」从静默风险变成可推进队列（只报告，不自动放行）。"""
    items = []
    for qc in sorted(Path(task).glob('out/**/*.qc.json')):
        try:
            d = json.loads(qc.read_text(encoding='utf-8'))
        except Exception:
            items.append({'qc': str(qc), 'status': 'unreadable',
                          'note': '读不到 ≠ 没有，须人工确认'})
            continue
        unscored = d.get('soft_unscored') or []
        if not unscored:
            continue
        auto, manual = [], []
        for gate in unscored:
            if gate == 'soft/semantic':
                auto.append((gate, '由 plan 的 evidence/inference 生成 semantic.json'
                             '（produce.py _write_semantic_report 同源），再复跑 quality_gate'))
            elif gate == 'soft/speech':
                manual.append((gate, 'narration_qc 仍为 provisional / '
                               'allow_auto_pass=false → **必须人工回听**，禁止自动放行'))
            else:
                manual.append((gate, '无自动补测路径，须人工复核'))
        items.append({
            'qc': str(qc), 'prefix': qc.parent.name,
            'unscored': unscored, 'soft_grade': d.get('soft_grade'),
            'auto_actions': [{'gate': g, 'action': a} for g, a in auto],
            'manual_actions': [{'gate': g, 'action': a} for g, a in manual],
        })
    return {
        'total_episodes_with_unscored': len(items),
        'status': ('all_scored' if not items else 'pending_review'),
        'items': items,
        'discipline': ('未打分的软门是**已知风险，不是通过**（SKILL.md §2.5）；'
                       '本队列只推动复核，绝不把 provisional 校准伪装成自动放行'),
    }


def build(task, ep, video, gencfg=None, prefix=''):
    """→ 结构化物料 dict（不改任何文件、不发布）。"""
    label = POL['ai_label']
    short_edge = int(_EXPLICIT.get('reference_short_edge_px', 1080))
    min_px = round(short_edge * float(_EXPLICIT.get('min_text_height_ratio_of_short_edge', 0.05)))
    min_sec = float(_EXPLICIT.get('min_duration_sec', 2.0))
    sec = _LABELS.get('secondary_candidates') or []
    cover = {}
    if gencfg and Path(gencfg).is_file():
        try:
            cover = json.loads(Path(gencfg).read_text(encoding='utf-8')) or {}
        except Exception:
            cover = {}
    main = str(cover.get('cover_main') or '')
    sub = str(cover.get('cover_sub') or '')
    title = str(cover.get('title') or '')
    desc = (f'{title}｜{main}｜{sub}'.strip('｜') if (title or main) else '（从 gencfg 未读到标题，发布前补齐）')

    kit = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'engine_version': CFG.get('engine_version'),
        'task': str(task), 'ep': str(ep), 'prefix': prefix,
        'video': str(video),
        'explicit_label': {
            'text': label,
            'video_positions': POL['ai_label_positions'],
            'optional_positions': POL.get('ai_label_positions_optional', []),
            'publish_side_positions': POL['publish_side_positions'],
            'spec_hint': {
                'min_text_height_px': min_px,
                'min_duration_sec': min_sec,
                'note': ('强标实现范例（媒体转述，存疑）：文字高度 ≥ 短边 5%、'
                         '持续 ≥2s；本引擎不自动烧字，只作发布物料提示'),
            },
            'ready_to_copy': {
                '片头起始画面': label,
                '封面文案': f'{label}｜{main}' if main else label,
                '发布简介': f'{label}。{desc}。{POL["ai_participation"]}。',
            },
        },
        'publish_labels': {
            'required_choose_one': bool(_LABELS.get('required_choose_one')),
            'recommended': _LABELS.get('recommended'),
            'secondary_candidates': sec,
            'options': _LABELS.get('options', []),
            'note': ('引擎不自动勾选（F7）；发布页按平台实际标签为准，'
                     '6 类标签来自网信中国转述，标注存疑'),
        },
        'implicit_label': _aigc_state(video),
        'provenance': _provenance(task, ep),
        'review_queue': _review_queue(task),
        'manual_checklist': [
            '抽帧确认片头起始画面与播放周边确已显示显式标识（F12 校验通过 ≠ 画面正确）',
            '抽帧确认原片烧录字幕与解说字幕未双层叠字（P87：压盖 ≠ 擦除）',
            '确认主角音色克隆的素材授权范围；无授权改用题材预设音色',
            '确认解说为评论性引用、未形成对原片的实质性替代（调研 06 号文档司法口径）',
            '发布页勾选 AI 创作声明与必选标签；平台侧备案/审核由人完成',
            '软门未打分项按 review_queue 人工回听，不得当作通过',
        ],
        'disclaimer': ('本物料包不代替法律意见；法规条文以官方原文为准。'
                       '强标参数、6 类标签、平台留存数字均为媒体转述/二手数据，'
                       '一律标注存疑，禁止写进质检硬门。'),
    }
    return kit


def to_markdown(k):
    """→ 可直接复制的发布物料 Markdown。"""
    L = []
    L.append(f'# 发布物料包 · {k["prefix"] or k["ep"]} · {k["ts"]}')
    L.append('')
    L.append('> 引擎只产出物料，**不自动发布**（F7）。以下带存疑标注的内容以官方原文为准。')
    L.append('')
    L.append('## 1. AI 显式标识（必做）')
    L.append('')
    e = k['explicit_label']
    L.append(f'- 标识文案：`{e["text"]}`')
    L.append(f'- 视频落位：{" / ".join(e["video_positions"])}'
             f'（可选：{" / ".join(e["optional_positions"])}）')
    L.append(f'- 发布侧落位：{" / ".join(e["publish_side_positions"])}')
    L.append(f'- 强标参考（存疑）：文字高度 ≥ {e["spec_hint"]["min_text_height_px"]}px、'
             f'持续 ≥ {e["spec_hint"]["min_duration_sec"]}s')
    L.append('')
    L.append('| 位置 | 可直接复制的文案 |')
    L.append('|---|---|')
    for pos, txt in e['ready_to_copy'].items():
        L.append(f'| {pos} | {txt} |')
    L.append('')
    L.append('## 2. 发布页必选标签')
    L.append('')
    pl = k['publish_labels']
    L.append(f'- 必须选一项：{pl["required_choose_one"]}')
    L.append(f'- 推荐：`{pl["recommended"]}`；视内容叠加：{", ".join(pl["secondary_candidates"])}')
    L.append(f'- 全部选项：{" / ".join(pl["options"])}')
    L.append('')
    L.append('## 3. 隐式标识实测')
    L.append('')
    im = k['implicit_label']
    L.append(f'- 状态：`{im["status"]}` — {im["detail"]}')
    if im.get('action'):
        L.append(f'- 待办：{im["action"]}')
    L.append(f'- 制作要素：{", ".join(im.get("required_ingredients", []))}')
    L.append(f'- 已知缺口：{im.get("gap", "—")}')
    L.append('')
    L.append('## 4. 生成链路留痕')
    L.append('')
    pr = k['provenance']
    L.append(f'- 状态：`{pr["status"]}` — {pr["path"]}')
    if pr.get('action'):
        L.append(f'- 待办：{pr["action"]}')
    L.append('')
    L.append('## 5. 软门复核队列（未打分 ≠ 通过）')
    L.append('')
    rq = k['review_queue']
    L.append(f'- 待复核集数：{rq["total_episodes_with_unscored"]} / 状态 `{rq["status"]}`')
    for it in rq['items'][:20]:
        L.append(f'- `{it.get("prefix", "?")}` 未打分 {", ".join(it.get("unscored", []))}'
                 f'（soft_grade={it.get("soft_grade")}）')
        for x in it.get('auto_actions', []):
            L.append(f'  - [可自动补测] {x["gate"]}：{x["action"]}')
        for x in it.get('manual_actions', []):
            L.append(f'  - [必须人工] {x["gate"]}：{x["action"]}')
    L.append('')
    L.append('## 6. 人工复核清单')
    L.append('')
    for i, x in enumerate(k['manual_checklist'], 1):
        L.append(f'{i}. {x}')
    L.append('')
    L.append('---')
    L.append('')
    L.append(k['disclaimer'])
    return '\n'.join(L) + '\n'


def main():
    ap = argparse.ArgumentParser(description='生成发布物料包与复核队列（不发布）')
    ap.add_argument('--task', required=True)
    ap.add_argument('--ep', required=True)
    ap.add_argument('--video', required=True)
    ap.add_argument('--gencfg', default='')
    ap.add_argument('--prefix', default='')
    ap.add_argument('--out', default='', help='输出目录；默认 <task>/_plan')
    a = ap.parse_args()
    v = Path(a.video)
    if not v.is_file():
        print(f'[FAIL] 成片不存在：{v}')
        return 2
    kit = build(a.task, a.ep, v, a.gencfg or None, a.prefix)
    outdir = Path(a.out) if a.out else Path(a.task) / '_plan'
    outdir.mkdir(parents=True, exist_ok=True)
    jp = outdir / f'publish_kit-{a.ep}.json'
    mp = outdir / f'publish_kit-{a.ep}.md'
    jp.write_text(json.dumps(kit, ensure_ascii=False, indent=2), encoding='utf-8')
    mp.write_text(to_markdown(kit), encoding='utf-8')
    print(f'── 发布物料包 · ep{a.ep} ──')
    print(f'  显式标识落位: {" / ".join(kit["explicit_label"]["video_positions"])}')
    print(f'  隐式标识实测: {kit["implicit_label"]["status"]} — {kit["implicit_label"]["detail"]}')
    print(f'  溯源: {kit["provenance"]["status"]}')
    print(f'  复核队列: {kit["review_queue"]["total_episodes_with_unscored"]} 集待复核')
    print(f'  [out] {jp}')
    print(f'  [out] {mp}')
    print('  [F7] 引擎不自动发布；平台勾选与上传由人执行')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
