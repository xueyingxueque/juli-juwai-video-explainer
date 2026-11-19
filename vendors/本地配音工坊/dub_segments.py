#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分段配音脚本 v2：8 镜分别 TTS 合成（★ 语速档位制）
每镜按语速档位配置 interval_silence（句间静音控制语速）+ emotion（情绪），
最后生成 segments.json 记录每段实测时长，供 dub_align.py 按镜长对齐。

语速档位 → interval_silence：
  慢速 3.0-3.5 字/s → 400-600ms（铺垫/沉重/高潮一字一顿/收尾）
  中速 3.8-4.3 字/s → 200-300ms（陈述/论证）
  快速 4.4-5.0 字/s → 100-150ms（钩子/惊喜/递进）
  无台词镜：跳过（不 TTS），由 dub_align 插 BGM/音效
"""
import json, subprocess, sys, time
from pathlib import Path

WS = Path(r"D:\Agent引擎\vox_full_test\qwen38_v4\视频")
DUB = r"C:\Users\Administrator\.workbuddy\skills\本地配音工坊\dub.py"
OUT = WS / "配音段"
OUT.mkdir(exist_ok=True)

# (镜, 台词, 语速档位, interval_silence, emotion)
# 无台词镜 = ("S0x", None, "无台词", None, None)
SEGS = [
    ("S01", "全球开发者天天看的AI排行榜，前六名全是中国AI模型。中国AI，彻底站起来了。", "快速", 120, None),
    ("S02", "可两年前，说国产能追平，是要被骂收钱的。这榜单，以前不是这样的。", "慢速", 500, "calm"),
    ("S03", "阿里把Qwen3.8旗舰，直接开源了——一个脑子，装得下全中国一年说的话。", "中速", 250, None),
    ("S04", "这脑子记性大到离谱，扔套三体进去，第几页都记得。", "中速", 250, None),
    ("S05", "DeepSeek V4 Pro转正，修bug分数一夜翻五倍：昨天二年级，今天及格。", "快速", 120, "happy"),
    ("S06", "百万字输出六块，海外同级十倍价。", "中速", 250, None),
    ("S07", "下载悄悄破了100亿，当年骂收钱的，这回安静了。", "慢速", 600, "intense"),
    ("S08", "别再问中国AI行不行，下载跑一跑，答案在数字里。", "慢速", 500, "calm"),
]

def log(msg):
    print(msg, flush=True)

results = []
for sid, text, speed, interval, emotion in SEGS:
    out_file = OUT / f"{sid}.wav"
    if text is None:
        log(f"[SKIP] {sid} 无台词镜（纯音效/BGM）——不 TTS")
        continue
    if out_file.exists() and out_file.stat().st_size > 50000:
        log(f"[SKIP] {sid} 已存在")
        results.append((sid, out_file))
        continue
    log(f"[TTS] {sid} [{speed} 档, interval={interval}ms] 合成中...")
    cmd = [sys.executable, DUB, "--engine", "indextts2", "--voice", "小欧",
           "--interval-silence", str(interval),
           "--text", text, "--out", str(out_file)]
    if emotion:
        cmd += ["--emotion", emotion]
    rc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if rc.returncode != 0 or not out_file.exists():
        log(f"[ERROR] {sid} 失败: {rc.stderr[-300:]}")
        sys.exit(1)
    log(f"[OK] {sid} 完成")
    results.append((sid, out_file))
    time.sleep(1)

# 读每段时长
meta = []
for sid, f in results:
    p = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(f)],
                       capture_output=True, text=True)
    d = json.loads(p.stdout)
    dur = round(float(d["format"]["duration"]), 2)
    meta.append({"seg": sid, "file": str(f), "duration": dur})
    log(f"[META] {sid} = {dur}s")

(WS / "配音段.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
log(f"[ALL] 分段配音完成，共 {len(meta)} 段，总时长 {round(sum(m['duration'] for m in meta),1)}s")
log(f"[SAVE] 元数据 -> {WS / '配音段.json'}")
