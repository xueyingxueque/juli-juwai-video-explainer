# -*- coding: utf-8 -*-
"""
本地配音工坊 · TTS 产物客观打分器
=================================
用途：对一批同文案的 TTS 产物做客观质量打分，用于横评与「音色×引擎」搭配回填。

**能力边界（务必先看）**
  本脚本只测**客观可测**的维度，测不了主观自然度 / 情感 / 机械感 —— 那部分必须人耳盲测。
  能测：
    - 咬字清晰度：faster-whisper 回听转写 → 与原文案算 CER（字错率）。越低越清楚。
    - 音色相似度：SpeechBrain ECAPA(spkrec-ecapa-voxceleb) embedding 余弦，对比参考音。
    - 有效带宽：频谱 -60dB 截止频率（48k 引擎明显高于 22.05k）。
    - 韵律：pyin 基频均值/标准差/有声率 —— 标准差过小 = 念稿机器人，异常抖动 = 电音。
    - 干净度：削波率、静音占比、最长内部停顿、峰值余量。

运行依赖（本机已验证）：
  python  = D:\\AITools\\TTS\\Breeze-TTS-2-T8\\T8star-Aix-Voice-Studio-*/resources/python/python.exe
            （内置 torch 2.9.1 / librosa / soundfile / faster_whisper）
  speechbrain = 隔离装在 C:\\Users\\Administrator\\.workbuddy\\tools\\ttsqa，用 PYTHONPATH 指过去
  离线模型缓存：HF_HOME=C:\\Users\\Administrator\\.cache\\huggingface，配 HF_HUB_OFFLINE=1

用法：
  python tests/score_tts.py --dir outputs/bench_20260904 --ref refs/小欧_24k.wav \
      --text 文案_30s.txt --json score.json
  python tests/score_tts.py --dir <dir> --ref <ref.wav> --text <txt> --no-cer   # 跳过 ASR（快）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import numpy as np
import soundfile as sf


# ──────────────────────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────────────────────
def load_audio(path: Path):
    y, sr = sf.read(str(path), always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    return y.astype(np.float32), int(sr)


def dbfs(x: float) -> float:
    x = max(float(x), 1e-12)
    return round(20 * np.log10(x), 2)


_T2S = None


def t2s(s: str) -> str:
    """繁体转简体：whisper 对某些音色会输出繁体，不做归一会把 CER 虚高"""
    global _T2S
    if _T2S is None:
        try:
            from opencc import OpenCC
            _T2S = OpenCC("t2s")
        except Exception:
            _T2S = False
    return _T2S.convert(s) if _T2S else s


def norm_text(s: str) -> str:
    """归一化：繁转简 → 全角转半角 → 只留汉字/数字/字母，用于 CER 比对"""
    s = t2s(unicodedata.normalize("NFKC", s))
    s = re.sub(r"[^\u4e00-\u9fff0-9A-Za-z]", "", s)
    return s.lower()


def levenshtein(a, b) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str) -> float:
    r, h = norm_text(ref), norm_text(hyp)
    if not r:
        return 0.0
    return levenshtein(r, h) / len(r)


# ──────────────────────────────────────────────────────────────
# 客观指标
# ──────────────────────────────────────────────────────────────
def measure_level(y: np.ndarray, sr: int) -> dict:
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    rms = float(np.sqrt(np.mean(y ** 2))) if y.size else 0.0
    clip = float(np.mean(np.abs(y) >= 0.999)) if y.size else 0.0
    # 静音切分（top_db=40）
    import librosa
    iv = librosa.effects.split(y, top_db=40)
    speech = int(sum(e - s for s, e in iv))
    pauses = [int(iv[i + 1][0] - iv[i][1]) for i in range(len(iv) - 1)]
    return {
        "duration": round(len(y) / sr, 2),
        "peak_dbfs": dbfs(peak),
        "rms_dbfs": dbfs(rms),
        "clip_ratio": round(clip * 100, 3),
        "speech_ratio": round(speech / len(y) * 100, 1) if y.size else 0.0,
        "max_pause_s": round(max(pauses) / sr, 2) if pauses else 0.0,
        "head_sil_s": round(iv[0][0] / sr, 2) if len(iv) else 0.0,
    }


def measure_bandwidth(y: np.ndarray, sr: int) -> dict:
    """-60dB 截止频率 + 99.9% 能量频率：反映高频细节保留（48k vs 22.05k 差距在这）"""
    n = 1 << int(np.floor(np.log2(len(y)))) if len(y) > 4096 else len(y)
    seg = y[:n] * np.hanning(n)
    mag = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    if mag.max() <= 0:
        return {"f_cut_60db": 0, "f_energy_999": 0}
    thr = mag.max() / 1000.0
    idx60 = np.where(mag > thr)[0]
    cum = np.cumsum(mag ** 2)
    idx999 = np.where(cum >= 0.999 * cum[-1])[0]
    return {
        "f_cut_60db": int(round(freqs[idx60[-1]])) if len(idx60) else 0,
        "f_energy_999": int(round(freqs[idx999[0]])) if len(idx999) else 0,
    }


def measure_f0(y: np.ndarray, sr: int) -> dict:
    """基频统计：std 小 = 念稿机器人；抖动异常 = 电音/破音"""
    import librosa
    try:
        f0, voiced, _ = librosa.pyin(y, fmin=50, fmax=500, sr=sr, frame_length=2048)
    except Exception as e:
        return {"f0_mean": 0.0, "f0_std": 0.0, "f0_std_semitone": 0.0, "voiced_ratio": 0.0, "err": str(e)}
    v = f0[~np.isnan(f0)]
    if v.size < 10:
        return {"f0_mean": 0.0, "f0_std": 0.0, "f0_std_semitone": 0.0, "voiced_ratio": 0.0}
    v = v[v > 0]
    mean = float(np.mean(v))
    # 半音标准差：与绝对音高无关，更适合跨男女声比较
    st = 12 * np.log2(v / mean)
    return {
        "f0_mean": round(mean, 1),
        "f0_std": round(float(np.std(v)), 1),
        "f0_std_semitone": round(float(np.std(st)), 2),
        "voiced_ratio": round(float(np.mean(~np.isnan(f0))), 3),
    }


def keyword_miss(gt: str, hyp: str, kws) -> list:
    """关键短语漏读/错读检查：直接定位咬字问题（如「十分之一」被读成「1分」）"""
    h = norm_text(hyp)
    return [k for k in kws if norm_text(k) and norm_text(k) not in h]


# ──────────────────────────────────────────────────────────────
# ASR 回听（咬字清晰度代理）
# ──────────────────────────────────────────────────────────────
_ASR = None


def get_asr():
    global _ASR
    if _ASR is None:
        from faster_whisper import WhisperModel
        # 内置 ctranslate2 缺 cublas64_12.dll → 固定 CPU int8（17s 音频 ≈2.6s，够快）
        _ASR = WhisperModel("Systran/faster-whisper-small", device="cpu", compute_type="int8")
    return _ASR


def transcribe(path: Path) -> str:
    segs, _ = get_asr().transcribe(str(path), language="zh", vad_filter=True)
    return "".join(s.text for s in segs)


# ──────────────────────────────────────────────────────────────
# 音色相似度（ECAPA）
# ──────────────────────────────────────────────────────────────
_ECAPA = None


def get_ecapa():
    global _ECAPA
    if _ECAPA is None:
        from speechbrain.inference.speaker import EncoderClassifier
        _ECAPA = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(Path(os.environ.get("HF_HOME", "~")).expanduser() / "hub" / "ecapa_local"),
            run_opts={"device": "cpu"},
        )
    return _ECAPA


def embed(path: Path) -> np.ndarray:
    import torch
    clf = get_ecapa()
    wav = clf.load_audio(str(path))          # 内部重采样到 16k
    emb = clf.encode_batch(wav.unsqueeze(0))
    return emb.squeeze().detach().cpu().numpy().astype(np.float64)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


# ──────────────────────────────────────────────────────────────
# 复合机器分（权重可辩，务必配合人耳盲测使用）
# ──────────────────────────────────────────────────────────────
W = {"clarity": 35, "similarity": 25, "bandwidth": 15, "clean": 15, "prosody": 10}


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def composite(m: dict) -> dict:
    """复合机器分。映射区间的标定依据（本机这批实测值域）：
      - ECAPA 余弦：同人 0.85~0.95，异人 ~0.60（合成语音同域，绝对值偏高，只能批内相对看）
      - -60dB 截止：22.05k 引擎 ~10.6kHz，24k 引擎 ~11.5kHz，48k 引擎 ~11.9kHz
      - CER：0.05（几乎全对）~0.13（明显吞字）
      - 半音标准差：3~5 为正常播报起伏，<2.5 偏念稿，>7 偏电音
    """
    parts = {}
    # 清晰度：CER 0%→1.0，≥15%→0
    if m.get("cer") is not None:
        parts["clarity"] = clamp01(1 - m["cer"] / 0.15)
    # 相似度：0.55→0，0.90→1（按本机实测值域标定，避免全体顶到 100 失去区分度）
    if m.get("sim") is not None:
        parts["similarity"] = clamp01((m["sim"] - 0.55) / 0.35)
    # 带宽：9kHz→0，12.5kHz→1
    if m.get("f_cut_60db"):
        parts["bandwidth"] = clamp01((m["f_cut_60db"] - 9000) / 3500)
    # 干净度：无削波 1.0；静音占比 >30% 或最长停顿 >1.5s 扣分
    pen = min(m.get("clip_ratio", 0) * 20, 0.5)                       # 削波惩罚
    pen += 0.3 if m.get("speech_ratio", 100) < 70 else 0.0            # 大段静音
    pen += 0.2 if m.get("max_pause_s", 0) > 1.5 else 0.0              # 异常长停
    parts["clean"] = clamp01(1 - pen)
    # 韵律：以 4 半音为最佳起伏，偏离越远越差（<2.5 念稿感，>7 电音感）
    st = m.get("f0_std_semitone", 0)
    if st:
        parts["prosody"] = clamp01(1 - abs(st - 4.0) / 4.0)
    score = sum(W[k] * v for k, v in parts.items()) / sum(W[k] for k in parts)
    return {"parts": {k: round(v * 100, 1) for k, v in parts.items()}, "score": round(score * 100, 1)}


# ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="待打分 wav 所在目录")
    ap.add_argument("--ref", required=True, help="参考音（目标音色）")
    ap.add_argument("--text", required=True, help="原文案 txt（用于算 CER）")
    ap.add_argument("--out", default=None, help="JSON 输出路径，默认 <dir>/score.json")
    ap.add_argument("--no-cer", action="store_true", help="跳过 ASR 转写（快）")
    ap.add_argument("--alt-text", default=None,
                    help="第二候选 GT（如未经品牌词替换的原文案）。两份 GT 各算一次 CER 取小者，"
                         "用于兼容「同一批产物里有的走了 dub.py（品牌词已替换）、有的直连引擎（未替换）」"
                         "的口径不一致场景")
    ap.add_argument("--kws", default=None, help="关键短语，逗号分隔，检查是否被读错/漏读")
    ap.add_argument("--no-sim", action="store_true", help="跳过音色相似度")
    ap.add_argument("--pattern", default="*.wav")
    ap.add_argument("--rescore", action="store_true",
                    help="只读已有 score.json 重算复合分（调权重/标定后免重跑 ASR）")
    args = ap.parse_args()

    if args.rescore:
        sp = Path(args.out) if args.out else Path(args.dir) / "score.json"
        data = json.loads(sp.read_text(encoding="utf-8"))
        for r in data["rows"]:
            r.update(composite(r))
        data["rows"].sort(key=lambda r: -r["score"])
        sp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for r in data["rows"]:
            print(f"{r['file']:26s} score={r['score']:5.1f}  {r['parts']}")
        print(f"\n已重算并写回 {sp}")
        return data["rows"]

    d = Path(args.dir)
    raw_text = Path(args.text).read_text(encoding="utf-8").strip()
    # 走 dub.py 的产物都被品牌词替换过 → ground truth 必须是替换后的文本
    try:
        import lexicon
        gt_text = lexicon.apply_brands(raw_text, "zh")
    except Exception:
        gt_text = raw_text
    alt_text = Path(args.alt_text).read_text(encoding="utf-8").strip() if args.alt_text else None
    kws = [k.strip() for k in args.kws.split(",")] if args.kws else []

    files = sorted(p for p in d.glob(args.pattern) if p.is_file())
    ref_emb = None if args.no_sim else embed(Path(args.ref))

    rows = []
    for f in files:
        y, sr = load_audio(f)
        m = {"file": f.name, "sr": sr}
        m.update(measure_level(y, sr))
        m.update(measure_bandwidth(y, sr))
        m.update(measure_f0(y, sr))
        if not args.no_cer:
            hyp = transcribe(f)
            c_main = cer(gt_text, hyp)
            c_alt = cer(alt_text, hyp) if alt_text else None
            if c_alt is not None and c_alt < c_main:
                m["cer"], m["gt_used"] = round(c_alt, 4), "alt"
            else:
                m["cer"], m["gt_used"] = round(c_main, 4), "main"
            m["asr"] = hyp[:120]
            if kws:
                m["kw_miss"] = keyword_miss(alt_text or gt_text if m["gt_used"] == "alt" else gt_text, hyp, kws)
        if ref_emb is not None:
            m["sim"] = round(cosine(embed(f), ref_emb), 4)
        m.update(composite(m))
        rows.append(m)
        print(f"[done] {m['file']:26s} score={m['score']:5.1f} "
              f"cer={m.get('cer')} sim={m.get('sim')} bw={m['f_cut_60db']}Hz")

    out = Path(args.out) if args.out else d / "score.json"
    out.write_text(json.dumps({"ref": str(args.ref), "text_gt": gt_text, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")
    return rows


if __name__ == "__main__":
    main()
