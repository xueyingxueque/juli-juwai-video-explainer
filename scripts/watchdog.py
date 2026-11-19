# -*- coding: utf-8 -*-
"""watchdog.py —— 产线看门狗：**心跳 + 停滞检测 + 失败特征扫描**（2026-11-02 入技能）。

★ 为什么必须有（2026-11-02 血泪）：
  昨晚 build 因沙箱删除阈值卡死 **11 小时零产出、不报错**；
  今天 build 又在第 16 段才被 lint 拦下，我 12 分钟后才发现。
  两次的共同点：**没有任何东西在报「它不动了」**。
  → 看门狗盯的不是「进程还活着」，而是**产出物的 mtime 心跳**：
     进程活着但没产出 = 已经在死了。

它盯什么：
  ① 心跳：所有任务 out/ 目录里最新文件的 mtime（真在产出的证据）
  ② 停滞：心跳超过阈值没动 → ALERT（阈值要按实测节拍定，别拍脑袋）
  ③ 失败特征扫描：删除阈值卡死 / TTS 敌对字符 / 显存 OOM / VLM 假死 / build 失败 / 锚点失效
  ④ 断点状态：各集 _plan/state-<ep>.json 的当前步

用法：
    python watchdog.py                     # 前台跑（每 60s 采样）
    python watchdog.py --once              # 只采样一次，打印快照
    python watchdog.py --stall-min 8       # 停滞阈值（分钟）
产出：
    <任务根>/watchdog.log   人类可读逐分钟状态 + ALERT
    <任务根>/status.json    机器可读快照（随时 cat 就知道现状）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_state as ps

TASK_ROOT = Path(os.environ.get('TASK_ROOT', r'D:/剧里剧外/视频解说/任务'))

FAIL_SIGNS = {
    'SAFE_DELETE_BULK_CONFIRM_REQUIRED': '删除阈值卡死（沙箱等确认，会无限挂住）',
    'TTS 敌对字符': '文案含 —…（TTS 确定性退化，重试无用）',
    'CUDA out of memory': '显存 OOM',
    'out of memory': '显存 OOM',
    'VLM 请求失败': 'VLM 假死/连不上',
    'build 失败': 'build 失败',
    '二创图缺失': '二创图缺失',
    '锚点未找到': '锚点失效（gencfg 与 spec 对不上）',
    '证据采集失败': '证据采集失败',
}

# ★★★ 失败行前缀（2026-11-04 治根：告警自我误报）★★★
#   实测：auto_resume.py 的日志会把**上一次采样的告警快照**原样打印出来
#   （JSON 里的 "produce-T2.log: build 失败"）。
#   而 scan_failures 是「日志全文 grep 关键词」→ 这段**快照文本**里的
#   "build 失败" 被当成本轮新失败 → **刚成功的集仍在告警里**（T2 已出片
#   仍报 build 失败；05:30 那轮成功后仍挂着 5 条告警）。
#   → 治法：只认**真正表示失败的那一行**：
#     ① 行首是 [FAIL] / [失败]（产线的失败标记）
#     ② 关键词出现在 RuntimeError / Traceback 之类的异常上下文里
#     ③ 关键词在 JSON 快照行内（"xxx": "..." 形式）→ **排除**
#   这样「抄录的旧告警」不再被误判，「真的 build 失败」仍会被抓到。
_FAIL_LINE = re.compile(r'^\s*(\[FAIL\]|\[失败\]|FAIL\b|Traceback \(most recent call last\))')
_JSON_SNAPSHOT_LINE = re.compile(r'^\s*["\']?\w[\w\s./-]*["\']?\s*:\s*["\'].*["\']\s*,?\s*$')


def _is_real_failure_line(line: str, sign: str) -> bool:
    """这一行是否**真的**表示失败（而不是抄录/转述旧告警）。

    ★ 判据必须同时满足：
      ① 关键词 sign 就在这一行（否则 [失败] 校验未通过 那行根本不含 "build 失败"，
         不该被算成 build 失败 —— 关键词在**别的行**就归别的行管）
      ② 这一行是**失败行**而非转述行：
         · 行首 [FAIL] / [失败] / FAIL → 算
         · 含 RuntimeError / Error: / Traceback → 算
         · JSON 快照行（"xxx": "..."）→ 不算（那是抄录的旧告警）
    """
    s = line.strip()
    if not s or sign not in s:
        return False
    # JSON 快照行（告警数组里的字符串）→ 转述，不算本轮失败
    if _JSON_SNAPSHOT_LINE.match(s):
        return False
    # 产线显式失败标记 → 算
    if _FAIL_LINE.match(s):
        return True
    # 异常堆栈里的关键词 → 算（如 RuntimeError: VLM 请求失败…）
    if 'RuntimeError' in s or 'Error:' in s or 'Traceback' in s:
        return True
    return False


def sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout)
        return (r.stdout or '').strip()
    except Exception:
        return ''


def discover(root):
    """发现所有集：<任务>/out/<前缀> 目录 + 同任务 _plan/state-<ep>.json。

    ★ 2026-11-04 修（实测 K2 在跑却报 current=K1）：
      原来这里把**同一个任务的全部 state-*.json** 绑给该任务的每个 out 目录，
      于是 current_step() 遍历时，同一个任务下所有集的状态被反复比较，
      而「prefix」取自 out 目录名 → 谁的状态最新就报谁的 prefix，
      **但 prefix 与该状态所属的集根本不是同一集**。
      → 治法：state 按其自身记录的 prefix（state-<ep>.json 里的 prefix 字段）
        精确匹配到对应 out 目录；匹配不上的 state 不参与该目录的判定。
        （state 里可能没有 prefix 字段的旧档 → 回退用文件名集号匹配。）
    """
    eps = []
    for task in sorted(root.glob('*-e2e')):
        outroot = task / 'out'
        if not outroot.is_dir():
            continue
        # 先把该任务的 state 按 prefix 归位
        states_by_prefix = {}
        for sf in sorted((task / '_plan').glob('state-*.json')):
            pfx, ep_no = None, None
            try:
                d = json.loads(sf.read_text(encoding='utf-8'))
                pfx = (d.get('prefix') or '').strip() or None
                ep_no = (d.get('ep') or '').strip() or None
            except Exception:
                pass
            if pfx is None:
                # 旧档没有 prefix 字段 → 用文件名里的集号兜底
                m = re.search(r'state-(\d+)\.json$', sf.name)
                ep_no = ep_no or (m.group(1) if m else None)
            key = pfx or f'__ep{ep_no}'
            states_by_prefix.setdefault(key, []).append(sf)
        for out in sorted(outroot.glob('*')):
            if not out.is_dir():
                continue
            states = list(states_by_prefix.get(out.name, []))
            if not states:
                # 没有 prefix 匹配的 state → 回退按集号数字匹配
                m = re.search(r'(\d+)$', out.name)
                if m:
                    states = list(states_by_prefix.get(f'__ep{m.group(1)}', []))
            eps.append({'task': task.name, 'prefix': out.name, 'dir': out,
                        'states': states})
    return eps


def heartbeat(eps):
    newest, where = 0, ''
    for e in eps:
        for p in e['dir'].rglob('*'):
            try:
                if p.is_file():
                    m = p.stat().st_mtime
                    if m > newest:
                        newest, where = m, f"{e['task']}/{e['prefix']}/{p.relative_to(e['dir'])}"
            except Exception:
                continue
    return newest, where


def current_step(eps):
    """当前正在推进的集与步（按 state 的最近更新判断）。"""
    best = None
    for e in eps:
        for sf in e['states']:
            try:
                d = json.loads(sf.read_text(encoding='utf-8'))
            except Exception:
                continue
            ts = d.get('updated', '')
            steps = d.get('steps', {})
            todo = [k for k, v in steps.items() if v.get('status') != 'done']
            cand = {'task': e['task'], 'prefix': e['prefix'], 'updated': ts, 'todo': todo,
                    'done': sum(1 for v in steps.values() if v.get('status') == 'done')}
            if best is None or ts > best['updated']:
                best = cand
    return best


def scan_failures(root, recent_min=120):
    """只扫**近 recent_min 分钟内修改过**的日志——历史失败早处理了，
    一直挂在告警里只会淹没真正的新问题。"""
    import time as _t
    hits = []
    logs = list(root.glob('*/out/*.log')) + list(root.glob('_tools/*.log'))
    cutoff = _t.time() - recent_min * 60
    for lg in logs:
        try:
            if lg.stat().st_mtime < cutoff:
                continue
        except Exception:
            continue
        try:
            txt = lg.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        # ★ 逐行判定（不是全文 grep）：只认**真的表示失败**的行，
        #   排除「抄录的旧告警快照」造成的自我误报（见 _is_real_failure_line）。
        for sign, desc in FAIL_SIGNS.items():
            for line in txt.splitlines():
                if sign in line and _is_real_failure_line(line, sign):
                    hits.append(f'{lg.name}: {desc}')
                    break
    return hits


def sample(root, stall_min=6):
    now = time.time()
    eps = discover(root)
    hb, where = heartbeat(eps)
    age = int(now - hb) if hb else 10 ** 6
    cur = current_step(eps)
    vram = sh(['nvidia-smi', '--query-gpu=memory.used,memory.free,utilization.gpu',
               '--format=csv,noheader'])
    alive = ps.production_alive()

    # ★★ 2026-11-04 治根（告警永久误报的第二个变体）：
    #   「心跳没前进」有**两种**完全不同的含义，之前被混为一谈：
    #     ① 产线卡死了（真停滞）→ 必须告警
    #     ② 待产清单已全部出片 → **没东西可产**，心跳当然不前进（正常待命）
    #   实测：清单 6 集（X1/D1/T1/K1/T2/K2）全部出片后，
    #   心跳永远停在最后一个成片（K2 封面版）→ **每轮都报「停滞 N 分钟」**，
    #   永久误报 → 告警信噪比归零 → 真停滞会被淹没。
    #   → 停滞判定必须叠加「还有没有待产的集」这个前提。
    #   判据用**待产清单**（pipeline_state 单一真相源），不是 out/ 目录：
    #   out/ 里躺着大量历史集（46 个目录，其中 40 个早已出片），
    #   用 out/ 判「有没有活干」永远是「有活」→ 永远误报。
    pilot = ps.pilot_path(root)
    pending = ps.pending_entries(pilot)          # None = 清单读不到（未知）
    if pending is None:
        # 清单读不到 → **保守**：按「可能有活」处理，继续告警（宁可多报不可漏报）
        has_pending, pending_n, pending_where = True, -1, '清单读不到（保守按有活处理）'
    else:
        has_pending = bool(pending)
        pending_n = len(pending)
        pending_where = ('、'.join(e.get('prefix', '?') for e in pending)
                         if pending else '无（清单已全部出片）')
    stalled = age > stall_min * 60 and has_pending

    # ★ 失败告警同理：看门狗只对「当前计划内还没完成的工作」负责。
    #   计划内全部出片后，旧日志里的 build 失败是**历史**（已由后续重跑解决），
    #   继续挂着就是噪声。实测本轮就挂着一条 10:07 的 build 失败，
    #   而 K2 已在 10:44 重跑成功出片交付 —— 报「失败」是假的。
    #   → 有待产集时才报失败；清单读不到时照报（保守，宁可多报不可漏报）。
    fails = scan_failures(root) if has_pending else []

    snap = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'episodes': len(eps),
        'current': f"{cur['task']}/{cur['prefix']}" if cur else '-',
        'current_step': ((cur['todo'][0] if cur['todo'] else '（全部 done）') if cur else '-'),
        'steps_done': cur['done'] if cur else 0,
        'heartbeat_age_sec': age,
        'heartbeat_where': where,
        'stalled': stalled,
        'pending_episodes': pending_n,
        'pending_where': pending_where,
        'gpu': vram,
        'process_alive': alive,
        'alerts': fails + ([f'停滞 {age // 60} 分钟无新产出（阈值 {stall_min} 分钟，'
                            f'待产 {pending_where}）'] if stalled else []),
    }
    return snap


def persist(snap, wlog, status):
    """把快照落到 watchdog.log + status.json（常驻模式与 --once 共用）。

    ★ 为什么必须共用（2026-11-03 实踩）：
      `--once` 原来只 print 不落盘 → 调度器驱动的续跑每次只把状态打在 stdout，
      status.json/watchdog.log 永远停在**最后一次常驻看门狗的时刻**（实测停了 9.5 小时，
      status.json 还写着上一次 build 失败），看板上的一切都是过期信息。
      → 采样必须**每次都落盘**，否则「采样」这件事等于没做。
    """
    alert = ' ⚠ALERT' if snap['alerts'] else ''
    line = (f"[{snap['ts']}] {snap['current']:22s} 步={snap['current_step']:10s} "
            f"心跳{snap['heartbeat_age_sec']:5d}s GPU[{snap['gpu']}] "
            f"{snap['heartbeat_where']}{alert}")
    try:
        with wlog.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
            for x in snap['alerts']:
                f.write(f'    ⚠ {x}\n')
        status.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        line += f' [落盘失败：{e}]'
    return line


def main():
    ap = argparse.ArgumentParser(description='产线看门狗')
    ap.add_argument('--root', default=str(TASK_ROOT))
    ap.add_argument('--stall-min', type=int, default=6)
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args()
    root = Path(a.root)
    wlog, status = root / 'watchdog.log', root / 'status.json'
    if a.once:
        snap = sample(root, a.stall_min)
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        persist(snap, wlog, status)
        return 0
    print(f'[看门狗] 每 60s 采样 → {wlog}（停滞阈值 {a.stall_min} 分钟）')
    while True:
        snap = sample(root, a.stall_min)
        line = persist(snap, wlog, status)
        print(line, flush=True)
        for x in snap['alerts']:
            print(f'    ⚠ {x}', flush=True)
        time.sleep(60)


if __name__ == '__main__':
    main()
