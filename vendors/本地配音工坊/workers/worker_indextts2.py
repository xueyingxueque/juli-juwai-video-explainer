#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
IndexTTS2 配音 worker（绿色整合包 · 直接用其自带便携 Python 运行）
==============================================================
复用 app/python/webui.py 的已验证调用路径：
  - sys.path 加入 app/python 与 app/python/indextts
  - IndexTTS2(model_dir, cfg_path, use_fp16, device).infer(...)
  - 支持三种情感控制：参考音频(emo_audio) / 自然语言(emo_text) / 情感向量(emo_vector)
  - 支持多角色配音（复刻官方 webui 的 _core_multi_role）：
    台词格式 [角色名]: 内容，配合 --multi-role <剧本文件> --roles <角色映射JSON>

本脚本由 dub.py 用引擎自带 python 调起：
  <engine_root>/app/python/build_venv/python.exe workers/worker_indextts2.py \
      --engine-root <root> --ref <ref.wav> --text <text> --out <out.wav> [--emo-...]
  或（多角色）：
  ... --engine-root <root> --multi-role <剧本.txt> \
      --roles '{"子言":"D:/voices/子言.wav","小棠":"D:/voices/小棠.wav"}' --out out.wav
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys


def build_argparser():
    ap = argparse.ArgumentParser(description="IndexTTS2 worker")
    ap.add_argument("--engine-root", required=True)
    ap.add_argument("--ref", default=None, help="参考音频(5-15s 清晰单人)")
    ap.add_argument("--text", default=None, help="要合成的文本")
    # 多角色模式（与 --ref/--text 二选一）
    ap.add_argument("--multi-role", default=None, help="多角色剧本文件路径（[角色名]: 内容 每行一条）")
    ap.add_argument("--roles", default=None, help="角色映射 JSON：{\"角色名\":\"参考音绝对路径\",...}")
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default=None, help="cuda / cuda:0 / cpu；留空自动")
    ap.add_argument("--fp16", action="store_true", help="开启 fp16(省显存,可能略降质)")
    # 情感控制（三种互斥优先：emo_text > emo_vector > emo_audio）
    ap.add_argument("--emo-audio", default=None, help="情感参考音频路径")
    ap.add_argument("--emo-alpha", type=float, default=1.0, help="情感强度 0~2，默认1.0")
    ap.add_argument("--emo-text", default=None, help="自然语言情感描述(如'悲伤地念')")
    ap.add_argument("--emo-vector", default=None, help="8维情感向量,逗号分隔(高级)")
    ap.add_argument("--max-tokens", type=int, default=120, help="每段最大 token 数")
    # 生成采样参数（透传给 infer 的 **generation_kwargs）
    ap.add_argument("--top-p", type=float, default=None, help="核采样阈值(默认0.8)")
    ap.add_argument("--top-k", type=int, default=None, help="Top-K(默认30)")
    ap.add_argument("--temperature", type=float, default=None, help="采样温度(默认0.8)")
    ap.add_argument("--num-beams", type=int, default=3, help="beam 数(确定性，默认3)")
    ap.add_argument("--repetition-penalty", type=float, default=None, help="重复惩罚(默认10.0)")
    ap.add_argument("--max-mel-tokens", type=int, default=1500, help="单段最大 mel token(默认1500)")
    ap.add_argument("--interval-silence", type=int, default=200, help="句间静音 ms(默认200)")
    ap.add_argument("--more-segment-before", type=int, default=0, help="前瞻段数(默认0)")
    ap.add_argument("--use-random", action="store_true", help="关闭 beam，走随机采样")
    return ap


def _build_infer_kwargs(args):
    """构造 IndexTTS2.infer 的显式参数 + generation_kwargs。
    None 值回退到官方推荐默认。"""
    emo_vector = None
    if args.emo_vector:
        try:
            emo_vector = [float(x) for x in args.emo_vector.split(",")]
        except Exception:
            emo_vector = None
    gen_kwargs = dict(
        do_sample=bool(args.use_random),
        top_p=args.top_p if args.top_p is not None else 0.8,
        top_k=args.top_k if args.top_k is not None else 30,
        temperature=args.temperature if args.temperature is not None else 0.8,
        num_beams=args.num_beams,
        repetition_penalty=args.repetition_penalty if args.repetition_penalty is not None else 10.0,
        max_mel_tokens=args.max_mel_tokens,
    )
    base = dict(
        emo_audio_prompt=args.emo_audio,
        emo_alpha=args.emo_alpha,
        emo_vector=emo_vector,
        use_emo_text=bool(args.emo_text),
        emo_text=args.emo_text or None,
        use_random=False,
        verbose=False,
        max_text_tokens_per_segment=args.max_tokens,
        interval_silence=args.interval_silence,
        more_segment_before=args.more_segment_before,
    )
    return base, gen_kwargs


# ── 多角色台词解析（与官方 webui 完全一致的规则）──
_MULTI_ROLE_LINE_RE = re.compile(r'^\s*\[([^\[\]]+)\]\s*[:：]\s*(.+?)\s*$')


def parse_multi_role_text(text):
    """解析多角色文本。返回 [(role_name, line_text), ...]
    不符合 [角色]:内容 格式的行会作为续行追加到上一条角色台词。"""
    segments = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _MULTI_ROLE_LINE_RE.match(line)
        if m:
            role = m.group(1).strip()
            content = m.group(2).strip()
            if content:
                segments.append([role, content])
        else:
            if segments:
                segments[-1][1] += " " + line
    return [(r, c) for r, c in segments]


