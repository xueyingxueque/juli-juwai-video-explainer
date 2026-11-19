# -*- coding: utf-8 -*-
"""make_plan_v37.py —— v37 成片 plan 组装（plan-N.json → plan-N-v37.json）。

★ 为什么收进技能（2026-11-02）：此前这一步只活在任务目录的 _tools/ 下，
  技能文档的八步里根本没有它 → 照文档做的人做不出二创图/表情包，
  而能力散落在技能外，教训（锚点必须精确命中）就进不了技能。

从 gencfg 读二创图/表情包/封面配置，插进 assemble 产出的 plan：
  · 二创图：插在锚点段之后，该段改取 AI 镜头（版权门 footage=0）
  · 表情包：段级 stickers 数组（meme_path/anchor/scale/show）

★ 锚点**必须精确命中**（前缀匹配），不许 difflib 兜底：
  兜底会把二创图插到「相似度 0.43 的另一句」上 —— 图与解说对不上，
  而画面看起来完全正常，人眼很难发现（2026-11-02 X1 实踩）。
  找不到就报错，让人去改锚点，不许猜。

用法：
    python make_plan_v37.py <任务目录> <ep> <gencfg.json>
    python make_plan_v37.py <任务目录> <ep> <gencfg.json> --allow-fuzzy
"""
import argparse
import difflib
import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
MEME_ROOT = Path(CFG.get('paths', {}).get('sticker_root')
                 or 'D:/Agent引擎/表情包合集/EmojiPackage')


def find_seg(segs, prefix, allow_fuzzy=False):
    """锚点 → 段。精确前缀优先；找不到时仅在 --allow-fuzzy 下才相似度兜底（并告警）。"""
    prefix = (prefix or '').strip()
    for s in segs:
        if (s.get('text') or '').strip().startswith(prefix):
            return s
    if not allow_fuzzy:
        raise SystemExit(
            f'[FAIL] 锚点未精确命中（拒绝模糊兜底）：「{prefix[:30]}」\n'
            f'  修法：把 gencfg 里的 anchor 改成 plan 里某段解说的**开头几个字**；\n'
            f'  或先跑 spec_precheck.py 看该 anchor 是否根本不存在于 spec。')
    cands = [(difflib.SequenceMatcher(None, prefix, (s.get('text') or '').strip()).ratio(), s)
             for s in segs if (s.get('text') or '').strip()]
    if cands:
        ratio, best = max(cands, key=lambda x: x[0])
        if ratio >= 0.35:
            print(f'[warn] 锚点「{prefix[:20]}」未精确匹配，用相似句（{ratio:.2f}）: '
                  f'{(best.get("text") or "")[:30]}')
            return best
    raise SystemExit(f'[FAIL] 锚点未找到且无相似句：「{prefix[:30]}」')


def pick_gen(gen_dir, prefix, img):
    """二创图定位：文件名约定 `{集前缀}_{img}_*.png`（如 X1_x1a_00001_.png）。

    ★ 坑（2026-11-02 实踩）：只按 `{img}_*.png` 匹配会**找不到图**——
      归档命名带集前缀，而 gencfg 里的 img 只有 "x1a"。先带前缀找，
      找不到再退回不带前缀（兼容早期归档）。
    """
    c = sorted(gen_dir.glob(f'{prefix}_{img}_*.png'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not c:
        c = sorted(gen_dir.glob(f'{img}_*.png'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not c:
        raise SystemExit(f'[FAIL] 二创图缺失：{prefix}_{img}_*.png（目录 {gen_dir}）')
    return c[0]


def resolve_meme(name):
    """表情包名 → 绝对路径（相对 MEME_ROOT，支持 '分类/文件名'）。"""
    p = MEME_ROOT / name
    if p.is_file():
        return p
    hits = list(MEME_ROOT.rglob(Path(name).name))
    if hits:
        return hits[0]
    raise SystemExit(f'[FAIL] 表情包不存在：{name}（库 {MEME_ROOT}）')


def main():
    ap = argparse.ArgumentParser(description='v37 成片 plan 组装（严格锚点）')
    ap.add_argument('task')
    ap.add_argument('ep')
    ap.add_argument('gencfg')
    ap.add_argument('--prefix', default='',
                    help='集前缀（如 X1）——二创图命名约定 {prefix}_{img}_*.png')
    ap.add_argument('--allow-fuzzy', action='store_true',
                    help='锚点未精确命中时允许相似句兜底（不推荐）')
    a = ap.parse_args()

    task = Path(a.task)
    ep = str(a.ep)
    gencfg = json.loads(Path(a.gencfg).read_text(encoding='utf-8'))
    base = task / '_plan'
    gen_dir = task / 'gen'
    plan = json.loads((base / f'plan-{ep}.json').read_text(encoding='utf-8'))
    segs = plan['segments']

    # ── ① 二创图（插在锚点段之后）──
    for g in gencfg.get('gen', []):
        seg = find_seg(segs, g['anchor'], a.allow_fuzzy)
        idx = segs.index(seg)
        eid = f"GEN-{seg['ep']:03d}-{seg['t']:.2f}"
        segs.insert(idx + 1, {
            'ep': seg['ep'], 't': seg['t'], 'text': g['text'], 'raw': 0,
            'gen': str(pick_gen(gen_dir, a.prefix, g['img'])),
            'gen_start': 0, 'event_id': eid,
            'primary_evidence_id': '', 'evidence_ids': [], 'visual_anchor_only': True,
            'block': g.get('block', 'core'), 'tone': g.get('tone', 'neutral'),
            'inference': False,
            'evidence': {'event_id': eid, 'description': '(AI 二创镜头)',
                         'subtitle_text': '', 'speaker': '', 'frame_who': ''},
        })
        print(f'  二创图插入: {g["img"]} → ep{seg["ep"]} 「{g["text"][:18]}」')

    # ── ② 表情包（段级 stickers 数组；原声段不贴，build.py 会跳过）──
    for st in gencfg.get('stickers', []):
        seg = find_seg(segs, st['anchor'], a.allow_fuzzy)
        idx = segs.index(seg)
        eid = f"STM-{seg['ep']:03d}-{seg['t']:.2f}"
        mp = resolve_meme(st['meme'])
        segs.insert(idx + 1, {
            'ep': seg['ep'], 't': seg['t'], 'text': '', 'raw': 0,
            'stickers': [{'meme_path': str(mp),
                          'anchor': st.get('anchor_pos', 'top_right'),
                          'scale': st.get('scale', 0.22)}],
            'sticker_dur': st.get('dur', 1.6), 'event_id': eid,
            'primary_evidence_id': '', 'evidence_ids': [], 'visual_anchor_only': True,
            'block': 'core', 'tone': 'neutral', 'inference': False,
            'evidence': {'event_id': eid, 'description': '(表情包贴纸)',
                         'subtitle_text': '', 'speaker': '', 'frame_who': ''},
        })
        print(f'  表情包插入: {st["meme"]} → ep{seg["ep"]} 「{st["anchor"][:16]}」')

    out = base / f'plan-{ep}-v37.json'
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'plan-{ep}-v37 → {out}（{len(segs)} 段）')


if __name__ == '__main__':
    main()
