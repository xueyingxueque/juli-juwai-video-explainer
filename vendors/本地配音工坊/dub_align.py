#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配音对齐脚本：分段配音 → 按镜长时间码对齐 → 合成完整音轨
解决"口播稿怎么跟画面对的上"的执行层问题。

流程：
1. 输入：分段配音 wav（每镜一段，命名 S01.wav...S08.wav）+ 镜长表
2. 实测每段配音时长 vs 镜长 → 计算对齐策略
3. 策略：配音 ≤ 镜长-0.5s → 放段首（尾帧定格收余量）；配音 > 镜长-0.5s → atempo 微压（限 1.0-1.15）
4. 按时间码 adelay 插入 → amix 合成完整音轨 → 输出对齐配音

用法：
python dub_align.py --dir 配音段/ --durations 8,6,7,5,6,4,5,5 -o 配音_对齐.wav
"""
import argparse, json, subprocess, sys
from pathlib import Path

def log(m):
    print(m, flush=True)

def probe_duration(f):
    p = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(f)],
                       capture_output=True, text=True)
    d = json.loads(p.stdout)
    return float(d["format"]["duration"])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="分段配音目录（含 S01.wav..S08.wav）")
    ap.add_argument("--durations", required=True, help="每镜时长，逗号分隔：8,6,7,5,6,4,5,5")
    ap.add_argument("--margin", type=float, default=0.5, help="定格余量（配音须 ≤ 镜长-margin）")
    ap.add_argument("--max-atempo", type=float, default=1.15, help="atempo 上限（超了警告）")
    ap.add_argument("-o", "--out", required=True, help="输出对齐音轨")
    args = ap.parse_args()

    d = Path(args.dir)
    durations = [float(x) for x in args.durations.split(",")]
    # A7：排除自己上一轮生成的 *_tempo.wav，保证重跑幂等（否则段数翻倍）
    segs = sorted([f for f in d.glob("S*.wav") if not f.stem.endswith("_tempo")])
    if len(segs) != len(durations):
        log(f"[ERROR] 段数 {len(segs)} != 镜长数 {len(durations)}")
        sys.exit(1)

    # 先验证分段配音已存在且可测
    report = []
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    filters = []
    for i, (seg, dur) in enumerate(zip(segs, durations)):
        real = probe_duration(seg)
        target = dur - args.margin  # 目标配音时长（镜长 - 定格余量）
        if real <= target + 0.05:
            # 配音不超 → 原速，放段首，余量自然定格
            atempo = 1.0
            note = "原速+定格余量"
            tmp = seg
        else:
            # 配音超 → atempo 微压
            atempo = real / target
            if atempo > args.max_atempo:
                log(f"[WARN] {seg.name} atempo {atempo:.2f} 超上限 {args.max_atempo}（超压失真）——源头应减台词！")
            note = f"atempo {atempo:.2f}"
            tmp = d / f"{seg.stem}_tempo.wav"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(seg),
                            "-filter:a", f"atempo={atempo:.4f}", str(tmp)], check=True)
        report.append((seg.name, round(real,2), round(target,2), note))
        # 时间码 = 前序镜长累加
        t_start = sum(durations[:i])
        filters.append(f"[{i}:a]adelay={int(t_start*1000)}|{int(t_start*1000)}[a{i}]")
        cmd += ["-i", str(tmp)]

    # 合成（amix 各段不相交，等权归一）
    amix_ins = "".join(f"[a{i}]" for i in range(len(segs)))
    cmd += ["-filter_complex", f"{';'.join(filters)};{amix_ins}amix=inputs={len(segs)}:normalize=0[aout]",
            "-map", "[aout]", "-c:a", "pcm_s16le", args.out]

    log("=== 配音对齐报告（配音时长 vs 目标=镜长-定格余量）===")
    for name, real, target, note in report:
        flag = "OK" if note == "原速+定格余量" else ("⚠️压速" if "atempo" in note else "?")
        log(f"  {name}: 实测 {real}s → 目标 {target}s  [{note}]")
    total_vid = sum(durations)
    log(f"=== 总画面 {total_vid}s ===")

    rc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if rc.returncode != 0:
        log(f"[ERROR] 合成失败: {rc.stderr[-500:]}")
        sys.exit(1)
    out_dur = probe_duration(args.out)
    log(f"[OK] 对齐音轨: {args.out} ({out_dur:.1f}s)")

if __name__ == "__main__":
    main()
