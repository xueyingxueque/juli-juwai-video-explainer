# -*- coding: utf-8 -*-
r"""watch_downloads.py —— **素材下载就绪监控**（v4.0.0 前置能力）。

用户令（2026-11-07）：「D:\BaiduNetdiskDownload 是素材下载文件夹，正在下载俩部剧，
等下载完开始剪辑，要监控，下载完立马剪辑，不要问，自动执行」。

★ 为什么不能用「目录存在 / 有 mp4」当完成判据：
  百度网盘客户端是**队列式**下载（同时 5 个 `*.mp4.baiduyun.p.downloading`），
  完成一批到起下一批之间有一小段「临时文件为 0」的空档。
  只用「临时文件消失」判完成 → 会把**批次空档**误判成全剧下完 →
  拿 5 集素材开工，产出残片（正是坑典 P84「告警/闸门成立条件」的同族病）。

判据（全部满足才 READY）：
  ① 临时下载文件数 == 0
  ② mp4 数 >= 1
  ③ 目录名含「N集」时，mp4 数 >= N；否则集号从 1 连续
  ④ **连续 STABLE_SAMPLES 次采样** mp4 数与总字节都不变
  ⑤ 距最后一次文件变化 >= QUIET_SEC（无 expected 的剧用更长的 QUIET_SEC_NO_EXPECTED）
  ⑥ 每个 mp4 都能被 ffprobe 解析出 duration > 10s（防半成品）
  ⑦ 该集尚未出片（`pipeline_state.is_produced` 口径）——「这件事还需要被做吗」

状态持久化在 `_tools/download_state.json`；READY 时写 `_tools/READY_<task>.json`。
**幂等**：重复调用不重复触发；READY 文件存在且未出片才算待办。

子命令：
    python watch_downloads.py once                 # 采样一次，打印状态 JSON
    python watch_downloads.py wait --interval 45   # 持续监控（后台链用）
    python watch_downloads.py bootstrap            # 对 READY 的剧建档 + ingest（串行）
    python watch_downloads.py status               # 只看台账，不采样
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_state as ps          # noqa: E402  「已出片」唯一判据
from plan_story import ep_from_name  # noqa: E402  集号解析唯一正典

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / 'scripts'
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
FFPROBE = CFG['runtime']['ffprobe']
FFMPEG = CFG['runtime']['ffmpeg']
ROOT = Path(CFG.get('paths', {}).get('material_root', 'D:/BaiduNetdiskDownload'))
TASK_ROOT = Path(CFG.get('paths', {}).get('task_root', 'D:/剧里剧外/视频解说/任务'))
TOOLS = TASK_ROOT / '_tools'
STATE = TOOLS / 'download_state.json'
PILOT = TOOLS / 'episodes.json'
DASH_DIR = TOOLS / 'dashboard'
DASH_DATA = DASH_DIR / 'data.js'
STAGES = ['precheck', 'evidence', 'assemble', 'bgm', 'gen', 'v37',
          'build', 'qc', 'cover', 'deliver']
# 中文解释版（看板 10 步用；与 SKILL.md §0.2 十步状态机一一对应）
STAGES_ZH = [
    ['预检去重', 'spec 预检 + 跨集内容查重，秒级拦截硬伤，不许带病进证据'],
    ['采集证据', '本地 VLM 逐镜看原片，生成带画面描述的逐句证据 ev-N.json'],
    ['组装脚本', '证据 → 剪辑 plan，自动挂 CTA，校验镜头序不按集数顺序灌'],
    ['选配乐', '自动选 BGM 并回写 plan；解说段压低、原声段抬升，有口播禁人声'],
    ['生成二创图', 'ComfyUI 本地出图；先停 VLM 腾显存，避免 12GB 卡 OOM'],
    ['插入二创', '二创图与表情包按精确锚点插入 plan，不许模糊兜底插错句'],
    ['配音渲染', 'TTS 配音 → 切画面 → 混音 → 烧字幕 → 校验，不过不产出'],
    ['质量门禁', '硬门/软门质检 + A/B/C 分档；时长实测 60–300s，不过不交付'],
    ['制作封面', '官方海报做底 + 主副标 + 集数角标，烧进成片首帧'],
    ['交付网盘', '单文件交付到 <剧名>/解说成品/，收尾跑 13 项成品校验'],
]
# 剧名 → 任务目录（显式映射；未映射的剧自动用 ASCII slug 兜底）
CONF = TOOLS / 'download_watch.json'
DEFAULT_NAMING = {
    '离职厨娘自力更生': 'lcnc-e2e',
    '萌娃锦鲤三岁半，荒界异兽排队送宝箱30集': 'mwjl-e2e',
}
TMP_SUFFIX = ('.downloading', '.part', '.tmp', '.crdownload', '.!ut', '.baiduyun.p')
VIDEO_EXT = ('.mp4', '.mkv', '.avi', '.mov')
STABLE_SAMPLES = 3
QUIET_SEC = 180
QUIET_SEC_NO_EXPECTED = 420
MIN_DURATION = 10.0


def log(m):
    print(f'[watch {time.strftime("%H:%M:%S")}] {m}', flush=True)


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding='utf-8'))
    except Exception:
        return default


def naming():
    """剧名 → 任务目录名。未配置的新剧自动生成纯 ASCII slug 并写回配置。"""
    conf = load_json(CONF, {})
    changed = False
    for series, task in DEFAULT_NAMING.items():
        if series not in conf:
            conf[series] = {'task': task}
            changed = True
    if ROOT.is_dir():
        for d in sorted(ROOT.iterdir()):
            if not d.is_dir() or d.name in conf:
                continue
            slug = 'dw-' + hashlib.md5(d.name.encode('utf-8')).hexdigest()[:6] + '-e2e'
            conf[d.name] = {'task': slug, 'auto': True}
            changed = True
            log(f'发现新剧「{d.name}」→ 自动分配任务目录 {slug}')
    if changed:
        CONF.parent.mkdir(parents=True, exist_ok=True)
        CONF.write_text(json.dumps(conf, ensure_ascii=False, indent=1), encoding='utf-8')
    return conf


def expected_eps(name):
    m = re.search(r'(\d+)\s*集', name)
    return int(m.group(1)) if m else None


def scan_dir(d):
    files = [p for p in d.iterdir() if p.is_file()]
    tmp = [p for p in files if any(str(p).lower().endswith(s.lower()) for s in TMP_SUFFIX)]
    vids = [p for p in files if p.suffix.lower() in VIDEO_EXT]
    total = sum(p.stat().st_size for p in files)
    last = max((p.stat().st_mtime for p in files), default=0.0)
    eps = []
    for p in vids:
        try:
            n = ep_from_name(p.stem)
            if n:
                eps.append(int(n))
        except Exception:
            pass
    return {'tmp': len(tmp), 'mp4': len(vids), 'bytes': total,
            'last_mtime': last, 'eps': sorted(set(eps)),
            'tmp_names': [p.name for p in tmp[:3]]}


def probe_ok(d):
    """所有 mp4 必须能被 ffprobe 解析出时长 > MIN_DURATION（防半成品）。"""
    vids = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]
    if not vids:
        return False, '无 mp4'
    for p in vids:
        try:
            r = subprocess.run([FFPROBE, '-v', 'error', '-show_entries',
                                'format=duration', '-of', 'csv=p=0', str(p)],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=30)
            dur = float((r.stdout or '').strip().splitlines()[0])
            if dur < MIN_DURATION:
                return False, f'{p.name} 时长 {dur:.1f}s < {MIN_DURATION}s'
        except Exception as e:
            return False, f'{p.name} 不可解析: {type(e).__name__}'
    return True, f'{len(vids)} 个文件均可解析'


def sample(state, conf):
    """采样一次并更新台账 → 返回 (reports, newly_ready)。"""
    now = time.time()
    newly = []
    reports = []
    for series, meta in conf.items():
        d = ROOT / series
        task = meta['task']
        if not d.is_dir():
            reports.append({'series': series, 'state': 'MISSING', 'reason': '目录不存在'})
            continue
        cur = scan_dir(d)
        exp = expected_eps(series)
        rec = state.setdefault(series, {'samples': [], 'ready': False, 'bootstrapped': False})
        sig = (cur['mp4'], cur['bytes'], cur['tmp'])
        # ★★ 每次采样都要留痕（2026-11-07 实踩的死锁）：
        #   旧版只在「sig 变化」时 append，结果下载一稳定就不再追加样本，
        #   「连续 N 次相同」永远凑不满 → READY 永远不成立 →
        #   素材 12:46 下完、到 14:00 仍没开工（实测卡了 9 小时）。
        #   正确做法：每轮都记录，判定看**最后 N 轮是否全等于当前值**。
        samples = rec['samples']
        samples.append({'ts': now, 'sig': list(sig)})
        rec['samples'] = samples[-12:]
        stable = len(rec['samples']) >= STABLE_SAMPLES and all(
            s['sig'] == list(sig) for s in rec['samples'][-STABLE_SAMPLES:])
        quiet = now - cur['last_mtime'] >= (QUIET_SEC if exp else QUIET_SEC_NO_EXPECTED)
        eps_ok = True
        reason = ''
        if exp:
            eps_ok = cur['mp4'] >= exp
            if not eps_ok:
                reason = f'集数 {cur["mp4"]}/{exp}'
        elif cur['eps']:
            eps_ok = cur['eps'] == list(range(1, len(cur['eps']) + 1))
            if not eps_ok:
                reason = f'集号不连续 {cur["eps"][:8]}'
        checks = {
            'no_tmp': cur['tmp'] == 0,
            'has_mp4': cur['mp4'] >= 1,
            'count_ok': eps_ok,
            'stable': stable,
            'quiet': quiet,
        }
        if all(checks.values()):
            pok, pmsg = probe_ok(d)
            checks['probe'] = pok
            if not pok:
                reason = pmsg
        else:
            checks['probe'] = None      # 未到这一步，别浪费时间 ffprobe
        # ⑦「这件事还需要被做吗」：已出片的集不该再触发开工（P84 判据）
        task_dir = TASK_ROOT / task
        produced = bool(list((task_dir / 'out').glob('*/*-封面版.mp4')))
        checks['needs_work'] = not produced
        if produced:
            reason = '已有成片，无待办'
        ready = all(v is True for v in checks.values())
        prefix = ''.join(c for c in task.split('-')[0].upper() if c.isalnum())[:4] or 'EP'
        rec['last'] = {**cur, 'expected': exp, 'checks': checks,
                       'ts': time.strftime('%Y-%m-%d %H:%M:%S')}
        if ready and not rec.get('ready'):
            rec['ready'] = True
            rec['ready_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            rf = TOOLS / f'READY_{task}.json'
            rf.write_text(json.dumps({
                'series': series, 'dataset': str(d), 'task': str(TASK_ROOT / task),
                'task_name': task, 'expected_eps': exp, 'mp4': cur['mp4'],
                'ready_at': rec['ready_at'], 'prefix': prefix,
                'note': '下载完成判据全过；待 Agent 写 spec 后生产',
            }, ensure_ascii=False, indent=1), encoding='utf-8')
            newly.append(rf)
            log(f'★ READY {series}（{cur["mp4"]} 集 / {cur["bytes"]/1048576:.0f}MB）')
        reports.append({'series': series, 'task': task, 'mp4': cur['mp4'],
                        'expected': exp, 'tmp': cur['tmp'],
                        'MB': round(cur['bytes'] / 1048576, 1),
                        'checks': checks, 'state': 'READY' if rec.get('ready') else 'DOWNLOADING',
                        'reason': reason,
                        'bootstrapped': ps.ingest_ready(
                            TASK_ROOT / task, min(10, max(1, cur['mp4'])))})
    return reports, newly


def write_dashboard(reports, state, conf):
    """把真实状态写成 `data.js` —— HTML 看板用 <script> 加载（不受 file:// CORS 限制）。

    ★ 为什么用 .js 而不是 .json：本地 `file://` 页面 fetch JSON 会被 CORS 拒绝，
      但 <script src> 加载同目录 JS 一定可用。看板每 3 秒带 cache-buster 重载它。
    """
    DASH_DIR.mkdir(parents=True, exist_ok=True)
    try:
        g = subprocess.run(['nvidia-smi', '--query-gpu=memory.used,memory.free',
                            '--format=csv,noheader'], capture_output=True,
                           text=True, timeout=15)
        gpu = (g.stdout or '').strip()
    except Exception:
        gpu = '读取失败'
    try:
        nproc = len(ps.production_pids())
    except Exception:
        nproc = 0
    pilot = load_json(PILOT, [])
    ready_files = sorted(TOOLS.glob('READY_*.json'))

    dramas = []
    for r in reports:
        ep_exp = r.get('expected')
        if ep_exp:
            pct = min(100, round(r['mp4'] / ep_exp * 100))
        else:
            pct = min(95, round((r['mp4'] + r['tmp']) * 8))
        rec = state.get(r['series'], {})
        # ★ 展示也按实际产物判定，不认 bootstrapped 布尔标志（与 bootstrap 同一判据）
        need = min(10, max(1, r['mp4']))
        ingested = ps.ingest_ready(TASK_ROOT / r['task'], need)
        if r['state'] == 'READY' and ingested:
            stage_index = 2
        elif r['state'] == 'READY':
            stage_index = 0
        else:
            stage_index = -1
        # 若该任务已有成片 → 10 步全完成
        task_dir = TASK_ROOT / r['task']
        if list((task_dir / 'out').glob('*/*-封面版.mp4')):
            stage_index = 10
            pct = 100
        dramas.append({**r, 'pct': pct, 'stage_index': stage_index,
                       'ready_at': rec.get('ready_at', '')})

    # 自动播放源：优先播**新剧原片片段**。
    # ★ 不能直接给 file:// URL：看板是 http 预览页，浏览器禁止 http 页面加载 file:// 视频。
    #   → 用 ffmpeg 截 12s（**保留音轨**）到看板同目录，用相对路径播放。
    #   签名（文件名+大小+mtime）不变时不重复转码。
    video_url = 'demo.mp4'
    newsrc = None
    for d in dramas:
        ds = ROOT / d['series']
        vids = sorted(p for p in ds.glob('*.mp4') if p.is_file()) if ds.is_dir() else []
        if vids:
            newsrc = vids[0]
            break
    if newsrc:
        try:
            st = newsrc.stat()
            sig = f'{newsrc.name}|{st.st_size}|{int(st.st_mtime)}'
            clip, sigf = DASH_DIR / 'current.mp4', DASH_DIR / 'current.sig'
            old_sig = sigf.read_text(encoding='utf-8').strip() if sigf.is_file() else ''
            if old_sig != sig or not clip.is_file():
                r = subprocess.run(
                    [FFMPEG, '-v', 'error', '-y', '-ss', '3', '-i', str(newsrc), '-t', '12',
                     '-vf', 'scale=270:480:force_original_aspect_ratio=decrease,'
                            'pad=270:480:(ow-iw)/2:(oh-ih)/2',
                     '-c:v', 'libx264', '-crf', '28', '-preset', 'veryfast',
                     '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k',
                     '-ar', '44100', '-ac', '2', '-movflags', '+faststart', str(clip)],
                    capture_output=True, text=True, timeout=180)
                if r.returncode == 0 and clip.is_file() and clip.stat().st_size > 10000:
                    sigf.write_text(sig, encoding='utf-8')
                    log(f'看板播放源已更新 → {newsrc.name}（12s 片段，含原声）')
            if clip.is_file() and clip.stat().st_size > 10000:
                video_url = 'current.mp4'
        except Exception as e:
            log(f'[warn] 新剧片段生成失败，回退演示片段：{type(e).__name__}: {e}')

    logs = []
    for d in dramas:
        if d['state'] == 'READY':
            logs.append(f"[{d['ready_at'][-8:] or '现在'}] READY · {d['series']} 下载完成判据全过")
        elif d['reason']:
            logs.append(f"[{time.strftime('%H:%M:%S')}] {d['series']} · {d['reason']}")
        else:
            logs.append(f"[{time.strftime('%H:%M:%S')}] {d['series']} · 下载中 "
                        f"{d['mp4']} 集成片 / {d['tmp']} 个临时文件 / {d['MB']}MB")
    logs.append(f'[{time.strftime("%H:%M:%S")}] 监控采样完成 · 待办 {len(ready_files)} 部 / 清单 {len(pilot)} 集')
    logs.append(f'[{time.strftime("%H:%M:%S")}] GPU {gpu} · 产线进程 {nproc}')

    data = {
        'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'gpu': gpu, 'processes': nproc, 'pilot_count': len(pilot),
        'ready_count': len([d for d in dramas if d['state'] == 'READY']),
        'dramas': dramas, 'stages': STAGES, 'stages_zh': STAGES_ZH,
        'logs': logs[-8:],
        'video_url': video_url,
        'policy': '60s < 实测 < 300s · 每集内容强制去重 · 清单驱动',
    }
    DASH_DATA.write_text('window.DASH_DATA = ' +
                         json.dumps(data, ensure_ascii=False, indent=1) + ';\n',
                         encoding='utf-8')


def bootstrap(conf):
    """对 READY 剧建档 + 补字幕入库（串行，幂等）。

    ★★ 2026-11-07 治根：**不再用 `bootstrapped` 布尔标志当门**。
      旧版进程被会话回收时标志没写 → 队列链一直等它 → 永不启动，而盘上其实已有
      10/16 集。布尔标志是第二份状态，必然与磁盘漂移（P76/P84 同族）。
      → 唯一判据 = `pipeline_state.ingest_ready(task, need)` 数 `_s1/ep*` 实际目录。
    ★ 只做纯脚本能做的部分；写 spec / gencfg 属内容创作，交给 Agent 层。
    """
    state = load_json(STATE, {})
    jobs = []
    for series, meta in conf.items():
        rec = state.get(series) or {}
        if not rec.get('ready'):
            continue
        rf = TOOLS / f'READY_{meta["task"]}.json'
        if not rf.is_file():
            continue
        ready = json.loads(rf.read_text(encoding='utf-8'))
        need = min(10, max(1, ready.get('mp4', 1)))
        if ps.ingest_ready(Path(ready['task']), need):
            continue                      # 实际产物已够，跳过（不管标志写没写）
        jobs.append((series, meta, ready, need))
    if not jobs:
        return []
    done = []
    for series, meta, ready, need in jobs:
        d, task, n = Path(ready['dataset']), Path(ready['task']), ready['mp4']
        log(f'bootstrap {series} → {task.name}（{n} 集，补到 {need}）')
        if not (task / '_plan').is_dir():
            r = subprocess.run([sys.executable, str(SCRIPTS / 'new_task.py'),
                                '--name', meta['task'], '--dataset', str(d),
                                '--episodes', str(n)],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=300)
            log(f'  new_task rc={r.returncode} {(r.stdout or "")[-200:]}')
            if r.returncode != 0:
                log(f'  [FAIL] 建档失败，跳过 ingest：{(r.stderr or "")[-300:]}')
                continue
        ingest = TOOLS / 'ingest_all.py'
        if ingest.is_file():
            log(f'  ingest 1..{need} / 共 {n} 集（日志 {task.name}/out/ingest.log）')
            r = subprocess.run([sys.executable, '-u', str(ingest), str(task), str(d),
                                '1', str(need)],
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=7200)
            (task / 'out').mkdir(parents=True, exist_ok=True)
            (task / 'out' / 'ingest.log').write_text(
                (r.stdout or '') + '\n' + (r.stderr or ''), encoding='utf-8')
            log(f'  ingest rc={r.returncode}')
        else:
            log('  [warn] _tools/ingest_all.py 不存在，跳过 ingest')
        # 标志只是**展示用快照**，真正判据仍是 ingest_ready()
        state.setdefault(series, {})['bootstrapped'] = ps.ingest_ready(task, need)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
        done.append(ready['task_name'])
    return done


def main():
    ap = argparse.ArgumentParser(description='素材下载就绪监控')
    ap.add_argument('cmd', nargs='?', default='once',
                    choices=['once', 'wait', 'bootstrap', 'status'])
    ap.add_argument('--interval', type=int, default=45)
    ap.add_argument('--max-minutes', type=int, default=0, help='wait 最长分钟数，0=不限')
    a = ap.parse_args()
    TOOLS.mkdir(parents=True, exist_ok=True)

    if a.cmd == 'status':
        print(json.dumps(load_json(STATE, {}), ensure_ascii=False, indent=2))
        return 0

    conf = naming()
    state = load_json(STATE, {})
    if a.cmd == 'bootstrap':
        done = bootstrap(conf)
        print(json.dumps({'bootstrapped': done}, ensure_ascii=False))
        return 0

    deadline = time.time() + a.max_minutes * 60 if a.max_minutes else None
    while True:
        reports, newly = sample(state, conf)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
        write_dashboard(reports, state, conf)     # 看板真实数据（HTML 每 3s 重载）
        print(json.dumps({'ts': time.strftime('%H:%M:%S'),
                          'reports': reports,
                          'newly_ready': [p.name for p in newly]},
                         ensure_ascii=False, indent=1), flush=True)
        if a.cmd == 'once':
            return 0
        # wait：发现 READY 立刻 bootstrap（不等下一轮）
        if newly:
            bootstrap(conf)
            write_dashboard(reports, state, conf)   # bootstrap 后立刻刷新看板
        if deadline and time.time() >= deadline:
            log('达到最长监控时长，退出')
            return 0
        time.sleep(a.interval)


if __name__ == '__main__':
    sys.exit(main())