def concat_wavs(wav_paths, out_path):
    """按顺序拼接多个 wav（要求采样率/位深/声道一致，否则报错）。"""
    import contextlib
    import wave
    if not wav_paths:
        raise ValueError("没有可拼接的片段")
    with contextlib.ExitStack() as stack:
        readers = [stack.enter_context(wave.open(p, "rb")) for p in wav_paths]
        params = readers[0].getparams()
        for i, r in enumerate(readers):
            if (r.getframerate(), r.getsampwidth(), r.getnchannels()) != \
               (params.framerate, params.sampwidth, params.nchannels):
                raise ValueError(
                    f"音频参数不一致，无法拼接：{wav_paths[0]} 为 "
                    f"{params.framerate}Hz/{params.sampwidth*8}bit/{params.nchannels}ch，"
                    f"而 {wav_paths[i]} 为 "
                    f"{r.getframerate()}Hz/{r.getsampwidth()*8}bit/{r.getnchannels}ch。"
                    f"请确保多角色音频产自同一模型且参考音色采样率一致。"
                )
        with wave.open(out_path, "wb") as w:
            w.setparams(params)
            for r in readers:
                w.writeframes(r.readframes(r.getnframes()))
    return out_path


def setup_env(args):
    """设置路径与离线环境（与 webui.py 一致）。返回 (root, app_py)。"""
    root = os.path.abspath(args.engine_root)
    app_py = os.path.join(root, "app", "python")
    sys.path.insert(0, app_py)
    sys.path.insert(0, os.path.join(app_py, "indextts"))
    os.environ["PATH"] = os.path.join(app_py, "ffmpeg", "bin") + os.pathsep + os.environ.get("PATH", "")
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        os.environ.setdefault(k, "1")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    return root, app_py


def load_model(args):
    """加载 IndexTTS2 模型（仅此一步慢，~90s）。返回 (tts, device)。
    持久化服务模式会只调用一次本函数，之后复用 tts 做多次合成。"""
    root, app_py = setup_env(args)
    import torch  # noqa: E402

    try:
        from indextts.infer_v2 import IndexTTS2  # noqa: E402
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"import IndexTTS2 失败: {e}"}))
        sys.exit(2)

    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    model_dir = os.path.join(app_py, "checkpoints")
    cfg_path = os.path.join(model_dir, "config.yaml")
    if not os.path.exists(cfg_path):
        print(json.dumps({"ok": False, "error": f"配置文件缺失: {cfg_path}"}))
        sys.exit(3)

    tts = IndexTTS2(
        model_dir=model_dir,
        cfg_path=cfg_path,
        use_fp16=args.fp16,
        device=device,
    )
    return tts, device


def synthesize(tts_and_device, args):
    """用已加载的模型合成。返回 True/False。tts_and_device 为 load_model 的返回值。"""
    tts, device = tts_and_device
    base, gen_kwargs = _build_infer_kwargs(args)

    # 多角色模式：--multi-role 剧本 + --roles 角色映射JSON
    multi = args.multi_role is not None
    if multi and (args.roles is None):
        print(json.dumps({"ok": False, "error": "多角色模式必须提供 --roles 角色映射JSON"}))
        return False

    try:
        if not multi:
            # ── 单人合成 ──
            out = tts.infer(
                spk_audio_prompt=args.ref,
                text=args.text,
                output_path=args.out,
                **base,
                **gen_kwargs,
            )
            ok = os.path.isfile(args.out) and os.path.getsize(args.out) > 0
            print(json.dumps({
                "ok": ok, "engine": "indextts2", "mode": "single",
                "out": args.out, "returned": out is not None, "device": device,
            }))
            return ok

        # ── 多角色合成（复刻官方 webui _core_multi_role）──
        try:
            role_map = json.loads(args.roles)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"角色映射JSON解析失败: {e}"}))
            return False
        if not isinstance(role_map, dict) or not role_map:
            print(json.dumps({"ok": False, "error": "角色映射不能为空"}))
            return False

        try:
            script_text = open(args.multi_role, encoding="utf-8").read()
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"剧本文件读取失败: {e}"}))
            return False

        segments = parse_multi_role_text(script_text)
        if not segments:
            print(json.dumps({"ok": False, "error": "剧本未解析到任何台词，请用 [角色名]: 内容 格式"}))
            return False

        # 校验角色都已配置
        missing = sorted({r for r, _ in segments if r not in role_map})
        if missing:
            print(json.dumps({"ok": False, "error": f"以下角色未配置参考音色: {', '.join(missing)}"}))
            return False

        # 逐段合成
        import tempfile
        seg_paths = []
        seg_dir = tempfile.mkdtemp(prefix="indextts2_multi_")
        total = len(segments)
        for idx, (role, line_text) in enumerate(segments):
            print(f"[{idx + 1}/{total}] {role}", flush=True)
            seg_path = os.path.join(seg_dir, f"{idx:03d}_{role}.wav")
            tts.infer(
                spk_audio_prompt=role_map[role],
                text=line_text,
                output_path=seg_path,
                **base,
                **gen_kwargs,
            )
            if not (os.path.isfile(seg_path) and os.path.getsize(seg_path) > 0):
                print(json.dumps({"ok": False, "error": f"第 {idx+1} 段合成失败: {role}"}))
                return False
            seg_paths.append(seg_path)

        # 拼接
        print("正在拼接音频...", flush=True)
        concat_wavs(seg_paths, args.out)

        ok = os.path.isfile(args.out) and os.path.getsize(args.out) > 0
        print(json.dumps({
            "ok": ok, "engine": "indextts2", "mode": "multi-role",
            "roles": len(role_map), "segments": total,
            "out": args.out, "device": device,
        }))
        return ok

    except Exception as e:
        print(json.dumps({"ok": False, "error": f"合成异常: {e}"}))
        return False


def main():
    args = build_argparser().parse_args()
    tts_and_device = load_model(args)
    ok = synthesize(tts_and_device, args)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
