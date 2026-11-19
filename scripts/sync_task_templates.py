# -*- coding: utf-8 -*-
r"""sync_task_templates.py —— 把技能正典模板同步到**已建任务目录**的 `_plan/` 副本。

★ 为什么必须有（2026-11-06 重构，坑典 P76/P85 同族病）：
  `new_task.py` 会把 `_task_template/` 三脚本**拷贝**进每个任务目录。
  于是同一份逻辑在磁盘上有 N+1 份：改了技能正典 → 新任务拿到新版，
  **老任务仍跑旧副本**（实测：本次把 assemble_plan.py 的 60/900 硬门改成
  policy 单档后，4 个已建任务的副本还是旧值）。
  → 「改一处必漏一处」又来了。必须在技能里提供一个**可重复执行的收口动作**。

纪律：
  · 只同步三份正典模板，不动任务目录里的 spec/ev/plan 等产物；
  · 被替换的旧副本 **rename 到 `_plan/_trash/`**，不真删（P82）；
  · 内容已一致则跳过；有未知自定义改动的副本默认**只报告不覆盖**（--force 才覆盖）。

用法：
    python scripts/sync_task_templates.py                # 预演（只报告）
    python scripts/sync_task_templates.py --apply        # 实际同步
    python scripts/sync_task_templates.py --apply --force  # 连同自定义副本一起覆盖（仍留备份）
"""
import argparse
import json
import shutil
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TPL = SKILL / 'scripts' / '_task_template'
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
ROOT = Path(CFG.get('paths', {}).get('task_root', 'D:/剧里剧外/视频解说/任务'))
NAMES = ['assemble_plan.py', 'shot_evidence.py', 'delta_evidence.py']


def main():
    ap = argparse.ArgumentParser(description='同步技能正典模板到已建任务目录')
    ap.add_argument('--apply', action='store_true', help='实际写入（默认只报告）')
    ap.add_argument('--force', action='store_true', help='覆盖有自定义改动的副本（仍留备份）')
    a = ap.parse_args()

    if not ROOT.is_dir():
        print(f'[WARN] 任务根不存在：{ROOT}（素材/任务目录被移走时属正常，跳过）')
        return 0
    tasks = sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / '_plan').is_dir())
    print(f'任务根 {ROOT} → {len(tasks)} 个含 _plan/ 的任务')
    n_new, n_skip, n_custom = 0, 0, 0
    for task in tasks:
        plan = task / '_plan'
        for name in NAMES:
            src, dst = TPL / name, plan / name
            if not src.is_file():
                continue
            if not dst.is_file():
                if a.apply:
                    shutil.copy2(str(src), str(dst))
                print(f'  + {task.name}/{name} 缺失 → {"已补" if a.apply else "待补"}')
                n_new += 1
                continue
            same = dst.read_bytes() == src.read_bytes()
            if same:
                n_skip += 1
                continue
            # 有差异：判断是否只是「旧正典版本」（含旧硬门特征）还是自定义改动
            old_text = dst.read_text(encoding='utf-8', errors='replace')
            legacy = ('> 900' in old_text or 'est > 900' in old_text or
                      '60-900s' in old_text or '分档' in old_text)
            if legacy or a.force:
                if a.apply:
                    trash = plan / '_trash'
                    trash.mkdir(exist_ok=True)
                    stamp = time.strftime('%Y%m%d-%H%M%S')
                    shutil.move(str(dst), str(trash / f'{name}.{stamp}.bak'))
                    shutil.copy2(str(src), str(dst))
                tag = '旧版硬门' if legacy else '自定义'
                print(f'  ~ {task.name}/{name}（{tag}）→ {"已同步+留备份" if a.apply else "待同步"}')
                n_new += 1
            else:
                print(f'  ! {task.name}/{name} 有自定义改动 → 未覆盖（--force 才覆盖）')
                n_custom += 1
    print(f'\n合计：需更新 {n_new} / 已一致 {n_skip} / 自定义未动 {n_custom}')
    if not a.apply and n_new:
        print('（预演模式，未写入。加 --apply 实际同步）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
