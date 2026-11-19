# -*- coding: utf-8 -*-
r"""成片体积预算 —— 平台体积上限 → 编码参数的**唯一真相源**。

★ 为什么抽成独立模块（2026-11-03 M3 实踩，同类缺陷第 3 次出现）：
  这段逻辑曾在两个文件里各存一份**同源副本**：
    · scripts/build.py    的 `_size_budget()`   （烧字幕层）
    · scripts/make_cover.py 的 `size_budget()` （烧封面层）
  两份副本犯了**同一个错**，且 H3 与 M3 连续两集各撞一次 128MB 硬门。
  **副本必然漂移，改一处必漏另一处** → 现在两边都从这里取。

★★★ 实测铁律（勿再退回老写法）★★★

  ❶ **`-crf` 与 `-b:v` 不能共存** —— 这是本缺陷的真凶。
     libx264 一旦看到 `-crf` 就进 CRF 模式（画质驱动），
     `-b:v` **不再是平均码率目标**，只有 `-maxrate/-bufsize`（VBV）还起作用。
     实测同一段 30s 素材：
       -crf 22 + -b:v 500000 + -maxrate 1597987 → 实际 1347975 bps
         （-b:v 被无视，只剩 VBV 天花板在起作用）
       -crf 22（无 VBV）                      → 实际 2151092 bps
       仅 -b:v 1304113 + VBV（无 -crf）       → 实际 1190031 bps（≈目标的 91%）
     → 有体积上限时**必须去掉 -crf**，改用纯码率模式（-b:v + VBV）。
     → 体积预算**够不着**的平台档（短片）才保留 `-crf`（画质优先，行为不变）。

  ❷ **音频码率不能写死 128000**。
     母带是 `mix()` 产出的 aac 320k（实测成片音轨 289799bps），
     按 128k 记账会**少算约 12MB** 预算 → 长片必然撞硬门。
     → 一律**实测**输入文件的音频码率。

  ❸ **预算只是预测，编码器才是事实**。
     内容复杂度、VBV 波动、容器开销都会让实际体积偏离预算
     → 编码完**必须实测体积**，超限就按实测比例降码率重编（enforce_cap）。
     绝不把超限成片流到下游（下游质检门 FAIL 时整集白跑）。
"""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / 'config' / 'engine.json').read_text(encoding='utf-8'))
FF = CFG['runtime']['ffmpeg']
FPROBE = CFG['runtime']['ffprobe']


# ── 探测 ────────────────────────────────────────────────
def probe_audio_bps(path) -> int:
    """实测媒体文件的音频码率（bps）；取不到返回 0。"""
    try:
        r = subprocess.run(
            [FPROBE, '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=bit_rate', '-of', 'csv=p=0', str(path)],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=60)
        v = (r.stdout or '').strip().splitlines()
        return int(v[0]) if v and v[0].strip().isdigit() and int(v[0]) > 0 else 0
    except Exception:
        return 0


