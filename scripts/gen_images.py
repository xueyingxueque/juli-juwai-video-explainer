# -*- coding: utf-8 -*-
"""gen_images.py —— ComfyUI 出 AI 二创镜头（技能八步里原本**缺**这一步）。

★ 为什么补（2026-11-02）：技能文档的八步只有「手写 spec → 采证据 → 装配 →
build」，**二创图与表情包从哪来、怎么出，全文没写** → 照文档做的人做不出
二创镜头；而这部分能力一直只活在任务目录的 _tools/ 下（技能外）。
现在收敛进技能：基础工作流在 skills/.../assets/comfy/c2a.json，路径在 engine.json。

链路（全部走本地 ComfyUI，**不用付费生图、不带任何水印**）：
    clone_wf.py      基础工作流 + 提示词/种子/前缀 → UI 工作流
    ui_to_api2.py    UI 工作流 → API 格式
    run_api_prompt.py  提交渲染（带重试：首次提交要加载模型，常失败）

用法：
    python gen_images.py <任务目录> <集前缀> <gencfg.json>
    python gen_images.py <任务目录> <集前缀> <gencfg.json> --skip-comfy   # 只查已有图
产物：<任务目录>/gen/<集前缀>_<img>_*.png（归档命名，make_plan_v37 按它取图）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
P = CFG.get('paths', {})
COMFY_PORT = 8188
PY = sys.executable


def log(msg):
    print(f'[gen] {msg}', flush=True)


def port_alive(port, path='/system_stats'):
    import urllib.request
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=4).read()
        return True
    except Exception:
        return False


def ensure_comfy():
    """干净重启 ComfyUI（复用旧实例可能 CUDA 上下文已损坏，且会与 llama-server 抢显存）。"""
    if port_alive(COMFY_PORT):
        log('清理旧 ComfyUI 实例…')
        subprocess.run(['taskkill', '/IM', 'python.exe', '/FI',
                        f'COMMANDLINE like *ComfyUI*', '/T', '/F'],
                       capture_output=True, text=True)
        time.sleep(5)
    cmd = [str(Path(P.get('comfy_home', 'D:/AITools/ComfyUI')) / '.venv/Scripts/python.exe'),
           'main.py', '--listen', '127.0.0.1', '--port', str(COMFY_PORT),
           '--cuda-malloc', '--preview-method', 'auto']
    subprocess.Popen(cmd, cwd=str(P.get('comfy_home', 'D:/AITools/ComfyUI')),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    for _ in range(60):
        if port_alive(COMFY_PORT):
            log('ComfyUI 端口已通（模型懒加载，出图环节会重试）')
            return True
        time.sleep(1)
    log('★ ComfyUI 启动失败')
    return False


def stop_comfy():
    subprocess.run(['taskkill', '/IM', 'python.exe', '/FI',
                    'COMMANDLINE like *ComfyUI*', '/T', '/F'],
                   capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser(description='ComfyUI 出 AI 二创镜头')
    ap.add_argument('task')
    ap.add_argument('prefix')
    ap.add_argument('gencfg')
    ap.add_argument('--skip-comfy', action='store_true', help='只检查已有图，不出图')
    a = ap.parse_args()

    task = Path(a.task)
    gencfg = json.loads(Path(a.gencfg).read_text(encoding='utf-8'))
    agent = Path(P.get('comfy_agent', 'D:/AITools/comfy-agent'))
    home = Path(P.get('comfy_home', 'D:/AITools/ComfyUI'))
    outdir = Path(P.get('comfy_output', str(home / 'output')))
    base = SKILL / P.get('comfy_base', 'assets/comfy/c2a.json')
    gen_dir = task / 'gen'
    wf_dir = gen_dir / '_wf'
    wf_dir.mkdir(parents=True, exist_ok=True)

    entries = gencfg.get('gen', [])
    if not entries:
        log('gencfg 无 gen 条目，跳过')
        return 0

    # 已有图就够 → 直接过（断点续跑）
    have = {g['img']: sorted(gen_dir.glob(f"{a.prefix}_{g['img']}_*.png")) for g in entries}
    if all(v for v in have.values()):
        log(f'二创图已齐全（{len(entries)} 张），跳过出图')
        for k, v in have.items():
            log(f'  {k}: {v[-1].name}')
        return 0
    if a.skip_comfy:
        log('★ 缺图且指定 --skip-comfy：')
        for k, v in have.items():
            if not v:
                log(f'  缺 {k}')
        return 1

    if not ensure_comfy():
        return 2

    apis = []
    for g in entries:
        wfp = wf_dir / f"{a.prefix.lower()}_{g['img']}.json"
        apip = wf_dir / f"{a.prefix.lower()}_{g['img']}_api.json"
        r = subprocess.run([PY, str(agent / 'clone_wf.py'), str(base), str(wfp),
                            '--prompt', g['prompt'], '--prefix', f"{a.prefix}_{g['img']}",
                            '--seed', str(g.get('seed', 12345))],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=120)
        if r.returncode != 0:
            log(f'★ 工作流克隆失败：{r.stderr[-200:]}')
            stop_comfy()
            return 3
        r = subprocess.run([PY, str(agent / 'ui_to_api2.py'), str(wfp), '--out', str(apip)],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=120)
        if r.returncode != 0:
            log(f'★ UI→API 失败：{r.stderr[-200:]}')
            stop_comfy()
            return 3
        apis.append((str(apip), g['img']))

    log(f'提交 {len(apis)} 张二创图…')
    done = []
    for attempt in range(1, 4):
        subprocess.run([PY, str(agent / 'run_api_prompt.py')] + [x[0] for x in apis]
                       + ['--timeout', '1200'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=2400)
        done = []
        for apip, img in apis:
            hits = sorted(outdir.glob(f'{a.prefix}_{img}_*.png'))
            if hits:
                done.append((img, hits[-1]))
        if len(done) == len(apis):
            break
        log(f'  第 {attempt} 次提交：{len(done)}/{len(apis)} 张成功，20s 后重试')
        time.sleep(20)
    stop_comfy()

    for img, src in done:
        shutil.copy(str(src), str(gen_dir / src.name))
        log(f'  图 OK: {src.name}')
    if len(done) < len(apis):
        miss = [i for i, _ in apis if i not in [d[0] for d in done]]
        log(f'★ 二创图未生成: {miss}')
        return 4
    return 0


if __name__ == '__main__':
    sys.exit(main())
