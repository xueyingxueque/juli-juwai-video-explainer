#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""tts_precheck.py —— 配音前置体检：build 之前先把「会说崩的文案」筛出来。

## 为什么要有这个脚本

dots.tts 对**特定文案**会确定性退化（坑典分类导航 · ASR/TTS/显存 类）：同一句连试 5 次
都吐 <8 字/秒 或 <3.5 字/秒 的垃圾，重试毫无意义，只能改文案。
实测样本（都在真实生产里撞过，每次都把整条 build 打断）：

- 「我还不太适应这个身体——这叫人话吗」  ← 破折号，5 次全废
- 「要不是大黄扑上去，我们俩就交代在那儿了」 ← 一次性模式也失败
- 「大伯娘天天骂我们几个吃白饭」       ← 5 次全废（8.3-9.0 字/秒）
- 「可工业券我没有，正卡在这上头」      ← 生僻词「工业券」，5 次全废

旧做法是在 build 跑到第 N 段时才炸，此时已经跑了几十分钟，
改完文案还得靠断点续跑重来——**事后救火**。

本脚本把拦截点前移到 spec 定稿后、build 之前：**一次跑完全部文案，
输出退化句清单**，改完再 build，从此不在中途炸。

## 用法

    python scripts/tts_precheck.py <spec-N.json 或 plan-N.json> \
        [--voice ref:<wav>] [--tmp <临时目录>] [--fast]

`--fast` 只试 1 次（快速粗筛，适合几十上百段）；默认 2 次，
连续 2 次都退化的判为**确定性退化**（判据见坑典同族条目）。

退出码：0 = 全部通过；3 = 存在退化句（清单已打印，需改文案）。
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

# 与 build.py narrate() 的硬闸保持一致，别各写一份
# ★ 2026-11-04：这三个值正常路径下会被 build_trim_silence() 用 build 模块的
#   同源定义**覆盖**（唯一真相源）。这里先给一份退化兜底，
#   保证「导入 build 失败」的降级路径不会因缺符号而崩。
RATE_MAX = 9.0
RATE_MIN = 3.5
TTS_TIMEOUT = 300


def rate_of(text: str, dur_engine: float) -> float:
    """语速（字/秒）—— 退化兜底实现；正常路径由 build_trim_silence() 覆盖为 build 的同源版本。"""
    return (len(text) / dur_engine) if dur_engine > 0 else 999.0


CJK = re.compile(r'[\u4e00-\u9fff]')
_FFPROBE = ['']   # 供 build_trim_silence 的退化实现兜底


def load_segments(path: Path):
    d = json.loads(path.read_text(encoding='utf-8'))
    segs = d.get('segments') or d.get('shots') or []
    return d, segs


