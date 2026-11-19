# -*- coding: utf-8 -*-
r"""pipeline_state.py —— 产线状态判定的**唯一真相源**（出片判定 / 待产清单 / 产线在跑）。

★ 为什么必须单独抽出来（2026-11-04 治根）：
  「这一集算不算已出片」这**一个**判定，之前在三处各写一遍：
    · auto_resume.py（已移除）   → final = outdir / f"{e['name']}-封面版.mp4"  → 决定「跳过还是续跑」
    · deliver_check.py → cover_mp4 = outdir / f"{a.name}-封面版.mp4" → 决定「成品齐不齐」
    · watchdog.py      → 压根没这个概念（只看 out/ 最新 mtime）
  同一规则写多遍 = **改一处必漏另一处**（用户铁律）。

  2026-11-04 实测的两个具体后果（都是「架构允许误报/静默」）：
    ① watchdog 不知道「还有没有该产的集」→ 清单 6 集全部出片后，
       心跳永远停在最后一个成片（K2 封面版 10:44）→ **每轮都报「停滞 N 分钟」**。
       永久误报 → 告警信噪比归零 → 真停滞会被淹没（这是 05:05 轮修过的
       「告警自我误报」的**第二个变体**，同一类问题第三次出现）。
    ② watchdog 的「进程活着」用 `tasklist python.exe` 判定（**任意** python 都算，
       本机常驻的 comfy-mcp-server 就算），
       而 auto_resume 的 busy() 精确匹配产线脚本 → 两个口径漂移 →
       看板能报 process_alive=true 而产线其实根本没在跑。

  → 全部收口到本模块：调用方只调函数，不再自己写规则。

用法：
    from pipeline_state import load_pilot, is_produced, pending_entries, production_alive
"""
import json
import subprocess
from pathlib import Path

# 待产清单位置（相对任务根）——与 auto_resume.py 的 PILOT 同一份
PILOT_REL = Path('_tools') / 'episodes.json'

# 产线进程特征：判定「产线在不在跑」
# ★ 必须同时含 ingest_all.py / s1_ingest.py —— 素材入库也要占 GPU
#   （VLM 抽帧 + ASR 转写），漏检就会与生产抢显存。
_PROC_PATTERNS = r'produce\.py|build\.py|run_episode|ingest_all\.py|s1_ingest\.py'


def pilot_path(root):
    """待产清单路径（任务根下 _tools/episodes.json）。"""
    return Path(root) / PILOT_REL


def load_pilot(pilot_file):
    """读待产清单。

    返回 list = 清单可读（可能为空）；
    返回 None  = **清单读不到**（路径错 / 文件坏 / 非法 JSON）。

    ★ 为什么要区分 None 和 []（防静默失败）：
      「读不到清单」≠「没有待产集」。若把读不到当成空清单，
      看门狗会认为「没活要干 → 不报停滞」→ **清单路径写错 = 悄悄失去告警**。
      → 读不到时上层必须保守处理（按「可能有活」对待）。
    """
    try:
        data = json.loads(Path(pilot_file).read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return data


def final_path(entry):
    """该集的最终交付物（封面版）路径 —— **「已出片」的唯一判据**。"""
    return Path(entry['task']) / 'out' / entry['prefix'] / f"{entry['name']}-封面版.mp4"


def is_produced(entry):
    """该集是否已出片（封面版存在即算完成 —— 与续跑口径一致）。"""
    try:
        return final_path(entry).is_file()
    except OSError:
        return False


def pending_entries(pilot_file):
    """待产集（清单里还没出片的集）。

    返回 list = 确切待产集；
    返回 None  = 清单读不到（未知，**别当空**）。
    """
    eps = load_pilot(pilot_file)
    if eps is None:
        return None
    return [e for e in eps if not is_produced(e)]


def production_pids():
    """当前在跑的产线进程 PID 列表。

    ★ 口径（2026-11-04 修「自我误判」时定的，此处沿用，不另写一份）：
      只认 python 宿主（^(python|pythonw)）—— cmd/bash/powershell/conhost
      一律不算。因为调度器常被 `bash -c "... auto_resume.py ... produce.py ..."`
      这么调起来，那条 bash 的命令行里含 'produce.py' 字样 → 命中 →
      永远判定「在跑」→ 产线再也启动不了，且看起来一切正常。
    """
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process | Where-Object { "
             "$_.Name -match '^(python|pythonw)' -and "
             f"$_.CommandLine -match '{_PROC_PATTERNS}' -and "
             "$_.CommandLine -notmatch 'Get-CimInstance' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=60)
    except Exception:
        return []
    return [t for t in (r.stdout or '').split() if t.isdigit()]


def production_alive():
    """产线进程在跑？（精确匹配产线脚本，不把常驻 python 算进来）。"""
    return bool(production_pids())


def ingest_ready(task_dir, need=10):
    """字幕入库是否够用 —— **按实际产物判定，不认布尔标志**。

    ★ 为什么（2026-11-07 实测死锁）：旧版 `watch_downloads.bootstrap()` 用
      `state[剧].bootstrapped` 布尔标志判断「入库做完没」，而进程被会话回收时
      标志没来得及写 → 队列链一直等这个标志 → 永不启动；实际盘上却已有 10/16 集。
      布尔标志是**第二份状态**，必然与磁盘漂移（P84 同族）。
      → 唯一真相 = `_s1/ep*` 目录数。调用方一律调本函数，禁止再写 `bootstrapped` 判定。
    """
    try:
        s1 = Path(task_dir) / '_s1'
        return len([p for p in s1.glob('ep*') if p.is_dir()]) >= int(need)
    except OSError:
        return False
