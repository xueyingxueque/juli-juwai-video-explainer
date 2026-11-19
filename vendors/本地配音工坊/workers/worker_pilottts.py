#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PilotTTS-Studio 配音 worker（高德 AMAPVOICE 开源 · SIM 0.862 + 方言/副语言）
===================================================================
复用 _internal/bootstrap.py --mode cli 的已验证调用路径：
  - 注册项目根 / third_party / Matcha-TTS 到 sys.path（与 bootstrap 一致）
  - InferenceEngine(config, device).synthesize(prompt_wav, text, language)
  - 情感走 <|emo|> 成对标签；副语言走 <|LAUGH|> 等点标签
  - base 模型(pilot_tts.pt)做零样本克隆；instruct 模型(pilot_tts_instruct.pt)做情感/副语言

由 dub.py 用引擎自带 python 调起：
  <root>/python/python.exe workers/worker_pilottts.py \
      --engine-root <root> --ref <ref.wav> --text <text> --out <out.wav> \
      [--emotion happy] [--language zh]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from shutil import which


def build_argparser():
    ap = argparse.ArgumentParser(description="PilotTTS worker")
    ap.add_argument("--engine-root", required=True)
    ap.add_argument("--ref", required=True, help="参考音频(5-15s)")
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--emotion", default=None,
                    help="情感标签(逗号分隔可叠加): happy/sad/angry/surprise/fear/"
                         "disgust/serious/concern/blue/disdain/neutral/psychology/unknown")
    ap.add_argument("--language", default="zh", help="zh / en / 方言代码")
    # 生成采样参数（覆盖 configs/infer_pilot_tts*.yaml 的 inference 段）
    ap.add_argument("--top-p", type=float, default=None, help="核采样阈值(默认0.95)")
    ap.add_argument("--top-k", type=int, default=None, help="Top-K(默认 base=1 / instruct=20)")
    ap.add_argument("--temperature", type=float, default=None, help="采样温度(默认1.0)")
    return ap


def setup_env(args):
    """设置路径与离线环境。返回 root。"""
    root = os.path.abspath(args.engine_root)
    third = os.path.join(root, "third_party")
    matcha = os.path.join(third, "Matcha-TTS")
    for p in (root, third, matcha):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "NO_TORCH_COMPILE"):
        os.environ.setdefault(k, "1")
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
    os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
    return root


def load_model(args):
    """加载 PilotTTS 引擎（仅此一步慢）。返回 (engine, config, sr, root)。
    注意：--emotion 决定加载 base 还是 instruct 权重（加载期参数），
    持久化服务会按情感模式缓存两套模型。"""
    root = setup_env(args)
    try:
        import yaml  # noqa: E402
        import torch  # noqa: E402
        import soundfile as sf  # noqa: E402
        from pilot_voice.engine import InferenceEngine  # noqa: E402
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"import PilotTTS 失败: {e}"}))
        sys.exit(2)

    # base / instruct 模型选择
    if args.emotion:
        cfg_path = os.path.join(root, "configs", "infer_pilot_tts_instruct.yaml")
        ckpt = os.path.join(root, "model_assets", "pilot_tts_instruct.pt")
    else:
        cfg_path = os.path.join(root, "configs", "infer_pilot_tts.yaml")
        ckpt = os.path.join(root, "model_assets", "pilot_tts.pt")
    if not os.path.exists(ckpt):
        print(json.dumps({"ok": False, "error": f"模型权重缺失: {ckpt}"}))
        sys.exit(3)

    with open(cfg_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config["checkpoint_path"] = ckpt
    # 用 CLI 采样参数覆盖 configs 里的 inference 段（None 时保留 yaml 默认）
    inf = config.setdefault("inference", {})
    if args.top_p is not None:
        inf["top_p"] = args.top_p
    if args.top_k is not None:
        inf["top_k"] = args.top_k
    if args.temperature is not None:
        inf["temperature"] = args.temperature
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[pilottts] 加载引擎 emotion={'是' if args.emotion else '否'} device={device}", flush=True)
    engine = InferenceEngine(config, device)
    sr = config["vocoder"].get("sample_rate", 24000)
    return engine, config, sr, root, sf


def synthesize(model_and_meta, args):
    """用已加载的引擎合成。返回 True/False。"""
    engine, config, sr, root, sf = model_and_meta
    text = args.text
    if args.emotion:
        # 支持逗号分隔多重情感：happy,angry → <|happy|><|angry|>...<|/angry|><|/happy|>
        emos = [e.strip() for e in args.emotion.split(",") if e.strip()]
        if emos:
            open_tags = "".join(f"<|{e}|>" for e in emos)
            close_tags = "".join(f"<|/{e}|>" for e in reversed(emos))
            text = f"{open_tags}{text}{close_tags}"

    try:
        _, speech = engine.synthesize(args.ref, text, language=args.language)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        sf.write(args.out, speech.squeeze().cpu().numpy(), sr)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"合成异常: {e}"}))
        return False

    ok = os.path.isfile(args.out) and os.path.getsize(args.out) > 0
    print(json.dumps({
        "ok": ok,
        "engine": "pilottts",
        "emotion": args.emotion,
        "out": args.out,
        "sample_rate": sr,
    }))
    return ok


_WAV_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "pilottts_refs"


def _ffmpeg() -> str | None:
    for c in (r"D:/AITools/TTS/emotionTTS_v4.7.0/core/bin/ffmpeg.exe",
              r"D:/软件/JianyingPro/10.5.0.13988/ffmpeg.exe"):
        if os.path.isfile(c):
            return c
    return which("ffmpeg")


def ensure_wav(ref: str) -> str:
    """PilotTTS 引擎内部用 soundfile 读参考音，只认 wav/flac/ogg，不认 m4a/mp3。
    音色包里的 m4a/mp3 先转成 24k mono wav 并缓存 —— 保证「同一音色包全引擎通用」。"""
    p = Path(ref)
    if p.suffix.lower() in (".wav", ".flac", ".ogg"):
        return ref
    _WAV_CACHE.mkdir(parents=True, exist_ok=True)
    dst = _WAV_CACHE / f"{p.stem}_24k.wav"
    if dst.exists() and dst.stat().st_mtime >= p.stat().st_mtime:
        return str(dst)
    ff = _ffmpeg()
    if not ff:
        print(json.dumps({"ok": False, "error":
            f"参考音 {p.name} 非 wav 且本机无 ffmpeg；PilotTTS 读不了（请转 wav 后再用）"}))
        sys.exit(1)
    subprocess.run([ff, "-v", "error", "-y", "-i", str(p),
                    "-ar", "24000", "-ac", "1", str(dst)], check=True)
    print(f"[pilottts] 参考音已转 wav：{dst.name}", flush=True)
    return str(dst)


def main():
    args = build_argparser().parse_args()
    args.ref = ensure_wav(args.ref)  # m4a/mp3 先转 wav，否则引擎报 Format not recognised
    model_and_meta = load_model(args)
    ok = synthesize(model_and_meta, args)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
