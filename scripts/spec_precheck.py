# -*- coding: utf-8 -*-
"""spec 阶段预检（2026-11-02 入技能） —— 把 build 的权威闸门前移，避免「跑到第 16 段才炸」。

★ 为什么需要（2026-11-02 实测）：
  X1 在 build 阶段才被拦下「段28 文案含 TTS 敌对字符（—…）」——
  技能的自检是好的，但它只在 build 开头跑，而 build 一旦跑起来就是
  「证据已采完、出图已出、跑了 9 分钟」才发现白跑。
  → 同一套判据（直接 import build.precheck_tts_text，**不另造标准**）
    在 spec 生成完就查一遍，1 秒内失败。

检查项：
  ① TTS 敌对字符（—…）——复用 build.precheck_tts_text
  ② 解说词为空 / 超长
  ③ gencfg 的 anchor 是否真的存在于 spec 的解说词里（★ X1 踩过：
     某拍改成原声段后文案为空 → 二创图锚点失效 → 被 difflib 兜底插到
     相似度 0.43 的**错句**上，图和解说对不上）
  ④ CTA 与末段文案是否重复（成片「一句话说两遍」）
  ⑤ 预估时长是否落在安全区（仅预警，硬门认实测）
  ⑥ 跨集内容去重（每集内容不能一样）——委托 episode_dedupe.check()

用法：
    python spec_precheck.py <任务目录> <ep> [<gencfg.json>] [--pilot <episodes.json>]
    # 例：python spec_precheck.py D:/剧里剧外/视频解说/任务/xts-e2e 1 D:/剧里剧外/视频解说/任务/_gencfg/X1.json
退出码 0 = 通过；非 0 = 有硬伤（附逐条明细）。
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build import precheck_tts_text, TTS_HOSTILE_CHARS  # noqa: E402  权威判据，不另造标准
from plan_est import est_total  # noqa: E402
from policy import check_estimate  # noqa: E402  时长规范唯一真相源
import episode_dedupe  # noqa: E402  跨集去重唯一判据


def main():
    import argparse as _ap
    _p = _ap.ArgumentParser(description='spec 阶段预检（含跨集去重）')
    _p.add_argument('task')
    _p.add_argument('ep')
    _p.add_argument('gencfg', nargs='?', default='')
    _p.add_argument('--pilot', default='', help='待产清单（提供历史集 gencfg 路径）')
    _a = _p.parse_args()
    task, ep = Path(_a.task), _a.ep
    gencfg = Path(_a.gencfg) if _a.gencfg else None

    spec_path = task / "_plan" / f"spec-{ep}.json"
    if not spec_path.is_file():
        print(f"[FAIL] 找不到 spec: {spec_path}")
        return 2
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    shots = spec["shots"]
    errors, warns = [], []

    # ① TTS 敌对字符（权威判据）
    segs = [{"text": s.get("text") or ""} for s in shots]
    try:
        precheck_tts_text(segs)
    except Exception as e:
        errors.append(f"TTS 敌对字符（{TTS_HOSTILE_CHARS}）：{e}")

    # ② 文案为空 / 超长
    for i, s in enumerate(shots):
        t = (s.get("text") or "").strip()
        if s.get("cold_open") or s.get("raw"):
            continue
        if not t:
            errors.append(f"段{i:02d} 解说词为空（raw/cold_open 之外不允许空）")
        elif len(t) > 40:
            warns.append(f"段{i:02d} 解说词 {len(t)} 字（>40 字，字幕两行且易超估时长）：{t[:24]}")

    # ③ gencfg 锚点必须命中真实解说词
    if gencfg:
        cfg = json.loads(gencfg.read_text(encoding="utf-8"))
        texts = [(s.get("text") or "").strip() for s in shots]
        for g in cfg.get("gen", []):
            a = (g.get("anchor") or "").strip()
            if not any(t.startswith(a) for t in texts if t):
                errors.append(f"gencfg[{g.get('img')}] 锚点在 spec 里不存在：{a[:24]}")
        for st in cfg.get("stickers", []):
            a = (st.get("anchor") or "").strip()
            if not any(t.startswith(a) for t in texts if t):
                errors.append(f"gencfg[表情包] 锚点在 spec 里不存在：{a[:24]}")

    # ④ CTA 与末段重复
    cta = (spec.get("cta") or "").strip()
    last = next((s for s in reversed(shots) if (s.get("text") or "").strip()), None)
    if cta and last:
        def core(t):
            t = re.sub(r"点[赞个些]?关[注汪]|给[个些]?关[注汪]|关注我", "|", t)
            parts = [p.strip("。，,！! ") for p in t.split("|") if p.strip("。，,！! ")]
            return max(parts, key=len) if parts else ""
        c, e = core(cta), core((last.get("text") or ""))
        if len(c) >= 3 and len(e) >= 3 and (c in e or e in c):
            errors.append(f"CTA 与末段重复：CTA「{c}」vs 末段「{e}」")

    # ⑤ 时长
    segs2 = [{"is_cold_open": True, "duration": s.get("duration", 5.0)} if s.get("cold_open")
             else ({"raw": 1, "text": ""} if s.get("raw") else {"raw": 0, "text": s.get("text", "")})
             for s in shots]
    est = est_total({"segments": segs2})
    ok_est, msg_est = check_estimate(est)
    print(f"[时长] {msg_est}")
    if not ok_est:
        warns.append(msg_est)
    # spec 里的旧 tier 字段不再参与任何判定（2026-11-06 取消三档）
    if spec.get("tier"):
        warns.append("spec 含遗留 tier 字段（已废弃，不影响判定，可删）")

    # ⑥ 跨集去重：判据只在 episode_dedupe 一处
    cur_cfg = None
    if gencfg and gencfg.is_file():
        cur_cfg = json.loads(gencfg.read_text(encoding="utf-8"))
    pilot = None
    if _a.pilot:
        try:
            pilot = json.loads(Path(_a.pilot).read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"清单读不到（未知 ≠ 没有）：{e}")
    dedup_ok, rep = episode_dedupe.check(task, ep, cur_cfg, pilot)
    print(f"[跨集去重] 历史集 {rep.get('history') or '无（首集）'}；"
          f"检查 {len(rep.get('checks', []))} 项，失败 {rep.get('failed_count', 0)} 项")
    if rep.get('fatal'):
        errors.append(f"跨集去重：{rep['fatal']}")
    for c in rep.get('checks', []):
        if not c['ok']:
            errors.append(f"跨集去重[{c['name']}]：{c['detail']}")

    print(f"[规模] {len(shots)} 段（cold_open {sum(1 for s in shots if s.get('cold_open'))} / "
          f"原声 {sum(1 for s in shots if s.get('raw'))}）")
    for w in warns:
        print(f"[warn] {w}")
    if errors:
        print(f"[FAIL] {len(errors)} 处硬伤：")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("[OK] spec 预检通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
