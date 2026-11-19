#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dots.tts 配音 worker（小红书 hilab · 48kHz 高保真）
================================================
复用 src/dots_tts/cli.py 的已验证调用路径：
  - sys.path 加入 <root>/src，使 dots_tts 包可导入
  - DotsTtsRuntime.from_pretrained(model_dir).generate(text, prompt_audio_path, prompt_text, ...)
  - 支持多语种 + prompt_text（参考音频转写文本），音色相似度 SOTA

由 dub.py 用引擎自带 python 调起：
  <root>/wzf/python.exe workers/worker_dotstts.py \
      --engine-root <root> --ref <ref.wav> --text <text> --out <out.wav> \
      [--model dots-tts-soar] [--prompt-text ...] [--language zh] [--num-steps 10]
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def build_argparser():
    ap = argparse.ArgumentParser(description="dots.tts worker")
    ap.add_argument("--engine-root", required=True)
    ap.add_argument("--ref", required=True, help="参考音频")
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=None, help="模型变体: dots-tts-soar / dots-tts-mf；留空用 soar")
    ap.add_argument("--prompt-text", default=None, help="参考音频的转写文本(提升相似度)")
    ap.add_argument("--language", default=None, help="none/auto_detect/EN/en/english/chinese…")
    ap.add_argument("--num-steps", type=int, default=10, help="扩散采样步数")
    ap.add_argument("--guidance-scale", type=float, default=1.2)
    ap.add_argument("--speaker-scale", type=float, default=1.5)
    ap.add_argument("--precision", default="bfloat16")
    ap.add_argument("--max-len", type=int, default=500, help="最大音频 patch 数")
    ap.add_argument("--normalize-text", action="store_true",
                   help="文本归一化(去标点/数字标准化等)")
    ap.add_argument("--ode-method", default="euler",
                   help="ODE 求解器: euler / midpoint / rk4 等")
    return ap


_LANG_MAP = {"chinese": "zh", "english": "en", "japanese": "ja", "korean": "ko",
             "french": "fr", "german": "de", "spanish": "es", "russian": "ru",
             "auto": "auto_detect", "auto_detect": "auto_detect", "none": None, "": None}


def setup_env(args):
    """设置离线环境。返回 root。"""
    root = os.path.abspath(args.engine_root)
    sys.path.insert(0, os.path.join(root, "src"))
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ.setdefault(k, "1")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    return root


def load_model(args):
    """加载 dots.tts 运行时（仅此一步慢）。返回 (runtime, model, model_dir)。
    持久化服务模式只调用一次，之后复用 runtime 做多次合成。"""
    root = setup_env(args)
    try:
        import soundfile as sf  # noqa: E402
        from dots_tts.runtime import DotsTtsRuntime  # noqa: E402
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"import dots_tts 失败: {e}"}))
        sys.exit(2)

    model = args.model or "dots-tts-soar"
    if args.language:
        args.language = _LANG_MAP.get(args.language.lower(), args.language)
    model_dir = os.path.join(root, "pretrained_models", model)
    if not os.path.isdir(model_dir):
        print(json.dumps({"ok": False, "error": f"模型目录不存在: {model_dir}"}))
        sys.exit(3)

    print(f"[dotstts] 加载运行时 model={model} precision={args.precision}", flush=True)
    runtime = DotsTtsRuntime.from_pretrained(
        model_dir,
        precision=args.precision,
        max_generate_length=args.max_len,
    )
    return runtime, model, model_dir, sf


def synthesize(model_and_meta, args):
    """用已加载的运行时合成。返回 True/False。"""
    runtime, model, model_dir, sf = model_and_meta
    # 语言名归一为 ISO 代码（langcodes 在无 language_data 包时只认代码）
    lang = _LANG_MAP.get(args.language.lower(), args.language) if args.language else args.language
    try:
        result = runtime.generate(
            text=args.text,
            prompt_audio_path=args.ref,
            prompt_text=args.prompt_text,
            language=lang,
            num_steps=args.num_steps,
            guidance_scale=args.guidance_scale,
            speaker_scale=args.speaker_scale,
            normalize_text=args.normalize_text,
            ode_method=args.ode_method,
        )
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        sf.write(
            args.out,
            result["audio"].float().cpu().squeeze().numpy(),
            result["sample_rate"],
        )
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"合成异常: {e}"}))
        return False

    ok = os.path.isfile(args.out) and os.path.getsize(args.out) > 0
    print(json.dumps({
        "ok": ok,
        "engine": "dotstts",
        "model": model,
        "out": args.out,
        "sample_rate": result.get("sample_rate"),
    }))
    return ok


def main():
    args = build_argparser().parse_args()
    model_and_meta = load_model(args)
    ok = synthesize(model_and_meta, args)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
