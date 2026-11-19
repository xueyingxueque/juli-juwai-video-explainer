"""BGM 生成 —— 用五声音阶合成一条适合修仙题材的底乐。

★ 为什么自己合成而不是找现成：
   素材库里没有可用的 BGM（只有 TTS 测试文件）。
   而版权干净的合成音乐反而更安全——不会撞别人的曲子。

设计（对应 EP02 的 7 段结构）：
   - 五声音阶（宫商角徵羽），中国风底色，适合修仙/古风
   - 32 秒，够铺满全片
   - 结构：起（弱）→ 渐强 → 收（留白）
   - 音色：三角波 + 轻微低通，去掉刺耳高频，当背景不抢戏
"""
import math
import wave
from pathlib import Path

SR = 44100
DUR = 32.0

# 中国五声音阶（宫商角徵羽），对应 C D E G A
PENTA = [0, 2, 4, 7, 9]

# 音级：低八度为主，干净不吵
BASE = 261.63  # C4


def note_freq(step: int) -> float:
    """step 为半音相对值，映射到五声音阶。"""
    octave = step // len(PENTA)
    idx = step % len(PENTA)
    semi = PENTA[idx] + octave * 12
    return BASE * (2 ** (semi / 12))


def build(dur: float = DUR, sr: int = SR) -> list:
    n = int(dur * sr)
    # 简易包络：淡入 3s，淡出 4s，中间保持
    fade_in, fade_out = 3.0, 4.0
    out = [0.0] * n

    # 主旋律：每 4 秒换一个音，做缓慢移动（修仙题材要稳，不要跳）
    seg = 4.0
    melody = [0, 2, 4, 7, 9, 7, 4, 2]   # 五声音阶上下行

    for si in range(int(dur / seg) + 1):
        t0 = si * seg
        f = note_freq(melody[si % len(melody)])
        # 叠加一个低八度的根音，增加厚度
        f2 = f / 2

        i0 = int(t0 * sr)
        i1 = min(n, int((t0 + seg + 0.6) * sr))  # 略交叠，避免断音
        length = i1 - i0
        if length <= 0:
            break

        for i in range(i0, min(i1, n)):
            k = (i - i0) / length          # 段内位置 0-1
            t = i / sr
            # 段内淡入淡出，避免爆音
            env = math.sin(math.pi * min(k, 1.0)) ** 0.7
            # 全局包络
            if t < fade_in:
                env *= t / fade_in
            if t > dur - fade_out:
                env *= max(0.0, (dur - t) / fade_out)

            # 三和弦式叠加（主音 + 八度 + 十二度），听起来像"神性铺底"
            s = (math.sin(2 * math.pi * f * t) * 0.5
                 + math.sin(2 * math.pi * f2 * t) * 0.3
                 + math.sin(2 * math.pi * f * 1.5 * t) * 0.12)
            out[i] += s * env * 0.18

    # 低通：削掉高频，让它不抢人声
    for i in range(2, n):
        out[i] = out[i] * 0.72 + out[i - 1] * 0.28

    peak = max(abs(x) for x in out) or 1.0
    g = 0.72 / peak
    return [x * g for x in out]


def write(path: Path, samples: list, sr: int = SR):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'w') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b''.join(
            int(max(-1.0, min(1.0, s)) * 32767).to_bytes(2, 'little', signed=True)
            for s in samples))


if __name__ == '__main__':
    out = Path(r'D:/剧里剧外/视频解说/新引擎/series/_bgm/bgm.wav')
    s = build()
    write(out, s)
    print('BGM 已生成:', out)
    print(f'时长 {len(s)/SR:.1f}s  五声音阶  三和弦铺底  峰值归一到 -2.9dB')
