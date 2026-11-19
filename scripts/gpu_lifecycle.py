# -*- coding: utf-8 -*-
"""gpu_lifecycle.py —— **GPU 资源串行管理**（VLM / TTS 互斥，2026-11-02 入技能）。

★ 为什么必须有：技能里 `shot_evidence.py` **不起 VLM**（假定外面起好了），
  `build.py` **不起 TTS 服务**（假定外面起好了）→ 整条链没有一处负责
  「现在该谁占着卡」。而本机 12.28GB 显存：VLM(-ngl 999) ≈8GB、
  dotstts 服务 ≈6GB、ASR ≈1.4GB → **两个大件叠加必 OOM**
  （2026-10-28 P80：TTS 常驻不释放，下一集起 VLM 启动失败，三集连挂）。

  纪律（来自 engine.json 的 runtime_policy.gpu_serialization=true）：
    采证据阶段 → **先停 TTS**、再起 VLM；
    渲染阶段   → **先停 VLM**、再起 TTS；
    任何一步失败都要确保不留半开的进程占着卡。

★ 真推理探针（2026-11-02 治根）：`/health` 只证明 HTTP 服务活着，
  **不能证明模型还能推理**。被 OOM 打断过的 llama-server 仍应答 /health，
  但 /chat/completions 永不返回 → 证据采集静默卡死（CPU≈0、显存不涨、
  临时帧一个不出）。故起 VLM 后必须发一次极小的补全请求做真探测，
  不通过就杀掉重启。

用法：
    python gpu_lifecycle.py ensure-vlm     # 停 TTS → 起 VLM → 真探测
    python gpu_lifecycle.py stop-vlm
    python gpu_lifecycle.py ensure-tts     # 起 dotstts 常驻服务
    python gpu_lifecycle.py stop-tts
    python gpu_lifecycle.py status
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
RT = CFG.get('runtime', {})
DUB_PY = Path('D:/AITools/TTS/Dots.tts/wzf/python.exe')
DUB = Path(__file__).resolve().parent.parent.parent / '本地配音工坊' / 'dub.py'
VLM_ENDPOINT = str(RT.get('vlm_endpoint') or 'http://127.0.0.1:18088').rstrip('/')
VLM_PORT = int(VLM_ENDPOINT.rsplit(':', 1)[-1])
# ★ dub.py 的 --engine 取的是**引擎目录名**（Dots.tts → dotstts），
#   不是 engine.json 的 tts.engine（那是模型名 dots-tts-mf）——填错会起不来。
TTS_ENGINE = 'dotstts'


def log(m):
    print(f'[gpu] {m}', flush=True)


def http_alive(url, timeout=4):
    try:
        urllib.request.urlopen(url, timeout=timeout).read()
        return True
    except Exception:
        return False


def vlm_inference_alive(port=None, timeout=90):
    """真推理探针：发一个极小的补全请求，90s 内不返回就判定假死。"""
    port = port or VLM_PORT
    try:
        body = json.dumps({'model': 'local',
                           'messages': [{'role': 'user', 'content': '只回复数字1'}],
                           'max_tokens': 8, 'temperature': 0}).encode('utf-8')
        req = urllib.request.Request(f'http://127.0.0.1:{port}/chat/completions',
                                     data=body,
                                     headers={'Content-Type': 'application/json'},
                                     method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(r.read())
    except Exception:
        return False


def kill_by_cmdline(match, name=None):
    """按命令行特征杀进程（Windows）。"""
    ps = ('Get-CimInstance Win32_Process | Where-Object { '
          f'$_.CommandLine -match "{match}" -and $_.CommandLine -notmatch '
          "'Get-CimInstance' } | Select-Object -ExpandProperty ProcessId")
    r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=60)
    n = 0
    for tok in (r.stdout or '').split():
        if tok.isdigit():
            if subprocess.run(['taskkill', '/PID', tok, '/T', '/F'],
                              capture_output=True).returncode == 0:
                n += 1
    return n


def stop_tts():
    """停 dotstts 常驻服务（--stop-all-serve：只停默认引擎停不掉 dotstts）。"""
    if not DUB.is_file():
        log('配音工坊 dub.py 不存在，跳过停 TTS')
        return
    subprocess.run([str(DUB_PY), '-X', 'utf8', str(DUB), '--stop-all-serve'],
                   capture_output=True, text=True, encoding='utf-8', errors='replace',
                   timeout=90)
    log('TTS 常驻服务已停')


def ensure_tts():
    """起 dotstts 常驻服务（起不来也不阻断：dub.py 会自动回退一次性子进程）。"""
    r = subprocess.run([str(DUB_PY), '-X', 'utf8', str(DUB), '--serve',
                        '--engine', TTS_ENGINE],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=120)
    tail = (r.stdout or '').strip().splitlines()[-1:] or ['']
    log(f'TTS 常驻：{tail[0][:70]}')


def stop_vlm():
    n = kill_by_cmdline('llama-server')
    log(f'VLM 已停（kill {n} 个进程）')
    time.sleep(2)


def ensure_vlm(probe=True):
    """停 TTS → 起 VLM → 真推理探针；探不过就重启一次。"""
    stop_tts()
    if http_alive(f'http://127.0.0.1:{VLM_PORT}/health'):
        if not probe or vlm_inference_alive(VLM_PORT):
            log('VLM 已在运行（真探测通过）')
            return True
        log('VLM 端口活着但推理无响应（假死）→ 重启')
        stop_vlm()
    llama = RT.get('llama_server', 'D:/AITools/VLM/bin/llama-server.exe')
    model = RT.get('vlm_model', 'D:/AITools/VLM/models/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf')
    mmproj = RT.get('mmproj', 'D:/AITools/VLM/models/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-F16.gguf')
    cmd = [llama, '-m', model, '--mmproj', mmproj, '--host', '127.0.0.1',
           '--port', str(VLM_PORT), '-ngl', '999', '-c', '8192',
           '--image-min-tokens', '1024', '--jinja']
    subprocess.Popen(cmd, cwd=str(Path(llama).parent),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    for _ in range(90):
        if http_alive(f'http://127.0.0.1:{VLM_PORT}/health'):
            log('VLM 端口已通')
            break
        time.sleep(1)
    if probe and not vlm_inference_alive(VLM_PORT):
        log('★ VLM 真探测失败（模型未就绪或假死）')
        return False
    log('VLM 就绪（真探测通过）')
    return True


def status():
    import subprocess as _sp
    vram = _sp.run(['nvidia-smi', '--query-gpu=memory.used,memory.free',
                     '--format=csv,noheader'], capture_output=True, text=True).stdout.strip()
    print(json.dumps({
        'vlm_health': http_alive(f'http://127.0.0.1:{VLM_PORT}/health'),
        'vlm_inference': vlm_inference_alive(VLM_PORT, timeout=20),
        'gpu': vram,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'ensure-vlm':
        ok = ensure_vlm()
        sys.exit(0 if ok else 1)
    elif cmd == 'stop-vlm':
        stop_vlm()
    elif cmd == 'ensure-tts':
        ensure_tts()
    elif cmd == 'stop-tts':
        stop_tts()
    else:
        status()
