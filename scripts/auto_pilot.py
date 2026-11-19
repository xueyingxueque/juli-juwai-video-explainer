# -*- coding: utf-8 -*-
r"""auto_pilot.py —— **无人值守产线推进器**（单次、幂等、无常驻进程）。

★ 为什么必须有这个（2026-11-07 停机复盘，架构级治根）：
  旧版是「会话内守护(watch_daemon) + 等待标志的队列(queue_produce) + 调度器」三层：
    · 14:22 MW1 开始生产 → 15:14 全部进程被会话回收，零成片；
    · `.watch.lock` / `.produce.lock` 变成**僵尸锁**（进程死了文件还在），
      下一轮互斥检查误判「已有产线在跑」→ 兜底也失效；
    · 队列链等 `state.bootstrapped == True`，而那个布尔标志在被杀时没写 → **死锁**；
    · automation 每小时触发，却既没接上 MW1、也没补 LC1。
  → 根因不是某个 bug，是**任何依赖常驻进程 / 文件锁 / 布尔标志的设计都会漂移**。

  正解：**调度器每轮拉起一个单次脚本，跑完一轮就退出**。
    · 无常驻进程 → 会话回收无所谓；
    · 无文件锁 → 互斥用 `pipeline_state.production_alive()` 精确进程检测；
    · 无等待标志 → 入库是否够用看 `_s1/ep*` 实际目录数（`ps.ingest_ready()`）；
    · 无「从头创作」 → 清单里未出片的集**重跑同一条 produce.py 命令**（指纹续跑）。

一轮做什么（全部幂等，可重复执行任意次）：
  ① 采样下载状态并刷新看板      → watch_downloads.sample() / write_dashboard()
  ② 互斥检查（精确进程检测）    → 有产线在跑就只报告、不启动第二条
  ③ 对 READY 剧补字幕入库       → 按实际产物，只补前 10 集
  ④ 清单里第一个未出片的集      → produce.py --pilot … --prefix …（一次一集）
  ⑤ 写心跳 `pilot_runs.jsonl` + 输出结构化 JSON 报告

用法（调度器只需这一条命令）：
    python scripts/auto_pilot.py                 # 推进一轮（会真的生产）
    python scripts/auto_pilot.py --dry-run       # 只报告会做什么，不启动任何生产
    python scripts/auto_pilot.py --no-produce    # 只做 ①⑤，停机/维护时用
退出码：0 = 本轮正常（含「无需动作」）；1 = 有错误（报告里逐条列出）。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_state as ps       # noqa: E402  状态/互斥唯一真相源
import watch_downloads as W       # noqa: E402  下载采样与看板
import contracts                  # noqa: E402  配置契约（启动即校验，缺段直接报错）

SCRIPTS = W.SCRIPTS if hasattr(W, 'SCRIPTS') else Path(__file__).resolve().parent
TOOLS = W.TOOLS
PILOT = W.PILOT
ROOT = W.ROOT
TASK_ROOT = W.TASK_ROOT
INGEST = TOOLS / 'ingest_all.py'
HEARTBEAT = W.DASH_DIR / 'pilot_runs.jsonl'


def log(m):
    print(f'[pilot {time.strftime("%H:%M:%S")}] {m}', flush=True)


def load_state():
    try:
        return json.loads(W.STATE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _pilot_once(a):
    """一轮完整推进（采样→互斥→入库→续跑一集→心跳）。返回结构化报告。"""
    contracts.load()          # 配置正典读不到 → 立刻失败，不静默跑
    rep = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'mode':
           'dry' if a.dry_run else ('observe' if a.no_produce else 'run'),
           'download': [], 'actions': [], 'skipped': [], 'errors': []}

    # ── ① 采样（只读 + 刷新看板，任何时候都可以做）────────────────
    try:
        conf = W.naming()
        state = load_state()
        reports, _newly = W.sample(state, conf)
        W.STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
        W.write_dashboard(reports, state, conf)
        rep['download'] = [{'series': r['series'], 'state': r['state'],
                            'mp4': r['mp4'], 'tmp': r['tmp']} for r in reports]
    except Exception as e:
        rep['errors'].append(f'采样失败: {type(e).__name__}: {e}')
        reports = []

    # ── ② 互斥：认**精确进程检测**，不认任何锁文件 ──────────────────
    alive = ps.production_alive()
    if alive:
        rep['skipped'].append('检测到产线进程在跑（精确匹配），本轮不启动新生产')
    if a.no_produce:
        rep['skipped'].append('--no-produce：仅观测')
    if a.dry_run:
        rep['skipped'].append('--dry-run：仅预演')

    blocked = alive or a.dry_run or a.no_produce

    # ── ③ 补字幕入库：按实际 `_s1/ep*` 判定，不等布尔标志 ───────────
    if not blocked:
        for r in reports:
            if r.get('state') != 'READY':
                continue
            task = TASK_ROOT / r['task']
            need = min(10, max(1, r['mp4']))
            if ps.ingest_ready(task, need):
                continue
            if not INGEST.is_file():
                rep['errors'].append(f'{r["task"]}: _tools/ingest_all.py 不存在，无法入库')
                continue
            log(f'{r["task"]} 补入库 1..{need}（当前不足）')
            try:
                rr = subprocess.run(
                    [sys.executable, '-u', str(INGEST), str(task),
                     str(ROOT / r['series']), '1', str(need)],
                    capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=3600)
                (task / 'out').mkdir(parents=True, exist_ok=True)
                (task / 'out' / 'ingest.log').write_text(
                    (rr.stdout or '') + '\n' + (rr.stderr or ''), encoding='utf-8')
                rep['actions'].append({'action': 'ingest', 'task': r['task'],
                                       'rc': rr.returncode, 'need': need})
                if rr.returncode != 0:
                    rep['errors'].append(f'{r["task"]} 入库 rc={rr.returncode}（详见 out/ingest.log）')
            except Exception as e:
                rep['errors'].append(f'{r["task"]} 入库异常: {type(e).__name__}: {e}')

    # ── ④ 清单里第一个未出片的集：重跑同一条命令（幂等续跑）────────
    if not blocked:
        pending = ps.pending_entries(PILOT)
        if pending is None:
            # ★ 读不到 ≠ 没有（P84）：保守报错，不要静默当空
            rep['errors'].append('待产清单读不到（未知 ≠ 没有），本轮不启动生产')
        elif not pending:
            rep['skipped'].append('清单无待产集 → 正常待命，不是停滞')
        else:
            e = pending[0]
            prefix = e['prefix']
            cmd = [sys.executable, '-u', str(SCRIPTS / 'produce.py'),
                   '--pilot', str(PILOT), '--prefix', prefix]
            log(f'续跑 {prefix}：{" ".join(cmd)}')
            if a.dry_run:
                rep['actions'].append({'action': 'produce(dry)', 'prefix': prefix})
            else:
                try:
                    rr = subprocess.run(cmd, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=7200)
                    logpath = Path(e['task']) / 'out' / f'produce-{prefix}.log'
                    logpath.parent.mkdir(parents=True, exist_ok=True)
                    logpath.write_text((rr.stdout or '') + '\n' + (rr.stderr or ''),
                                       encoding='utf-8')
                    rep['actions'].append({'action': 'produce', 'prefix': prefix,
                                           'rc': rr.returncode, 'log': str(logpath)})
                    if rr.returncode != 0:
                        rep['errors'].append(f'{prefix} 生产 rc={rr.returncode}（详见日志）')
                except subprocess.TimeoutExpired:
                    rep['errors'].append(f'{prefix} 生产超过 2 小时被中止（下次重跑会从断点续）')
                except Exception as ex:
                    rep['errors'].append(f'{prefix} 生产异常: {type(ex).__name__}: {ex}')

    # ── ⑤ 心跳（看板可显示「上次推进时间」，不参与任何判定）────────
    try:
        W.DASH_DIR.mkdir(parents=True, exist_ok=True)
        with HEARTBEAT.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': rep['ts'], 'mode': rep['mode'],
                                'actions': rep['actions'],
                                'errors': rep['errors']}, ensure_ascii=False) + '\n')
    except Exception:
        pass

    rep['ok'] = not rep['errors']
    return rep


def main():
    """调度器入口：默认一轮；--rounds N 可连续推进 N 集。

    ★ 为什么需要多轮（2026-11-08）：两部剧各三集共 6 集，若每小时只推一集要排 6 小时。
      但**不引入常驻守护/锁/标志**（P86）—— 只是把「一轮」这个幂等函数循环调用几次，
      每轮仍是全新的完整判定，任何一轮被中断，下一轮调度重跑同一条命令即从断点续上。
      清单清空 / 本轮无动作 / 出错，都会提前结束，不会空转。
    """
    ap = argparse.ArgumentParser(description='无人值守产线推进器（单次幂等，可连续多轮）')
    ap.add_argument('--dry-run', action='store_true', help='只报告，不启动生产')
    ap.add_argument('--no-produce', action='store_true', help='只采样刷新看板')
    ap.add_argument('--rounds', type=int, default=1,
                    help='最多连续推进几集（默认 1；清单清空/无动作/出错提前退出）')
    a = ap.parse_args()
    reps = []
    for i in range(max(1, a.rounds)):
        r = _pilot_once(a)
        r['round'] = i + 1
        reps.append(r)
        print(json.dumps(r, ensure_ascii=False, indent=1), flush=True)
        if r['mode'] != 'run' or not r['ok'] or not r['actions']:
            break
        pending = ps.pending_entries(PILOT)
        if pending == []:
            log('清单已无待产集 → 正常待命，提前结束')
            break
    return 0 if all(r['ok'] for r in reps) else 1


if __name__ == '__main__':
    sys.exit(main())