def probe_dur(ffprobe: str, p: Path) -> float:
    r = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(p)],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=60)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def build_trim_silence():
    """复用 build.py 的 trim_silence + compress_pauses + 语速判据 —— 预检判据必须与 build 完全一致。

    ★ 这是预检工具第一版最大的坑（2026-10-27 实测）：预检直接用 wav 原始时长
    算语速，而 build.py 的 narrate() 是**先裁首尾静音、再压缩段内长停顿**才算。
    实测同一句「她指着我鼻子，骂我一个寡妇疯了」：
      原始 2.40s → 15字/2.40 = 6.2 字/秒（预检判「通过」）
      build 链路过一遍后 1.42s → 15字/1.42 = 10.6 字/秒（build 判「垃圾」）
    差的 0.98s = 首尾静音 + 段内长停顿（引擎对某些句子会在词间塞长静音）。
    漏任何一步，预检就是 100% 假阳性（本该拦住的退化句全放行）。

    ★ 2026-11-04：语速判据的分母也统一为**引擎语音时长**（trim 后、压停顿前），
      与 build.narrate 完全同源（见 build.rate_of 的 docstring）。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        '_build_mod', str(ROOT / 'scripts' / 'build.py'))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        # 语速判据与阈值一律取自 build（唯一真相源），本文件不另写一份
        globals()['RATE_MAX'] = mod.RATE_MAX
        globals()['RATE_MIN'] = mod.RATE_MIN
        globals()['rate_of'] = mod.rate_of
        return mod.trim_silence, mod.compress_pauses
    except Exception as e:
        print(f'[warn] 无法导入 build 的 trim_silence/compress_pauses（{e}），'
              f'预检判据将与 build 有偏差', file=sys.stderr)
        return (lambda p: probe_dur(_FFPROBE[0], p)), (lambda p: probe_dur(_FFPROBE[0], p))


def synth_once(cfg, voice_args, text, out: Path, engine_root: Path):
    dub = Path(cfg['runtime']['tts_python'])
    script = Path(r'C:/Users/Administrator/.workbuddy/skills/本地配音工坊/dub.py')
    cmd = [str(dub), '-X', 'utf8', str(script), '--engine', 'dotstts', *voice_args,
           '--text', text, '--out', str(out)]
    p = subprocess.Popen(cmd, cwd=str(engine_root), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding='utf-8',
                         errors='replace',
                         creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    try:
        p.communicate(timeout=TTS_TIMEOUT)
    except subprocess.TimeoutExpired:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)],
                       capture_output=True, timeout=60)
        try:
            p.communicate(timeout=30)
        except Exception:
            pass
        return 'timeout'
    return 'ok' if (out.is_file() and out.stat().st_size > 2048) else 'fail'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('plan')
    ap.add_argument('--voice', default=None,
                    help='音色：ref:<wav> 或音色名；不传则读 plan.voice')
    ap.add_argument('--tmp', default=None)
    ap.add_argument('--fast', action='store_true', help='每段只试 1 次（快速粗筛）')
    a = ap.parse_args()

    cfg = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
    ffprobe = cfg['runtime']['ffprobe']
    engine_root = Path(r'D:/AITools/TTS/Dots.tts')
    _FFPROBE[0] = ffprobe
    trim_silence, compress_pauses = build_trim_silence()

    plan_path = Path(a.plan)
    d, segs = load_segments(plan_path)
    voice = a.voice or d.get('voice') or ''
    if not voice:
        print('[FAIL] 未指定音色：--voice 或 plan.voice 必填（技能 §3.7）')
        return 4
    voice_args = ['--ref', voice[4:]] if voice.startswith('ref:') else ['--voice', voice]

    tmp = Path(a.tmp) if a.tmp else Path(tempfile.mkdtemp(prefix='tts_precheck_'))
    tmp.mkdir(parents=True, exist_ok=True)
    tries = 1 if a.fast else 2

    bad, slow, short = [], [], []
    n = 0
    for k, s in enumerate(segs):
        text = (s.get('text') or '').strip()
        if not text:            # 原声段跳过
            continue
        n += 1
        ncjk = len(CJK.findall(text))
        worst = None
        for t in range(tries):
            out = tmp / f'p{k:03d}.wav'
            if out.exists():
                out.unlink()
            st = synth_once(cfg, voice_args, text, out, engine_root)
            if st != 'ok':
                worst = (st, 0.0)
                break
            # ★ 语速判据必须与 build.narrate **完全一致**（坑典 P69）：
            #   复用 build 的 rate_ok() + RATE_MAX/RATE_MIN，不在本文件另写一份。
            # ★ 2026-11-04：分母改为**引擎语音时长**（trim 后、压段内停顿前）——
            #   build.narrate 已改用同一口径（见 build.rate_of 的 docstring）：
            #   压缩后的时长混进了「我们删了多少停顿」，会让同一句话的语速随机漂移
            #   （seg37 实测 8.9/9.4/11.3），闸门变成随机抽签、误杀正常音频。
            dur_engine = trim_silence(out)
            rate = rate_of(text, dur_engine)
            if rate > RATE_MAX or (ncjk >= 4 and rate < RATE_MIN):
                worst = (f'{rate:.1f} 字/秒', dur_engine)
            else:
                worst = None
                break
        if worst:
            tag, dur = worst
            item = f'    段{k:03d}  [{tag}]  {text}'
            if isinstance(tag, str) and '字/秒' in tag and float(tag.split()[0]) > RATE_MAX:
                short.append(item)
            else:
                slow.append(item)
            bad.append((k, text))
        print(f'  seg{k:03d} {"✗ " + str(worst[0]) if worst else "✓"}  {text[:30]}',
              flush=True)

    print()
    if short:
        print(f'[退化·过快] {len(short)} 句（音频偏短，语速 >{RATE_MAX}）：')
        print('\n'.join(short))
    if slow:
        print(f'[退化·过慢/超时] {len(slow)} 句：')
        print('\n'.join(slow))
    if bad:
        print(f'\n[FAIL] {len(bad)}/{n} 句确定性退化。判据：连续 {tries} 次都超闸。')
        print('  改法：换词/换行/避生僻词（如「工业券」），改完重跑本脚本确认。')
        print('  ★ 改 spec 源头后记得跑 delta_evidence.py 同步 ev（坑典 ⑨）。')
        if not a.tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        return 3
    print(f'[OK] {n} 句全部通过语速闸（>{RATE_MAX} 或 ≥4字且 <{RATE_MIN} 为退化）。')
    if not a.tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
