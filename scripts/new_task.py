# -*- coding: utf-8 -*-
r"""新建解说任务脚手架（技能级，2026-10-22 立）

为什么需要它：_plan 三个脚本（assemble_plan / shot_evidence / delta_evidence）
此前靠「从旧任务目录手工 cp」同步，2026-10-22 实测踩出串剧 bug——
三副本的 EPS_DIR 都硬编码指向 gq6 事件库，同集数 ±1.2s 命中就复用错剧证据
（详见 references/00-pitfalls.md 的「单一真相源」相关条目）。**副本同步是 bug 温床，正典必须唯一。**

本脚本一次做齐：
  1. 建任务目录骨架（_plan/_s1/_covers/out/_qc_frames）；
  2. 从技能内正典 scripts/_task_template/ 拷三个 _plan 脚本（唯一正典入口）；
  3. 打印后续命令清单（s1_ingest 逐集 → 写 spec → evidence → assemble → build）。

用法：
  python scripts/new_task.py --name ff-e2e --dataset "D:/BaiduNetdiskDownload/福福自带甜趣" --episodes 5
任务根目录默认 D:/剧里剧外/视频解说/任务，可用环境变量 TASK_ROOT 覆盖。
★ docstring 内禁写三引号字符本身（2026-10-22 实踩，见坑典 P39）。
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL / 'scripts' / '_task_template'
TASK_ROOT = Path(os.environ.get('TASK_ROOT') or r"D:/剧里剧外/视频解说/任务")


def main() -> int:
    ap = argparse.ArgumentParser(description='新建解说任务目录并装配正典脚本')
    ap.add_argument('--name', required=True, help='任务名（建议 <剧简名>-e2e）')
    ap.add_argument('--dataset', required=True, help='素材目录（原片所在文件夹）')
    ap.add_argument('--episodes', type=int, default=0, help='总集数（仅用于打印清单）')
    a = ap.parse_args()

    dataset = Path(a.dataset)
    if not dataset.is_dir():
        print(f'[警告] 素材目录不存在: {dataset}（下载未完成？仍建档，ingest 前须就绪）')
    if not TEMPLATE.is_dir():
        raise SystemExit(f'正典模板目录缺失: {TEMPLATE}')

    task = TASK_ROOT / a.name
    if task.exists():
        raise SystemExit(f'任务目录已存在: {task}（换名或先清理）')
    for sub in ('_plan', '_s1', '_covers', 'out', '_qc_frames'):
        (task / sub).mkdir(parents=True, exist_ok=True)

    copied = []
    for src in sorted(TEMPLATE.glob('*.py')):
        dst = task / '_plan' / src.name
        shutil.copy2(src, dst)
        copied.append(dst.name)

    ds = str(dataset).replace('\\', '/')
    tk = str(task).replace('\\', '/')
    print(f'[OK] 任务目录 -> {task}')
    print(f'  正典脚本 -> _plan/{", ".join(copied)}')
    print()
    print('后续步骤（E2E_DATASET 环境变量驱动，换剧不改代码）：')
    print(f'  export E2E_DATASET="{ds}"')
    n = a.episodes or 0
    if n:
        for ep in range(1, n + 1):
            # ★ --out 是「目录」不是文件：每集独立子目录（2026-10-22 修）
            #   旧示例 `--out _s1/epN.json` 被 mkdir 成空目录垃圾；且多集共用
            #   同一目录会互相覆盖（source.json/asr_full.json 同名），只剩一份。
            print(f'  python scripts/s1_ingest.py "<第{ep}集文件>" --episode {ep} '
                  f'--out "{tk}/_s1/ep{ep}" --interval 4.0')
    else:
        print('  python scripts/s1_ingest.py "<原片>" --episode <集号> --out <任务>/_s1 --interval 4.0')
    print('  # 然后：读全剧字幕懂剧情 → 写 spec-N.json → shot_evidence.py spec-N.json '
          '→ assemble_plan.py ev-N.json → build.py plan-N.json --platform douyin --voice ref:<wav>')
    print('  # 封面：python scripts/make_cover.py make/burn（用带 PIL 的 tts_python）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
