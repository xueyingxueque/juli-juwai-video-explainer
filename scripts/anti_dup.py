# -*- coding: utf-8 -*-
r"""原创盾牌 · 反同质化后处理（技能级，2026-10-22；evolution-roadmap v3.6 第一件）

解决什么：平台视觉层指纹已进化到 pHash/AI 语义级（2026-10-21 五路调研结论）。
同部短剧多人解说，选镜趋同 → 帧序 pHash 相似 → 同质化限流。
本脚本在成片后做**观众无感**的差异化后处理，打散帧级指纹：

  1. 色相微旋转（hue=h=2°）+ 对比度/饱和度微调 —— 改变色彩直方图与帧哈希；
  2. 极轻时域噪点（noise=alls=2:allf=t）—— 仿胶片颗粒，破坏逐帧比对；
  3. 元数据剥离重写（-map_metadata -1）—— 去编码器指纹。

纪律（roadmap v3.6 定，本脚本遵守）：
  - **观众无感优先**：任何变换超过「肉眼不可辨」即是 bug；
  - **不做几何变换**（裁剪/缩放/旋转会切字幕安全区——短剧字幕位置不一，
    估错就糊人脸/切字，待 safe-zone 感知实现后再议，见 roadmap v3.6 待办）；
  - **音频流直接拷贝**（-c:a copy）：不碰响度/时长，质检结论不受影响；
  - 默认关：本脚本是**可选后处理**，主链不改；用不用由当部剧的碰撞风险决定。

用法：
  python scripts/anti_dup.py --video 成片.mp4 --out 成片_原创盾.mp4 [--strength light|medium]
验证（必做）：抽帧肉眼对比 + ffprobe 核对时长/音画流 + quality_gate 复跑不退化。
★ docstring 内禁写三引号字符本身（2026-10-22 实踩，见坑典 P39）。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
CFG = json.loads((SKILL / 'config' / 'engine.json').read_text(encoding='utf-8'))
FF = CFG['runtime']['ffmpeg']
FFPROBE = CFG['runtime']['ffprobe']

STRENGTHS = {
    # hue 角度 / eq / 噪点强度：medium 仅建议高碰撞爆款剧使用
    'light': {'hue': 2, 'contrast': 1.02, 'saturation': 1.04, 'noise': 2},
    'medium': {'hue': 4, 'contrast': 1.03, 'saturation': 1.06, 'noise': 4},
}


def probe(video: str) -> dict:
    r = subprocess.run([FFPROBE, '-v', 'error', '-show_entries',
                        'stream=codec_type,duration', '-of', 'json', video],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=60)
    streams = json.loads(r.stdout).get('streams', [])
    return {s['codec_type']: float(s['duration']) for s in streams if 'duration' in s}


def main() -> int:
    ap = argparse.ArgumentParser(description='反同质化后处理（观众无感）')
    ap.add_argument('--video', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--strength', choices=sorted(STRENGTHS), default='light')
    a = ap.parse_args()

    s = STRENGTHS[a.strength]
    vf = (f"hue=h={s['hue']}:s={s['contrast']},"
          f"eq=contrast={s['contrast']}:saturation={s['saturation']},"
          f"noise=alls={s['noise']}:allf=t")
    cmd = [FF, '-v', 'error', '-y', '-i', a.video,
           '-vf', vf, '-c:v', 'libx264', '-preset', 'slow', '-crf', '18',
           '-pix_fmt', 'yuv420p', '-c:a', 'copy',
           '-map_metadata', '-1', '-movflags', '+faststart', a.out]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=900)
    if r.returncode != 0:
        print(r.stderr[-2000:])
        raise SystemExit(f'反查重处理失败 exit={r.returncode}')

    before, after = probe(a.video), probe(a.out)
    if 'video' not in after or 'audio' not in after:
        raise SystemExit(f'输出流不完整: {after} —— 拒绝交付')
    for k in ('video', 'audio'):
        if abs(before[k] - after[k]) > 0.3:
            raise SystemExit(f'{k} 时长不符: {before[k]:.1f}s vs {after[k]:.1f}s —— 拒绝交付')
    print(f'原创盾牌({a.strength}) -> {a.out}')
    print(f'  时长 video {after["video"]:.1f}s / audio {after["audio"]:.1f}s ✓ 与原片一致')
    print('  [待人工] 抽帧肉眼确认无可见失真，再跑 quality_gate 复核')
    return 0


if __name__ == '__main__':
    sys.exit(main())
