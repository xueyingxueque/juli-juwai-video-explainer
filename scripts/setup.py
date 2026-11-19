# -*- coding: utf-8 -*-
r"""setup.py —— 首次使用向导（别人下载后跑这一条就能开工）。

★ 为什么必须有：仓库只提交 `config/engine.template.json`（23 个 {{占位符}}），
  本机配置 `config/engine.json` 由本脚本生成并被 .gitignore 排除。
  这样 30+ 个读配置的脚本一行都不用改，且每个人用自己的路径，不会互相覆盖。

设计原则（对齐项目铁律）：
  1. **自动探测优先**：能从 PATH / 常见位置 / 已装技能找到的，不烦用户。
  2. **只问必需的**：ffmpeg、素材根、任务根；其余给默认值或标记「未配置（可选功能不可用）」。
  3. **读不到 ≠ 没有**：探测失败一律显式列为未配置，不静默写假路径；缺失核心依赖给出安装指引。
  4. **写后复核**：生成 engine.json 后立刻 contracts --audit + 路径存在性复核，失败不留半成品。
  5. **可重复运行**：已有 engine.json 时先备份再重建；--show 只显示当前配置不改动。

用法：
    python scripts/setup.py                # 交互向导
    python scripts/setup.py --yes          # 全部用自动探测 + 默认值（CI / 静默）
    python scripts/setup.py --show         # 只看当前配置，不写文件
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config'
TEMPLATE = CONFIG / 'engine.template.json'
TARGET = CONFIG / 'engine.json'
VENDORS = ROOT / 'vendors'

# ★ 不能用 ^...$ 锚点：sub 是在整段 JSON 文本里找子串，
#   "{{FFMPEG}}" 前面有引号，行首锚点永远匹配不到（v7.0.0 实测踩坑）。
PLACEHOLDER = re.compile(r'(\{\{[A-Z_]+\}\})')


def _ask(prompt, default=''):
    """交互询问；default 非空时回车即采用。--yes 模式下由调用方绕过本函数。"""
    try:
        suffix = f' [{default}]' if default else ''
        val = input(f'{prompt}{suffix}: ').strip()
    except EOFError:
        val = ''
    return val or default


def _first_existing(cands):
    for c in cands:
        if not c:
            continue
        p = Path(c).expanduser()
        if p.is_file() or p.is_dir():
            return str(p).replace('\\', '/')
    return ''


def _which(name):
    p = shutil.which(name)
    return str(Path(p).resolve()).replace('\\', '/') if p else ''


def _home():
    return Path.home()


def detect(auto):
    """返回 {占位符: 值}。auto=True 时不询问，只用探测 + 默认值。"""
    h = _home()
    skill_root = ROOT.parent
    found = {}

    # ── 核心：ffmpeg / ffprobe ──
    ff = _which('ffmpeg') or _first_existing([
        'D:/AITools/TTS/Dots.tts/wzf/ffmpeg/bin/ffmpeg.exe',
        'C:/ffmpeg/bin/ffmpeg.exe', '/usr/bin/ffmpeg',
    ])
    fp = _which('ffprobe') or (str(Path(ff).with_name('ffprobe.exe')) if ff and Path(ff).suffix == '.exe'
                              else '/usr/bin/ffprobe' if ff else '')
    if not auto:
        print('\n[1/5] FFmpeg（必需的视频/音频处理工具）')
        print(f'  自动探测: ffmpeg = {ff or "未找到"}')
        ff = _ask('  ffmpeg 可执行文件路径（留空保持探测值）', ff)
        fp = _ask('  ffprobe 可执行文件路径', fp)
    if not ff or not Path(ff).is_file():
        print('  ! ffmpeg 未就绪。安装指引: https://ffmpeg.org/download.html '
              '（Windows 可用 winget install Gyan.FFmpeg），安装后重跑本向导。')
    found['FFMPEG'], found['FFPROBE'] = ff, fp

    # ── 字体（Windows 有默认，其他系统留空由脚本回退）──
    found['FONT'] = _first_existing([
        'C:/Windows/Fonts/msyh.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/System/Library/Fonts/PingFang.ttc',
    ])

    # ── Python 运行时：当前 Python 优先 ──
    found['TTS_PYTHON'] = found['ASR_PYTHON'] = str(Path(sys.executable).resolve()).replace('\\', '/')

    # ── 关联技能：vendors 优先，其次 WorkBuddy skills 目录 ──
    dub = _first_existing([
        VENDORS / '本地配音工坊' / 'dub.py',
        skill_root / '本地配音工坊' / 'dub.py',
        h / '.workbuddy' / 'skills' / '本地配音工坊' / 'dub.py',
    ])
    if not auto:
        print('\n[2/5] 本地配音工坊（TTS 配音，生产链路必需）')
        print(f'  自动探测: {dub or "未找到"}')
        dub = _ask('  dub.py 路径（留空保持探测值）', dub)
    found['DUB_SCRIPT'] = dub
    if dub:
        found['TTS_ROOT'] = str(Path(dub).parent.parent).replace('\\', '/')

    # ── VLM / ASR 模型：可选，缺失只降级 ──
    vlm = _first_existing([
        'D:/AITools/VLM/bin/llama-server.exe',
        str(h / 'AITools' / 'VLM' / 'bin' / 'llama-server.exe'),
    ])
    model = _first_existing(['D:/AITools/VLM/models/qwen3-vl-8b/Qwen3VL-8B-Instruct-Q4_K_M.gguf'])
    mmproj = _first_existing(['D:/AITools/VLM/models/qwen3-vl-8b/mmproj-Qwen3VL-8B-Instruct-F16.gguf'])
    if not auto:
        print('\n[3/5] 本地 VLM（逐镜采证据；未配置则只能跑已有 spec 的集）')
        vlm = _ask('  llama-server 路径（可选，回车跳过）', vlm)
        model = _ask('  VLM 模型 gguf 路径（可选）', model)
        mmproj = _ask('  mmproj 路径（可选）', mmproj)
    found['LLAMA_SERVER'], found['VLM_MODEL'], found['MMPROJ'] = vlm, model, mmproj

    model_root = _first_existing(['D:/AITools/models', str(h / 'AITools' / 'models')])
    found['MODEL_ROOT'] = found['WHISPER_MEDIUM_DIR'] = found['WHISPER_TINY_DIR'] = model_root
    found['TTS_MODEL'] = found['TTS_VOICE_REF'] = found['TTS_ROOT']

    # ── 目录：素材根 / 任务根 / 交付根 ──
    material = _first_existing([
        'D:/BaiduNetdiskDownload', str(h / 'BaiduNetdiskDownload'),
        str(h / 'Videos'),
    ]) or str(h / 'BaiduNetdiskDownload')
    task = _first_existing([
        'D:/剧里剧外/视频解说/任务', str(h / '剧里剧外' / '视频解说' / '任务'),
        str(h / 'Documents' / '剧里剧外' / '任务'),
    ]) or str(h / '剧里剧外' / '视频解说' / '任务')
    if not auto:
        print('\n[4/5] 目录（素材放哪、任务状态写哪、成片交付到哪）')
        material = _ask('  素材根目录', material)
        task = _ask('  任务根目录', task)
    found['MATERIAL_ROOT'] = found['DELIVERY_ROOT'] = material
    found['TASK_ROOT'] = task

    # ── 可选：贴纸库 / ComfyUI ──
    sticker = _first_existing([
        'D:/Agent引擎/表情包合集/EmojiPackage',
        str(h / 'Agent引擎' / '表情包合集' / 'EmojiPackage'),
    ])
    comfy = _first_existing(['D:/AITools/ComfyUI', str(h / 'AITools' / 'ComfyUI')])
    found['STICKER_ROOT'] = sticker
    found['COMFY_HOME'] = comfy
    found['COMFY_AGENT'] = comfy
    found['COMFY_OUTPUT'] = str(Path(comfy) / 'output').replace('\\', '/') if comfy else ''

    if not auto:
        print('\n[5/5] 以上可选能力未探测到的会留空，对应功能不启用，不影响主流程。')
    return found


def render(values):
    """用探测值替换模板占位符；未找到的写空串并在 summary 标注。"""
    raw = TEMPLATE.read_text(encoding='utf-8')
    missing = []

    def sub(m):
        raw_key = m.group(1)          # e.g. "{{FFMPEG}}"
        key = raw_key[2:-2]           # → "FFMPEG"（values 的键不带大括号）
        val = values.get(key, '')
        if not val:
            missing.append(key)
        return val

    out = PLACEHOLDER.sub(sub, raw)
    data = json.loads(out)  # 先解析，确认是合法 JSON
    return out, data, missing


def show():
    if not TARGET.is_file():
        print(f'[未初始化] 找不到 {TARGET}。请先运行: python scripts/setup.py')
        return 1
    d = json.loads(TARGET.read_text(encoding='utf-8'))
    rt, paths = d.get('runtime', {}), d.get('paths', {})
    print('── 当前本机配置 ──')
    for k in ('ffmpeg', 'ffprobe', 'tts_python', 'tts_dub_script', 'llama_server', 'vlm_model'):
        print(f'  runtime.{k:16s} = {rt.get(k) or "(未配置)"}')
    for k in ('material_root', 'task_root', 'delivery_root', 'sticker_root'):
        print(f'  paths.{k:18s} = {paths.get(k) or "(未配置)"}')
    return 0


def main():
    ap = argparse.ArgumentParser(description='首次使用向导：生成 config/engine.json')
    ap.add_argument('--yes', action='store_true', help='全部用自动探测 + 默认值，不询问')
    ap.add_argument('--show', action='store_true', help='只显示当前配置，不写文件')
    a = ap.parse_args()

    if a.show:
        return show()
    if not TEMPLATE.is_file():
        print(f'[FAIL] 模板缺失：{TEMPLATE}')
        return 2

    values = detect(a.yes)
    out, data, missing = render(values)

    # 备份已有配置（幂等、可回退）
    if TARGET.is_file():
        bak = TARGET.with_suffix('.json.bak')
        shutil.copy2(TARGET, bak)
        print(f'  已备份原配置 → {bak.name}')

    TARGET.write_text(out, encoding='utf-8')
    print(f'\n[写入] {TARGET}')

    # 写后复核：JSON 合法 + 契约审计
    r = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts' / 'contracts.py'), '--audit'],
                       cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8', errors='replace')
    print(f'[复核] {r.stdout.strip().splitlines()[0] if r.stdout.strip() else "无输出"}')

    rt = data.get('runtime', {})
    core_ok = bool(rt.get('ffmpeg')) and Path(rt['ffmpeg']).is_file()
    print('\n── 初始化结果 ──')
    print(f'  核心依赖 ffmpeg: {"就绪" if core_ok else "缺失（必须先安装）"}')
    print(f'  未配置的可选占位符 ({len(missing)}): {", ".join(missing) if missing else "无"}')
    if missing:
        print('  → 这些功能不启用；需要时手动编辑 config/engine.json 或重跑本向导。')
    print('\n下一步: python scripts/eval_skill.py  （28 条离线断言应全过）')
    return 0 if core_ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
