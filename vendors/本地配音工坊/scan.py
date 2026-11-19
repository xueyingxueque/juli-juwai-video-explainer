# -*- coding: utf-8 -*-
"""
本地配音工坊 · 扫描器
=====================
用法：
    python scan.py              # 全量重扫（引擎 + 音色），写 engines.json / voices.json
    python scan.py --engines    # 只重扫引擎
    python scan.py --voices     # 只重扫音色
    python scan.py --report     # 只打印，不落盘

往 D:\\AITools\\TTS 丢新配音工具、往 D:\\AITools\\TTS\\音色包 丢新音频后，跑一次即可。
"""
from __future__ import annotations

import argparse
import json
import sys

from registry import (
    ENGINES_JSON, VOICES_JSON, TTS_ROOT, VOICE_POOL,
    discover_engines, discover_voices,
)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="扫描本地 TTS 引擎与音色包")
    ap.add_argument("--engines", action="store_true", help="只扫引擎")
    ap.add_argument("--voices", action="store_true", help="只扫音色")
    ap.add_argument("--report", action="store_true", help="只打印不落盘")
    a = ap.parse_args()

    do_e = a.engines or not (a.engines or a.voices)
    do_v = a.voices or not (a.engines or a.voices)

    if do_e:
        data = discover_engines()
        if not a.report:
            ENGINES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n引擎扫描  根目录: {data['tts_root']}")
        print("-" * 78)
        for e in data["engines"]:
            flag = "就绪" if e["ready"] else "未就绪"
            role = "编排层" if e.get("role") == "orchestrator" else "引擎"
            extra = ""
            if e.get("models"):
                extra = f"  模型: {', '.join(e['models'])}"
            print(f"[{flag}] {e['name']:<12} ({role})  {e['tagline']}{extra}")
            print(f"         {e['root']}")
        if data["unknown_dirs"]:
            print("\n未识别的目录（需要在 registry.FINGERPRINTS 补指纹 + 写 worker 才能挂载）：")
            for d in data["unknown_dirs"]:
                print(f"  - {d}")

    if do_v:
        data = discover_voices()
        if not a.report:
            VOICES_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            _write_scene_table(data)
        print(f"\n音色扫描  音色包: {data.get('pool')}   共 {data.get('count', 0)} 个")
        print("-" * 78)
        print(f"{'分组':<6}{'别名':<12}{'性别':<8}{'时长':>7}  {'质量':<14}场景")
        for v in data.get("voices", []):
            g = {"female": "女", "male": "男", "unknown": "—"}[v["gender"]]
            d = f"{v['duration']}s" if v["duration"] else "—"
            star = "⭐" if v.get("common") else " "
            print(f"{star}{v.get('group','根目录'):<5}{v['alias']:<12}{g:<8}{d:>7}  {v['quality']:<14}{'/'.join(v['scenes'][:2])}")

    if not a.report:
        print(f"\n已写入: {ENGINES_JSON.name} / {VOICES_JSON.name} / 音色场景表.md")


def _write_scene_table(data):
    """把音色索引落成人类可读的 音色场景表.md（scan 自动生成，勿手改）。"""
    from datetime import datetime
    from pathlib import Path
    lines = [
        "# 音色场景表（scan.py 自动生成）",
        "",
        f"> 由 `python scan.py` 自动生成于 {datetime.now():%Y-%m-%d %H:%M}，勿手改。",
        "> 别名 = `dub.py --voice <别名>`。⭐ = 常用分组（**配音优先选**）。",
        "> 常用音色明细与推荐用途见 `音色说明.md`。",
        "",
        f"音色包：`{data.get('pool')}` ｜ 共 {data.get('count', 0)} 个",
        "",
        "| 分组 | 别名 | 性别 | 时长 | 质量 | 场景 |",
        "|---|---|---|---|---|---|",
    ]
    for v in data.get("voices", []):
        g = {"female": "女", "male": "男", "unknown": "—"}[v["gender"]]
        star = "⭐" if v.get("common") else ""
        alias = f"{star} {v['alias']}"
        dur = f"{v['duration']}s" if v["duration"] else "—"
        scenes = " / ".join(v.get("scenes", [])[:4]) or "通用"
        lines.append(f"| {v.get('group', '根目录')} | {alias} | {g} | {dur} | {v['quality']} | {scenes} |")
    Path(__file__).resolve().parent.joinpath("音色场景表.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
