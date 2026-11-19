# -*- coding: utf-8 -*-
"""
本地配音工坊 · 长文本截断检测器
===============================
用途：检测 TTS 产物是否**静默丢字**。

**为什么需要它**
  TTS 模型都有生成长度上限（Breeze 是 max_new_tokens=1500 / max_seq_len=2048）。
  超限时它不会报错，而是**直接停在那里**——产物听着完整，其实结尾整段没了。
  配 3 分钟长稿时这是最危险的一类故障：**不报错、不失败、只有丢失**。
  本脚本用"回听转写 ↔ 原文"的覆盖率把它抓出来。

**判定口径**
  把归一化后的「原文 / 转写」做一次序列对齐（difflib.SequenceMatcher），
  取**最后一个匹配块在原文里的终点** ÷ 原文长度 = 覆盖率。
  为什么测尾部：开头几乎永远念对，截断点永远在尾部，测开头没有意义。
  为什么不用「尾部 N 字去原文找位置」：重复文本（同一段话循环多遍）里尾部串
  出现多次，取最靠后的会**高估覆盖率 → 假通过**。序列对齐天然单调递增，不会跳跃。

判定：
  ≥ 99%  完整（ASR 误差范围内）
  95–99% 轻微偏差，人工核一下尾部
  < 95%  **判定截断**，产物不可用

用法（必须用 Breeze 自带 Python，内有 faster_whisper）：
  set PY=D:\\AITools\\TTS\\Breeze-TTS-2-T8\\T8star-Aix-Voice-Studio-*/resources/python/python.exe
  %PY% tests/check_coverage.py --text outputs/long_text.txt --wav outputs/long_chunked.wav

  # 一次比对多个产物（修分段参数前后对比用）
  %PY% tests/check_coverage.py --text outputs/long_text.txt \
       --wav outputs/long_test.wav outputs/long_chunked.wav
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用打分器的归一化与 ASR，避免两处实现漂移
from score_tts import norm_text, get_asr  # noqa: E402

TAIL_N = 20          # 取转写尾部多少字去原文里定位
WARN_AT = 0.99       # 低于此值提示人工核对
FAIL_AT = 0.95       # 低于此值判定截断


def is_repetitive(s: str, probe: int = 40) -> int:
    """源文本是否自我重复：返回开头 probe 字在全文中的出现次数。

    为什么要查：重复文本会让对齐彻底失效。实测同一段话循环 3.3 遍的稿子，
    音频其实念完了（转写字数 576/587、结尾句对得上），但对齐只报到 54.2%
    —— 因为贪心匹配把早段锚到了后面的重复位置，后续块就无原文可对齐了。
    """
    head = s[:probe]
    return s.count(head) if len(head) == probe else 1


def _align_coverage(orig: str, trans: str):
    """返回 (覆盖率, 原文归一化长度, 转写归一化长度, 对齐终点位置, 终点附近原文)

    为什么用 SequenceMatcher 而不是「尾部串找位置」：
      最早的写法是取转写尾部 20 字去原文里 rfind。这在**重复文本**上会翻车：
      原文是同一段话循环 3 遍时，尾部串在原文里出现多次，rfind 取最靠后的那次
      → 明明只念到第 2 遍，却报出第 3 遍的位置 → **高估覆盖率 = 假通过**。
      漏报只是没查出问题，假通过是"以为修好了其实没修"，后者更坏。

      SequenceMatcher 的匹配块在两个序列里都**单调递增**，等价于一次对齐，
      天然不会跳到后面去重复匹配。最后一个匹配块的终点 = 真实覆盖终点。
      重复文本下它取最早能对齐上的位置，偏保守（宁可误报，不可假通过）。
    """
    o, t = norm_text(orig), norm_text(trans)
    if not o:
        return 0.0, 0, len(t), -1, ""
    if not t:
        return 0.0, len(o), 0, -1, ""

    # autojunk=False 必须显式关：中文归一化后只剩几百字，某些高频字会被误判为 junk 元素
    # 从而被排除在匹配之外，导致覆盖率被严重低估。
    sm = difflib.SequenceMatcher(None, o, t, autojunk=False)
    blocks = sm.get_matching_blocks()
    # blocks[-1] 是哨兵 (len(a), len(b), 0)，真实块取到 -2
    real = [b for b in blocks if b.size]
    if not real:
        return 0.0, len(o), len(t), -1, t[-TAIL_N:]
    last = real[-1]
    end = last.a + last.size
    tail = o[max(0, end - TAIL_N):end]
    return min(1.0, end / len(o)), len(o), len(t), end, tail


def check(text_path: Path, wav_path: Path) -> dict:
    orig = text_path.read_text(encoding="utf-8")
    segs, _ = get_asr().transcribe(str(wav_path), language="zh", vad_filter=True)
    trans = "".join(s.text for s in segs)
    cov, n_o, n_t, end, tail = _align_coverage(orig, trans)
    reps = is_repetitive(norm_text(orig))

    try:
        import soundfile as sf
        dur = float(len(sf.read(str(wav_path))[0])) / sf.info(str(wav_path)).samplerate
    except Exception:
        dur = -1.0

    # 长度比：转写字数 / 原文字数。重复文本下对齐不可信，但长度比依然可靠 ——
    # 音频念到哪，转写就有多长；丢字会直接体现为长度缺口。
    ratio = (n_t / n_o) if n_o else 0.0

    if reps > 1:
        verdict = f"⚠️ 源文本自我重复 {reps} 次，对齐覆盖率不可信 → 改看长度比"
        reliable = False
        if ratio >= 0.97:
            verdict += "（长度比正常，大概率完整，但仍建议换非重复稿复测）"
    else:
        reliable = True
        if cov >= WARN_AT:
            verdict = "完整"
        elif cov >= FAIL_AT:
            verdict = "轻微偏差，人工核尾部"
        else:
            verdict = "★截断★ 产物不可用"

    return {
        "wav": str(wav_path),
        "duration_s": round(dur, 2),
        "orig_chars": n_o,
        "trans_chars": n_t,
        "align_endpoint": end,
        "tail_text": tail,
        "coverage": round(cov, 4),
        "length_ratio": round(ratio, 4),
        "source_repetitions": reps,
        "coverage_reliable": reliable,
        "verdict": verdict,
    }


def main():
    ap = argparse.ArgumentParser(description="长文本 TTS 静默截断检测")
    ap.add_argument("--text", required=True, help="原文案 txt")
    ap.add_argument("--wav", required=True, nargs="+", help="待检测 wav（可多个）")
    ap.add_argument("--out", default=None, help="JSON 输出路径")
    a = ap.parse_args()

    tp = Path(a.text)
    if not tp.is_file():
        sys.exit(f"[x] 原文案不存在: {tp}")

    rows = []
    for w in a.wav:
        wp = Path(w)
        if not wp.is_file():
            print(f"[跳过] 文件不存在: {wp}")
            continue
        r = check(tp, wp)
        rows.append(r)
        print(f"\n── {wp.name}")
        print(f"  时长        : {r['duration_s']}s")
        print(f"  原文/转写字数: {r['orig_chars']} / {r['trans_chars']}")
        print(f"  对齐终点     : 原文 {r['align_endpoint']}/{r['orig_chars']}")
        print(f"  终点附近原文 : …{r['tail_text']}")
        print(f"  长度比       : {r['length_ratio'] * 100:.1f}%   （源文本重复 {r['source_repetitions']} 次）")
        print(f"  覆盖率      : {r['coverage'] * 100:.1f}%   →  {r['verdict']}")

    if a.out and rows:
        Path(a.out).write_text(
            __import__("json").dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[JSON] {a.out}")

    # 只对「对齐可信」的样本判失败；重复文本只提示，不判失败（避免基于失真指标误杀）
    judged = [r for r in rows if r["coverage_reliable"]]
    if judged and min(r["coverage"] for r in judged) < FAIL_AT:
        sys.exit(1)


if __name__ == "__main__":
    main()