def probe_video_bps(path) -> int:
    """实测媒体文件的视频码率（bps）；取不到返回 0。"""
    try:
        r = subprocess.run(
            [FPROBE, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=bit_rate', '-of', 'csv=p=0', str(path)],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=60)
        v = (r.stdout or '').strip().splitlines()
        return int(v[0]) if v and v[0].strip().isdigit() and int(v[0]) > 0 else 0
    except Exception:
        return 0


def probe_duration(path) -> float:
    """实测媒体时长（秒）；取不到返回 0。"""
    try:
        r = subprocess.run(
            [FPROBE, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', str(path)],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=60)
        v = (r.stdout or '').strip()
        return float(v) if v and v not in ('N/A', '') else 0.0
    except Exception:
        return 0.0


# ── 预算 ────────────────────────────────────────────────
def cap_bytes(platform: str = 'douyin') -> int:
    """平台成片体积上限（字节）；该平台无上限时返回 0。"""
    prof = CFG.get('platform_profiles', {}).get(platform, {})
    mb = prof.get('max_file_mb')
    return int(float(mb) * 1024 * 1024) if mb else 0


def plan(total_sec: float, platform: str = 'douyin', audio_bps: int = 0,
         crf: int = 22, preset: str = 'veryfast', preset_capped: str = None):
    """按「平台体积上限 + 实际时长 + 实测音频码率」规划视频编码参数。

    返回 dict：
      applied    —— 是否受体积上限约束（False = 短片，走 -crf 画质模式）
      cap_bytes  —— 上限字节数（0 = 无上限）
      audio_bps  —— 记账用的音频码率
      target_bps —— 目标视频码率（applied 时有效）
      est_mb     —— 预估成片体积（MB）
      make_args  —— make_args(factor=1.0) -> list[str] 视频编码参数
                    factor < 1 用于超限后按比例降码率重编

    ★ 短片（预算够不着）时 make_args 给 `-crf`，画质与旧版完全一致，
      **不影响任何既有短片成片**。
    ★ preset_capped 默认为 preset → 受限时**编码速度与旧版一致**，
      只把「画质模式」换成「码率模式」，不额外拖慢产线。
    """
    if preset_capped is None:
        preset_capped = preset
    cap = cap_bytes(platform)
    abps = int(audio_bps or 0) or 128000
    info = {'applied': False, 'cap_bytes': cap, 'cap_mb': cap // 1048576,
            'audio_bps': abps, 'total_sec': total_sec, 'target_bps': 0,
            'est_mb': 0.0, 'platform': platform}

    def _crf_args(factor=1.0):
        return ['-c:v', 'libx264', '-preset', preset, '-crf', str(crf)]

    if not cap or total_sec <= 0:
        info['make_args'] = _crf_args
        return info

    # 音频先扣（用实测码率），剩下的全是视频的
    video_bytes = cap * 0.97 - total_sec * abps / 8
    if video_bytes <= 0:
        info['make_args'] = _crf_args
        return info
    max_bps = video_bytes * 8 / total_sec
    if max_bps >= 8_000_000:
        # 预算宽松 → 不限码率，保持 -crf 画质优先（行为与旧版一致）
        info['make_args'] = _crf_args
        return info

    target = int(max_bps * 0.92)

    def _make_args(factor=1.0):
        t = max(int(target * factor), 120_000)
        return ['-c:v', 'libx264', '-preset', preset_capped,
                '-b:v', str(t), '-maxrate', str(int(t * 1.05)),
                '-bufsize', str(int(t * 2))]

    info.update({'applied': True, 'target_bps': target,
                 'est_mb': round((target * total_sec / 8 + total_sec * abps / 8)
                                 / 1048576, 1),
                 'make_args': _make_args})
    return info


def describe(info: dict, indent: str = '    ') -> str:
    """把预算算成一行人话日志。"""
    if not info.get('applied'):
        return (f"{indent}体积预算：无平台上限 → 保持 -crf 画质模式"
                f"（{info.get('total_sec', 0):.0f}s）")
    return (f"{indent}体积预算：上限 {info['cap_mb']}MB / {info['total_sec']:.0f}s"
            f" → 视频码率上限 {info['target_bps'] // 1000}kbps"
            f"（音频实测 {info['audio_bps'] // 1000}kbps，估算 {info['est_mb']}MB）")


# ── 体积硬闸 ────────────────────────────────────────────
def enforce_cap(video, platform: str = 'douyin', max_tries: int = 3,
                log=print) -> int:
    """成片体积硬闸：超平台上限就按**实测比例**降码率重编（最多 max_tries 次）。

    ★ 为什么不能只靠预算（2026-11-03 实踩）：
      预算是估算，实际体积还会受内容复杂度、VBV 波动、容器开销影响。
      撞了上限就是整集白跑（渲染 30 分钟 → 质检 FAIL → 从头再来）。
      → 这里兜底：宁可降一档码率，也不让超限成片流到下游。

    返回最终文件体积（字节）；仍超限则抛 RuntimeError（绝不留下半成品）。
    """
    video = Path(video)
    cap = cap_bytes(platform)
    if not cap:
        return video.stat().st_size if video.is_file() else 0
    size = video.stat().st_size
    if size <= cap:
        return size

    dur = probe_duration(video)
    abps = probe_audio_bps(video)
    vbps = probe_video_bps(video)
    log(f'    体积超标：{size / 1048576:.1f}MB > 上限 {cap / 1048576:.0f}MB'
        f' → 降码率重编')
    for attempt in range(1, max_tries + 1):
        if dur <= 0 or vbps <= 0:
            break
        # 新目标码率 = 当前视频码率 × (可用体积 / 当前体积)
        target = int(vbps * (cap * 0.97) / size)
        target = max(target, 150_000)
        tmp = video.with_name(video.stem + '.shrink.mp4')
        cmd = [FF, '-v', 'error', '-y', '-i', str(video),
               '-c:v', 'libx264', '-preset', 'medium',
               '-b:v', str(target), '-maxrate', str(int(target * 1.05)),
               '-bufsize', str(int(target * 2)),
               '-c:a', 'copy', '-movflags', '+faststart', str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=3600)
        if r.returncode != 0 or not tmp.is_file():
            log(f'    降码率重编失败（第 {attempt} 次）：{(r.stderr or "")[-200:]}')
            break
        os.replace(str(tmp), str(video))
        size = video.stat().st_size
        if size <= cap:
            log(f'    体积收敛：{size / 1048576:.1f}MB ≤ {cap / 1048576:.0f}MB ✓')
            return size
        vbps = probe_video_bps(video)
        log(f'    仍超限（{size / 1048576:.1f}MB），继续降码率（第 {attempt} 次）')
    raise RuntimeError(
        f'成片体积无法压进平台上限：{video.name} {size / 1048576:.1f}MB '
        f'> {cap / 1048576:.0f}MB（重编 {max_tries} 次仍未达标）')
