#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分段配音模板（v0.6.0，由旧 dub_segments.py 项目一次性脚本参数化重构）

用途：一个项目目录里按镜放好 S01.mp4/S02.mp4… 和 S01.txt/S02.txt…，
     本脚本逐镜调 dub.py 合成，再调 dub_align.py 按各镜视频时长对齐。
与旧脚本的区别：
  - 不再硬编码任何项目路径 / .workbuddy 路径；技能根用本文件位置自动定位；
  - 情绪参数按引擎自动选对：indextts2 用 --emo-text，pilottts 才用 --emotion
    （旧脚本给 indextts2 传 --emotion，会被白名单静默丢弃=情绪根本没生效）。

用法示例：
  python examples/分段配音模板.py --workdir D:/某项目 --engine indextts2 \
      --voice 子言 --emo-text "冷静叙述" --out-name 配音_对齐.wav
"""
import argparse
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent  # examples/ 的上一级=技能根
DUB = SKILL_ROOT / "dub.py"
ALIGN = SKILL_ROOT / "dub_align.py"

# 各引擎的"情绪/风格"参数名（与 dub.py _ENGINE_OPTS 白名单严格一致）
EMOTION_FLAG = {
    "pilottts": "--emotion",      # happy/sad/angry/... 11 类
    "indextts2": "--emo-text",    # 中文情绪描述
    "dotstts": None,              # 不支持情绪
    "chatterbox": None,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True, help="项目目录（含 S01.mp4 + S01.txt …）")
    ap.add_argument("--engine", default="indextts2", choices=list(EMOTION_FLAG))
    ap.add_argument("--voice", required=True, help="音色别名（dub.py --list-voices 可查）")
    ap.add_argument("--emo-text", default="", help="情绪/风格描述（indextts2 走 --emo-text；pilottts 自动转 --emotion）")
    ap.add_argument("--fast", action="store_true", help="快速预设（beam/steps 下调）")
    ap.add_argument("--out-name", default="配音_对齐.wav")
    ap.add_argument("--python", default=sys.executable, help="跑 dub.py 的解释器（默认当前）")
    args = ap.parse_args()

    wd = Path(args.workdir).resolve()
    videos = sorted(wd.glob("S*.mp4"))
    if not videos:
        print(f"[ERROR] {wd} 下没有 S*.mp4")
        sys.exit(1)

    seg_files, durations = [], []
    for vp in videos:
        tp = vp.with_suffix(".txt")
        if not tp.exists():
            print(f"[ERROR] 缺台词文件 {tp.name}")
            sys.exit(1)
        out = wd / f"{vp.stem}.wav"
        cmd = [args.python, str(DUB), args.engine, "--voice", args.voice,
               "--text-file", str(tp), "--out", str(out)]
        flag = EMOTION_FLAG[args.engine]
        if args.emo_text and flag:
            cmd += [flag, args.emo_text]
        if args.fast:
            cmd += ["--fast"]
        print(f"[合成] {vp.stem}: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
        seg_files.append(str(out))
        # 时长由 ffprobe 取
        rp = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                             "-of", "csv=p=0", str(vp)], capture_output=True, text=True)
        durations.append(rp.stdout.strip() or "0")

    # dub_align 对齐
    cmd = [args.python, str(ALIGN), "--segments", ",".join(seg_files),
           "--durations", ",".join(durations), "-o", str(wd / args.out_name)]
    print("[对齐]", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"[OK] 完成：{wd / args.out_name}")


if __name__ == "__main__":
    main()
