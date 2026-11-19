# -*- coding: utf-8 -*-
"""
Chatterbox-V3 配音 worker（薄封装，复用官方 from_local 路径）
==============================================================
调用方式（由 dub.py 用引擎自带 wzf/python.exe 拉起）：
  wzf/python.exe worker_chatterbox.py \
      --engine-root <Chatterbox-V3 根目录> \
      --ref <参考音频 wav，可选；省略则用内置默认音色 conds.pt> \
      --text "要合成的文本" --out <输出.wav> \
      --model v3          # v3=通用多语(23语种) / zh-cmn=中文精调
      --language zh       # zh/en/ja/ko/fr/de/es/ru ...
      --exaggeration 0.5  # 情绪夸张度 0.25-2.0
      --cfg-weight 0.5    # CFG/节奏权重 0.2-1.0
      --temperature 0.8   # 随机性
      --seed 0            # 0=随机

特点（基于官方 multilingual_app.py 验证路径）：
  - 离线运行：HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE，权重全在 checkpoints/
  - 清系统代理，避免 localhost 503
  - 输出 24kHz 单声道 WAV，自带 Perth 隐式水印
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SUPPORTED = {
    "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it",
    "ja", "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr", "zh",
}


def build_argparser():
    ap = argparse.ArgumentParser(description="Chatterbox-V3 worker")
    ap.add_argument("--engine-root", required=True)
    ap.add_argument("--ref", default=None, help="参考音频(可选，省略则用内置默认音色)")
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="v3", help="v3(通用多语) / zh-cmn(中文精调)")
    ap.add_argument("--language", default="zh", help="语言代码: zh/en/ja/ko/...")
    ap.add_argument("--exaggeration", type=float, default=0.5)
    ap.add_argument("--cfg-weight", type=float, default=0.5)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--min-p", type=float, default=0.05, help="最小概率阈值 0-1")
    ap.add_argument("--top-p", type=float, default=1.0, help="核采样阈值 0-1")
    ap.add_argument("--repetition-penalty", type=float, default=1.2, help="重复惩罚 1-2")
    ap.add_argument("--device", default="cuda")
    return ap


def setup_env(args):
    """设置离线 + 清代理 + 本地 pkuseg/缓存（与启动器.bat 一致）。返回 ROOT。"""
    ROOT = Path(args.engine_root).resolve()
    SRC = ROOT / "src"
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    os.environ["PYTHONPATH"] = str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.environ["PKUSEG_HOME"] = str(ROOT / "models" / "pkuseg")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(ROOT / "hf_download")
    os.environ["TRANSFORMERS_CACHE"] = str(ROOT / "tf_download")
    os.environ["GRADIO_TEMP_DIR"] = str(ROOT / "tmp")
    os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy", "NO_PROXY"):
        os.environ.pop(k, None)
    return ROOT


def _resolve_lang(lang):
    lang = (lang or "zh").lower()
    if lang not in SUPPORTED:
        print(f"[chatterbox] 警告：语言 '{lang}' 不在支持列表，已回退到 zh")
        lang = "zh"
    return lang


def load_model(args):
    """加载 Chatterbox 模型（仅此一步慢）。返回 (model, device, root)。
    持久化服务模式只调用一次，之后复用 model 做多次合成。"""
    ROOT = setup_env(args)
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    device = args.device
    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"
    print(f"[chatterbox] 加载模型 device={device} model={args.model}", flush=True)
    model = ChatterboxMultilingualTTS.from_local(
        str(ROOT / "checkpoints"), device, t3_model=args.model
    )
    return model, device, ROOT


def synthesize(model_and_meta, args):
    """用已加载的模型合成。返回 True/False。"""
    model, device, ROOT = model_and_meta
    import numpy as np
    import torch

    lang = _resolve_lang(args.language)
    print(f"[chatterbox] device={device} model={args.model} lang={lang} "
          f"exag={args.exaggeration} cfg={args.cfg_weight} temp={args.temperature}", flush=True)

    if args.seed and args.seed != 0:
        torch.manual_seed(args.seed)
        if device == "cuda":
            torch.cuda.manual_seed(args.seed)
            torch.cuda.manual_seed_all(args.seed)

    gen_kwargs = dict(
        exaggeration=args.exaggeration,
        cfg_weight=args.cfg_weight,
        temperature=args.temperature,
        repetition_penalty=args.repetition_penalty,
        min_p=args.min_p,
        top_p=args.top_p,
    )
    if args.ref and os.path.isfile(args.ref):
        gen_kwargs["audio_prompt_path"] = args.ref
        print(f"[chatterbox] 参考音色: {args.ref}", flush=True)
    else:
        print("[chatterbox] 未提供参考音频，使用内置默认音色 (conds.pt)", flush=True)

    try:
        wav = model.generate(args.text, language_id=lang, **gen_kwargs)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"合成异常: {e}"}))
        return False
    sr = getattr(model, "sr", 24000)
    # generate 返回 (1, N) 的 tensor；统一写成 16-bit PCM WAV，
    # 与其它引擎（IndexTTS2/dots/PilotTTS）输出格式一致，剪辑软件通用
    data = wav.squeeze(0).cpu().numpy().astype(np.float32)
    data = np.clip(data, -1.0, 1.0)
    pcm = (data * 32767.0).astype(np.int16)

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    try:
        import soundfile as sf
        sf.write(out, pcm, sr, subtype="PCM_16")
    except Exception as e1:
        try:
            import torchaudio
            torchaudio.save(out, torch.from_numpy(pcm).unsqueeze(0), sr,
                            encoding=torchaudio.io.Encoding.PCM_S16)
        except Exception as e2:
            print(json.dumps({"ok": False, "error": f"保存 WAV 失败：soundfile={e1}; torchaudio={e2}"}))
            return False
    ok = os.path.isfile(out) and os.path.getsize(out) > 0
    print(f"[chatterbox] 已保存: {out} ({os.path.getsize(out)/1024:.1f} KB, {sr} Hz, PCM16)")
    return ok


def main():
    args = build_argparser().parse_args()
    model_and_meta = load_model(args)
    ok = synthesize(model_and_meta, args)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
